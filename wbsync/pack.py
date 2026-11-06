"""同步包（.wbsync.zip）v2 —— 会话与配置装进同一个包。

## 为什么不是把两个包塞在一起

v1 时代会话包与配置包各写一份 `manifest.json` 在 zip 根目录，两者**同名冲突**，
不可能简单合并。v2 的做法是：顶层一份索引 manifest，两节各在自己的子目录里
自描述，互不干扰：

    manifest.json                 顶层索引：format/version/sections
    sessions/manifest.json        会话节清单（结构与 v1 会话包完全一致）
    sessions/bodies/<cid>.jsonl
    config/manifest.json          配置节清单（结构与独立配置包完全一致）
    config/payloads/NNN/...

这样两个既有模块（bundle / config_sync）只需支持「带前缀读取」，不必重写解析逻辑，
同时顶层索引让「包里到底有什么」一眼可见。

## 为什么旧工具读新包必须报错而不是尽力而为

v1 工具读 v2 包时，`bundle.read_bundle` 会因 `version=2 > 1` 直接抛「请升级工具」。
这是刻意的：若它静默跳过 `config/` 分支，用户会以为配置也同步了，实际一件没写。
**宁可拒绝，不要半懂不懂地执行。**

## 向后兼容

    旧会话包 v1  → 照旧只有 sessions 节
    旧配置包     → 照旧只有 config 节（format 为 wbsync-config）
    新包 v2      → 按 sections 读取，缺哪节就是 None
"""
from __future__ import annotations

import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import bundle as bundle_mod
from . import config_sync, inventory, memory_pack, paths
from .config_items import Item

FORMAT_NAME = "wbsync-bundle"
FORMAT_VERSION = 2
MANIFEST_NAME = "manifest.json"

SESSIONS_PREFIX = "sessions/"
CONFIG_PREFIX = "config/"
MEMORY_PREFIX = memory_pack.PREFIX

# 与单独导出保持一致的压缩级别（6 是实测甜点，9 对本场景收益极小）
_COMPRESS_LEVEL = 6

SUFFIX = ".wbsync.zip"

# 内容档：给文件名用的英文标识
CONTENT_FULL = "full"
CONTENT_SESSIONS = "sessions"
CONTENT_CONFIG = "config"
CONTENT_MEMORY = "memory"

# 账号档
SCOPE_ACCOUNT = "acct"
SCOPE_ALL = "all"

# Windows 文件名非法字符
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


@dataclass
class Package:
    """一个已打开、已解析的包。各节都可能为 None。"""

    path: Path
    manifest: dict[str, object]
    sessions: bundle_mod.Bundle | None = None
    config: config_sync.ConfigBundle | None = None
    memory: memory_pack.MemoryBundle | None = None

    @property
    def format_version(self) -> int:
        return int(self.manifest.get("version") or 0)

    @property
    def has_sessions(self) -> bool:
        return self.sessions is not None and self.sessions.total > 0

    @property
    def has_config(self) -> bool:
        return self.config is not None and self.config.total > 0

    @property
    def has_memory(self) -> bool:
        return self.memory is not None and self.memory.total > 0

    @property
    def session_count(self) -> int:
        return self.sessions.total if self.sessions is not None else 0

    @property
    def config_count(self) -> int:
        return self.config.total if self.config is not None else 0

    @property
    def memory_count(self) -> int:
        return self.memory.total if self.memory is not None else 0

    @property
    def source_label(self) -> str:
        src = self.manifest.get("source")
        if isinstance(src, dict):
            return str(src.get("label") or src.get("host") or "")
        return ""

    @property
    def source_uid(self) -> str:
        src = self.manifest.get("source")
        return str(src.get("uid") or "") if isinstance(src, dict) else ""

    @property
    def created_at(self) -> int:
        return int(self.manifest.get("created_at") or 0)

    @property
    def is_legacy(self) -> bool:
        """v1 会话包或旧的独立配置包。"""
        return self.format_version < FORMAT_VERSION

    def describe(self) -> str:
        """一行摘要，给导入确认页用。"""
        parts: list[str] = []
        if self.has_sessions:
            parts.append(f"会话 {self.session_count} 条")
        if self.has_config:
            parts.append(f"配置 {self.config_count} 项")
        if self.has_memory:
            parts.append(f"工作空间记忆 {self.memory_count} 个")
        return " + ".join(parts) or "空包"


# ------------------------------------------------------------ 导出


