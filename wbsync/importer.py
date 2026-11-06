"""导入器 —— 把包里的会话并入本机，加法语义，绝不覆盖。

## 动作判定

| 本地状态               | 动作        | 说明                                     |
|------------------------|-------------|------------------------------------------|
| 本地无此会话           | add         | 写三件套，整体新增                       |
| 指纹完全一致           | skip-synced | 无变化                                   |
| 本地是源的严格前缀     | append      | 把源多出的行追加到本地                   |
| 本地是源的超集         | local-newer | 不动本地，列入「回传候选」               |
| 双方各有新改动         | overwrite / local-newer / diverged-both | 整条取舍，见下 |
| 公共前缀触到 512 上限  | unknown     | **拒绝**，判据不足宁可不动               |
| 包里没有正文           | no-body     | 跳过（没有正文的会话导过去也没有意义）   |

## 为什么「分叉」取舍整条、而不是拼两段

两条各自延伸的会话拼接后会产生**一条不存在的对话历史**：A 机器问的问题，
B 机器给出的回答，拼在一起语义断裂，客户端续接时会读到错乱上下文。
丢消息是可发现的，编造历史不是。

所以分叉**不合并**，只做整条取舍：默认按**正文末行 timestamp** 保留续写更晚的一方
（`div_choices` 可逐条覆盖为 src / local / both）。取「更晚」而不是取 `updated_at`：
后者会被改标题、校正归属这类不碰正文的动作污染，用它选会选错。

被覆盖那一份由 `guard` 单独备份（`backups/merge-<时间>/`），可整批回滚 ——
这是「只保留最新」这个选择的唯一安全垫。

「保留两份」给包内那份换一个新 cid 落进来，两份并存互不影响（`diverged-both`）。

## 写三件套（缺一不可）

1. `projects/<workspace>/<cid>.jsonl`  正文
2. `workbuddy.db` → sessions            侧边栏索引
3. `edge-sync-mapping-v{N}.db`          云端映射

只写前两件的后果是「会话存在但界面里找不到」—— 等于没导。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import accounts
from . import bundle as bundle_mod
from . import copysession, dbcopy, fpcache
from . import guard, importlog, paths
from . import rehome
from .fingerprint import (
    BEHIND,
    DIVERGED,
    PROBABLY_SYNCED,
    SYNCED,
    UNKNOWN,
    compare,
    fingerprint_of_file,
)

# 动作常量
ADD = "add"
SKIP_SYNCED = "skip-synced"
APPEND = "append"
REHOME = "rehome"          # 内容一致但归属账号不对，只改归属
RESTORE = "restore"        # 本地已删除，恢复它
DUPLICATE = "duplicate"    # 本地已有**内容相同、cid 不同**的副本
OVERWRITE = "overwrite"    # 分叉且包内更新：用包内那一版整条替换本地
LOCAL_NEWER = "local-newer"  # 本地更新（超集或分叉后取本地）：不动，列入回传候选
DIVERGED_BOTH = "diverged-both"  # 分叉且两份都要：包内那份换新 cid 落进来
BLOCK_DIVERGED = "diverged"   # 旧版动作，保留常量供旧界面/旧日志识别
BLOCK_UNKNOWN = "unknown"
SKIP_NO_BODY = "no-body"
SKIP_NO_CWD = "no-cwd"

_BLOCKING = {BLOCK_DIVERGED, BLOCK_UNKNOWN}

_ACTION_LABEL = {
    ADD: "新增",
    SKIP_SYNCED: "一致·跳过",
    APPEND: "追加",
    REHOME: "校正归属",
    RESTORE: "恢复已删除",
    DUPLICATE: "内容重复·跳过",
    OVERWRITE: "分叉·取包内",
    LOCAL_NEWER: "本地更新·待回传",
    DIVERGED_BOTH: "分叉·保留两份",
    BLOCK_DIVERGED: "分叉·拒绝",
    BLOCK_UNKNOWN: "无法判定·拒绝",
    SKIP_NO_BODY: "无正文·跳过",
    SKIP_NO_CWD: "缺工作空间·跳过",
}

# 内容判重的最小正文尺寸（字节）。与 align.MIN_DEDUP_BYTES 同值：
# 极短正文（空会话、只有一行 session-meta）把自身 cid 抹掉后几乎必然相同，
# 拿它认亲会把两条互不相干的短会话当成同一份。
MIN_DEDUP_BYTES = 256

# sessions 表的 NOT NULL 列：缺了会插不进去，必须兜底
_REQUIRED_DEFAULTS: dict[str, object] = {
    "cwd": "",
    "user_id": "",
    "status": "Pending",
    "created_at": 0,
    "updated_at": 0,
    "is_playground": 0,
    "transport": "local",
    "unread": 0,
}


@dataclass
class ImportAction:
    """一条会话的导入决策。"""

    session: bundle_mod.BundleSession
    action: str
    reason: str = ""
    local_cid: str = ""
    local_line_count: int = 0
    append_from: int = 0  # 从源端第几行开始追加
    body_target: Path | None = None
    remapped_cwd: str = ""  # 非空表示这条 ADD 会话的 cwd 已被重映射到本机路径
    # 本地已有内容等价、只是 cid 不同的那条（空串 = 没有）。只对 DUPLICATE 有意义。
    duplicate_of: str = ""
    # ---- 分叉取舍（只有 DIVERGED 走到这里）
    # "src" / "local" / "both"，空串表示这条不是分叉
    div_keep: str = ""
    local_ts: int = 0          # 本地正文末行 timestamp（毫秒）
    src_ts: int = 0            # 包内正文末行 timestamp（毫秒）
    dropped_lines: int = 0     # 本次取舍会丢掉的行数（用于汇总与告警）
    # 分叉的三个基数：界面切换取舍时要就地重算，不必重新 plan（那会重扫全部正文）
    div_common: int = 0
    div_local_new: int = 0
    div_src_new: int = 0
    # 本机这一行原来的 cwd。「保留两份」的副本要落在同一个工程里。
    local_cwd: str = ""
    # 「保留两份」时给包内那份分配的新 cid（空串 = 沿用原 cid）
    new_cid: str = ""

    @property
    def label(self) -> str:
        return _ACTION_LABEL.get(self.action, self.action)

    @property
    def blocking(self) -> bool:
        return self.action in _BLOCKING

    @property
    def will_write(self) -> bool:
        return self.action in (ADD, APPEND, REHOME, RESTORE, OVERWRITE, DIVERGED_BOTH)

    @property
    def overridable(self) -> bool:
        """默认不写、但用户执意要写就能写 —— 目前只有「内容重复」。

        与 `blocking` 的区别：无法判定是**做不到**（做了会编造历史），
        重复只是**不建议**（做了会多一份，用户可能真想要两份独立副本）。
        """
        return self.action == DUPLICATE

    @property
    def add_like(self) -> bool:
        """写入路径与「新增」一致的动作（重复项被强制导入时也走这条路）。"""
        return self.action in (ADD, DUPLICATE, DIVERGED_BOTH)

    @property
    def back_candidate(self) -> bool:
        """本地比包新 —— 可以被「回传包」带走的那批。"""
        return self.action == LOCAL_NEWER

    @property
    def effective_cid(self) -> str:
        """这条导入实际写入/登记的 cid。

        ★ 只有「保留两份」才换成新 cid，且必须按 action 判断，不能只看
          `new_cid` 是否非空：界面来回切换取舍时会保留已分配的 cid（避免
          每次切换都换一个、散落空文件），此时 action 可能已不是 both。
        """
        if self.action == DIVERGED_BOTH and self.new_cid:
            return self.new_cid
        return self.session.cid


@dataclass
class ImportReport:
    actions: list[ImportAction] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    # 失败项的标题（cid -> title）。失败清单是给人看的，只给一串 uuid 等于没说清
    # 是哪条 —— 用户只能看到「失败 1 项」和一段 id，无从判断要不要重导。
    titles: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    backup: guard.Backup | None = None
    # 分叉取舍覆盖掉的正文单独留一份，供「回滚本次」使用（None = 本次没覆盖任何文件）
    merge_backup: guard.Backup | None = None
    dry_run: bool = False

    def fail(self, cid: str, title: str, msg: str) -> None:
        self.failed.append((cid, msg))
        if title:
            self.titles[cid] = title

    def label_of(self, cid: str) -> str:
        """失败项的展示名：标题 > 短 id。"""
        title = self.titles.get(cid, "")
        return title or cid[:8]

    @property
    def planned(self) -> list[ImportAction]:
        return [a for a in self.actions if a.will_write]

    @property
    def ok(self) -> bool:
        """没有硬失败。与 rehome / switch 的报告同一口径，界面直接判这个。"""
        return not self.failed

    @property
    def blocked(self) -> list[ImportAction]:
        return [a for a in self.actions if a.blocking]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.action] = counts.get(a.action, 0) + 1
        parts = [_ACTION_LABEL.get(k, k) + f" {v}" for k, v in sorted(counts.items())]
        head = "（演练，未写入）" if self.dry_run else ""
        return head + ("；".join(parts) if parts else "无动作")


@dataclass
class CwdRemapPlan:
    """导入时一条「源工作空间 → 本机落点」的待确认映射。"""

    source_cwd: str          # 归一化后的源机路径（聚合键）
    session_count: int       # 包中该路径下的会话数
    suggested_target: str    # 建议本机落点（记忆优先，其次同名工程，最后推测）
    from_memory: bool        # 建议是否来自本地记忆
    # 建议的来源：memory / named / weak / guess。界面据此打不同的标，
    # 不把「按经验猜的」和「本机真有这个工程」说成同一件事。
    origin: str = "guess"
    # 同名工程的候选（可能为空）。weak 档不进 suggested_target 的自动采用，
    # 但列出来让用户一键改过去，比让他手输整条路径强。
    candidates: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.source_cwd)

    @property
    def confident(self) -> bool:
        """建议值是否足够可信到可以默认勾选（记忆 / 精确 / 强匹配）。"""
        return self.origin in ("memory", rehome.MATCH_EXACT, rehome.MATCH_STRONG)

    @property
    def origin_text(self) -> str:
        return {
            "memory": "记忆",
            rehome.MATCH_EXACT: "同路径命中",
            rehome.MATCH_STRONG: "同名工程命中",
            rehome.MATCH_WEAK: "同名候选",
        }.get(self.origin, "推测")


def suggest_target(
    source_cwd: str,
    base: str | os.PathLike[str] | None = None,
    dirs: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    """给一条源 cwd 算建议落点。返回 `(落点, 来源, 候选)`。

    优先级：**记忆 > 同名工程 > 经验猜测**。

    ★ 同名工程排在记忆之后而不是之前：记忆是用户自己确认过的答案，
      任何自动匹配都不该推翻它。同名匹配只在「没被确认过」时出手。
    """
    from . import cwd_map

    if cwd_map.has(source_cwd, base):
        return cwd_map.suggest_new_cwd(source_cwd, base), "memory", []

    tier, hits = rehome.match_named_workspace(source_cwd, base=base, candidates=dirs)
    if tier in (rehome.MATCH_EXACT, rehome.MATCH_STRONG) and hits:
        return hits[0], tier, hits
    if tier == rehome.MATCH_WEAK and hits:
        # 弱匹配不自动采用：末段同名、上面几层完全不同，很可能是两个不相干的
        # 工程。落到设备目录下（可预测、可回退），把候选交给用户点。
        return rehome.default_new_cwd(source_cwd), "weak", hits
    return rehome.default_new_cwd(source_cwd), "guess", []


def plan_cwd_remaps(
    b: bundle_mod.Bundle,
    base: str | os.PathLike[str] | None = None,
    actions: list["ImportAction"] | None = None,
) -> list[CwdRemapPlan]:
    """收集包里那些「本机不存在的源 cwd」，并给每条算本机建议落点。

    只关心会带入本机、且源路径在本机失效的会话 —— 这些才是导入后会
    变成「死工作区」的。命中本地记忆的优先用记忆值，否则按经验推测。

    ★ 传 actions 时只聚合 `action == ADD` 的会话：那些会被 SKIP/APPEND/REHOME
      的会话即便 cwd 失效，也不会在本次导入里写正文，映射它们既无意义，
      又会让用户勾了「却什么都没发生」，还会被写进记忆污染下次预填。
      （典型场景：重复导入同一个包，全会话 skip-synced，映射页签应为空。）
      不传 actions 时回退遍历全部会话，保留「只想看包里有哪些失效 cwd」的能力。

    纯计算、不写数据。同一条 cwd 只在去重后 stat 一次，避免大包重复 IO。
    """
    sources = (
        [a.session for a in actions if a.action in (ADD, DIVERGED_BOTH)]
        if actions is not None
        else list(b.sessions)
    )

    counts: dict[str, int] = {}
    for s in sources:
        raw = (s.cwd or "").strip()
        if not raw:
            continue
        key = rehome.normalize(raw)
        counts[key] = counts.get(key, 0) + 1

    out: list[CwdRemapPlan] = []
    # 本机候选目录只算一次：每条映射都重新开库 + stat 一遍是不可接受的开销
    dirs = rehome.known_workspace_dirs(base)
    for key, cnt in counts.items():
        if os.path.isdir(key):
            continue  # 本机存在的 cwd 不是失效（去重后只 stat 一次）
        target, origin, hits = suggest_target(key, base, dirs=dirs)
        out.append(
            CwdRemapPlan(
                source_cwd=key,
                session_count=cnt,
                suggested_target=target,
                from_memory=(origin == "memory"),
                origin=origin,
                candidates=list(hits),
            )
        )
    out.sort(key=lambda x: (-x.session_count, x.source_cwd))
    return out


# ------------------------------------------------------------ 本地 uid


def detect_local_uid(base: str | os.PathLike[str] | None = None) -> str:
    """判定「导入的会话该归属到本机哪个账号」。

    ★ 必须和界面用的是同一套判据。这里曾经自己按目录名排序取第一个，
      多账号机器上取到的是**历史账号**，于是从 A 机导过来的会话全被并进了
      一个早就不用的账号里 —— 界面上「本机账号」干干净净，历史账号里多出一堆。

    会话导入后 `user_id` 会被统一改写成这个 uid，
    效果就是「整合到本机当前登录的账号」。
    """
    return accounts.detect_primary_uid(base)


# ------------------------------------------------------------ 本地内容索引


class _LocalContent:
    """本地**未删除**会话的内容索引 —— 认出「同一段对话的另一份副本」。

    ## 为什么导入也需要它

    导入器原本只按 cid 判重：`find_project_jsonl(cid)` 找得到就比指纹，找不到就
    新增。这在「同一个包导两次」时是安全的（第二次 cid 命中）。但换机搬运、参考
    工具、客户端自身都会造出**内容相同、cid 不同**的副本 —— 目标机器上明明躺着
    一段一模一样的对话，导入器看不见，于是又 ADD 一份，侧边栏里就成了两条。

    这与本机账号对齐里那个「清单里同一段对话出现两行」是同一个根因：判重只做了
    id 这一个维度。

    ## 代价

    包里的那条不用重算：`fingerprint.digest` 是导出时算好的归一化全文 sha256。
    本地这条按需算，且**只在字节数撞车时**才算（副本是 cid 等长替换，尺寸必相同；
    尺寸在本地唯一，就不可能是同一段对话，不值得读盘）。
    """

    def __init__(self, base: str | os.PathLike[str] | None = None) -> None:
        self._bodies = copysession._bodies_index(base)
        self._by_size: dict[int, list[str]] = {}
        # uid -> 字节数 -> [cid]。给「校正归属」用：改归属之前得先看**目标账号
        # 自己**是不是已经有一段一样的，否则把它搬过来就制造了账号内重复。
        self._by_uid: dict[str, dict[int, list[str]]] = {}
        self._digests: dict[str, str] = {}
        self._cache = fpcache.open_cache(base)

        db = paths.workbuddy_db_path(base)
        if not db.is_file():
            return
        try:
            with dbcopy.readonly_db_copy(db) as conn:
                rows = conn.execute(
                    'SELECT id, user_id FROM "sessions" WHERE deleted_at IS NULL'
                ).fetchall()
        except sqlite3.Error:
            # 读不到就不做内容判重：退回只按 cid 判重，宁可多导入也不误跳过
            return
        # ★ 只认「未删除」：本地那条已删除的话，用户切过去看不到，
        #   算作「已有」等于这次导入白做。
        for cid, uid in rows:
            cid = str(cid or "")
            found = self._bodies.get(cid)
            if not found:
                continue
            # ⚠️ 索引量的是**原始字节数**（与本机正文一致），查询侧量的是包内 body_size，
            #   两者都可能带 CRLF。这里不逐条读正文去归一化（那等于每次导入都把库读一遍），
            #   改由 `_size_probe` 按行数把上下邻居一并纳入候选。
            try:
                size = found[0].stat().st_size
            except OSError:
                continue
            if size < MIN_DEDUP_BYTES:
                continue
            self._by_size.setdefault(size, []).append(cid)
            owner = str(uid or "")
            if owner:
                self._by_uid.setdefault(owner, {}).setdefault(size, []).append(cid)

    def close(self) -> None:
        self._cache.close()

    def _digest_of(self, cid: str) -> str:
        got = self._digests.get(cid)
        if got is not None:
            return got
        out = ""
        for p in self._bodies.get(cid, []):
            fp = self._cache.resolve(cid, p)
            if fp is not None and fp.digest:
                out = fp.digest
                break
        self._digests[cid] = out
        return out

    def _size_probe(self, size: int, line_count: int) -> tuple[int, ...]:
        """字节数预筛的候选尺寸 —— 必须容忍「CRLF 副本」。

        ★ 2026-11-03 实测：本地那条是 LF、包里那条是 CRLF（旧版本复制会话写盘时
          做了换行转换），两者内容完全等价，字节数却正好差「行数」个字节。
          预筛只认**完全相同的字节数**，这对副本就永远撞不上车 —— 于是判重失效，
          导入后侧边栏多出一条同名会话（用户实际看到的就是这个）。

          而 `fingerprint.digest` 是**文本模式**读出来的（`\\r\\n` 已归一成 `\\n`），
          所以摘要本来就相等，问题只在预筛这一关。按行数把上下邻居一并纳入候选，
          代价是几次字典查找，不会多读盘。

          末行没有换行符的文件会差一个字节，故把 `line_count ± 1` 也带上。
        """
        if line_count <= 0:
            return (size,)
        out = [size]
        for delta in (line_count - 1, line_count, line_count + 1):
            for cand in (size - delta, size + delta):
                if cand >= MIN_DEDUP_BYTES and cand not in out:
                    out.append(cand)
        return tuple(out)

    def twin_of(self, size: int, digest: str, line_count: int = 0) -> str:
        """本地是否已有内容等价的那条。返回它的 cid，没有则空串。"""
        if not digest or size < MIN_DEDUP_BYTES:
            return ""
        for probe in self._size_probe(size, line_count):
            for cid in self._by_size.get(probe, ()):
                if self._digest_of(cid) == digest:
                    return cid
        return ""

    def twin_in(
        self, uid: str, size: int, digest: str, exclude: str = "", line_count: int = 0
    ) -> str:
        """**指定账号**里是否已有内容等价的那条（`exclude` 是包里那条自己的 cid）。

        改归属之前必须先问这一句：否则把 A 的会话搬到 B，而 B 名下一模一样的
        副本早就躺着，B 就凭空多出一段重复对话 —— 本机实测一次导入制造了 9 组。
        """
        if not uid or not digest or size < MIN_DEDUP_BYTES:
            return ""
        bucket = self._by_uid.get(uid, {})
        for probe in self._size_probe(size, line_count):
            for cid in bucket.get(probe, ()):
                if cid == exclude:
                    continue
                if self._digest_of(cid) == digest:
                    return cid
        return ""


# ------------------------------------------------------------ 计划

# 毫秒时间戳的合理下限（1973 年左右）。小于这个数按「秒」再乘 1000。
_TS_MIN_MS = 10**11

# 「末行」最多回看多少行：末行可能是空白或心跳行，往上找几行仍属同一次续写
_TS_LOOKBACK = 20


def _parse_ts(value) -> int:
    """把正文里的 timestamp 归一成毫秒。认不出就返回 0。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return n if n >= _TS_MIN_MS else n * 1000


