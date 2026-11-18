"""只读盘点：把本机会话的「三件套」状态摸清楚。

第一版**不写入任何数据**。产出每条会话的：
  - 元数据（workbuddy.db sessions 表）
  - 正文（projects/<workspace>/<cid>.jsonl）是否存在、多大
  - 内容指纹（行数 + 前 512 行 hash）
  - 云端映射（edge-sync-mapping-v{N}.db edge_sync_mapping 表）归属到哪个 uid

⚠️ sessions 表已历经十几次 migration，列可能增减，所有读取都做列存在性防御。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import dbcopy, paths, sidebar
from .fingerprint import SessionFingerprint, fingerprint_of_file

# 标题展示：custom_title 优先于自动生成 title（与参考项目 session_display_title 同款）
_TITLE_FIELDS = ("custom_title", "title")


@dataclass
class SessionRecord:
    """一条会话的盘点结果。"""

    cid: str
    user_id: str = ""
    title: str = ""
    cwd: str = ""
    status: str = ""
    transport: str = ""
    conversation_origin: str = ""
    created_at: int = 0
    updated_at: int = 0
    last_activity_at: int | None = None
    jsonl_path: Path | None = None
    jsonl_size: int | None = None
    fingerprint: SessionFingerprint | None = None
    cloud_channel: str | None = None  # convmsg:{uid}，None 表示未注册云端映射
    # 侧边栏可见性：出现在哪些账号的 sidebar-list-snapshot 里
    sidebar_uids: tuple[str, ...] = ()
    sidebar_space: str = ""
    sidebar_state: str = ""
    is_pinned: bool = False
    # 本工具导入过、但客户端还没把它写进侧边栏快照
    imported_at: int = 0
    imported_host: str = ""
    # 客户端软删除标记（毫秒时间戳；老版本用 -1 表示"已删除但无时间"）
    deleted_at: int | None = None
    # 会话挂在哪一个空间（`sessions.space_id`）。跨机导入时源机的空间 id 会被
    # 原样带过来，而本机 `spaces` 表里没有那一行 —— 客户端挂不上分组。
    space_id: str = ""
    # 上面那个空间在本机 `spaces` 表里能否解析到（表不存在时一律按可解析）。
    space_ok: bool = True
    # 侧边栏快照的写入时刻（毫秒）。用来判断「客户端有没有机会认领这条导入项」。
    sidebar_saved_at: int = 0

    @property
    def has_body(self) -> bool:
        return self.jsonl_path is not None and self.jsonl_path.is_file()

    @property
    def is_deleted(self) -> bool:
        """客户端里已删除。

        ★ 判据是「非 NULL」而不是「> 0」：实测本机 37 条非空记录里，
          22 条是毫秒时间戳、15 条是 -1，而**这 37 条全部不在侧边栏快照里**，
          55 条 NULL 则全部在。说明客户端把任何非 NULL 都当已删除。
          只判 > 0 会让那 15 条以"正常会话"的面目留在列表里。
        """
        return self.deleted_at is not None

    @property
    def space_dangling(self) -> bool:
        """挂着的空间在本机 `spaces` 表里找不到。

        ★ `space_id` 为空**不算**悬空：实测三个「一个空间都没有」的账号，
          会话 `space_id` 全是 NULL，侧边栏照常全量显示（客户端按 cwd 兜底归组）。
          只有「指着一个已经不存在的空间」才会挂不上分组 —— 本机 20 条未删除会话里
          12 条属于这种情况（另有 1 条 `space_id` 为空），客户端只渲染了其中一部分。
        """
        return bool(self.space_id) and not self.space_ok

    @property
    def in_sidebar(self) -> bool:
        """客户端侧边栏快照里能看到它。已删除的一律不算。"""
        return bool(self.sidebar_uids) and not self.is_deleted

    @property
    def imported_pending(self) -> bool:
        """本工具搬过来、还没被客户端认领（且没被删）。

        注意这只是「待认领」的事实，**不等于客户端会显示**：空间悬空的那批
        也在这个集合里，但它们挂不上分组。判断「算不算可见」看
        `visible_by_default`。
        """
        return self.imported_at > 0 and not self.in_sidebar and not self.is_deleted

    @property
    def sidebar_written_after_import(self) -> bool:
        """客户端在导入之后**重写过**侧边栏快照（却仍没把这条写进去）。

        有快照时刻才判：没有快照时无从比较，按「还没机会认领」处理。
        `imported_at` 是秒，`sidebar_saved_at` 是毫秒。
        """
        return bool(self.sidebar_saved_at) and self.imported_at * 1000 <= self.sidebar_saved_at

    @property
    def ignored_import(self) -> bool:
        """导入过、客户端已重写快照却没纳入 —— 客户端就是不认这条。

        实测 `b4ad0006`（cwd 是临时时间戳目录、space_id 为 NULL）就属于这类：
        导入后客户端写过快照（00:26）仍未收录它，界面上却按「刚导入」放行，
        于是凭空多出一个「对话（1）」分组（2026-11-14 用户反馈）。
        """
        return self.imported_pending and not self.space_dangling and self.sidebar_written_after_import

    @property
    def visible_by_default(self) -> bool:
        """默认视图是否展示 —— 严格对齐「客户端侧边栏里真有它」。

        = 快照里有 **或** 本工具刚导入且客户端**还没机会**重写快照。

        ★ 已删除的一律不展示 —— 用户在客户端删掉的会话，
          不该因为"本工具导入过"又冒出来。
        ★ 空间悬空的导入项不展示：客户端挂不上分组。
        ★ 客户端已重写快照却没纳入的导入项也不展示（`ignored_import`）：
          「刚导入」的宽限期只能用到客户端下一次写快照为止，否则就会像
          v1.15.3 那样多报一条客户端根本没有的会话。
        """
        if self.is_deleted:
            return False
        if self.in_sidebar:
            return True
        if not self.imported_at:
            return False
        return not self.space_dangling and not self.sidebar_written_after_import

    def visible_to(self, uid: str) -> bool:
        return not uid or uid in self.sidebar_uids

    @property
    def has_cloud_mapping(self) -> bool:
        return self.cloud_channel is not None

    @property
    def is_orphan(self) -> bool:
        """有元数据但找不到正文 —— 三件套缺件。"""
        return not self.has_body


def _display_title(row: dict[str, object]) -> str:
    for key in _TITLE_FIELDS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(无标题)"


def _safe_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def read_sessions(conn) -> list[SessionRecord]:
    """读 sessions 表。列缺失时回退到空值，绝不抛异常。"""
    cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
    if not cols:
        return []

    def pick(*names: str) -> str | None:
        for n in names:
            if n in cols:
                return n
        return None

    c_id = pick("id")
    if c_id is None:
        return []

    select = ", ".join(sorted(cols))
    records: list[SessionRecord] = []
    for row in conn.execute(f'SELECT {select} FROM "sessions"'):
        d = dict(row)
        updated = _safe_int(d.get("updated_at")) or 0
        records.append(
            SessionRecord(
                cid=str(d.get(c_id) or ""),
                user_id=str(d.get("user_id") or "") if "user_id" in cols else "",
                title=_display_title(d),
                cwd=str(d.get("cwd") or ""),
                status=str(d.get("status") or ""),
                transport=str(d.get("transport") or ""),
                conversation_origin=str(d.get("conversation_origin") or ""),
                created_at=_safe_int(d.get("created_at")) or 0,
                updated_at=updated,
                last_activity_at=_safe_int(d.get("last_activity_at")),
                # 列可能不存在（客户端版本差异），缺失时视为未删除
                deleted_at=_safe_int(d["deleted_at"]) if "deleted_at" in cols else None,
                space_id=str(d.get("space_id") or "") if "space_id" in cols else "",
            )
        )
    return [r for r in records if r.cid]


def read_space_ids(conn) -> set[str] | None:
    """本机 `spaces` 表里的空间 id 集合；**表不存在时返回 None**（区别于空表）。

    返回 None 表示这套客户端版本根本没有空间概念，调用方按「全部可解析」处理
    （fail-open）：否则老版本上所有会话都会被判成「空间悬空」，
    「可见」直接掉到 0，比不判还糟。

    ★ 不过滤 `deleted_at`：实测本机两行空间的 `deleted_at` 都是 -1 而客户端照常
      显示它们 —— 这一列在这张表里的含义与 `sessions` 表不同，按它过滤会把
      唯一两个真实空间也判成不存在。
    """
    if not dbcopy.table_exists(conn, "spaces"):
        return None
    try:
        return {str(r[0]) for r in conn.execute('SELECT id FROM "spaces"') if r[0]}
    except Exception:  # noqa: BLE001 - 只读诊断不该把盘点带崩
        return None


def read_cloud_map(conn) -> dict[str, str]:
    """读 edge_sync_mapping：cid -> msg_channel。表不存在返回空字典（fail-closed）。"""
    if not dbcopy.table_exists(conn, "edge_sync_mapping"):
        return {}
    out: dict[str, str] = {}
    for row in conn.execute("SELECT session_id, msg_channel FROM edge_sync_mapping"):
        out[str(row["session_id"])] = str(row["msg_channel"] or "")
    return out


@dataclass
class Inventory:
    """一次完整盘点的结果。"""

    records: list[SessionRecord] = field(default_factory=list)
    db_path: Path | None = None
    edge_db_path: Path | None = None
    projects_root: Path | None = None
    warnings: list[str] = field(default_factory=list)
    sidebar_info: sidebar.SidebarInfo = field(default_factory=sidebar.SidebarInfo)
    # 本次盘点的账号范围。空串 = 全部账号。
    # ★ 界面上的所有统计都必须跟着它走，否则「盘了 20 条却报库内 95 条」
    #   会让人以为盘点没生效。
    scope_uid: str = ""
    # 库里出现过的全部 user_id —— **不受盘点范围影响**。
    # 只盘当前账号时，若拿已盘点的记录去生成账号下拉，别的账号会整个消失，
    # 用户就再也切不过去了。下拉要列全，切过去时再重盘。
    all_uids: tuple[str, ...] = ()
    # 指纹缓存命中情况（给界面/日志看的一句话，无缓存时为空）
    cache_stats: str = ""
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def visible(self) -> list[SessionRecord]:
        """默认视图展示的会话：侧边栏可见的 + 本工具刚导入的。"""
        return [r for r in self.records if r.visible_by_default]

    @property
    def pending_imports(self) -> list[SessionRecord]:
        """导入了但客户端还没认领的 —— 提示用户重启客户端。"""
        return [r for r in self.records if r.imported_pending]

    @property
    def hidden_count(self) -> int:
        """库里有、但默认视图不展示的（已删除 / 未展示）。"""
        return sum(1 for r in self.records if not r.visible_by_default)

    @property
    def pending_claim(self) -> list[SessionRecord]:
        """导入过、客户端还没写过快照，但**仍按可见处理**的那批（真正的「等重启」）。

        空间悬空的、以及客户端已重写快照却没纳入的，都不算在内。
        """
        return [r for r in self.records if r.imported_pending and r.visible_by_default]

    @property
    def ignored(self) -> list[SessionRecord]:
        """导入过、客户端已重写快照却没纳入 —— 客户端不认这批。"""
        return [r for r in self.records if r.ignored_import]

    @property
    def ignored_count(self) -> int:
        return len(self.ignored)

    @property
    def dangling(self) -> list[SessionRecord]:
        """未删除、但 `space_id` 指向本机不存在的空间 —— 客户端多半不显示。

        这是跨机导入的副作用：只搬了 `sessions` 表，源机的空间 id 一并带过来，
        本机 `spaces` 表里没有那一行。
        """
        return [r for r in self.records if not r.is_deleted and r.space_dangling]

    @property
    def dangling_count(self) -> int:
        return len(self.dangling)

    @property
    def deleted(self) -> list[SessionRecord]:
        """客户端里已删除的会话。"""
        return [r for r in self.records if r.is_deleted]

    @property
    def alive_count(self) -> int:
        """未删除的会话数 —— 这是和客户端对得上的口径。"""
        return sum(1 for r in self.records if not r.is_deleted)

    @property
    def sidebar_saved_at(self) -> int:
        return self.sidebar_info.latest_saved_at

    def sidebar_count_of(self, uid: str) -> int:
        snap = self.sidebar_info.snapshots.get(uid)
        return snap.count if snap is not None else 0

    @property
    def orphans(self) -> list[SessionRecord]:
        return [r for r in self.records if r.is_orphan]

    @property
    def unmapped(self) -> list[SessionRecord]:
        """有正文但没注册云端映射的会话。"""
        return [r for r in self.records if r.has_body and not r.has_cloud_mapping]

    def by_uid(self) -> dict[str, list[SessionRecord]]:
        grouped: dict[str, list[SessionRecord]] = {}
        for r in self.records:
            grouped.setdefault(r.user_id or "(未知)", []).append(r)
        return grouped


def build(
    base: str | os.PathLike[str] | None = None,
    with_fingerprint: bool = True,
    progress=None,
    uid: str = "",
    use_cache: bool = True,
    cache=None,
) -> Inventory:
    """只读盘点本机会话。

    with_fingerprint=False 时跳过正文 hash（用于快速列出，68MB 正文会慢）。
    progress 为可选回调 progress(done, total)。

    uid 非空 = **只盘这个账号**，判据与主窗口的账号下拉完全一致：
    `user_id == uid 或 uid in sidebar_uids`（并集）。口径必须共用 ——
    只按 user_id 过滤会漏掉「属于别的账号、但在你侧边栏里看得到」的副本，
    导出条数就会比界面看到的少。uid 为空则盘全部账号。

    use_cache=True 时指纹走 wbsync/fpcache 的增量缓存（mtime+size 命中就复用）。
    cache 可注入一个 FingerprintCache 实例，用于测试。
    """
    inv = Inventory()
    inv.scope_uid = uid
    db_path = paths.workbuddy_db_path(base)
    edge_path = paths.edge_sync_db_path(base)

    inv.db_path = db_path
    inv.edge_db_path = edge_path
    inv.projects_root = paths.projects_dir(base)

    if not db_path.is_file():
        inv.warnings.append(f"找不到会话数据库：{db_path}")
        return inv

    index = paths.build_projects_index(base)

    with dbcopy.readonly_db_copy(db_path) as conn:
        inv.records = read_sessions(conn)
        # 空间 id 集合与 sessions 同一个只读快照里取，避免两次拷贝之间被客户端改动
        space_ids = read_space_ids(conn)

    if edge_path.is_file():
        with dbcopy.readonly_db_copy(edge_path) as conn:
            cloud = read_cloud_map(conn)
    else:
        cloud = {}
        inv.warnings.append(f"找不到云端映射库：{edge_path.name}（会话将全部标记为未注册）")

    # 侧边栏快照：决定「客户端界面上能看到哪些」，界面默认按它过滤
    inv.sidebar_info = sidebar.build(base)
    if not inv.sidebar_info.snapshots:
        inv.warnings.append(
            "未找到侧边栏快照（sidebar-list-snapshot.json），"
            "无法判断哪些会话在客户端界面上可见，将展示全部"
        )

    # 导入记录：本工具搬过来、客户端还没认领的会话
    from . import importlog

    import_log = importlog.ImportLog.load(base)

    for rec in inv.records:
        rec.cloud_channel = cloud.get(rec.cid)
        rec.sidebar_uids = inv.sidebar_info.uids_for(rec.cid)
        rec.imported_at = import_log.at_of(rec.cid)
        rec.imported_host = import_log.host_of(rec.cid)
        # 快照时刻决定「客户端有没有机会认领这条导入项」（见 visible_by_default）
        rec.sidebar_saved_at = inv.sidebar_info.latest_saved_at
        # space_ids 为 None = 这套客户端没有 spaces 表，一律按可解析（fail-open）
        rec.space_ok = space_ids is None or not rec.space_id or rec.space_id in space_ids
        entry = inv.sidebar_info.entries.get(rec.cid)
        if entry is not None:
            rec.sidebar_space = entry.space_name
            rec.sidebar_state = entry.state
            rec.is_pinned = entry.is_pinned
            # 快照里的标题是用户看到的那一个，优先于数据库的自动标题
            if entry.title.strip():
                rec.title = entry.title.strip()
        jp = index.get(rec.cid)
        if jp is not None:
            rec.jsonl_path = jp
            try:
                rec.jsonl_size = jp.stat().st_size
            except OSError:
                rec.jsonl_size = None

    # 缓存清理要按**全量** cid 来判存活：本次没盘的会话正文还在磁盘上，
    # 拿过滤后的集合去扫会把它们当成「已删除」清掉，下次切回来又得全部重算。
    alive_cids = {r.cid for r in inv.records if r.has_body}

    # 账号名单要在**过滤之前**收：只盘单账号时下拉仍要列出全部账号，
    # 否则别的账号整个消失，用户就再也切不过去了。
    inv.all_uids = tuple(sorted({r.user_id for r in inv.records if r.user_id}))

    if uid:
        inv.records = [
            r for r in inv.records if r.user_id == uid or uid in r.sidebar_uids
        ]

    # 指纹只算留下的这批 —— 这才是「默认只盘当前账号」省时间的意义所在
    cache_obj = cache
    owns_cache = False
    if with_fingerprint and use_cache and cache_obj is None:
        from . import fpcache

        cache_obj = fpcache.open_cache(base)
        owns_cache = True

    total = len(inv.records)
    for i, rec in enumerate(inv.records):
        jp = rec.jsonl_path
        if jp is not None and with_fingerprint:
            if cache_obj is not None:
                rec.fingerprint = cache_obj.resolve(rec.cid, jp)
            else:
                rec.fingerprint = fingerprint_of_file(jp, rec.cid)
        if progress is not None:
            progress(i + 1, total)

    if cache_obj is not None:
        cache_obj.sweep(alive_cids)
        inv.cache_stats = cache_obj.stats_text()
        inv.cache_hits = cache_obj.hits
        inv.cache_misses = cache_obj.misses
        if owns_cache:
            cache_obj.save()

    inv.records.sort(key=lambda r: r.updated_at, reverse=True)
    return inv
