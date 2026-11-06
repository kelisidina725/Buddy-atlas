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
    def in_sidebar(self) -> bool:
        """客户端侧边栏快照里能看到它。已删除的一律不算。"""
        return bool(self.sidebar_uids) and not self.is_deleted

    @property
    def imported_pending(self) -> bool:
        """本工具搬过来、还没被客户端认领（且没被删）。"""
        return self.imported_at > 0 and not self.in_sidebar and not self.is_deleted

    @property
    def visible_by_default(self) -> bool:
        """默认视图是否展示。

        = 侧边栏快照里有 **或** 本工具刚导入。后者不进快照，
        但用户刚导完就该看得见，否则会以为没导成功。

        ★ 已删除的一律不展示 —— 用户在客户端删掉的会话，
          不该因为"本工具导入过"又冒出来。
        """
        if self.is_deleted:
            return False
        return self.in_sidebar or self.imported_at > 0

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
            )
        )
    return [r for r in records if r.cid]


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