def _ts_label(ms: int) -> str:
    """给人看的时间标签：当天只给时分，跨天带日期。"""
    if not ms:
        return "时间未知"
    try:
        t = time.localtime(ms / 1000)
    except (OverflowError, OSError, ValueError):
        return "时间未知"
    today = time.localtime()
    if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", t)
    return time.strftime("%m-%d %H:%M", t)


def _line_ts(raw: bytes) -> int:
    try:
        d = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return 0
    return _parse_ts(d.get("timestamp")) if isinstance(d, dict) else 0


def body_last_ts(path: str | os.PathLike[str] | None) -> int:
    """正文**末行**的 timestamp（毫秒）。取不到返回 0。

    ★ 为什么不用 `sessions.updated_at` 判「谁续写更晚」：改标题、校正归属、
      清删除标记都会刷新 updated_at，而正文一个字没变。用它选「保留哪一份」，
      会把「只是被工具碰过」的那份误判成更新 —— 那正是要避免的错误覆盖。

    ★ 为什么只看末行、不取最大值：末行才是这次续写的落点；取最大值会被早期的
      异常时间戳（时钟回拨、从别的机器带来的旧数据）带偏。

    只读文件尾部 256 KB：一行的体积通常远小于这个数，够用且是常数开销。
    """
    if not path:
        return 0
    p = Path(path)
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return 0
            take = min(size, 256 * 1024)
            f.seek(size - take)
            blob = f.read(take)
    except OSError:
        return 0
    lines = blob.split(b"\n")
    # 从尾部取，跳过文件末尾的空行
    checked = 0
    for raw in reversed(lines):
        if not raw.strip():
            continue
        ts = _line_ts(raw)
        if ts:
            return ts
        checked += 1
        if checked >= _TS_LOOKBACK:
            break
    return 0


