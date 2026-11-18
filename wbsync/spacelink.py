"""会话的空间引用（`sessions.space_id`）本机化。

## 为什么需要

`spaces` 表存的是「本机有哪些工作空间」，`sessions.space_id` 指过去。跨机导入只搬
`sessions` 表，源机的空间 id 会被原样带进来 —— 本机没有那行空间，客户端就挂不上分组。
实测（2026-11-14）：本机 20 条未删除会话里 12 条的 `space_id` 指向不存在的空间，
客户端只渲染了其中 4 条，其余既不在侧边栏、也不进快照。

反过来也有正面证据：三个 `space_id` 全为 NULL 的账号，侧边栏照常全量显示 ——
客户端能按 cwd 兜底归组。所以这里把外来引用换算成本机可用值：

    原 id 在本机 spaces 里存在        → 保留
    按 cwd 匹配到本机同名目录的空间    → 换成那个 id（remap）
    都匹配不到                        → 置 NULL（让客户端按 cwd 兜底）

## 边界

- `spaces` 表不存在（老客户端）→ `load()` 返回 None，调用方一律原样保留，不改数据。
- `spaces.deleted_at` 不参与过滤：本机两行的值都是 -1 而客户端照常显示它们，
  按它过滤会把唯一两个真实空间判成不存在。
- 不新建空间行：凭空造 `spaces` 记录等于替客户端做它没做的决定，
  而且 `device_id` / `sort_order` 这些列我们填不出正确值。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import dbcopy, guard, rehome

# localize() 的三种结局
KEPT = "kept"          # 原引用在本机有效（或本来就为空）
REMAP = "remapped"     # 按 cwd 换成了本机空间 id
CLEAR = "cleared"      # 本机找不到对应空间，置 NULL


def path_key(raw: object) -> str:
    """路径的比较键：`rehome.normalize` 之后整体小写。

    Windows 路径不区分大小写，`C:/x` 与 `c:/x` 必须算同一个空间，
    否则同一个目录会裂成两个同名分组（历史上踩过）。
    """
    return rehome.normalize(str(raw or "")).lower()


@dataclass(frozen=True)
class Space:
    id: str
    name: str = ""
    path: str = ""
    uid: str = ""


@dataclass
class SpaceIndex:
    """本机空间清单 + 按目录路径反查。"""

    spaces: list[Space] = field(default_factory=list)
    by_path: dict[str, str] = field(default_factory=dict)
    # (账号, 目录键) -> 空间 id：复制会话到另一个账号时，优先用它自己名下的空间
    by_uid_path: dict[tuple[str, str], str] = field(default_factory=dict)
    owner: dict[str, str] = field(default_factory=dict)

    @property
    def ids(self) -> set[str]:
        return {s.id for s in self.spaces}

    def __bool__(self) -> bool:
        return bool(self.spaces)


def load(conn: sqlite3.Connection) -> SpaceIndex | None:
    """读本机空间。表不存在返回 None（老客户端，调用方按「无空间概念」处理）。"""
    if not dbcopy.table_exists(conn, "spaces"):
        return None
    try:
        rows = list(conn.execute('SELECT * FROM "spaces"'))
    except sqlite3.Error:
        return None
    if not rows:
        return SpaceIndex()

    out = SpaceIndex()
    for r in rows:
        d = dict(r)
        sid = str(d.get("id") or "")
        if not sid:
            continue
        # create_folder_path 是客户端建空间时用的目录；primary 是历史列，取之一
        raw_path = d.get("create_folder_path") or d.get("primary_folder_path") or ""
        path = rehome.normalize(str(raw_path))
        uid = str(d.get("user_id") or "")
        out.spaces.append(Space(id=sid, name=str(d.get("name") or ""), path=path, uid=uid))
        out.owner[sid] = uid
        key = path_key(path)
        if key:
            out.by_path.setdefault(key, sid)
            if uid:
                out.by_uid_path.setdefault((uid, key), sid)
    return out


def localize(
    space_id: object,
    cwd: object,
    index: SpaceIndex | None,
    uid: str = "",
) -> tuple[str | None, str]:
    """把一条会话的空间引用换算成本机可用的值。返回 (新值, 结局)。

    `uid` 是这条会话归属的账号：复制会话到另一个账号时，源账号名下的空间
    对目标账号同样算「不存在」，要按目录改挂到目标账号自己名下的空间上。
    """
    raw = str(space_id or "").strip()
    if index is None:
        # 老客户端没有空间概念：一个字节都不动
        return (raw or None), KEPT
    if raw and raw in index.ids and (not uid or index.owner.get(raw, "") in ("", uid)):
        return raw, KEPT
    key = path_key(cwd)
    if key:
        if uid:
            hit = index.by_uid_path.get((uid, key))
            if hit:
                return hit, REMAP
        hit = index.by_path.get(key)
        if hit:
            return hit, REMAP
    if not raw:
        return None, KEPT
    return None, CLEAR


def localize_row(row: dict, index: SpaceIndex | None, uid: str = "") -> str:
    """就地改写一行（只在确实需要改时动它），返回结局。"""
    if index is None or "space_id" not in row:
        return KEPT
    new, why = localize(row.get("space_id"), row.get("cwd"), index, uid=uid)
    if why in (REMAP, CLEAR):
        row["space_id"] = new
    return why


@dataclass
class Dangling:
    """一条「指向不存在空间」的会话，以及修好之后会变成什么。"""

    cid: str
    title: str = ""
    cwd: str = ""
    old_space: str = ""
    new_space: str | None = None
    why: str = CLEAR
    uid: str = ""

    @property
    def action_label(self) -> str:
        return "改用本机空间" if self.why == REMAP else "置空（按目录归组）"


def plan(db_path: str | os.PathLike[str]) -> tuple[list[Dangling], SpaceIndex | None]:
    """只读扫描：列出所有需要修的会话。不写任何东西。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        return [], None
    with dbcopy.readonly_db_copy(db_path) as conn:
        index = load(conn)
        if index is None:
            return [], None
        cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
        if "space_id" not in cols:
            return [], index
        select = ", ".join(
            c for c in ("id", "title", "cwd", "space_id", "user_id") if c in cols
        )
        out: list[Dangling] = []
        for r in conn.execute(f'SELECT {select} FROM "sessions"'):
            d = dict(r)
            sid = str(d.get("space_id") or "")
            if not sid:
                continue
            uid = str(d.get("user_id") or "")
            new, why = localize(sid, d.get("cwd"), index, uid=uid)
            if why == KEPT:
                continue
            out.append(
                Dangling(
                    cid=str(d.get("id") or ""),
                    title=str(d.get("title") or ""),
                    cwd=str(d.get("cwd") or ""),
                    old_space=sid,
                    new_space=new,
                    why=why,
                    uid=uid,
                )
            )
    out.sort(key=lambda x: (x.why != REMAP, x.title))
    return out, index


