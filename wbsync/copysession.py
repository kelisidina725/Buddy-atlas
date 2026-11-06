"""会话复制 —— 把 A 账号的会话复制成 B 账号的一份**新**会话。

## 为什么必须换新 id

`sessions.id` 是主键，一条会话只能属于一个账号。想让两个账号「都有这条对话」，
唯一办法是**复制**：新的 uuid + 新的正文文件 + 新的云端映射。原会话一个字节不动。

之前得出过「会话没法复制、只能搬移」的结论，是因为只看了主键约束、没看正文命名规则：
正文是 `projects/{workspace}/{cid}.jsonl`，**按 cid 命名**，所以只要换个 cid 就能并出第二份。
同理正文里所有 `sessionId` 引用（实测一条 2.4 MB 的会话里有 1166 处）必须一起换掉，
否则客户端会把副本当成原会话的影子。

## 一条会话的四个落点（复制时缺一不可）

    1) workbuddy.db      -> sessions            索引行（id/user_id/时间戳）
    2) projects/**/      -> <new_cid>.jsonl     正文（cid 已替换）
    3) edge-sync-mapping -> edge_sync_mapping   云端归属 convmsg:{目标 uid}
    4) wbsync-copies.json -> 账本               防重复复制（本工具自有）

`session_usage`（token 用量）**不复制**：它是源账号的消耗账，复制过去等于给目标账号
虚报用量。副本缺这一行只会让用量显示为 0，不影响会话本身。

## 三条拒绝规则（不是保守，是实测）

- **云端会话**（`transport == 'cloud'`）本地没有正文，复制出来是个空壳；
- **Claw 工作区**（`cwd` 末段是 `claw`）绑定的是当前账号的 IM 渠道，复制过去目标账号也用不了；
- **缺正文**的本地会话同样没有可复制的内容。

## 顺序：先正文，再写库

正文先落地，库写失败时最坏是多出几个没人引用的 jsonl（可清理、不损坏任何数据）；
反过来则会造出「库里有行、正文不存在」的坏会话，客户端打开就是报错。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import dbcopy, guard, paths
from .fingerprint import lf_size

LEDGER_NAME = "wbsync-copies.json"
LEDGER_VERSION = 1

# 正文复制的并行度：纯文件 I/O，SSD 上 4 条队列基本吃满；
# 再多只会让同一目录里同时出现更多新文件，收益抵不过目录锁竞争。
_MAX_WORKERS = 4

# 复制时强制改写的列。其余列一律照抄，避免客户端新增字段被我们丢掉。
_OVERRIDE_NOTE = {
    "id": "新 uuid",
    "user_id": "目标账号",
    "created_at": "复制时刻",
    "updated_at": "复制时刻",
    "deleted_at": "清空（副本是活的）",
    "status": "working/active 归一为 completed",
    "unread": "0（副本不该是未读）",
}

_CLAW_DIR = "claw"


# ---------------------------------------------------------------- 数据结构


@dataclass
class CopyItem:
    """一条待复制会话的实测状态。"""

    cid: str
    title: str = ""
    user_id: str = ""
    cwd: str = ""
    transport: str = ""
    deleted: bool = False
    body: Path | None = None
    body_bytes: int = 0
    updated_at: int = 0
    # 不可复制的原因（空串 = 可复制）
    block_reason: str = ""
    # 已经复制过：目标账号里那条副本的 cid（空串 = 没复制过）
    duplicate_of: str = ""
    # 目标账号里已有**内容等价、只是 cid 不同**的会话（空串 = 没有）。
    # 与 duplicate_of 的区别：这条账本里没记，是靠正文摘要认出来的。
    same_content_of: str = ""

    @property
    def copyable(self) -> bool:
        return not self.block_reason and not self.duplicate_of

    @property
    def selectable(self) -> bool:
        """能不能让用户勾着复制。

        云端 / Claw / 缺正文的**不能**（复制过去是空壳或不可用）；
        重复的**能** —— 那是「不建议」，不是「做不到」，用户可能执意要再复制一份。
        """
        return not self.block_reason

    @property
    def duplicate(self) -> bool:
        return bool(self.duplicate_of or self.same_content_of)

    @property
    def recommended(self) -> bool:
        """默认勾选：可复制、且不是重复内容。"""
        return self.selectable and not self.duplicate

    @property
    def kind_text(self) -> str:
        if self.block_reason:
            return self.block_reason
        if self.duplicate_of:
            return "已复制过"
        if self.same_content_of:
            return "已有相同内容"
        return "有正文"


@dataclass
class CopyPlan:
    """复制计划。`plan_copy` 只读，不写任何东西。"""

    source_uid: str = ""
    target_uid: str = ""
    items: list[CopyItem] = field(default_factory=list)
    db_path: Path | None = None
    edge_db_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def copyable(self) -> list[CopyItem]:
        return [i for i in self.items if i.copyable]

    @property
    def blocked(self) -> list[CopyItem]:
        return [i for i in self.items if i.block_reason]

    @property
    def duplicates(self) -> list[CopyItem]:
        return [i for i in self.items if i.duplicate_of]

    @property
    def body_bytes(self) -> int:
        return sum(i.body_bytes for i in self.copyable)

    @property
    def same_account(self) -> bool:
        return bool(self.source_uid) and self.source_uid == self.target_uid

    @property
    def empty(self) -> bool:
        return not self.copyable

    def summary(self) -> str:
        parts = [f"可复制 {len(self.copyable)} 条"]
        if self.duplicates:
            parts.append(f"已复制过 {len(self.duplicates)} 条")
        if self.blocked:
            parts.append(f"不可复制 {len(self.blocked)} 条")
        if self.body_bytes:
            parts.append(f"正文约 {_human_size(self.body_bytes)}")
        return " · ".join(parts)


@dataclass
class CopyReport:
    """复制结果。"""

    copied: list[tuple[str, str]] = field(default_factory=list)  # (源 cid, 新 cid)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup: guard.Backup | None = None
    body_bytes: int = 0
    edge_registered: int = 0
    orphan_bodies: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.copied) and not self.failed

    def summary(self) -> str:
        parts = [f"已复制 {len(self.copied)} 条会话"]
        if self.body_bytes:
            parts.append(f"正文 {_human_size(self.body_bytes)}")
        if self.edge_registered and self.edge_registered < len(self.copied):
            parts.append(f"云端映射 {self.edge_registered}/{len(self.copied)}")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 条")
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 条")
        return "，".join(parts)


def _human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ---------------------------------------------------------------- 账本


def ledger_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / LEDGER_NAME


def load_ledger(base: str | os.PathLike[str] | None = None) -> dict[str, dict]:
    p = ledger_path(base)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    raw = data.get("copies") if isinstance(data, dict) else None
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}


def save_ledger(copies: dict[str, dict], base: str | os.PathLike[str] | None = None) -> None:
    p = ledger_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": LEDGER_VERSION, "copies": copies}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def ledger_key(source_cid: str, target_uid: str) -> str:
    return f"{source_cid}|{target_uid}"


def lineage(ledger: dict[str, dict]) -> dict[str, str]:
    """副本 cid -> 它的来源 cid。用于追一条会话的「血统」。

    ★ 为什么需要它：并集对齐里，A 的会话被复制到 B 之后，B 名下的那份是个
      **新 cid**。下一轮对齐时，它看起来就是「B 的独有会话」，会被再复制回 A ——
      而 A 本来就有它。跑两轮，每个账号里的副本数量就翻一倍，跑三轮翻三倍。
      按血统把「同一个内容的副本族」认出来，就能判出「目标其实已经有了」。
    """
    out: dict[str, str] = {}
    for key, entry in ledger.items():
        if not isinstance(entry, dict):
            continue
        new_cid = str(entry.get("new_cid") or "")
        source_cid = key.rsplit("|", 1)[0]
        if new_cid and source_cid:
            out[new_cid] = source_cid
    return out


def canonical_cid(cid: str, produced: dict[str, str], _depth: int = 0) -> str:
    """沿账本回溯到最初的那条 cid。带深度上限，坏账本不会把进程拖死。"""
    seen: set[str] = set()
    cur = cid
    while cur in produced and cur not in seen and _depth < 32:
        seen.add(cur)
        cur = produced[cur]
        _depth += 1
    return cur


def session_cids(uid: str, base: str | os.PathLike[str] | None = None) -> set[str]:
    """某账号在库里拥有的全部会话 id（含已删除 —— 删除标记不等于没有过）。

    ⚠️ 判重别用它：已删除的那条用户**前台看不到**，拿它当「目标已有」
    会漏复制（本机实测 4 个账号里有 2 个存在 25 / 37 条这种记录）。
    判重请用 `alive_session_cids`。
    """
    db = paths.workbuddy_db_path(base)
    if not db.is_file() or not uid:
        return set()
    try:
        with dbcopy.readonly_db_copy(db) as conn:
            return {str(r[0]) for r in conn.execute(
                "SELECT id FROM sessions WHERE user_id = ?", (uid,)
            )}
    except sqlite3.Error:
        return set()


def alive_session_cids(uid: str, base: str | os.PathLike[str] | None = None) -> set[str]:
    """某账号**前台看得见**的会话 id（`deleted_at IS NULL`）。

    ★ 「看得见」是本工具所有判定的统一口径：计划页写「该账号已有」、
      同族/内容判重把某条当成「已有」，都必须以用户真正能看到的为准。
      已删除的那条用户切过去也看不到，算作「已有」等于对齐没做到。
    """
    db = paths.workbuddy_db_path(base)
    if not db.is_file() or not uid:
        return set()
    try:
        with dbcopy.readonly_db_copy(db) as conn:
            return {str(r[0]) for r in conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? AND deleted_at IS NULL",
                (uid,),
            )}
    except sqlite3.Error:
        return set()


# ---------------------------------------------------------------- 增量追加


def logical_line_count(text: str) -> int:
    """与 fingerprint / append 同一套行切分：末尾换行不额外算一行。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return len(lines)