def _bundle_last_ts(b: "bundle_mod.Bundle", s: "bundle_mod.BundleSession") -> int:
    """包内那条会话的末行 timestamp。只在分叉时才调用（罕见），可以整条流过。

    用流式迭代 + 固定长度的环形缓冲：大包里的长会话不会一次性读进内存。
    """
    tail: list[bytes] = []
    try:
        for raw in b.iter_body_lines(s):
            tail.append(raw)
            if len(tail) > _TS_LOOKBACK:
                del tail[0]
    except (KeyError, OSError):
        return 0
    for raw in reversed(tail):
        if not raw.strip():
            continue
        ts = _line_ts(raw)
        if ts:
            return ts
    return 0


def _require_row(row: dict[str, object], cid: str, local_uid: str) -> dict[str, object]:
    """补齐 sessions 表的 NOT NULL 列并校正归属。"""
    out = {k: v for k, v in row.items() if v is not None}
    out["id"] = cid
    for key, default in _REQUIRED_DEFAULTS.items():
        if out.get(key) in (None, ""):
            out[key] = default
    if local_uid:
        out["user_id"] = local_uid
    return out


def local_session_state(base: str | os.PathLike[str] | None = None) -> dict[str, dict]:
    """cid -> {user_id, deleted_at, cwd}。只读，失败返回空表。

    deleted_at 非 NULL 表示客户端里已删除（软删除）。老版本客户端没有这列，
    此时整个字段按 None 处理，退化成"只看归属"。

    cwd 是**本机这一行**的工作目录 —— 分叉「保留两份」要靠它给副本找落点：
    副本应当落在原会话所在的那个工程里，而不是照着源机器的路径去猜。
    """
    from . import dbcopy

    db_path = paths.workbuddy_db_path(base)
    if not db_path.is_file():
        return {}
    try:
        with dbcopy.readonly_db_copy(db_path) as conn:
            cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
            if "deleted_at" in cols:
                return {
                    str(r[0]): {
                        "user_id": str(r[1] or ""),
                        "deleted_at": r[2],
                        "cwd": str(r[3] or ""),
                    }
                    for r in conn.execute(
                        "SELECT id, user_id, deleted_at, cwd FROM sessions"
                    )
                }
            return {
                str(r[0]): {
                    "user_id": str(r[1] or ""),
                    "deleted_at": None,
                    "cwd": str(r[2] or ""),
                }
                for r in conn.execute("SELECT id, user_id, cwd FROM sessions")
            }
    except Exception:  # noqa: BLE001 - 读不到就当没有，不影响主流程
        return {}


