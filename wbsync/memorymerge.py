"""长期记忆合并 —— `<uid>_memory.md` 的按内容去重合并。

## 文件长什么样（实测三种账号）

    # User Memory Profile
    > Last updated: 2026-10-26T02:37:04+08:00
    > Version: 2

    ## Memory Block

    **工作背景**
    ……正文……

    **个人背景**
    ……

    ---

    <!-- RAW_JSON_START
    {"uid": "...", "memoryBlock": "<与上面正文完全相同>", "version": 2, "updatedAt": "..."}
    RAW_JSON_END -->

★ 两处内容必须**同时**更新：正文是给人看的，`RAW_JSON_START` 里那份是给客户端读的。
只改一处会造出「正文有、JSON 没有」的分裂状态 —— 客户端下次按 JSON 覆写，
合并结果就白做了。

## 合并规则（按「目标优先、只补差集」）

记忆是**账号自己的**认知，不是可覆盖的数据。所以：

1. 目标已有的小节**不动**（本地优先）；
2. 目标没有的小节，整节追加；
3. 两边同名且都是项目符号列表（如「近期动态」）→ 条目级并集，目标顺序在前；
4. 目标是空记忆（新账号常见）→ 等于整份复制。

「当前关注」这类散文小节不做事无巨细的拼接：目标是自己的当下关注，合并别人的
只会让它变得不可信。要的是「不丢知识」，不是「把所有句子塞进一个桶」。

## 为什么按小节而不是按行

按行去重的结果是：两边都有 `**工作背景**` 时，标题保留一份、正文却并排出现两段，
读起来像两个人各自陈述。按小节切、按小节判，输出才是人写得出来的样子。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths

RAW_START = "<!-- RAW_JSON_START"
RAW_END = "RAW_JSON_END -->"

_HEADER_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_BULLET_RE = re.compile(r"^[-*+]\s+")


def memory_dir(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / "memory"


def memory_path(uid: str, base: str | os.PathLike[str] | None = None) -> Path:
    return memory_dir(base) / f"{uid}_memory.md"


@dataclass
class Section:
    """记忆里的一个小节：`**标题**` + 正文行。"""

    heading: str
    lines: list[str] = field(default_factory=list)

    @property
    def is_bullets(self) -> bool:
        body = [ln for ln in self.lines if ln.strip()]
        if not body:
            return False
        bullets = sum(1 for ln in body if _BULLET_RE.match(ln))
        return bullets / len(body) >= 0.8

    def bullets(self) -> list[str]:
        return [ln for ln in self.lines if _BULLET_RE.match(ln)]

    def text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class ParsedMemory:
    """一份 `_memory.md` 的结构化视图。"""

    path: Path
    block: str = ""
    version: int = 0
    updated_at: str = ""
    uid: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.block.strip()

    def sections(self) -> list[Section]:
        return _split_sections(self.block)


@dataclass
class MergeReport:
    """合并结果。"""

    target_path: Path | None = None
    appended_sections: list[str] = field(default_factory=list)
    appended_bullets: int = 0
    kept_sections: list[str] = field(default_factory=list)
    changed: bool = False
    backup: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.changed:
            return "记忆无需合并（目标已包含源的全部内容）"
        parts = [f"补入 {len(self.appended_sections)} 个小节"]
        if self.appended_bullets:
            parts.append(f"补入 {self.appended_bullets} 条动态")
        return "，".join(parts)


# ---------------------------------------------------------------- 解析


def _split_sections(block: str) -> list[Section]:
    """把 memoryBlock 切成小节。第一个 `**标题**` 之前的内容归入空标题小节。"""
    out: list[Section] = []
    current = Section(heading="")
    for line in block.splitlines():
        m = _HEADER_RE.match(line.strip())
        if m:
            if current.lines and any(ln.strip() for ln in current.lines):
                out.append(current)
            current = Section(heading=m.group(1).strip())
            continue
        current.lines.append(line)
    if any(ln.strip() for ln in current.lines) or current.heading:
        out.append(current)
    # 去掉首尾空行
    for sec in out:
        while sec.lines and not sec.lines[0].strip():
            sec.lines.pop(0)
        while sec.lines and not sec.lines[-1].strip():
            sec.lines.pop()
    return out


def parse(uid: str, base: str | os.PathLike[str] | None = None) -> ParsedMemory:
    """读一份记忆文件；不存在时返回空结构（不抛）。"""
    p = memory_path(uid, base)
    parsed = ParsedMemory(path=p, uid=uid)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return parsed

    if RAW_START in text and RAW_END in text:
        start = text.index(RAW_START)
        head = text.index("\n", start) + 1
        end = text.index(RAW_END)
        try:
            data = json.loads(text[head:end].strip())
        except (json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            parsed.raw = data
            block = data.get("memoryBlock")
            if isinstance(block, str):
                parsed.block = block
            ver = data.get("version")
            if isinstance(ver, (int, float)):
                parsed.version = int(ver)
            at = data.get("updatedAt")
            if isinstance(at, str):
                parsed.updated_at = at
            raw_uid = data.get("uid")
            if isinstance(raw_uid, str) and raw_uid:
                parsed.uid = raw_uid

    if not parsed.block:
        # 没有 JSON 块（或块里没内容）：退回从 markdown 正文里取
        parsed.block = _block_from_markdown(text)
    return parsed


def _block_from_markdown(text: str) -> str:
    marker = "## Memory Block"
    if marker not in text:
        return ""
    body = text.split(marker, 1)[1]
    for stop in (RAW_START, "\n---"):
        if stop in body:
            body = body.split(stop, 1)[0]
    return body.strip()


# ---------------------------------------------------------------- 合并


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def merge_blocks(target: str, source: str) -> tuple[str, list[str], int]:
    """合并两份 memoryBlock。返回 (新内容, 新增小节标题, 新增列表条目数)。"""
    if not source.strip():
        return target, [], 0
    if not target.strip():
        secs = _split_sections(source)
        return source.strip(), [s.heading or "（无标题）" for s in secs], 0

    t_secs = _split_sections(target)
    s_secs = _split_sections(source)
    index = {_norm(s.heading): s for s in t_secs}

    appended: list[str] = []
    added_bullets = 0
    extra: list[Section] = []

    for s_sec in s_secs:
        key = _norm(s_sec.heading)
        t_sec = index.get(key)
        if t_sec is None:
            extra.append(s_sec)
            appended.append(s_sec.heading or "（无标题）")
            continue
        # 同名小节：都是列表 → 条目并集；否则保留目标（本地优先）
        if t_sec.is_bullets and s_sec.is_bullets:
            have = {_norm(b) for b in t_sec.bullets()}
            fresh = [b for b in s_sec.bullets() if _norm(b) not in have]
            if fresh:
                t_sec.lines.extend(fresh)
                added_bullets += len(fresh)

    parts: list[str] = []
    for sec in t_secs:
        if sec.heading:
            parts.append(f"**{sec.heading}**")
        parts.extend(sec.lines)
        parts.append("")
    for sec in extra:
        if sec.heading:
            parts.append(f"**{sec.heading}**")
        parts.extend(sec.lines)
        parts.append("")

    text = "\n".join(parts).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, appended, added_bullets


def _render(parsed: ParsedMemory, block: str, version: int, updated_at: str) -> str:
    """渲染回客户端认识的格式（正文 + RAW_JSON 两处同时更新）。"""
    raw = dict(parsed.raw or {})
    raw["uid"] = parsed.uid
    raw["memoryBlock"] = block
    raw["version"] = version
    raw["updatedAt"] = updated_at
    payload = json.dumps(raw, ensure_ascii=False, indent=2)
    return (
        "# User Memory Profile\n"
        f"> Last updated: {updated_at}\n"
        f"> Version: {version}\n"
        "\n"
        "## Memory Block\n"
        "\n"
        f"{block}\n"
        "\n"
        "---\n"
        "\n"
        f"{RAW_START}\n"
        f"{payload}\n"
        f"{RAW_END}\n"
    )


def merge(
    source_uid: str,
    target_uid: str,
    base: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> MergeReport:
    """把 `source_uid` 的记忆并进 `target_uid`。改前备份目标文件。"""
    report = MergeReport()
    if not source_uid or not target_uid or source_uid == target_uid:
        report.warnings.append("源账号与目标账号相同，或缺少账号，未做合并")
        return report

    src = parse(source_uid, base)
    dst = parse(target_uid, base)
    report.target_path = dst.path
    if src.empty:
        report.warnings.append("源账号没有长期记忆，跳过")
        return report

    merged, appended, bullets = merge_blocks(dst.block, src.block)
    report.appended_sections = appended
    report.appended_bullets = bullets
    report.kept_sections = [
        s.heading for s in dst.sections() if s.heading and s.heading not in appended
    ]
    if not appended and not bullets:
        return report
    report.changed = True
    if dry_run:
        return report

    if dst.path.is_file():
        report.backup = _backup(dst.path, base)

    version = max(dst.version, src.version) + 1
    updated_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    dst.uid = target_uid  # 记忆文件归属必须写目标账号，不能沿用文件里的旧 uid
    content = _render(dst, merged, version, updated_at)
    dst.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.path.with_name(dst.path.name + ".wbsync-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, dst.path)

    # 写后校验：两处内容必须一致，否则宁可报错让人看见
    check = parse(target_uid, base)
    if check.empty or _norm(check.block) != _norm(merged):
        report.warnings.append("记忆文件写后校验失败：解析回来的内容与写入不一致")
    return report


def _backup(path: Path, base) -> Path | None:
    from . import guard

    bak = guard.create_backup([path], base=base, label="memory")
    for copy_path, _orig in bak.files:
        return copy_path
    return None


def available(base: str | os.PathLike[str] | None = None) -> list[str]:
    """本机存在记忆文件的账号 uid。"""
    d = memory_dir(base)
    if not d.is_dir():
        return []
    out: list[str] = []
    for entry in d.iterdir():
        if entry.is_file() and entry.name.endswith("_memory.md"):
            uid = entry.name[: -len("_memory.md")]
            if uid:
                out.append(uid)
    return out


def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    uids = available(base)
    if not uids:
        return "memorymerge: 未找到记忆文件（跳过）"
    parsed = parse(uids[0], base)
    return f"memorymerge: 接口就绪, {len(uids)} 个账号的记忆, 首个 {len(parsed.block)} 字"
