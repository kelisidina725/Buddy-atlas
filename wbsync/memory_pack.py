"""工作空间记忆节 —— 同步包的可选第三节（`memory/`）。

## 它装什么

只装**工作空间级**记忆：`<项目目录>/.workbuddy/memory/*.md`
（`MEMORY.md` 的跨会话摘要 + `YYYY-MM-DD.md` 的每日日志）。

不装账号级 `<uid>_memory.md`（那由 memorymerge 在切换/对齐时处理），
也不装 `.workbuddy/` 下的 `design/`、`docs/` 等其它私有数据 ——
范围收窄是刻意的：把「记忆」和「整个私有目录」混为一谈，用户就不敢勾了。

## 包结构

    memory/manifest.json          工作空间清单 + 每个文件的 size/hash/mtime
    memory/<wid>/MEMORY.md
    memory/<wid>/2026-11-02.md

`<wid>` = `<项目名>-<sha1(规范化源路径)[:8]>`：跨机稳定，且 manifest 里另存
源绝对路径，导入端据此做路径映射（复用 cwd_map 的记忆）。

## 导入语义（用户确认：按更新时间保留最新）

逐文件比对，**永不静默丢数据**：

    目标不存在           -> 新增
    hash 相同            -> 跳过（一致）
    hash 不同且源更新     -> 覆盖（覆盖前先备份）
    hash 不同且本地更新   -> 保留本地

判断「谁更新」用 manifest 里记录的源文件 mtime 对比本地 mtime。
mtime 跨机可比性有限，但这是用户明确选择的策略；为降低误判代价，
覆盖前一律走 guard 备份，任何一次覆盖都能回滚。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, rehome

FORMAT_NAME = "wbsync-memory"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
PREFIX = "memory/"

# 相对项目目录的记忆目录
MEMORY_REL = Path(".workbuddy") / "memory"

# 动作
ACT_NEW = "new"          # 目标不存在 -> 写入
ACT_SAME = "same"        # 内容一致 -> 跳过
ACT_UPDATE = "update"    # 源更新 -> 覆盖（先备份）
ACT_KEEP = "keep"        # 本地更新 -> 保留本地

_LABEL = {
    ACT_NEW: "新增",
    ACT_SAME: "一致·跳过",
    ACT_UPDATE: "覆盖（源更新）",
    ACT_KEEP: "保留本地（本地更新）",
}

_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


# ----------------------------------------------------------------- 数据结构


@dataclass
class MemFile:
    """记忆目录里的一个文件。"""

    name: str
    size: int = 0
    hash: str = ""
    mtime: int = 0


@dataclass
class MemWorkspace:
    """一个工作空间的记忆集合。"""

    wid: str
    path: str                      # 源绝对路径（rehome.normalize 口径）
    label: str = ""
    files: list[MemFile] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def memory_dir(self) -> Path:
        return Path(self.path) / MEMORY_REL


@dataclass
class MemoryBundle:
    """一个已打开的记忆节。"""

    path: Path
    manifest: dict[str, object] = field(default_factory=dict)
    workspaces: list[MemWorkspace] = field(default_factory=list)
    prefix: str = PREFIX

    @property
    def total(self) -> int:
        return len(self.workspaces)

    @property
    def file_total(self) -> int:
        return sum(w.file_count for w in self.workspaces)

    def read_member(self, ws: MemWorkspace, name: str) -> bytes:
        member = f"{self.prefix}{ws.wid}/{name}"
        with zipfile.ZipFile(self.path) as z:
            return z.read(member)


@dataclass
class MemAction:
    """一个待落地的文件。"""

    workspace: MemWorkspace
    file: MemFile
    target: Path
    action: str
    reason: str = ""
    root: str = ""      # 目标项目目录（target = root/.workbuddy/memory/name）

    @property
    def label(self) -> str:
        return _LABEL.get(self.action, self.action)

    @property
    def will_write(self) -> bool:
        return self.action in (ACT_NEW, ACT_UPDATE)


@dataclass
class MemReport:
    actions: list[MemAction] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup: object | None = None
    dry_run: bool = False

    def summary(self) -> str:
        head = "演练：" if self.dry_run else ""
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.label] = counts.get(a.label, 0) + 1
        parts = [f"{k} {v}" for k, v in sorted(counts.items())]
        return head + ("；".join(parts) if parts else "无动作")


# ----------------------------------------------------------------- 工具


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_name(text: str, limit: int = 40) -> str:
    s = _BAD_NAME_CHARS.sub("-", (text or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit] if s else "workspace"


def workspace_id(path: str) -> str:
    """跨机稳定的工作空间 id：项目名 + 规范化路径的短哈希。"""
    norm = rehome.normalize(path)
    name = sanitize_name(Path(norm).name or "workspace")
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def memory_dir_of(project_path: str) -> Path:
    return Path(project_path) / MEMORY_REL


# ----------------------------------------------------------------- 发现


def candidate_paths(records) -> list[str]:
    """从会话记录里取出去重后的工作空间目录（保留出现顺序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for rec in records or []:
        cwd = str(getattr(rec, "cwd", "") or "").strip()
        if not cwd:
            continue
        key = rehome.normalize(cwd)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def workspace_files(project_path: str) -> list[MemFile]:
    """列出一个工作空间记忆目录里的顶层 *.md（不含临时/隐藏文件）。"""
    d = memory_dir_of(project_path)
    if not d.is_dir():
        return []
    files: list[MemFile] = []
    try:
        entries = sorted(d.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for p in entries:
        if not p.is_file() or p.suffix.lower() != ".md":
            continue
        if p.name.startswith(".") or p.name.endswith(".wbsync-tmp"):
            continue
        try:
            st = p.stat()
            files.append(
                MemFile(name=p.name, size=st.st_size, hash=_sha256_file(p), mtime=int(st.st_mtime))
            )
        except OSError:
            continue
    return files


def discover(paths_in) -> list[MemWorkspace]:
    """把一组项目目录整理成记忆工作空间（只保留确实有 .md 的）。"""
    out: list[MemWorkspace] = []
    seen: set[str] = set()
    for raw in paths_in or []:
        norm = rehome.normalize(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        files = workspace_files(norm)
        if not files:
            continue
        out.append(
            MemWorkspace(wid=workspace_id(norm), path=norm, label=Path(norm).name, files=files)
        )
    return out


def discover_from_records(records) -> list[MemWorkspace]:
    return discover(candidate_paths(records))


# ----------------------------------------------------------------- 导出


def collect(workspaces: list[MemWorkspace]) -> list[dict[str, object]]:
    """清单条目（写入 manifest 的 workspaces 节）。"""
    return [
        {
            "id": ws.wid,
            "path": ws.path,
            "label": ws.label,
            "files": [
                {"name": f.name, "size": f.size, "hash": f.hash, "mtime": f.mtime}
                for f in ws.files
            ],
        }
        for ws in workspaces
    ]


def manifest(workspaces: list[MemWorkspace]) -> dict[str, object]:
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": int(time.time()),
        "source": {"host": socket.gethostname(), "os": platform.platform()},
        "workspaces": collect(workspaces),
    }


def write_payloads(
    z: zipfile.ZipFile,
    workspaces: list[MemWorkspace],
    prefix: str = PREFIX,
) -> int:
    """把记忆文件写进已打开的 zip，返回写入的文件数。"""
    written = 0
    for ws in workspaces:
        for f in ws.files:
            src = ws.memory_dir / f.name
            if not src.is_file():
                continue
            z.write(src, f"{prefix}{ws.wid}/{f.name}")
            written += 1
    return written


# ----------------------------------------------------------------- 读取


def _parse_workspaces(raw_list) -> list[MemWorkspace]:
    out: list[MemWorkspace] = []
    for item in raw_list or []:
        if not isinstance(item, dict):
            continue
        wid = str(item.get("id") or "")
        if not wid:
            continue
        files = [
            MemFile(
                name=str(f.get("name") or ""),
                size=int(f.get("size") or 0),
                hash=str(f.get("hash") or ""),
                mtime=int(f.get("mtime") or 0),
            )
            for f in (item.get("files") or [])
            if isinstance(f, dict) and f.get("name")
        ]
        out.append(
            MemWorkspace(
                wid=wid,
                path=str(item.get("path") or ""),
                label=str(item.get("label") or wid),
                files=files,
            )
        )
    return out


def read_bundle(path: str | os.PathLike[str], prefix: str = PREFIX) -> MemoryBundle:
    """打开并解析记忆节。格式不对直接抛 ValueError（fail-closed，不猜）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"包不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是有效的同步包（非 zip）：{path}")

    with zipfile.ZipFile(path) as z:
        if (prefix + MANIFEST_NAME) not in z.namelist():
            raise ValueError(f"包内缺少 {prefix}{MANIFEST_NAME}")
        data = json.loads(z.read(prefix + MANIFEST_NAME).decode("utf-8"))

    if data.get("format") != FORMAT_NAME:
        raise ValueError(f"不是记忆节：format={data.get('format')!r}")
    if int(data.get("version") or 0) > FORMAT_VERSION:
        raise ValueError(
            f"记忆节版本 {data.get('version')} 高于本工具支持的 {FORMAT_VERSION}，请升级工具"
        )
    return MemoryBundle(
        path=path,
        manifest=data,
        workspaces=_parse_workspaces(data.get("workspaces")),
        prefix=prefix,
    )


# ----------------------------------------------------------------- 导入计划


def _target_dir(ws: MemWorkspace, path_map: dict[str, str] | None) -> str:
    """解析目标项目目录：有映射用映射，否则沿用源路径。"""
    if path_map:
        mapped = path_map.get(rehome.normalize(ws.path))
        if mapped:
            return rehome.normalize(mapped)
    return ws.path


def plan_import(
    bundle: MemoryBundle,
    path_map: dict[str, str] | None = None,
) -> list[MemAction]:
    """逐文件算出落地动作。纯计算，不写任何东西。"""
    actions: list[MemAction] = []
    for ws in bundle.workspaces:
        target_root = _target_dir(ws, path_map)
        mem_dir = memory_dir_of(target_root)
        for f in ws.files:
            target = mem_dir / f.name
            if not target.is_file():
                action, reason = ACT_NEW, "目标不存在"
            else:
                try:
                    local_hash = _sha256_file(target)
                    local_mtime = int(target.stat().st_mtime)
                except OSError:
                    action, reason = ACT_UPDATE, "本地不可读，按源覆盖"
                else:
                    if local_hash == f.hash:
                        action, reason = ACT_SAME, "内容一致"
                    elif f.mtime > local_mtime:
                        action, reason = ACT_UPDATE, "源文件更新"
                    else:
                        action, reason = ACT_KEEP, "本地更新，保留本地"
            actions.append(
                MemAction(
                    workspace=ws, file=f, target=target, action=action,
                    reason=reason, root=target_root,
                )
            )
    return actions


# ----------------------------------------------------------------- 执行


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".wbsync-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def execute(
    actions: list[MemAction],
    bundle: MemoryBundle,
    base: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    progress=None,
) -> MemReport:
    """落地记忆文件。覆盖前整批备份一次。"""
    report = MemReport(actions=list(actions), dry_run=dry_run)
    todo = [a for a in actions if a.will_write]

    if dry_run:
        report.applied = [str(a.target) for a in todo]
        return report

    if not todo:
        report.skipped = [str(a.target) for a in actions if not a.will_write]
        return report

    # 覆盖前备份：只备份「已存在、将被覆盖」的目标文件
    overwrite = [a.target for a in todo if a.target.is_file()]
    if overwrite:
        from . import guard

        try:
            report.backup = guard.create_backup(overwrite, base=base, label="memory-pack")
        except OSError as exc:
            report.failed.append(("backup", f"备份失败：{exc}"))
            return report

    for idx, a in enumerate(todo):
        try:
            data = bundle.read_member(a.workspace, a.file.name)
            _atomic_write(a.target, data)
            report.applied.append(str(a.target))
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            report.failed.append((str(a.target), f"{type(exc).__name__}: {exc}"))
        if progress:
            progress(idx + 1, len(todo))

    report.skipped = [str(a.target) for a in actions if not a.will_write]
    return report


def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    root = paths.session_data_dir(base)
    if not root.is_dir():
        return "memorypack: 未找到数据目录（跳过）"
    return "memorypack: 接口就绪（工作空间记忆节）"