def local_owner_map(base: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """cid -> 本地 user_id（兼容旧调用）。"""
    return {cid: st.get("user_id", "") for cid, st in local_session_state(base).items()}


def apply_div_choice(
    a: ImportAction,
    keep: str,
    base: str | os.PathLike[str] | None = None,
) -> None:
    """按取舍结果改写一条分叉动作。**plan 与界面共用同一条规则**。

    界面切单选时直接调它，而不是重新 `plan_import`：后者的代价是重扫全部本地
    正文（指纹 + 内容索引），128 条的包每切一次都重算一遍，交互直接卡死。

    keep 取值：`src`（取包内，整条替换）/ `local`（保留本机）/ `both`（两份都留）。
    """
    s = a.session
    if keep not in ("src", "local", "both"):
        keep = "local"
    a.div_keep = keep
    a.dropped_lines = 0
    both = (
        f"共同旧版 {a.div_common} 行 · "
        f"本机 +{a.div_local_new} · 包内 +{a.div_src_new}"
    )

    if keep == "src":
        local_path = paths.find_project_jsonl(s.cid, base)
        if local_path is None or not Path(local_path).is_file():
            # 本地正文在计划之后被移走了（另一个窗口跑了修复/清理）。
            # 这时「替换」没有对象，退化成保留本机：不写，比写错强。
            keep = "local"
        else:
            a.action = OVERWRITE
            a.body_target = Path(local_path)
            a.dropped_lines = a.div_local_new
            a.reason = (
                f"分叉（{both}）：包内续写更晚"
                f"（{_ts_label(a.src_ts)} > {_ts_label(a.local_ts)}），"
                f"用包内替换本地，丢弃本机 +{a.div_local_new} 行（已单独备份）"
            )
            return

    if keep == "both":
        if not a.new_cid:
            a.new_cid = str(uuid.uuid4())
        # 副本该落在哪：**本机这一行原来的 cwd** 优先 —— 两份是同一段对话，
        # 理应在同一个工程里，而且那个目录几乎一定已经存在。
        #
        # ★ 不能在这里调 rehome.default_new_cwd：它会做「换用户名」的猜测，
        #   把 8.3 短名（`USERNA~1`）当成另一个用户换成 `Username`，
        #   于是「就地重建」变成了「在另一个目录重建」—— 同一个工程被劈成两份。
        #   目录丢了就直接按原路径建回来，这正是 rehome 对「新路径==旧路径」的口径。
        cwd = a.remapped_cwd or a.local_cwd
        if not cwd:
            # 本机没有这一行（分叉理论上不会走到），只能退回猜测
            cwd = rehome.default_new_cwd(s.cwd)
        if cwd:
            a.remapped_cwd = rehome.normalize(cwd) or cwd
        a.action = DIVERGED_BOTH
        a.body_target = (
            paths.projects_dir(base)
            / paths.escape_workspace_path(cwd)
            / f"{a.new_cid}.jsonl"
        )
        a.reason = (
            f"分叉（{both}）：两份都保留，包内那份以新 id "
            f"{a.new_cid[:8]} 落进来（+{a.div_src_new} 行）"
        )
        return

    a.div_keep = "local"
    a.action = LOCAL_NEWER
    a.body_target = None
    a.dropped_lines = a.div_src_new
    a.reason = (
        f"分叉（{both}）：本机续写更晚"
        f"（{_ts_label(a.local_ts)} > {_ts_label(a.src_ts)}），"
        f"保留本机，可回传给对方"
    )


def plan_import(
    b: bundle_mod.Bundle,
    base: str | os.PathLike[str] | None = None,
    with_fingerprint: bool = True,
    progress=None,
    dedup_content: bool = True,
    div_choices: dict[str, str] | None = None,
) -> list[ImportAction]:
    """比对包与本机，产出逐条决策。**不写入任何数据**。

    `dedup_content` —— 新增前先查「本地是否已有一段内容相同、只是 cid 不同的
    对话」。默认开：关掉它就会在换机搬运的场景下把同一段对话导入第二份。

    `div_choices` —— 分叉项的人工覆盖：`{cid: "src" | "local" | "both"}`。
    不传或某项缺失时按「正文末行 timestamp 更晚的一方」自动取舍，同刻取本地
    （不写 = 不会错，加法语义的铁律）。
    """
    actions: list[ImportAction] = []
    total = len(b.sessions)
    local_uid = detect_local_uid(base)
    labels = accounts.AccountLabels.load(base)
    states = local_session_state(base)
    # 只在本次确实要新增时才建索引：全是 skip 的重复导入不必扫一遍本地正文
    content = _LocalContent(base) if dedup_content and b.sessions else None

    try:
        for i, s in enumerate(b.sessions):
            act = ImportAction(session=s, action=SKIP_NO_BODY, local_cid=s.cid)

            if not s.has_body:
                act.reason = "包内无正文"
                actions.append(act)
                continue

            local_path = paths.find_project_jsonl(s.cid, base)

            if local_path is None:
                cwd = s.cwd
                if not cwd:
                    act.action = SKIP_NO_CWD
                    act.reason = "会话元数据缺少 cwd，无法确定正文落点"
                else:
                    act.action = ADD
                    act.body_target = (
                        paths.projects_dir(base)
                        / paths.escape_workspace_path(cwd)
                        / f"{s.cid}.jsonl"
                    )
                    twin = (
                        content.twin_of(
                            s.body_size, s.fingerprint.digest, s.fingerprint.line_count
                        )
                        if content is not None and s.fingerprint is not None
                        else ""
                    )
                    if twin:
                        # 同一段对话的另一份副本已经在本地躺着。复制过去会让它在
                        # 侧边栏里出现两次 —— 展示、标注、默认不写，用户执意要
                        # 两份时仍可强制导入（与对齐里的「已有相同内容」同口径）。
                        act.action = DUPLICATE
                        act.duplicate_of = twin
                        act.reason = (
                            f"本地已有内容完全相同的会话（{twin[:8]}，只是 id 不同），"
                            "导入后会出现两份"
                        )
                actions.append(act)
                if progress:
                    progress(i + 1, total)
                continue

                # 本地已有正文 —— 必须做内容级比对，不能只看行数
            local_fp = fingerprint_of_file(local_path, s.cid) if with_fingerprint else None
            if local_fp is None or s.fingerprint is None:
                act.action = BLOCK_UNKNOWN
                act.reason = "缺少指纹，无法判定是否可安全追加"
                act.local_line_count = local_fp.line_count if local_fp else 0
                actions.append(act)
                if progress:
                    progress(i + 1, total)
                continue

            # 判据方向：compare(local, source)
            #   BEHIND 表示 local 是 source 的前缀 -> 可追加
            res = compare(local_fp, s.fingerprint)
            act.local_line_count = local_fp.line_count

            if res.verdict == SYNCED:
                state = states.get(s.cid) or {}
                owner = str(state.get("user_id") or "")
                local_deleted = state.get("deleted_at") is not None

                if local_deleted:
                    # 客户端里删掉了，但包里还有 —— 重导就该把它恢复，
                    # 否则用户「删了想再要回来」这件事永远做不到。
                    act.action = RESTORE
                    act.reason = f"本地已删除，内容一致（{local_fp.line_count} 行），将恢复"
                elif local_uid and owner and owner != local_uid:
                    # 内容一样但归属错了（历史遗留导入、或换了登录账号）——
                    # 只需要改 user_id，不用碰正文。没有这一步，导错的会话
                    # 会因为「内容一致」被永远跳过去，再怎么重导都修不回来。
                    #
                    # ★ 但改归属之前必须再看一眼**当前账号自己**：如果它名下已经有
                    #   一段一模一样的对话（不同 cid），把这条搬过来就是凭空多一份。
                    #   本机实测一次导入这样造出了 9 组账号内重复（reconcile 的 A1 抓到）。
                    keep = (
                        content.twin_in(
                            local_uid,
                            s.body_size,
                            s.fingerprint.digest,
                            s.cid,
                            s.fingerprint.line_count,
                        )
                        if content is not None and s.fingerprint is not None
                        else ""
                    )
                    if keep:
                        act.action = SKIP_SYNCED
                        act.reason = (
                            "内容一致，且当前账号里已有同一段对话（只是 id 不同），"
                            "再搬过来会让侧边栏出现两份"
                        )
                    else:
                        # 文案里写账号**名字**，不写 uuid 前 8 位：这一行是给用户
                        # 判断「要不要让它搬走」的唯一依据，看到 `4316c067` 他只会
                        # 更困惑（实测就是被这么反馈的）。
                        act.action = REHOME
                        act.reason = (
                            f"内容一致（{local_fp.line_count} 行），"
                            f"归属将从「{labels.display(owner)}」改到「{labels.display(local_uid)}」"
                        )
                else:
                    act.action = SKIP_SYNCED
                    act.reason = f"两端一致（{local_fp.line_count} 行）"
            elif res.verdict == BEHIND:
                if local_fp.line_count < s.fingerprint.line_count:
                    act.action = APPEND
                    act.append_from = local_fp.line_count
                    act.body_target = local_path
                    act.reason = (
                        f"本地落后 {s.fingerprint.line_count - local_fp.line_count} 行，可安全追加"
                    )
                else:
                    # 源比本地短但本地是源前缀 —— 本地更新，无需回退（加法语义）。
                    # 单独成一个动作而不是混进「一致·跳过」：这批是「回传候选」，
                    # 官方导入最缺的就是把它们交还给另一台。
                    act.action = LOCAL_NEWER
                    act.local_ts = body_last_ts(local_path)
                    act.src_ts = _bundle_last_ts(b, s)
                    act.reason = (
                        f"本地更新（{local_fp.line_count} 行 ≥ 源 {s.fingerprint.line_count} 行），"
                        "可回传给对方"
                    )
            elif res.verdict == PROBABLY_SYNCED:
                # 行数相同 + 前缀一致，但第 512 行之后没有依据。
                # 只在「改归属 / 清删除标记」这类不动正文的动作上采信这个结论。
                state = states.get(s.cid) or {}
                owner = str(state.get("user_id") or "")
                if state.get("deleted_at") is not None:
                    act.action = RESTORE
                    act.reason = f"本地已删除，行数相同且前缀一致，将恢复"
                elif local_uid and owner and owner != local_uid:
                    act.action = REHOME
                    act.reason = f"行数相同、前 512 行一致，将校正归属（不修改正文）"
                else:
                    act.action = SKIP_SYNCED
                    act.reason = f"行数相同且前缀一致（{local_fp.line_count} 行）"
            elif res.verdict == DIVERGED:
                # 分叉不拼接（拼接会编造一段不存在的历史），只做整条取舍。
                # 判据是**正文末行 timestamp**，不是 updated_at —— 见 body_last_ts。
                act.local_ts = body_last_ts(local_path)
                act.src_ts = _bundle_last_ts(b, s)
                keep = (div_choices or {}).get(s.cid, "")
                if keep not in ("src", "local", "both"):
                    # 同刻取本地：不写就不会错，加法语义的铁律
                    keep = "src" if act.src_ts > act.local_ts else "local"
                act.local_cwd = str((states.get(s.cid) or {}).get("cwd") or "")
                act.div_keep = keep
                act.div_common = res.common
                act.div_local_new = max(0, local_fp.line_count - res.common)
                act.div_src_new = max(0, s.fingerprint.line_count - res.common)
                apply_div_choice(act, keep, base)
            else:
                act.action = BLOCK_UNKNOWN
                act.reason = res.note or "无法判定"

            actions.append(act)
            if progress:
                progress(i + 1, total)

        return actions
    finally:
        if content is not None:
            content.close()


# ------------------------------------------------------------ 执行


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _needs_leading_newline(p: Path) -> bool:
    """目标文件非空且末字节不是换行 —— 参考项目踩过的坑：
    不补这个换行，追加的第一行会粘到原末行上，JSON 直接解析失败。
    """
    try:
        size = p.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    with open(p, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) != b"\n"


def _write_body_new(
    z: zipfile.ZipFile, s: bundle_mod.BundleSession, dst: Path, prefix: str = ""
) -> None:
    """把包里的正文原样写到 dst。

    ★ `prefix` 不能省：同步包 v2 把会话节放在 `sessions/` 下，`body_member` 是
      **相对本节根**的路径。少了它 `z.open()` 会抛
      `There is no item named 'bodies/x.jsonl' in the archive` —— 而且只在
      「新增」时暴露：改归属/跳过根本不读正文，所以一整批导入里可能只失败一条。
    """
    _ensure_parent(dst)
    tmp = dst.with_name(dst.name + ".wbsync-tmp")
    with z.open(prefix + s.body_member) as src, open(tmp, "wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dst)


def _write_body_new_cid(
    z: zipfile.ZipFile,
    s: bundle_mod.BundleSession,
    dst: Path,
    new_cid: str,
    prefix: str = "",
) -> None:
    """写正文并把里面的 cid 换成新 cid —— 「保留两份」用。

    两份副本必须有各自的 id，否则客户端会把它们认成同一条。

    ★ 正文里可能出现非 UTF-8 字节：解不出来就原样写入，宁可 id 没改（副本
      在侧边栏里可能被认成重复），也不要把内容改坏 —— 内容不可重建，id 可再改。
    """
    _ensure_parent(dst)
    tmp = dst.with_name(dst.name + ".wbsync-tmp")
    with z.open(prefix + s.body_member) as src:
        raw = src.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        tmp.write_bytes(raw)
    else:
        # 与 copysession 同一套边界断言：裸 replace 会改坏恰好含该串的其他字段
        tmp.write_text(
            copysession.rewrite_cid(text, s.cid, new_cid), encoding="utf-8", newline=""
        )
    os.replace(tmp, dst)


def _append_body(
    z: zipfile.ZipFile,
    s: bundle_mod.BundleSession,
    dst: Path,
    skip: int,
    prefix: str = "",
) -> int:
    """把源端第 skip 行之后的内容追加到 dst，返回追加行数。

    ⚠️ 用二进制写、按字节处理换行：正文里可能有非 UTF-8 字节，
       文本模式解码再编码会造成静默改动。
    `prefix` 同 `_write_body_new`。
    """
    lead = b"\n" if _needs_leading_newline(dst) else b""
    written = 0
    with z.open(prefix + s.body_member) as src, open(dst, "ab") as out:
        if lead:
            out.write(lead)
        for idx, raw in enumerate(src):
            if idx < skip:
                continue
            out.write(raw if raw.endswith(b"\n") else raw + b"\n")
            written += 1
    return written


def _create_edge_db_if_missing(p: Path) -> bool:
    """edge 库不存在时按官方建表语句新建。返回是否新建。"""
    if p.is_file():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=guard.BUSY_TIMEOUT)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS edge_sync_mapping (
                session_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                msg_channel TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                PRIMARY KEY (session_id)
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_sync_mapping_conv "
            "ON edge_sync_mapping(conversation_id)"
        )
        conn.commit()
    finally:
        conn.close()
    return True


def downgrade_div_overwrite(actions: list[ImportAction]) -> int:
    """把「拿包内覆盖本机」降级成「保留本机」，返回降级了几条。

    ★ 界面与执行必须调同一个函数：同步包对话框按 actions 画表，若表上写
      「分叉·取包内」而执行时又悄悄不覆盖，用户看到的和发生的就不是一回事。
    """
    n = 0
    for a in actions:
        if a.action != OVERWRITE:
            continue
        a.action = LOCAL_NEWER
        a.div_keep = "local"
        a.dropped_lines = a.div_src_new
        a.body_target = None
        a.reason = (
            f"分叉：未确认覆盖，保留本机（包内 +{a.div_src_new} 行未采用，"
            "可在合并面板里逐条选「保留包内」）"
        )
        n += 1
    return n


def _ensure_landing_dir(new_cwd: str) -> None:
    """落点目录不存在就建出来。

    客户端打开会话要先落到 cwd，目录不在会卡在「信任此文件夹」上 ——
    与 rehome 的处理口径一致。建失败不阻断：正文已经在 projects 目录里，
    客户端仍然能找到内容。
    """
    if not new_cwd:
        return
    target = Path(new_cwd)
    if target.is_dir():
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def execute_import(
    actions: list[ImportAction],
    b: bundle_mod.Bundle,
    base: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    allow_blocking: bool = False,
    cwd_remaps: dict[str, str] | None = None,
    progress=None,
    include_duplicates: bool = False,
    allow_div_overwrite: bool = False,
) -> ImportReport:
    """执行导入。

    dry_run=True 时只算不写（用于演练）。
    allow_blocking=False 时，分叉/未知项会被剔除，其余照常导入。
    include_duplicates=True 时，「本地已有同内容副本」的项也照样写入 ——
    用户执意要两份独立副本时用，默认 False（与对齐里「重复项默认不勾」同口径）。

    `allow_div_overwrite` —— **要不要真的拿包内版本盖掉本地正文**。
    默认 False：分叉一律退化成「保留本机」。覆盖是本工具唯一会丢数据的动作，
    必须在**用户看得见取舍的界面**上得到明确同意（合并面板）才能进行；
    同步包那种「一屏表格 + 一键导入」的入口没有这个选择，就不能默认覆盖 ——
    否则老版本「分叉一律拒绝」的保险就被悄悄拆掉了。
    """
    report = ImportReport(actions=list(actions), dry_run=dry_run)

    if not allow_div_overwrite:
        downgrade_div_overwrite(actions)

    todo = [a for a in actions if a.will_write or (include_duplicates and a.overridable)]
    if not allow_blocking:
        # 被拒绝的项本来就不在 will_write 里，这里只是防御
        todo = [a for a in todo if not a.blocking]
    if not todo:
        return report

    if dry_run:
        report.applied = [a.session.cid for a in todo]
        return report

    # ---- cwd 重映射：把勾选的「源 cwd → 本机路径」作用到 ADD 会话上
    if cwd_remaps:
        for a in todo:
            if not a.add_like:      # 被强制导入的重复项走同一条新增路径
                continue
            key = rehome.normalize(a.session.cwd)
            new = cwd_remaps.get(key)
            if not new:
                continue
            # 归一化后再落库：UI 传进来的是用户手输的原始文本（可能大写盘符、
            # 带尾分隔符）。若原样写进 sessions.cwd，会与记忆 key / rehome.scan
            # 的归一化口径不一致，后续聚合、显示反复。这里统一按 normalize 存。
            new = rehome.normalize(new)
            if not new:
                continue
            a.remapped_cwd = new
            a.body_target = (
                paths.projects_dir(base)
                / paths.escape_workspace_path(new)
                / f"{a.effective_cid}.jsonl"
            )

    # 铁律 1：客户端没退出就别写
    guard.assert_safe_to_write()

    # 所有要落到 cwd 的项，占位目录都得建出来（无论在不在 UI 的映射表里）。
    # ★ 分叉「保留两份」会在 apply_div_choice 里自己算落点 —— 它不在 cwd_remaps
    #   里，早期只在映射循环内 mkdir，于是副本挂在一个不存在的目录上，客户端里
    #   就多出一个点不开的空间。
    # ★ 放在铁律之后：建目录也是写盘，客户端没退时不该动。
    for a in todo:
        if a.add_like and a.remapped_cwd:
            _ensure_landing_dir(a.remapped_cwd)

    local_uid = detect_local_uid(base)
    db_path = paths.workbuddy_db_path(base)
    edge_path = paths.edge_sync_db_path(base)

    # 铁律 3：整批只备份一次，只备份本批会动到的文件
    to_backup: list[Path] = []
    if db_path.is_file():
        to_backup.append(db_path)
    if edge_path.is_file():
        to_backup.append(edge_path)
    for a in todo:
        # APPEND 追加、OVERWRITE 整条替换 —— 两种都会改动已有的正文文件，
        # 全部纳入整批备份。OVERWRITE 另外再进 merge 专用备份（可单批回滚）。
        if a.action in (APPEND, OVERWRITE) and a.body_target is not None:
            to_backup.append(a.body_target)
    report.backup = guard.create_backup(to_backup, base=base, label="import")

    overwritten = [a.body_target for a in todo if a.action == OVERWRITE and a.body_target]
    if overwritten:
        report.merge_backup = guard.create_merge_backup(overwritten, base=base)

    # ---------------- 阶段一：正文（只碰互不相干的文件）
    with zipfile.ZipFile(b.path) as z:
        for a in todo:
            s = a.session
            try:
                if a.action == DIVERGED_BOTH:
                    assert a.body_target is not None
                    _write_body_new_cid(z, s, a.body_target, a.new_cid, b.prefix)
                elif a.add_like:
                    assert a.body_target is not None
                    _write_body_new(z, s, a.body_target, b.prefix)
                elif a.action == APPEND:
                    assert a.body_target is not None
                    _append_body(z, s, a.body_target, a.append_from, b.prefix)
                elif a.action == OVERWRITE:
                    assert a.body_target is not None
                    _write_body_new(z, s, a.body_target, b.prefix)
                report.applied.append(a.effective_cid)
            except KeyError:
                # zip 里没有这个成员。用户看到的是英文的
                # "There is no item named 'bodies/x.jsonl' in the archive"，
                # 既不懂又不知道是哪条 —— 换成中文，并记下标题供报告展示。
                report.fail(
                    s.cid, s.title, "包内缺少这段正文，包可能不完整（重新导出一次试试）"
                )
            except Exception as e:  # 单条失败不拖垮整批
                report.fail(s.cid, s.title, f"正文写入失败：{e}")
            if progress:
                progress(len(report.applied) + len(report.failed), len(todo))

    done = [a for a in todo if a.effective_cid in set(report.applied)]
    if not done:
        return report

    # ---------------- 阶段二：数据库（顺序写，一次一库）
    try:
        _write_sessions_db(
            db_path, b, done, local_uid, cwd_spellings=_local_cwd_spellings(db_path)
        )
    except Exception as e:
        report.failed.append(("workbuddy.db", f"写入失败：{e}"))

    try:
        _write_edge_db(edge_path, b, done, local_uid)
    except Exception as e:
        report.failed.append(("edge-sync-mapping", f"写入失败：{e}"))

    # ---------------- 阶段三：写后校验
    if not guard.verify_db_readable(db_path, "sessions"):
        report.failed.append(("workbuddy.db", "写后校验失败：库不可读或 sessions 表丢失"))
    if not guard.verify_db_readable(edge_path, "edge_sync_mapping"):
        report.failed.append(("edge-sync-mapping", "写后校验失败：库不可读或表丢失"))

    # ---------------- 阶段四：记账
    # 导入的会话进不了客户端侧边栏快照（那是客户端自己写的文件），
    # 不记这一笔，「本地可见」默认视图里就一条都看不到，像导入失败了一样。
    if report.applied:
        try:
            log = importlog.ImportLog.load(base)
            log.add(report.applied, uid=local_uid, source_host=str(b.source_host or ""))
            log.save()
        except OSError as e:
            report.failed.append(("importlog", f"导入记录保存失败：{e}"))

    if not local_uid:
        # 这是提示不是失败：会话确实写进去了，只是没改归属
        report.warnings.append(
            "未能判定本机当前账号，导入的会话会保留源账号归属。"
            "请先启动一次 WorkBuddy 完成登录后再导入，才能统一整合到本机账号。"
        )

    return report


def _local_cwd_spellings(db_path: Path) -> dict[str, str]:
    """归一化 cwd → 本机库里已有的**原始写法**（取出现次数最多的那种）。

    ★ 2026-11-03 实测的坑：从另一台机器导进来的会话 cwd 是
      `h:/project/demo`，本机客户端一直写 `H:/project/demo`。
      两种写法在 sessions 表里并存，会话页就裂成两个同名工作空间分组，
      用户看到的是「导入后重复了」。所以落库前先问一句本机怎么写，沿用它的写法。

    读不到就返回空表（fail-open）：宁可沿用包里的写法，也不能因此不导入。
    """
    out: dict[str, str] = {}
    if not db_path.is_file():
        return out
    try:
        with dbcopy.readonly_db_copy(db_path) as conn:
            rows = conn.execute(
                'SELECT cwd, COUNT(*) FROM "sessions" '
                "WHERE cwd IS NOT NULL AND cwd <> '' GROUP BY cwd"
            ).fetchall()
    except Exception:  # noqa: BLE001 - 读不到就按包里的写法写，不影响主流程
        return out

    counts: dict[str, int] = {}
    for raw, cnt in rows:
        text = str(raw or "")
        key = rehome.normalize(text).lower()
        if not key:
            continue
        n = int(cnt or 0)
        if key not in counts or n > counts[key]:
            counts[key] = n
            out[key] = text
    return out


def _cwd_spelling_of(spellings: dict[str, str], cwd: str) -> str:
    """在「本机已有写法」表里查这条 cwd 的等价路径（没有则空串）。

    键用**全小写**而不是 `rehome.normalize` 的「只小写盘符」：Windows 路径整体
    不区分大小写，`f:/demo_test` 与 `F:/DEMO_TEST` 是同一个目录；只比盘符的话，
    旧版本写进去的各种大小写变体仍然匹配不上，分组照样会裂。
    """
    key = rehome.normalize(cwd).lower()
    return spellings.get(key, "") if key else ""


def _write_sessions_db(
    db_path: Path,
    b: bundle_mod.Bundle,
    done: list[ImportAction],
    local_uid: str,
    cwd_spellings: dict[str, str] | None = None,
) -> None:
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到会话数据库：{db_path}")

    with guard.DbEditSession(db_path) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
        if not cols:
            raise RuntimeError("sessions 表不存在，客户端结构可能已变更")

        for a in done:
            s = a.session
            if a.add_like:
                # 「保留两份」时这条要落成新 cid，索引行也必须用新 id
                row = _require_row(s.row, a.effective_cid, local_uid)
                # 导进来的会话一律是"活着"的：即便源端那条已删除，
                # 用户主动导入就说明他想要它回来
                row.pop("deleted_at", None)
                # cwd 被重映射过：用本机路径替换源机路径，正文落点也跟着变
                if getattr(a, "remapped_cwd", ""):
                    # 重映射算出来的写法可能与本机既有写法只差盘符大小写
                    # （`c:/x` vs `C:/x`）。不拉齐的话，同一个目录在库里会有
                    # 两种写法，会话页裂成两个同名分组 —— 与下面那条兜底同一原因。
                    value = a.remapped_cwd
                    if cwd_spellings:
                        same = _cwd_spelling_of(cwd_spellings, value)
                        if same:
                            value = same
                    row["cwd"] = value
                elif cwd_spellings:
                    # 没重映射也要对齐写法：`h:/x` 与 `H:/x` 会让同一工作空间裂成两组
                    same = _cwd_spelling_of(cwd_spellings, str(row.get("cwd") or ""))
                    if same:
                        row["cwd"] = same
                row = {k: v for k, v in row.items() if k in cols}
                cols_sql = ", ".join(f'"{k}"' for k in row)
                marks = ", ".join("?" for _ in row)
                conn.execute(
                    f'INSERT OR REPLACE INTO "sessions" ({cols_sql}) VALUES ({marks})',
                    tuple(row.values()),
                )
            elif a.action in (APPEND, OVERWRITE):
                # 保留本地行，只把时间戳推到最新。
                # ⚠️ 列必须按需选取：sessions 表历经十几次 migration，
                #    硬编码 last_activity_at 会在旧版本库上直接报 no such column。
                #
                # OVERWRITE（分叉取舍）走同一条路径：正文已经整条换成包内那一版，
                # 索引行只推时间戳、校正归属、清删除标记。这里刻意不用
                # INSERT OR REPLACE —— 那会把本机独有列（group_id、buddy 绑定）
                # 一并抹掉，等于为了更新一行而重建一条会话。
                sel_cols = [c for c in ("updated_at", "last_activity_at") if c in cols]
                sel_sql = ", ".join(sel_cols) if sel_cols else "1"
                cur = conn.execute(
                    f'SELECT {sel_sql} FROM "sessions" WHERE id = ?', (s.cid,)
                ).fetchone()
                if cur is None:
                    # 有正文却没索引 —— 补一条，否则界面里看不到
                    row = _require_row(s.row, s.cid, local_uid)
                    row = {k: v for k, v in row.items() if k in cols}
                    cols_sql = ", ".join(f'"{k}"' for k in row)
                    marks = ", ".join("?" for _ in row)
                    conn.execute(
                        f'INSERT OR REPLACE INTO "sessions" ({cols_sql}) VALUES ({marks})',
                        tuple(row.values()),
                    )
                    continue

                def _cur(name: str) -> int:
                    try:
                        return int(cur[name])
                    except (IndexError, KeyError, TypeError, ValueError):
                        return 0

                sets: list[str] = []
                params: list[object] = []
                if "updated_at" in cols:
                    sets.append("updated_at = ?")
                    params.append(max(_cur("updated_at"), int(s.row.get("updated_at") or 0)))
                if "last_activity_at" in cols:
                    sets.append("last_activity_at = ?")
                    params.append(
                        max(
                            _cur("last_activity_at"),
                            int(s.row.get("last_activity_at") or s.row.get("updated_at") or 0),
                        )
                    )
                # 追加的同时顺带校正归属：一次导入内把所有会话统一到本机账号
                if local_uid and "user_id" in cols:
                    sets.append("user_id = ?")
                    params.append(local_uid)
                # 会走到追加说明用户想把内容补回来，那删除标记也该一并清掉
                if "deleted_at" in cols:
                    sets.append("deleted_at = NULL")
                if sets:
                    params.append(s.cid)
                    conn.execute(
                        f'UPDATE "sessions" SET {", ".join(sets)} WHERE id = ?', tuple(params)
                    )
            elif a.action == REHOME:
                # 只动归属，正文与时间戳一概不碰
                if local_uid and "user_id" in cols:
                    conn.execute(
                        'UPDATE "sessions" SET user_id = ? WHERE id = ?', (local_uid, s.cid)
                    )
            elif a.action == RESTORE:
                # 清掉软删除标记，让会话重新出现在客户端里；正文不动
                fields: list[str] = []
                params2: list[object] = []
                if local_uid and "user_id" in cols:
                    fields.append("user_id = ?")
                    params2.append(local_uid)
                if "deleted_at" in cols:
                    fields.append("deleted_at = NULL")
                if fields:
                    params2.append(s.cid)
                    cur = conn.execute(
                        f'UPDATE "sessions" SET {", ".join(fields)} WHERE id = ?', tuple(params2)
                    )
                    if cur.rowcount == 0:
                        # 只有正文没有索引（客户端删行时漏了清理），补一条完整的
                        row = _require_row(s.row, s.cid, local_uid)
                        row.pop("deleted_at", None)
                        row = {k: v for k, v in row.items() if k in cols}
                        cols_sql = ", ".join(f'"{k}"' for k in row)
                        marks = ", ".join("?" for _ in row)
                        conn.execute(
                            f'INSERT OR REPLACE INTO "sessions" ({cols_sql}) VALUES ({marks})',
                            tuple(row.values()),
                        )


def _write_edge_db(
    edge_path: Path,
    b: bundle_mod.Bundle,
    done: list[ImportAction],
    local_uid: str,
) -> None:
    """注册云端映射（三件套的第三件）。

    conversation_id 上有 UNIQUE 索引，且与 session_id 同值，
    所以 INSERT OR REPLACE 能同时覆盖两种冲突。
    """
    _create_edge_db_if_missing(edge_path)

    with guard.DbEditSession(edge_path) as conn:
        for a in done:
            s = a.session
            cid = a.effective_cid
            # 归属本机账号：否则这条会话在云端会挂在别人的通道上，本机看不到
            channel = f"convmsg:{local_uid}" if local_uid else (s.cloud_channel or "")
            conn.execute(
                "INSERT OR REPLACE INTO edge_sync_mapping "
                "(session_id, conversation_id, msg_channel, created_at) VALUES (?,?,?,?)",
                (
                    cid,
                    cid,
                    channel,
                    int(s.cloud_created_at) or int(time.time()),
                ),
            )


# ------------------------------------------------------------ 分组视图


def back_candidates(actions: list[ImportAction]) -> list[ImportAction]:
    """「本地更新」那批 —— 回传包的输入。

    单独成一个动作（LOCAL_NEWER）而不是混进「一致·跳过」：这批会话本机有
    对方没有的续写，官方导入既不会带走也不会告知，只有把它们显式列出来，
    「回传」这个动作才有落点。
    """
    return [a for a in actions if a.back_candidate]


def diverged_actions(actions: list[ImportAction]) -> list[ImportAction]:
    """本次判定为分叉的那些（含已取舍完成的），给界面画对比卡用。"""
    return [a for a in actions if a.div_keep]


def dropped_lines(actions: list[ImportAction]) -> int:
    """本次取舍会丢掉的正文行数合计 —— 执行前必须让用户看到这个数。"""
    return sum(a.dropped_lines for a in actions)