def append_delta(
    src: Path,
    dst: Path,
    src_cid: str,
    dst_cid: str,
    common: int,
) -> int:
    """把 `src` 第 `common` 行之后的增量追加到 `dst`，返回追加的字节数。

    ## 为什么这是唯一会改写已有正文的操作

    复制只新增文件，出错最坏是多几个没人引用的 jsonl。追加是就地改写一条
    **用户正在用的会话**，所以这里：先写临时文件再 `os.replace` 原子替换，
    绝不「以 a 模式追加」——中途失败会留下半行 JSON，客户端打开就报错。

    ## 前置条件（调用方必须保证）

    `common` 必须等于 `dst` 的行数，即「dst 的全文是 src 的前缀」。
    调用方在计划阶段用全量逐行 hash 验过；这里再核一次，不符就不动。

    `src_cid` → `dst_cid`：追加进来的行里嵌的是源的 sessionId，必须换成目标的，
    否则客户端会把这段当成另一条会话的影子。
    """
    try:
        src_text = src.read_text(encoding="utf-8", errors="strict")
        dst_text = dst.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return -1

    src_lines = src_text.split("\n")
    if src_lines and src_lines[-1] == "":
        src_lines.pop()
    dst_lines = dst_text.split("\n")
    if dst_lines and dst_lines[-1] == "":
        dst_lines.pop()

    # 前提校验：dst 必须正好是 src 的前缀。不符说明计划过期或内容已变。
    if len(dst_lines) != common or common > len(src_lines):
        return -2
    delta = src_lines[common:]
    if not delta:
        return 0

    body = "\n".join(_rewrite_cid(line, src_cid, dst_cid) for line in delta) + "\n"
    out = dst_text if dst_text.endswith("\n") or not dst_text else dst_text + "\n"
    tmp = dst.with_name(dst.name + ".wbsync-append")
    try:
        # newline="" 同上：追加会把整份正文重写一遍，默认文本模式会把原本 LF 的
        # 文件整体改成 CRLF —— 一次追加就让这条会话再也判不进「内容重复」。
        tmp.write_text(out + body, encoding="utf-8", newline="")
        os.replace(tmp, dst)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return -1
    return len(body.encode("utf-8"))


