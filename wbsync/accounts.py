"""账号显示名 —— 把裸 uuid 换成有意义的名字。

## 昵称在哪：`storage/skeleton/account-snapshot.json`

早期结论「昵称只在云端」是**错的**，取证漏了整个 `~/.workbuddy/storage/`。
实测该文件明文存着当前登录账号的骨架：

    {"primary": {"uid": "...", "nickname": "示例账号", "editionType": "free",
                 "type": "personal", "isPro": false, "savedAt": 1793454248793}}

客户端 asar 内的证据（`ACCOUNT_SNAPSHOT_KEY='primary'`、`SKELETON_CACHE_DIR='skeleton'`、
IPC 频道 `account-snapshot:persist`）：这是渲染进程拿到账号信息时写下的首屏快照，
**切账号即整份覆盖**，且只有 `primary` 一个键。

由此得出两条边界：
  1. 当前账号的昵称 —— 100% 可读，不需要联网、不需要 at-rest 解密；
  2. 历史账号的昵称 —— 本地没有明文（`allAccounts` 是运行时调接口才有的），
     只能靠 `nicknames` 缓存：用户切过去一次，客户端写下快照，我们顺手记住。

三层回落：**用户别名 > 客户端昵称缓存 > 默认标签（本机账号 / 历史账号）**。
别名与昵称缓存都只存本地、不参与配置同步 —— 它们描述的是「这台机器怎么理解这些账号」，
强行同步到另一台只会让两边叫法互相覆盖。

## 为什么主账号要用快照 + persona 目录判定

「当前登录的是哪个账号」没有本地标志位，但有两处强信号：骨架快照的 `primary.uid`
（客户端自己写的，最准）与 `user-<uid>-personal/` 目录（只有真用过的账号才有）。
快照优先，且必须校验该 uid 在本机仍有数据 —— 否则会选中一个已登出且目录被清掉的账号。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import paths

CONFIG_NAME = "wbsync-accounts.json"
# v2 起多存 nicknames（客户端昵称缓存）；v1 只有 aliases，读取时必须兼容
CONFIG_VERSION = 2

UID_PREFIX_LEN = 6

DEFAULT_PRIMARY = "本机账号"
DEFAULT_OTHER = "历史账号"

# 与客户端 `ACCOUNT_SNAPSHOT_MAX_STALENESS_MS` 对齐：30 天。
# 过期不代表昵称失效，只说明「客户端很久没刷新过它」，界面上不该把它当权威。
SNAPSHOT_MAX_AGE_MS = 30 * 24 * 3600 * 1000

_EDITION_LABELS = {
    "free": "免费版",
    "pro": "专业版",
    "professional": "专业版",
    "premium": "专业版",
    "team": "团队版",
    "enterprise": "企业版",
    "personal": "个人版",
}


@dataclass(frozen=True)
class Snapshot:
    """首屏账号骨架快照里的一个账号。"""

    uid: str
    nickname: str
    account_type: str = ""   # personal / enterprise
    edition: str = ""        # free / pro / enterprise ...
    is_pro: bool = False
    saved_at: int = 0        # 毫秒，客户端写入时刻

    @property
    def edition_label(self) -> str:
        """副行用的短标签；未知档位返回空串（不要拿英文原样展示）。"""
        if self.is_pro and not self.edition:
            return _EDITION_LABELS["pro"]
        return _EDITION_LABELS.get(self.edition.strip().lower(), "")

    def is_stale(self, now_ms: int | None = None) -> bool:
        if not self.saved_at:
            return True
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return (now - self.saved_at) > SNAPSHOT_MAX_AGE_MS


def snapshot_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.account_snapshot_path(base)


def read_snapshot(base: str | os.PathLike[str] | None = None) -> Snapshot | None:
    """读当前登录账号的骨架快照；缺失或损坏返回 None（fail-open，不抛）。

    校验条件照抄客户端的 `isValid`：uid 非空、nickname 是字符串、savedAt 是有限数。
    不满足就当没有 —— 一个字段残缺的快照比没有更危险，它会顶掉正确的默认标签。
    """
    p = snapshot_path(base)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    node = raw.get("primary")
    if not isinstance(node, dict):
        return None
    uid = node.get("uid")
    nickname = node.get("nickname")
    saved_at = node.get("savedAt")
    if not isinstance(uid, str) or not uid:
        return None
    if not isinstance(nickname, str):
        return None
    if not isinstance(saved_at, (int, float)) or not saved_at == saved_at:  # NaN 自比不相等
        return None
    return Snapshot(
        uid=uid,
        nickname=nickname.strip(),
        account_type=str(node.get("type") or ""),
        edition=str(node.get("editionType") or ""),
        is_pro=bool(node.get("isPro")),
        saved_at=int(saved_at),
    )


@dataclass
class AccountLabels:
    """uid -> 显示名的映射，带三层回落与昵称缓存。"""

    path: Path
    aliases: dict[str, str] = field(default_factory=dict)
    primary_uid: str = ""
    # 客户端昵称缓存：切过一次账号就会被快照带进来，之后即使切回去也还记得
    nicknames: dict[str, str] = field(default_factory=dict)
    nickname_saved_at: dict[str, int] = field(default_factory=dict)
    snapshot: Snapshot | None = None

    @classmethod
    def load(cls, base: str | os.PathLike[str] | None = None) -> "AccountLabels":
        p = paths.session_data_dir(base) / CONFIG_NAME
        aliases: dict[str, str] = {}
        nicknames: dict[str, str] = {}
        saved_at: dict[str, int] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                data = {}
            if isinstance(data, dict):
                raw = data.get("aliases")
                if isinstance(raw, dict):
                    aliases = {
                        str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()
                    }
                # v2：{"names": {"<uid>": {"name": "...", "savedAt": 1793...}}}
                raw_names = data.get("names")
                if isinstance(raw_names, dict):
                    for k, v in raw_names.items():
                        if not isinstance(v, dict):
                            continue
                        name = v.get("name")
                        if isinstance(name, str) and name.strip():
                            nicknames[str(k)] = name.strip()
                            at = v.get("savedAt")
                            if isinstance(at, (int, float)):
                                saved_at[str(k)] = int(at)
        self = cls(
            path=p,
            aliases=aliases,
            primary_uid="",
            nicknames=nicknames,
            nickname_saved_at=saved_at,
        )
        self._absorb_snapshot(base)
        self.primary_uid = detect_primary_uid(base, snapshot=self.snapshot)
        return self

    def _absorb_snapshot(self, base: str | os.PathLike[str] | None = None) -> None:
        """把客户端快照里的昵称并入缓存，变化时落盘。

        ★ 这是「历史账号也能显示真名」的唯一来源：客户端切号时会覆写快照，
          我们每次启动顺手收割一次，用户切过的账号就会陆续补全。
        """
        snap = read_snapshot(base)
        self.snapshot = snap
        if snap is None or not snap.nickname:
            return
        dirty = False
        if self.nicknames.get(snap.uid) != snap.nickname:
            self.nicknames[snap.uid] = snap.nickname
            dirty = True
        if self.nickname_saved_at.get(snap.uid) != snap.saved_at:
            self.nickname_saved_at[snap.uid] = snap.saved_at
            dirty = True
        if dirty:
            try:
                self.save()
            except OSError:
                pass  # 落盘失败不影响本次显示，下次启动会再收一次

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CONFIG_VERSION,
            "aliases": self.aliases,
            "names": {
                uid: {"name": name, "savedAt": self.nickname_saved_at.get(uid, 0)}
                for uid, name in self.nicknames.items()
            },
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 查询
    def is_primary(self, uid: str) -> bool:
        return bool(self.primary_uid) and uid == self.primary_uid

    def nickname_of(self, uid: str) -> str:
        """客户端记录的昵称；没有则空串（不回落别名，两者用途不同）。"""
        return self.nicknames.get(uid, "")

    def name_of(self, uid: str) -> str:
        """显示名：自定义别名 > 客户端昵称 > 默认标签。"""
        alias = self.aliases.get(uid)
        if alias:
            return alias
        nick = self.nicknames.get(uid)
        if nick:
            return nick
        return DEFAULT_PRIMARY if self.is_primary(uid) else DEFAULT_OTHER

    def edition_of(self, uid: str) -> str:
        """当前账号的版本档位标签（免费版 / 企业版…）；历史账号拿不到，返回空串。"""
        if self.snapshot is not None and self.snapshot.uid == uid:
            return self.snapshot.edition_label
        return ""

    def label(self, uid: str, with_id: bool = True) -> str:
        """下拉框里的一行文字，例如「示例账号 · ab12cd」。

        ★ 只用于**需要编号区分**的地方（下拉框、抽屉副行、起名提示）。
          正文里说「归属会改到 X」时请用 `display()` —— 那里挂一串短 uuid
          只会让每句话看起来像报错代码。
        """
        name = self.name_of(uid)
        if not with_id:
            return name
        return f"{name} · {short_uid(uid)}"

    def display(self, uid: str, peers: "Iterable[str] | None" = None) -> str:
        """面向用户的账号名：只给名字，不挂短 uuid。

        仅当**同名冲突**时才补编号 —— 同机有两个都没昵称的历史账号时，
        只写「历史账号」用户没法区分谁是谁。其余情况一律只给名字。
        """
        name = self.name_of(uid)
        pool = (
            {u for u in peers if u}
            if peers is not None
            else ({u for u in self.aliases if u} | {u for u in self.nicknames if u}
                  | ({self.primary_uid} if self.primary_uid else set()))
        )
        same = sum(1 for u in pool if self.name_of(u) == name)
        return f"{name}（{short_uid(uid)}）" if same > 1 else name

    # ------------------------------------------------------------ 修改
    def set_alias(self, uid: str, name: str | None) -> None:
        """设置别名；传空则恢复默认（有客户端昵称时回到昵称）。"""
        cleaned = (name or "").strip()
        if cleaned:
            self.aliases[uid] = cleaned
        else:
            self.aliases.pop(uid, None)


def short_uid(uid: str, n: int = UID_PREFIX_LEN) -> str:
    return (uid or "")[:n]


def _dir_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def detect_primary_uid(
    base: str | os.PathLike[str] | None = None,
    snapshot: Snapshot | None = None,
) -> str:
    """主账号 = 「客户端自己说是谁」> 「最近还在用」。

    ⚠️ 不能简单取 `user-<uid>-personal` 里的第一个：多账号机器上这个目录会有多个
       （切换过账号就留下一个），按名字排序取到的是历史账号，界面上就会把
       「本机账号」标到错的人头上。

    判据按可靠性排序：
      0. **认证文件里 `account.uid`** —— 这才是客户端真正在用的身份，
         切完账号立刻生效，不用等客户端启动后回写快照；同样要校验目录存在
      1. 骨架快照的 `primary.uid` —— 客户端切号后亲自写的，最权威；
         但要校验该 uid 在本机仍有目录，否则会选中一个被清空的历史账号
      2. 侧边栏快照的 savedAt —— 客户端每次启动/切换都会写，反映"上次用的是谁"
      3. persona 目录 mtime —— 兜底
    """
    from . import authfile, sidebar

    root = paths.session_data_dir(base)
    if not root.is_dir():
        return ""

    import re

    def _has_dir(uid: str) -> bool:
        return bool(uid) and (
            (root / f"user-{uid}-personal").is_dir() or (root / uid).is_dir()
        )

    # 0. 认证文件（切换账号的唯一权威来源，写入即生效）
    auth_uid = authfile.current_uid(base)
    if _has_dir(auth_uid):
        return auth_uid

    snaps = sidebar.load_all(base)
    uuid_pat = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
    )

    # 1. 客户端快照（校验目录存在，避免指向已清空的账号）
    snap = snapshot if snapshot is not None else read_snapshot(base)
    if snap is not None and snap.uid and _has_dir(snap.uid):
        return snap.uid

    persona: list[tuple[str, float]] = []
    plain: list[tuple[str, float]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith("user-") and name.endswith("-personal"):
            persona.append((name[len("user-") : -len("-personal")], _dir_mtime(entry)))
        elif uuid_pat.match(name):
            # 普通 uid 目录：还没生成 persona 目录的早期状态
            plain.append((name, _dir_mtime(entry)))

    def score(item: tuple[str, float]) -> tuple[int, float]:
        uid, mtime = item
        snap = snaps.get(uid)
        return (snap.saved_at if snap is not None else 0, mtime)

    # 优先级：persona 目录 > 普通 uid 目录 > 快照里的账号
    for candidates in (persona, plain):
        if candidates:
            candidates.sort(key=score, reverse=True)
            return candidates[0][0]

    if snaps:
        return max(snaps.items(), key=lambda kv: kv[1].saved_at)[0]
    return ""


@dataclass(frozen=True)
class AccountInfo:
    """一个账号的展示信息，供界面下拉框 / 抽屉使用。"""

    uid: str
    name: str
    short: str
    is_primary: bool
    session_count: int = 0
    sidebar_count: int = 0
    # 客户端记录的原始昵称（不经别名覆盖），供抽屉里展示「真名」
    nickname: str = ""
    edition: str = ""
    # 抽屉第二行的三个数：占用字节与最近使用时间（毫秒时间戳，0=未知）。
    # 这三个数决定用户要不要切过去，而它们在任何别处都看不到。
    body_bytes: int = 0
    last_used: int = 0

    @property
    def label(self) -> str:
        return f"{self.name} · {self.short}"

    @property
    def visible_count(self) -> int:
        """「能看到几条会话」——优先侧边栏口径。

        ★ 不能用 `session_count`：库里 92 条 ≠ 界面可见 8 条，
          两个数字摆在一起会让人以为盘点错了（这条坑在 v1.3 踩过）。
          只有没盘侧边栏的账号才退回库内总数，且调用方要自己判断要不要标「约」。
        """
        return self.sidebar_count or self.session_count

    @property
    def sub_line(self) -> str:
        """抽屉第二行：编号 · 版本档位（拿不到档位时只说编号）。"""
        return f"{self.short} · {self.edition}" if self.edition else self.short


def build_accounts(
    uids: list[str],
    labels: AccountLabels,
    session_counts: dict[str, int] | None = None,
    sidebar_counts: dict[str, int] | None = None,
    body_bytes: dict[str, int] | None = None,
    last_used: dict[str, int] | None = None,
) -> list[AccountInfo]:
    """按「主账号优先、其次按会话数降序」排好序的账号列表。

    `body_bytes` / `last_used` 只有抽屉会用；缺省为 0 时抽屉不渲染那一段，
    而不是显示「0 B · 未知」—— 拿不到就别假装有。
    """
    counts = session_counts or {}
    side = sidebar_counts or {}
    sizes = body_bytes or {}
    used = last_used or {}
    infos = [
        AccountInfo(
            uid=u,
            name=labels.name_of(u),
            short=short_uid(u),
            is_primary=labels.is_primary(u),
            session_count=counts.get(u, 0),
            sidebar_count=side.get(u, 0),
            nickname=labels.nickname_of(u),
            edition=labels.edition_of(u),
            body_bytes=int(sizes.get(u, 0) or 0),
            last_used=int(used.get(u, 0) or 0),
        )
        for u in uids
        if u
    ]
    infos.sort(key=lambda a: (not a.is_primary, -a.session_count, a.uid))
    return infos
