"""导入记录 —— 记住「哪些会话是本工具搬过来的还没被客户端认领」。

## 为什么需要这个

导入会把会话写进 `sessions` 表，但**不会**写进客户端的 `sidebar-list-snapshot.json`
（那是客户端自己的文件，我们不去改它，改错格式会让侧边栏整个渲染不出来）。

后果是：导入完打开工具，默认视图（本地可见）里**一条都看不到** ——
因为「本地可见」的判据是侧边栏快照。用户会以为导入失败了。

所以这里记一笔账：本工具导入过哪些 cid。界面把它们一并算作可见，
并标注「新导入 · 重启客户端后进入侧边栏」，两边都不骗。

## 为什么条目要设上限

每次导入都会追加，长期用下来这个文件会一直长。
只保留最近 MAX_ENTRIES 条，超出的丢掉 —— 那些早已被客户端写进快照，
不再需要这份账兜底。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

LOG_NAME = "wbsync-imported.json"
LOG_VERSION = 1
MAX_ENTRIES = 2000


@dataclass
class ImportLog:
    path: Path
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, base: str | os.PathLike[str] | None = None) -> "ImportLog":
        p = paths.session_data_dir(base) / LOG_NAME
        entries: dict[str, dict] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("cids"), dict):
                    for k, v in data["cids"].items():
                        if isinstance(v, dict):
                            entries[str(k)] = v
            except (OSError, json.JSONDecodeError):
                entries = {}
        return cls(path=p, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if len(self.entries) > MAX_ENTRIES:
            # 按导入时间留最近的，避免文件无限膨胀
            keep = sorted(
                self.entries.items(),
                key=lambda kv: int(kv[1].get("at") or 0),
                reverse=True,
            )[:MAX_ENTRIES]
            self.entries = dict(keep)
        payload = {"version": LOG_VERSION, "cids": self.entries}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, cids: list[str], uid: str = "", source_host: str = "") -> int:
        """登记一批刚导入的会话，返回新增的条数。"""
        now = int(time.time())
        added = 0
        for cid in cids:
            if not cid:
                continue
            if cid not in self.entries:
                added += 1
            self.entries[cid] = {"at": now, "uid": uid, "host": source_host}
        return added

    def forget(self, cids: list[str]) -> None:
        for cid in cids:
            self.entries.pop(cid, None)

    def is_pending(self, cid: str) -> bool:
        return cid in self.entries

    def at_of(self, cid: str) -> int:
        try:
            return int(self.entries.get(cid, {}).get("at") or 0)
        except (TypeError, ValueError):
            return 0

    def host_of(self, cid: str) -> str:
        return str(self.entries.get(cid, {}).get("host") or "")


def pending_cids(base: str | os.PathLike[str] | None = None) -> set[str]:
    return set(ImportLog.load(base).entries.keys())