def sanitize_label(text: str, limit: int = 24) -> str:
    """清洗账号别名，使其能安全出现在文件名里。

    只做必要处理：替换 Windows 非法字符、压缩空白、去掉首尾的点与空格、截断。
    中文保留原样 —— 用户就是用中文别名区分两台机器的，转拼音反而认不出。
    """
    s = _BAD_NAME_CHARS.sub("-", (text or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit] if s else "unknown"


def suggest_filename(
    alias: str,
    scope: str,
    content: str,
    when: float | None = None,
) -> str:
    """生成导出文件名：`别名-yyyyMMdd-HHmm-账号档-内容档.wbsync.zip`。

    时间用 `yyyyMMdd-HHmm` 而非带冒号的格式：Windows 文件名不允许冒号，
    且字典序即时间序，同目录里天然按时间排好。
    """
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when if when is not None else time.time()))
    scope = SCOPE_ALL if scope == SCOPE_ALL else SCOPE_ACCOUNT
    if content not in (CONTENT_FULL, CONTENT_SESSIONS, CONTENT_CONFIG, CONTENT_MEMORY):
        content = CONTENT_FULL
    return f"{sanitize_label(alias)}-{stamp}-{scope}-{content}{SUFFIX}"


def export_package(
    out_path: str | os.PathLike[str],
    records=None,
    config_items: list[Item] | None = None,
    base: str | os.PathLike[str] | None = None,
    source_label: str = "",
    source_uid: str = "",
    progress=None,
    memory_workspaces: list[memory_pack.MemWorkspace] | None = None,
) -> Path:
    """把会话 / 配置 / 工作空间记忆写进同步包。全空时抛 ValueError。"""
    records = list(records or [])
    config_items = list(config_items or [])
    memory_workspaces = list(memory_workspaces or [])
    if not records and not config_items and not memory_workspaces:
        raise ValueError("同步包里至少要有一节内容")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mem_files = sum(w.file_count for w in memory_workspaces)
    total = len(records) + len(config_items) + mem_files
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(min(done, total), total)

    sessions = bundle_mod.collect_sessions(records, base, True, lambda i, t: tick()) if records else []
    entries, members = (
        config_sync.collect_config(config_items, base, lambda i, t: tick())
        if config_items
        else ([], {})
    )
    for _ in range(mem_files):
        tick()

    sections: dict[str, object] = {}
    if sessions:
        sections["sessions"] = {
            "prefix": SESSIONS_PREFIX,
            "count": len(sessions),
            "with_body": sum(1 for s in sessions if s.body_member),
        }
    if entries:
        sections["config"] = {"prefix": CONFIG_PREFIX, "count": len(entries)}
    if memory_workspaces:
        sections["memory"] = {
            "prefix": MEMORY_PREFIX,
            "count": len(memory_workspaces),
            "files": mem_files,
        }

    src = bundle_mod.source_info(base)
    manifest: dict[str, object] = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": int(time.time()),
        "source": {
            "host": src.get("host", ""),
            "os": src.get("os", ""),
            "data_dir": str(paths.session_data_dir(base)),
            "label": source_label,
            "uid": source_uid,
        },
        "sections": sections,
    }

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=_COMPRESS_LEVEL) as z:
            z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))

            if sessions:
                z.writestr(
                    SESSIONS_PREFIX + MANIFEST_NAME,
                    json.dumps(
                        {
                            "format": bundle_mod.FORMAT_NAME,
                            "version": bundle_mod.FORMAT_VERSION,
                            "created_at": int(time.time()),
                            "source": src,
                            "sessions": bundle_mod.sessions_manifest(sessions),
                        },
                        ensure_ascii=False,
                        indent=1,
                    ),
                )
                bundle_mod.write_session_bodies(z, sessions, base, prefix=SESSIONS_PREFIX)

            if entries:
                z.writestr(
                    CONFIG_PREFIX + MANIFEST_NAME,
                    json.dumps(
                        config_sync.config_manifest(entries, base, host_label=source_label),
                        ensure_ascii=False,
                        indent=1,
                    ),
                )
                config_sync.write_config_payloads(
                    z, config_items, members, base, prefix=CONFIG_PREFIX
                )

            if memory_workspaces:
                z.writestr(
                    MEMORY_PREFIX + MANIFEST_NAME,
                    json.dumps(
                        memory_pack.manifest(memory_workspaces), ensure_ascii=False, indent=1
                    ),
                )
                memory_pack.write_payloads(z, memory_workspaces, prefix=MEMORY_PREFIX)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, out_path)
    return out_path