def repair(
    db_path: str | os.PathLike[str],
    items: list[Dangling],
    base: str | os.PathLike[str] | None = None,
) -> tuple[int, guard.Backup | None]:
    """写库：把列表里的空间引用改成本机可用的值。返回 (改动条数, 备份)。"""
    if not items:
        return 0, None
    db_path = Path(db_path)
    guard.assert_safe_to_write()
    backup = guard.create_backup([db_path], base=base, label="space-fix")

    changed = 0
    with guard.DbEditSession(db_path) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
        if "space_id" not in cols:
            raise RuntimeError("sessions 表没有 space_id 列，客户端结构可能已变更")
        for it in items:
            cur = conn.execute(
                'SELECT space_id FROM "sessions" WHERE id = ?', (it.cid,)
            ).fetchone()
            if cur is None:
                continue
            # 扫描到写入之间可能被客户端改过：只改仍是那个旧值的行
            if str(cur[0] or "") != it.old_space:
                continue
            conn.execute(
                'UPDATE "sessions" SET space_id = ? WHERE id = ?',
                (it.new_space, it.cid),
            )
            changed += 1
    if not guard.verify_db_readable(db_path, "sessions"):
        raise RuntimeError("写后校验失败：会话库未通过可读性检查，请从备份恢复")
    return changed, backup


def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    from . import paths

    p = paths.workbuddy_db_path(base)
    if not p.is_file():
        return "spacelink: 未找到会话库（跳过）"
    try:
        items, index = plan(p)
    except Exception as exc:  # noqa: BLE001 - 自检只报告，不阻断
        return f"spacelink: 扫描失败（{type(exc).__name__}: {exc}）"
    if index is None:
        return "spacelink: 老客户端无 spaces 表，跳过"
    n_remap = sum(1 for i in items if i.why == REMAP)
    return (
        f"spacelink: 本机空间 {len(index.spaces)} 个，"
        f"悬空引用 {len(items)} 条（可归组 {n_remap}）"
    )
