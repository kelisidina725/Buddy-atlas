"""会话指纹缓存 —— 让「打开工具」不再重算 96 MB 正文的 hash。

## 为什么只缓存指纹，不缓存盘点结果

本机实测：元数据盘点 0.09s，指纹计算 7.41s（80 个正文 / 96 MB）。
指纹占了 98%，缓存它有收益；元数据那么快，缓存它只会让**刚产生的会话在你
打开时不出现** —— 新会话立刻可见，比省下这 0.09s 重要得多。

## 命中判据为什么是 mtime_ns + size

内容没变 ⇔ 文件没被写过。两个判据同时相同而内容不同的概率可忽略，
这是 make / rsync 沿用几十年的同一套判据。
反过来，若只按 cid 做键、不校验 mtime，客户端追加消息之后界面会一直显示旧指纹。

## 每天清理的是什么

只清两类失效条目：正文已不在盘上的（会话被删了），以及连续 STALE_DAYS 天
一次都没命中的（早就不看的会话）。**仍命中的一律保留** ——
「每天清理一次」的目的是别让缓存无限膨胀，不是逼着每天重算一遍。

## 缓存坏掉怎么办

JSON 解析失败、结构不对、版本不认，一律当作空缓存重建。
缓存只是加速器，它坏了最坏的结果是慢，不能让它把工具拖崩。
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import fingerprint as fp_mod
from .fingerprint import SessionFingerprint

CACHE_DIR_NAME = "wbsync-cache"
CACHE_FILE_NAME = "fingerprints.json"
CACHE_VERSION = 1

# 连续这么多天没命中就清掉。设成 1 会让「昨天看过、今天没看」的会话反复重算
STALE_DAYS = 7


def cache_path(base: str | os.PathLike[str] | None = None) -> Path:
    """缓存文件路径。base 只影响会话数据目录，缓存始终跟 .workbuddy 走。"""
    from . import paths

    root = paths.session_data_dir(base)
    return root / CACHE_DIR_NAME / CACHE_FILE_NAME


def _today() -> str:
    return date.today().isoformat()


class FingerprintCache:
    """按 cid 索引的指纹缓存。

    用法：`with open_cache() as c: fp = c.resolve(cid, path)`，
    退出时自动做每日清理并落盘（与 sqlite 连接同理，必须显式收尾）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, object]] = {}
        self.hits = 0
        self.misses = 0
        self.swept = 0
        self._dirty = False
        self._last_sweep = ""
        self._loaded = False

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> FingerprintCache:
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def load(self) -> None:
        """读缓存。任何异常都退化成空缓存 —— 缓存不值得让工具起不来。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if int(data.get("version") or 0) != CACHE_VERSION:
            return
        entries = data.get("entries")
        if isinstance(entries, dict):
            self.entries = entries
        self._last_sweep = str(data.get("last_sweep") or "")

    def close(self) -> None:
        if self._dirty:
            self.save()

    def save(self) -> None:
        payload = {
            "version": CACHE_VERSION,
            "last_sweep": self._last_sweep,
            "entries": self.entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原子替换：写一半的缓存被读到会退化成空缓存，虽然不致命但白白重算一遍
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self._dirty = False

    def clear(self) -> None:
        """清空缓存并删文件（「清空缓存并重新盘点」用）。"""
        self.entries = {}
        self._dirty = False
        self._last_sweep = ""
        try:
            self.path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------ 读写
    def resolve(self, cid: str, path: Path) -> SessionFingerprint | None:
        """取指纹。命中就复用，否则计算后写回。

        返回 None 表示读不到该文件 —— 与 fingerprint_of_file 的
        fail-closed 语义保持一致，调用方不需要区分「没缓存」和「读不了」。
        """
        try:
            st = path.stat()
        except OSError:
            return None

        today = _today()
        entry = self.entries.get(cid)
        if isinstance(entry, dict):
            if (
                int(entry.get("mtime_ns") or 0) == st.st_mtime_ns
                and int(entry.get("size") or 0) == st.st_size
            ):
                cached = fp_mod.from_json(entry.get("fp"))
                if cached is not None:
                    entry["seen"] = today
                    self.hits += 1
                    self._dirty = True
                    return cached

        fresh = fp_mod.fingerprint_of_file(path, cid)
        self.misses += 1
        if fresh is not None:
            self.entries[cid] = {
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
                "fp": fp_mod.to_json(fresh),
                "seen": today,
            }
            self._dirty = True
        return fresh

    # ------------------------------------------------------------ 清理
    def sweep(self, alive_cids: set[str] | None = None) -> int:
        """清理失效条目，每天只真跑一次。返回本次清掉的条数。"""
        today = _today()
        if self._last_sweep == today:
            return 0

        cutoff = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
        keep: dict[str, dict[str, object]] = {}
        for cid, entry in self.entries.items():
            if not isinstance(entry, dict):
                self.swept += 1
                continue
            if alive_cids is not None and cid not in alive_cids:
                self.swept += 1
                continue
            if str(entry.get("seen") or "") < cutoff:
                self.swept += 1
                continue
            keep[cid] = entry

        removed = len(self.entries) - len(keep)
        self.entries = keep
        self._last_sweep = today
        self._dirty = True
        return removed

    # ------------------------------------------------------------ 统计
    @property
    def total(self) -> int:
        return len(self.entries)

    def stats_text(self) -> str:
        """给界面/日志用的一句话统计。"""
        tried = self.hits + self.misses
        if not tried:
            return "指纹缓存：未使用"
        return f"指纹缓存：命中 {self.hits}/{tried}"


def open_cache(base: str | os.PathLike[str] | None = None) -> FingerprintCache:
    """打开（并读入）缓存。用 with 包起来，退出时自动落盘。"""
    c = FingerprintCache(cache_path(base))
    c.load()
    return c