# ------------------------------------------------------------ 读取


def read_package(path: str | os.PathLike[str]) -> Package:
    """打开并解析包，自动识别 v2 / v1 会话包 / 旧配置包。

    格式无法识别时抛 ValueError（fail-closed，不猜）。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"包不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是有效的同步包（非 zip）：{path}")

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if MANIFEST_NAME not in names:
            raise ValueError(f"包内缺少 {MANIFEST_NAME}")
        top = json.loads(z.read(MANIFEST_NAME).decode("utf-8"))

    fmt = top.get("format")
    version = int(top.get("version") or 0)

    if fmt == bundle_mod.FORMAT_NAME and version >= FORMAT_VERSION:
        sessions = (
            bundle_mod.read_bundle(path, SESSIONS_PREFIX)
            if SESSIONS_PREFIX + MANIFEST_NAME in names
            else None
        )
        config = (
            config_sync.read_config_bundle(path, CONFIG_PREFIX)
            if CONFIG_PREFIX + MANIFEST_NAME in names
            else None
        )
        memory = (
            memory_pack.read_bundle(path, MEMORY_PREFIX)
            if MEMORY_PREFIX + MANIFEST_NAME in names
            else None
        )
        if sessions is None and config is None and memory is None:
            raise ValueError("同步包声明了 v2 格式，但三节内容都缺失")
        return Package(
            path=path, manifest=top, sessions=sessions, config=config, memory=memory
        )

    if fmt == bundle_mod.FORMAT_NAME:
        # v1：只有会话节的独立包
        return Package(path=path, manifest=top, sessions=bundle_mod.read_bundle(path))

    if fmt == config_sync.CONFIG_FORMAT:
        return Package(path=path, manifest=top, config=config_sync.read_config_bundle(path))

    raise ValueError(f"无法识别的包格式：format={fmt!r}")


def package_size(path: str | os.PathLike[str]) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def guess_content(
    records,
    config_items: list[Item] | None,
    memory_workspaces: list | None = None,
) -> str:
    """按勾选结果给出内容档标识，用于文件名。"""
    has_s = bool(records)
    has_c = bool(config_items)
    has_m = bool(memory_workspaces)
    picks = sum((has_s, has_c, has_m))
    if picks >= 2:
        return CONTENT_FULL
    if has_s:
        return CONTENT_SESSIONS
    if has_c:
        return CONTENT_CONFIG
    if has_m:
        return CONTENT_MEMORY
    return CONTENT_FULL


# ------------------------------------------------------------ 回传包


BACK_PACK_KIND = "back-pack"


def export_back_pack(
    out_path: str | os.PathLike[str],
    cids,
    base: str | os.PathLike[str] | None = None,
    progress=None,
) -> Path:
    """把指定 cid 的会话打成「回传包」—— 本机比对方新的那些，交还给对方。

    ## 为什么复用 v1 会话包，而不是新造一个格式

    对面机器用同一个工具导入就行，不需要第二条解析路径；旧版本工具也能读
    （它只会看到一个普通会话包）。所以这里只加一个 manifest 标记
    `kind=back-pack`，不动 `version`。

    ## 为什么要有这个包

    官方「数据导入」对已存在的 sessionId 一律 skip，不给增量回流的入口。
    两台机器各自续写之后，只有把「本地更新」的那批显式打出来、拿到对面导，
    双方的对话才可能重新并到一起。这是官方做不到、也最该补的一环。

    找不到正文的 cid 会被跳过（已删除 / 正文被清理），不报错：
    调用方传的是「本地更新」清单，清单里混进一两条空壳是正常现象。
    """
    wanted = {str(c).strip() for c in (cids or []) if str(c or "").strip()}
    if not wanted:
        raise ValueError("回传包里至少要有一条会话")

    inv = inventory.build(base=base, with_fingerprint=True)
    records = [r for r in inv.records if r.cid in wanted]
    if not records:
        raise ValueError("勾选的会话在本机都找不到（可能已被删除或正文已清理）")

    return bundle_mod.export_bundle(
        out_path,
        records,
        base=base,
        progress=progress,
        extra={"kind": BACK_PACK_KIND, "requested": len(wanted)},
    )


def suggest_back_pack_name(base: str | os.PathLike[str] | None = None) -> str:
    """给回传包起个不会撞车的默认名：`wbsync-back-<时间>.wbsync`。"""
    stamp = time.strftime("%Y%m%d-%H%M")
    return f"wbsync-back-{stamp}.wbsync"
