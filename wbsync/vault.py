"""账号凭据库 —— 「切回去」所需的那一对区块。

## 为什么必须有这个库

官方认证文件只存**当前**登录账号的那一份凭据（`accounts` / `allAccounts`
只是展示用的账号对象，里面没有 token）。所以想从 A 切到 B，必须先**在 B 登录时
把它的 `{account, auth}` 存下来**，否则没有任何东西可写。

这个库就是那本账：`~/.workbuddy/wbsync-vault.json`

    {"version": 1,
     "accounts": {"<uid>": {"uid": ..., "nickname": ..., "domain": ...,
                            "account": {...}, "auth": {...},
                            "capturedAt": 1793454248793, "source": "client"}}}

## 凭据从哪来（三条路，按优先级）

1. **自动采集**：每次启动本工具时读一次认证文件，若 uid 或凭据变了就更新。
   用户只要用某个账号登录过一次客户端，它就会自己进库 —— 零操作。
2. **从其它工具导入**：`~/.buddy-switch/accounts.json`（NextAgentX 那个切换工具）
   里存的是**明文** token，可以直接读进来，让老用户不必重新登录每个账号。
   只在用户显式点击时执行，且只读不写对方文件。
3. **从同步包导入**（未实现）：跨机搬凭据。刻意不做 —— 凭据默认不出本机。

## 不解密、不落明文（除非来源本来就是明文）

从客户端采集到的 `accessToken` 是加密信封，我们**原样存**。好处是整条链路
零加密依赖、零明文暴露；坏处是信封只有本机这把钥匙解得开（跨机导入的账号
因此需要重新登录 —— 这一条写进界面提示，不藏在代码里）。

## 这个文件不进同步包

`wbsync-vault.json` 是本机秘密，被 `pack.has_secret` 这类判定排除在同步范围外。
往另一台机器搬凭据是**用户不该被默认替做**的决定。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import authfile, paths

VAULT_NAME = "wbsync-vault.json"
VAULT_VERSION = 1

# 外部工具的账号库（存在时提供「一键导入」）
EXTERNAL_STORE = Path(".buddy-switch") / "accounts.json"

SOURCE_CLIENT = "client"
SOURCE_EXTERNAL = "buddy-switch"

_SOURCE_TEXT = {
    SOURCE_CLIENT: "客户端采集",
    SOURCE_EXTERNAL: "从其它工具导入",
}


def vault_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / VAULT_NAME


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Credential:
    """一个账号的登录凭据（`account` + `auth` 两个区块，原样保存）。"""

    uid: str
    nickname: str = ""
    domain: str = ""
    account: dict = field(default_factory=dict)
    auth: dict = field(default_factory=dict)
    captured_at: int = 0
    source: str = SOURCE_CLIENT

    @property
    def source_text(self) -> str:
        return _SOURCE_TEXT.get(self.source, self.source or "未知来源")

    @property
    def token_form(self) -> str:
        """凭据形态：信封 / 明文 / 缺失。界面用它解释「为什么可能还要重新登录」。"""
        token = self.auth.get("accessToken")
        if authfile.is_envelope(token):
            return "信封"
        return "明文" if authfile.secret_text(token) else "缺失"

    def expires_at(self) -> int:
        value = self.auth.get("expiresAt")
        return int(value) if isinstance(value, (int, float)) else 0

    def expiry_text(self) -> str:
        """登录有效期的一句话描述（不猜，只报事实）。"""
        at = self.expires_at()
        if not at:
            return "有效期未知"
        remain = at - now_ms()
        if remain <= 0:
            return "登录已过期，重启后需重新登录"
        days = remain // 86_400_000
        if days >= 1:
            return f"登录有效期剩 {days} 天"
        return f"登录有效期剩 {max(1, remain // 3_600_000)} 小时"


class Vault:
    """本机账号凭据库。读写都是整份 JSON，条目少（个位数），无需增量。"""

    def __init__(self, path: Path, entries: dict[str, Credential] | None = None):
        self.path = path
        self.entries: dict[str, Credential] = entries or {}

    # ------------------------------------------------------------ 读写
    @classmethod
    def load(cls, base: str | os.PathLike[str] | None = None) -> "Vault":
        p = vault_path(base)
        entries: dict[str, Credential] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                data = {}
            raw = data.get("accounts") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                for uid, node in raw.items():
                    cred = _parse_entry(str(uid), node)
                    if cred is not None:
                        entries[cred.uid] = cred
        return cls(path=p, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VAULT_VERSION,
            "accounts": {uid: _entry_json(c) for uid, c in self.entries.items()},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 查询
    def get(self, uid: str) -> Credential | None:
        return self.entries.get(uid or "")

    def uids(self) -> list[str]:
        return list(self.entries)

    def has(self, uid: str) -> bool:
        return bool(uid) and uid in self.entries

    def __len__(self) -> int:
        return len(self.entries)

    # ------------------------------------------------------------ 采集
    def capture_current(
        self,
        base: str | os.PathLike[str] | None = None,
        path: Path | None = None,
        save: bool = True,
    ) -> tuple[Credential | None, bool]:
        """采集当前登录账号。返回 (凭据, 是否有变化)。

        ★ 「有变化」的判据是 `account` 与 `auth` **两块内容的 JSON 全等**，
          不是时间戳：客户端每次启动都会重写认证文件（`lastRefreshTime` 变化），
          按 mtime 判会把库写成每启动一次更新一次，让「上次采集于」失去意义。
        """
        rec = authfile.current_record(base, path)
        if rec is None:
            return None, False
        nickname = rec["nickname"]
        if not nickname:
            # 认证文件里的昵称是加密信封（读不出明文），但客户端把当前账号的
            # 明文昵称写在了 storage/skeleton 快照里 —— 那里才是可读的那份。
            try:
                from . import accounts

                snap = accounts.read_snapshot(base)
                if snap is not None and snap.uid == rec["uid"]:
                    nickname = snap.nickname
            except (OSError, ValueError):
                pass
        cred = Credential(
            uid=rec["uid"],
            nickname=nickname,
            domain=rec["domain"],
            account=rec["account"],
            auth=rec["auth"],
            captured_at=now_ms(),
            source=SOURCE_CLIENT,
        )
        old = self.entries.get(cred.uid)
        changed = (
            old is None
            or not _same_blocks(old, cred)
            # 昵称从「读不出」变成「读到了」也算变化 —— 否则第一次采集时空昵称
            # 会一直留在库里，界面永远只能显示编号
            or (bool(cred.nickname) and old.nickname != cred.nickname)
        )
        if changed:
            # 昵称优先取新的；新的一侧为空（信封态读不出）就保留旧的可读值
            if not cred.nickname and old is not None:
                cred.nickname = old.nickname
            self.entries[cred.uid] = cred
            if save:
                try:
                    self.save()
                except OSError:
                    pass  # 落盘失败不影响本次切换，只是下次要重新采集
        return cred, changed

    # ------------------------------------------------------------ 导入
    def import_from_buddy_switch(
        self,
        store: Path | None = None,
        save: bool = True,
    ) -> tuple[int, int, list[str]]:
        """从 `~/.buddy-switch/accounts.json` 导入账号。返回 (新增, 更新, 跳过原因)。

        ★ 只读对方文件，不写、不改、不删 —— 那是另一个工具的数据。
        ★ 明文 token 直接存：客户端接受明文，下次它自己重新封装（见 authfile 文档）。
        """
        src = Path(store) if store is not None else Path(os.path.expanduser("~")) / EXTERNAL_STORE
        if not src.is_file():
            return 0, 0, [f"未找到其它工具的账号库：{src}"]
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return 0, 0, [f"账号库无法解析：{exc}"]
        rows = data if isinstance(data, list) else data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return 0, 0, ["账号库结构不是数组，已跳过"]

        added = updated = 0
        skipped: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cred = _from_external(row)
            if cred is None:
                skipped.append(str(row.get("uid") or row.get("id") or "（无 uid）"))
                continue
            old = self.entries.get(cred.uid)
            if old is not None and not _same_blocks(old, cred):
                # 已有条目：只在本地没有可用 token 时才覆盖，避免把客户端采集的
                # 信封凭据换成明文（两者都能用，但信封更新，且是客户端自己写的）
                if authfile.has_secret(old.auth.get("accessToken")):
                    continue
            if old is None:
                added += 1
            else:
                updated += 1
            if not cred.nickname and old is not None:
                cred.nickname = old.nickname
            self.entries[cred.uid] = cred

        if save and (added or updated):
            try:
                self.save()
            except OSError as exc:
                skipped.append(f"写入账号库失败：{exc.strerror or exc}")
        return added, updated, skipped


# ---------------------------------------------------------------- 解析辅助


def _same_blocks(a: Credential, b: Credential) -> bool:
    try:
        return json.dumps(a.account, sort_keys=True) == json.dumps(b.account, sort_keys=True) and (
            json.dumps(a.auth, sort_keys=True) == json.dumps(b.auth, sort_keys=True)
        )
    except (TypeError, ValueError):
        return False


def _parse_entry(uid: str, node) -> Credential | None:
    if not isinstance(node, dict):
        return None
    account = node.get("account")
    auth = node.get("auth")
    if not isinstance(account, dict) or not isinstance(auth, dict):
        return None
    real_uid = str(node.get("uid") or account.get("uid") or uid)
    if not real_uid:
        return None
    at = node.get("capturedAt")
    return Credential(
        uid=real_uid,
        nickname=str(node.get("nickname") or ""),
        domain=str(node.get("domain") or ""),
        account=account,
        auth=auth,
        captured_at=int(at) if isinstance(at, (int, float)) else 0,
        source=str(node.get("source") or SOURCE_CLIENT),
    )


def _entry_json(c: Credential) -> dict:
    return {
        "uid": c.uid,
        "nickname": c.nickname,
        "domain": c.domain,
        "account": c.account,
        "auth": c.auth,
        "capturedAt": c.captured_at,
        "source": c.source,
    }


def _from_external(row: dict) -> Credential | None:
    """把外部账号库的一行转成我们的条目。

    外部结构：`{uid, nickname, domain, access_token, auth_raw, profile_raw, ...}`
    `auth_raw` 可能是 `{account, auth}` 整份文档，也可能直接就是 `auth` 区块 ——
    两种都见过，所以要判。缺失时退回把顶层的 token 字段拼成 auth 区块。
    """
    uid = str(row.get("uid") or row.get("id") or "").strip()
    if not uid:
        return None

    auth_raw = row.get("auth_raw")
    account: dict = {}
    auth: dict = {}
    if isinstance(auth_raw, dict):
        inner = auth_raw.get("auth")
        if isinstance(inner, dict):
            auth = dict(inner)
            acc = auth_raw.get("account")
            if isinstance(acc, dict):
                account = dict(acc)
        else:
            auth = dict(auth_raw)

    profile = row.get("profile_raw")
    if isinstance(profile, dict):
        account = dict(profile)

    if not account:
        account = {"uid": uid}
    account.setdefault("uid", uid)
    nickname = row.get("nickname")
    if isinstance(nickname, str) and nickname.strip():
        account["nickname"] = nickname.strip()

    if not authfile.has_secret(auth.get("accessToken")):
        token = row.get("access_token")
        if authfile.has_secret(token):
            auth["accessToken"] = token
    if not authfile.has_secret(auth.get("accessToken")):
        return None  # 没有 token 的账号切过去只会是「未登录」，不如不要
    if not authfile.has_secret(auth.get("refreshToken")):
        token = row.get("refresh_token")
        if authfile.has_secret(token):
            auth["refreshToken"] = token

    domain = str(row.get("domain") or auth.get("domain") or "")
    if domain:
        auth["domain"] = domain
    auth.setdefault("tokenType", str(row.get("token_type") or "Bearer"))

    # 外部库只存相对有效期（expiresIn）。客户端读的是绝对时间 expiresAt，
    # 缺了就按「抓取时刻 + expiresIn」补一个 —— 补出来的即使已过期，客户端也会
    # 用 refreshToken 续期，与「本来就过期」走同一条路，不会更糟。
    if not isinstance(auth.get("expiresAt"), (int, float)):
        expires_in = auth.get("expiresIn")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            auth["expiresAt"] = now_ms() + int(expires_in) * 1000

    expires = auth.get("expiresAt")
    return Credential(
        uid=uid,
        nickname=str(nickname or "").strip(),
        domain=domain,
        account=account,
        auth=auth,
        captured_at=now_ms(),
        source=SOURCE_EXTERNAL,
    )


def external_store_path(store: Path | None = None) -> Path:
    if store is not None:
        return Path(store)
    return Path(os.path.expanduser("~")) / EXTERNAL_STORE


def load_external_entries(store: Path | None = None) -> list[Credential]:
    """只读预览：外部账号库能被识别成哪些账号（不落盘）。"""
    src = external_store_path(store)
    if not src.is_file():
        return []
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    rows = data if isinstance(data, list) else data.get("accounts") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    out: list[Credential] = []
    for row in rows:
        if isinstance(row, dict):
            cred = _from_external(row)
            if cred is not None:
                out.append(cred)
    return out


def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    v = Vault.load(base)
    ext = len(load_external_entries())
    return (
        f"vault: 已存凭据 {len(v)} 个"
        + (f", 可导入 {ext} 个" if ext else "")
        + f"（{v.path.name}）"
    )
