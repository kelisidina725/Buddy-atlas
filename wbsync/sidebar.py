"""侧边栏可见性 —— 判断「客户端界面上真正能看到哪些会话」。

## 为什么需要这个模块

`workbuddy.db` 的 `sessions` 表存的是**全部历史**：已删除的、其他账号的、
几年前就没再打开过的，都还在里面。而客户端侧边栏只渲染当前账号、当前可见的那部分。

以本机实测为例：sessions 表 74 条，但两个账号的侧边栏分别只有 8 条和 37 条，
剩下 29 条从未出现在任何侧边栏里。

如果工具直接展示 74 条，用户的第一反应就是「和我本地看到的不一致」——
这正是这个模块要解决的问题。

## 数据来源

`~/.workbuddy/<uid>/sidebar-list-snapshot.json` —— 客户端自己写下的侧边栏快照，
字段与界面上看到的一一对应（标题、置顶、最后活动时间、所属工作空间）。

## 局限（如实说明）

快照由客户端写入，**只在客户端运行过之后才更新**。
如果某台电脑很久没开客户端，快照可能偏旧，此时该账号的可见列表会少于实际。
所以界面上会同时给出「全部会话」入口，并标注快照时间。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

SNAPSHOT_NAME = "sidebar-list-snapshot.json"

# 快照版本兼容范围
_SUPPORTED_MAX_VERSION = 3


@dataclass(frozen=True)
class SidebarEntry:
    """侧边栏上的一条可见会话。"""

    cid: str
    title: str = ""
    state: str = ""
    transport: str = ""
    kind: str = ""
    last_activity_at: int = 0
    updated_at: int = 0
    is_pinned: bool = False
    space_name: str = ""
    space_type: str = "local"

    @property
    def display_title(self) -> str:
        return self.title.strip() or "(未命名会话)"


@dataclass
class SidebarSnapshot:
    """某个账号的侧边栏快照。"""

    uid: str
    path: Path
    saved_at: int = 0
    version: int = 0
    entries: list[SidebarEntry] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.entries)

    def by_cid(self) -> dict[str, SidebarEntry]:
        return {e.cid: e for e in self.entries}

    @property
    def is_empty(self) -> bool:
        return not self.entries


def _iter_uid_dirs(base: str | os.PathLike[str] | None = None) -> list[Path]:
    """遍历数据目录下形如 user-id 的 uuid 目录。

    ⚠️ 用正则形状判断而不是固定 uid：两台电脑 uid 不同，写死就全盘失配。
    """
    import re

    root = paths.session_data_dir(base)
    if not root.is_dir():
        return []
    pat = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    out: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and pat.match(entry.name):
            out.append(entry)
    return out


def read_snapshot(uid_dir: Path) -> SidebarSnapshot | None:
    """读一个账号目录下的快照。格式不认识就返回 None（fail-closed，不猜）。"""
    p = uid_dir / SNAPSHOT_NAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    version = int(data.get("version") or 0)
    if version and version > _SUPPORTED_MAX_VERSION:
        # 版本过新：字段含义可能已变，宁可不认也不误判
        return None

    entries: list[SidebarEntry] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("id") or "")
        if not cid:
            continue
        space = raw.get("space") if isinstance(raw.get("space"), dict) else {}
        entries.append(
            SidebarEntry(
                cid=cid,
                title=str(raw.get("title") or ""),
                state=str(raw.get("state") or ""),
                transport=str(raw.get("transport") or ""),
                kind=str(raw.get("kind") or ""),
                last_activity_at=int(raw.get("lastActivityAt") or 0),
                updated_at=int(raw.get("updatedAt") or 0),
                is_pinned=bool(raw.get("isPinned")),
                space_name=str(space.get("name") or ""),
                space_type=str(space.get("type") or "local"),
            )
        )

    return SidebarSnapshot(
        uid=uid_dir.name,
        path=p,
        saved_at=int(data.get("savedAt") or 0),
        version=version,
        entries=entries,
    )


def load_all(base: str | os.PathLike[str] | None = None) -> dict[str, SidebarSnapshot]:
    """读取所有账号的快照：uid -> snapshot。没有快照的账号不出现。"""
    out: dict[str, SidebarSnapshot] = {}
    for d in _iter_uid_dirs(base):
        snap = read_snapshot(d)
        if snap is not None:
            out[snap.uid] = snap
    return out


def visible_index(base: str | os.PathLike[str] | None = None) -> dict[str, set[str]]:
    """cid -> 它出现在哪些账号的侧边栏里。

    一个会话可能被多个账号看到（同一份副本切换账号后仍可见），所以是集合。
    """
    index: dict[str, set[str]] = {}
    for uid, snap in load_all(base).items():
        for e in snap.entries:
            index.setdefault(e.cid, set()).add(uid)
    return index


@dataclass
class SidebarInfo:
    """汇总结果，供 inventory 一次性使用。"""

    index: dict[str, set[str]] = field(default_factory=dict)
    entries: dict[str, SidebarEntry] = field(default_factory=dict)
    snapshots: dict[str, SidebarSnapshot] = field(default_factory=dict)

    @property
    def total_visible(self) -> int:
        return len(self.index)

    @property
    def latest_saved_at(self) -> int:
        return max((s.saved_at for s in self.snapshots.values()), default=0)

    def uids_for(self, cid: str) -> tuple[str, ...]:
        return tuple(sorted(self.index.get(cid, ())))


def build(base: str | os.PathLike[str] | None = None) -> SidebarInfo:
    """一次性读全部快照并建索引。"""
    snaps = load_all(base)
    info = SidebarInfo(snapshots=snaps)
    for uid, snap in snaps.items():
        for e in snap.entries:
            info.index.setdefault(e.cid, set()).add(uid)
            # 同一 cid 出现在多个快照时，保留活动时间较新的那条描述
            prev = info.entries.get(e.cid)
            if prev is None or e.last_activity_at > prev.last_activity_at:
                info.entries[e.cid] = e
    return info