# ---------------------------------------------------------------- 只读盘点


def _is_claw(cwd: str) -> bool:
    tail = (cwd or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail.strip().lower() == _CLAW_DIR


def _title_of(row: dict) -> str:
    for key in ("custom_title", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bodies_index(base) -> dict[str, list[Path]]:
    """cid -> 全部同名正文副本（同一 cid 可能在多个工作空间目录下都有）。"""
    root = paths.projects_dir(base)
    out: dict[str, list[Path]] = {}
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                out.setdefault(fn[: -len(".jsonl")], []).append(Path(dirpath) / fn)
    return out


def _read_rows(conn: sqlite3.Connection, uid: str) -> list[dict]:
    cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
    if not cols:
        return []
    want = [
        c
        for c in (
            "id",
            "user_id",
            "title",
            "custom_title",
            "cwd",
            "transport",
            "deleted_at",
            "updated_at",
            "created_at",
            "last_activity_at",
        )
        if c in cols
    ]
    if "id" not in want:
        return []
    rows = conn.execute(
        f'SELECT {", ".join(want)} FROM "sessions" WHERE user_id = ? '
        "ORDER BY COALESCE(updated_at, 0) DESC",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


def _target_cids(conn: sqlite3.Connection, uid: str) -> dict[str, bool]:
    """目标账号现有会话：cid -> 是否仍存活（未删除）。"""
    try:
        rows = conn.execute(
            'SELECT id, deleted_at FROM "sessions" WHERE user_id = ?', (uid,)
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(r[0]): r[1] is None for r in rows}


def plan_copy(
    target_uid: str,
    source_uid: str = "",
    cids: list[str] | None = None,
    base: str | os.PathLike[str] | None = None,
    include_deleted: bool = False,
) -> CopyPlan:
    """算出一份复制计划。**只读**。

    `cids` 为空 = 源账号全部未删除会话。`source_uid` 为空时自动取当前登录账号
    （认证文件优先，其次骨架快照 —— 与 accounts.detect_primary_uid 同判据）。
    """
    plan = CopyPlan(db_path=paths.workbuddy_db_path(base))
    plan.edge_db_path = paths.edge_sync_db_path(base)
    if not target_uid:
        plan.warnings.append("未指定目标账号")
        return plan
    if not source_uid:
        from . import authfile, accounts

        source_uid = authfile.current_uid(base) or accounts.detect_primary_uid(base)
    plan.source_uid = source_uid
    plan.target_uid = target_uid
    if not source_uid:
        plan.warnings.append("未识别到源账号（当前登录账号），无法列出可复制会话")
        return plan
    if plan.same_account:
        plan.warnings.append("源账号与目标账号相同，无需复制")
        return plan
    if not plan.db_path.is_file():
        plan.warnings.append(f"找不到会话数据库：{plan.db_path}")
        return plan

    wanted = {c for c in (cids or []) if c}
    bodies = _bodies_index(base)
    ledger = load_ledger(base)

    with dbcopy.readonly_db_copy(plan.db_path) as conn:
        rows = _read_rows(conn, source_uid)
        alive = _target_cids(conn, target_uid)

    for row in rows:
        cid = str(row.get("id") or "")
        if not cid:
            continue
        if wanted and cid not in wanted:
            continue
        deleted = row.get("deleted_at") is not None
        if deleted and not include_deleted:
            continue

        candidates = bodies.get(cid, [])
        body = candidates[0] if candidates else None
        # ★ 用 LF 口径而不是 st_size：判重走的是内容摘要（换行符不敏感），
        #   预筛若按原始字节数，LF / CRLF 两种写法的同一段对话永远撞不上车。
        size = 0
        if body is not None:
            got = lf_size(body)
            if got is None:
                body = None
            else:
                size = got

        item = CopyItem(
            cid=cid,
            title=_title_of(row),
            user_id=str(row.get("user_id") or ""),
            cwd=str(row.get("cwd") or ""),
            transport=str(row.get("transport") or ""),
            deleted=deleted,
            body=body,
            body_bytes=size,
            updated_at=int(row.get("updated_at") or 0),
        )

        if item.transport.lower() == "cloud":
            item.block_reason = "云端会话，本地没有正文"
        elif body is None:
            item.block_reason = "正文文件缺失"
        elif _is_claw(item.cwd):
            item.block_reason = "Claw 工作区绑定当前账号渠道"

        entry = ledger.get(ledger_key(cid, target_uid))
        if entry:
            new_cid = str(entry.get("new_cid") or "")
            # 副本还在且没被删 → 跳过；已被删 → 允许重新复制
            if new_cid and alive.get(new_cid, False):
                item.duplicate_of = new_cid

        plan.items.append(item)

    if wanted:
        missing = wanted - {i.cid for i in plan.items}
        if missing:
            plan.warnings.append(
                f"有 {len(missing)} 条选中的会话在源账号下不存在（可能属于别的账号或已被物理删除）"
            )
    cloud = sum(1 for i in plan.items if "云端" in i.block_reason)
    if cloud:
        plan.warnings.append(
            f"其中 {cloud} 条是云端会话：正文不在本机，复制过去会是空壳，已列入不可复制。"
            "如需转移，请先在客户端里打开它让正文落地。"
        )
    return plan


# ---------------------------------------------------------------- 执行


def _rewrite_cid(text: str, cid: str, new_cid: str) -> str:
    """把正文里的 cid 换成新 cid。

    ★ 用**边界断言**而不是裸 `str.replace`：实测一个 cid 在正文里出现上千次，
      全部是 `"sessionId":"<cid>"` 这类完整值；一旦某天它作为更长串的一段出现，
      裸替换会静默改坏内容。前后不允许再跟十六进制/连字符，正好卡住这种情形。
    """
    return re.sub(
        r"(?<![0-9A-Za-z_-])" + re.escape(cid) + r"(?![0-9A-Za-z_-])",
        new_cid,
        text,
    )


def rewrite_cid(text: str, cid: str, new_cid: str) -> str:
    """公开入口：把正文里的 cid 换成新 cid（供导入器「保留两份」复用）。

    与 `_rewrite_cid` 同一实现 —— 两边必须用同一套边界断言，否则同一条正文
    在「账号复制」和「分叉保留两份」两条路径上会被改出不同结果。
    """
    return _rewrite_cid(text, cid, new_cid)


def _dest_body_path(src: Path, new_cid: str) -> Path:
    return src.with_name(f"{new_cid}.jsonl")


def _copy_body(src: Path, dst: Path, cid: str, new_cid: str) -> int:
    """复制正文。**必须写 LF**（`newline=""`）。

    ★ 2026-11-03 实测的坑：`Path.write_text` 默认走文本模式，Windows 上把每个
      `\\n` 翻成 `\\r\\n`。同内容的两份副本因此字节数差「行数」个字节，而判重的
      预筛是**按字节数**找候选（见 importer._LocalContent），于是这对副本永远
      撞不上车 —— 换机导入后侧边栏里就多出一条一模一样的会话。
      读侧用的是 universal newlines（`\\r\\n` 已归一成 `\\n`），所以写侧一旦
      转换，两端口径就不一致了。这里显式声明「不转换」，让正文与客户端自己写的
      完全同构。
    """
    text = src.read_text(encoding="utf-8", errors="strict")
    text = _rewrite_cid(text, cid, new_cid)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8", newline="")
    return len(text.encode("utf-8"))


def _row_values(conn: sqlite3.Connection, cid: str) -> tuple[list[str], list]:
    cur = conn.execute('SELECT * FROM "sessions" WHERE id = ?', (cid,))
    row = cur.fetchone()
    if row is None:
        return [], []
    cols = [d[0] for d in cur.description]
    return cols, list(row)


def _insert_row(
    conn: sqlite3.Connection,
    cols: list[str],
    values: list,
    new_cid: str,
    target_uid: str,
    now: int,
) -> None:
    """插索引行：只改必要列，其余照抄（客户端新增字段不能被我们丢掉）。"""
    dst_cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
    out_cols: list[str] = []
    out_vals: list = []
    for col, value in zip(cols, values):
        if col not in dst_cols:
            continue
        if col == "id":
            value = new_cid
        elif col == "user_id":
            value = target_uid
        elif col in ("created_at", "updated_at"):
            value = now
        elif col == "deleted_at":
            value = None
        elif col == "status":
            value = "completed" if value in ("working", "active") else value
        elif col == "unread":
            value = 0
        out_cols.append(col)
        out_vals.append(value)
    if not out_cols:
        raise RuntimeError("sessions 表没有可用列")
    marks = ", ".join("?" for _ in out_cols)
    conn.execute(
        f'INSERT OR REPLACE INTO "sessions" ({", ".join(out_cols)}) VALUES ({marks})',
        tuple(out_vals),
    )


def _register_edge(conn: sqlite3.Connection, new_cid: str, target_uid: str) -> bool:
    """注册云端归属。表不存在 → False（绝不新建空库）。"""
    if not dbcopy.table_exists(conn, "edge_sync_mapping"):
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO edge_sync_mapping "
            "(session_id, conversation_id, msg_channel, created_at) VALUES (?, ?, ?, ?)",
            (new_cid, new_cid, f"convmsg:{target_uid}", int(time.time())),
        )
    except sqlite3.Error:
        return False
    return True


def execute_copy(
    plan: CopyPlan,
    base: str | os.PathLike[str] | None = None,
    progress=None,
    backup: bool = True,
) -> CopyReport:
    """执行复制。会先断言客户端已退出。

    `backup=False` 供「批量编排」使用（例如本机账号对齐要对多个目标各调用一次，
    每次都备份整库只是把同一份库复制 N 遍）。此时**调用方必须自己先备份**。
    """
    report = CopyReport()
    items = plan.copyable
    report.warnings.extend(plan.warnings)
    if not items:
        return report

    # 铁律 1：客户端运行中一律拒写
    guard.assert_safe_to_write()

    db_path = plan.db_path or paths.workbuddy_db_path(base)
    edge_path = plan.edge_db_path or paths.edge_sync_db_path(base)
    now = int(time.time() * 1000)

    # ---- 备份：整批一次（正文不改原文件，只新增，不需要备份）
    db_files = [p for p in (db_path, edge_path) if p.is_file()]
    if backup:
        try:
            report.backup = guard.create_backup(db_files, base=base, label="copy")
        except OSError as exc:
            report.warnings.append(f"备份失败（继续）：{exc.strerror or exc}")

    # ---- phase A：并行复制正文（纯新增文件，互不相干）
    total = len(items)
    if progress is not None:
        progress(0, total, "复制会话正文…")

    prepared: list[tuple[CopyItem, str, Path | None, int]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(1, total))) as pool:
        futures = {}
        for item in items:
            new_cid = str(uuid.uuid4())
            dst = _dest_body_path(item.body, new_cid) if item.body is not None else None
            prepared.append((item, new_cid, dst, 0))
            if item.body is not None and dst is not None:
                futures[pool.submit(_copy_body, item.body, dst, item.cid, new_cid)] = (
                    item,
                    new_cid,
                    dst,
                )
        sizes: dict[str, int] = {}
        for fut, (item, new_cid, dst) in futures.items():
            try:
                sizes[new_cid] = fut.result()
            except (OSError, UnicodeDecodeError) as exc:
                report.failed.append((item.cid, f"正文复制失败：{exc}"))
                sizes[new_cid] = -1
            done += 1
            if progress is not None:
                progress(done, total, f"复制正文 {done}/{total}")

    # ---- phase B：顺序写库（客户端已关闭，全部 db 写集中在这一段）
    usable: list[tuple[CopyItem, str, Path | None, int]] = []
    for item, new_cid, dst, _ in prepared:
        size = sizes.get(new_cid, 0)
        if size < 0:
            continue  # 正文失败，已在 failed 里
        usable.append((item, new_cid, dst, size))

    ledger = load_ledger(base)
    written: list[tuple[str, str]] = []
    try:
        with guard.DbEditSession(db_path) as conn:
            for item, new_cid, dst, size in usable:
                cols, values = _row_values(conn, item.cid)
                if not cols:
                    report.failed.append((item.cid, "源会话行已不存在"))
                    if dst is not None:
                        report.orphan_bodies.append(dst)
                    continue
                _insert_row(conn, cols, values, new_cid, plan.target_uid, now)
                written.append((item.cid, new_cid))
                ledger[ledger_key(item.cid, plan.target_uid)] = {
                    "new_cid": new_cid,
                    "source_uid": plan.source_uid,
                    "target_uid": plan.target_uid,
                    "title": item.title,
                    "at": now,
                }
                report.copied.append((item.cid, new_cid))
                report.body_bytes += max(0, size)
    except Exception as exc:  # noqa: BLE001 - 交给界面展示
        report.failed.append(("workbuddy.db", f"{type(exc).__name__}: {exc}"))
        # 整库写失败 = 一条都没成功：已 append 的 copied 是事务内的临时记账，
        # 留着会让报告写「已复制 3 条」，而库里一条都没有
        report.copied.clear()
        report.body_bytes = 0
        report.warnings.append(
            "写库失败：库文件未被修改，但新复制的正文文件已留在 projects 目录下（不会被客户端引用）。"
        )
        return report

    # ---- 云端映射：失败不阻断（会话行已写入，客户端重启后仍能看到本地会话）
    if edge_path.is_file():
        try:
            with guard.DbEditSession(edge_path) as conn:
                for _source, new_cid in written:
                    if _register_edge(conn, new_cid, plan.target_uid):
                        report.edge_registered += 1
        except Exception as exc:  # noqa: BLE001
            report.failed.append(("edge-sync-mapping", f"{type(exc).__name__}: {exc}"))
    else:
        report.warnings.append("未找到云端映射库：副本只在本地可见，不会同步到云端。")

    if report.edge_registered and report.edge_registered < len(report.copied):
        report.warnings.append(
            f"{len(report.copied) - report.edge_registered} 条副本未注册云端映射，"
            "它们只在本机可见。"
        )

    # ---- 账本落盘（失败只影响去重，不影响已复制的数据）
    try:
        save_ledger(ledger, base)
    except OSError as exc:
        report.warnings.append(f"复制记账写入失败（下次可能重复复制同一批）：{exc.strerror or exc}")

    # ---- 写后校验
    if not guard.verify_db_readable(db_path, "sessions"):
        report.failed.append(("workbuddy.db", "写后校验失败：库不可读或 sessions 表丢失"))
    if edge_path.is_file() and not guard.verify_db_readable(edge_path, "edge_sync_mapping"):
        report.failed.append(("edge-sync-mapping", "写后校验失败：库不可读或表丢失"))

    if report.copied:
        report.warnings.append(
            "副本会在客户端重启后出现在目标账号的侧边栏；侧边栏快照由客户端自行重建，本工具不改它。"
        )
    return report


# ---------------------------------------------------------------- 自检


def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    db = paths.workbuddy_db_path(base)
    if not db.is_file():
        return "copysession: 未找到会话数据库（跳过）"
    ledger = load_ledger(base)
    with dbcopy.readonly_db_copy(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = len(list(conn.execute('PRAGMA table_info("sessions")')))
        edge = "有" if "edge_sync_mapping" in tables else "无"
    return (
        f"copysession: 接口就绪, sessions {cols} 列, 账本 {len(ledger)} 条, "
        f"本库云端映射表 {edge}, 共 {len(tables)} 张表"
    )
