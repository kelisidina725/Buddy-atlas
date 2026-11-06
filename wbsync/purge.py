"""物理删除会话 —— 把软删除或任意选中会话从库里彻底移除。

## 为什么不是「删一行」

`workbuddy.db` 的 `sessions` 只是索引，一条会话分布在四处：

  1) workbuddy.db        -> sessions / session_usage / automation_runs
  2) edge-sync-mapping   -> edge_sync_mapping
  3) projects/**/<cid>.jsonl   正文（同一 cid 可能在多个工作空间目录下都有副本）
  4) wbsync-imported.json      本工具自己的导入记账

少删任何一处都会留下孤儿：行没了正文还在（浪费空间、重新导入时又冒出来），
或者映射还在（客户端继续把它当云端会话处理）。

## 顺序即正确性

必须先备份 → 再改库（副本改完原子替换）→ 最后删正文。
反过来（先删文件）会造出「库指向一个不存在的正文」的孤儿状态；
而库写失败时正文还在，什么也没丢。

## 云端回流（尽力而为）

`transport=cloud` 的会话本地没有正文，真正的删除信号来自客户端的软删除。
物理删行后这个信号消失，服务器仍可能把会话重新下发 —— 本地能做的只有
清掉 `edge_sync_mapping`，无法阻止服务器。所以这类目标一律在报告里提示。
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import dbcopy, guard, importlog, paths

# 单条 SQL 的参数上限，避免 IN (...) 超过 SQLite 的变量数限制
_CHUNK = 400


# ---------------------------------------------------------------- 数据结构


@dataclass
class PurgeTarget:
    """一条待删除会话的实测状态。"""

    cid: str
    title: str = ""
    user_id: str = ""
    transport: str = ""
    deleted: bool = False
    body_paths: tuple[Path, ...] = ()
    body_bytes: int = 0
    # 数据库里已经不存在（UI 视图过期）—— 只记录，不参与删除
    missing: bool = False

    @property
    def is_cloud(self) -> bool:
        return (self.transport or "").lower() == "cloud"

    @property
    def has_body(self) -> bool:
        return bool(self.body_paths)

    @property
    def kind_text(self) -> str:
        if self.missing:
            return "已不存在"
        if self.is_cloud:
            return "云端"
        if not self.body_paths:
            return "缺正文"
        return "本地"


@dataclass
class PurgePlan:
    """删除前算好的范围。plan_purge 只读，不写任何东西。"""

    targets: list[PurgeTarget] = field(default_factory=list)
    db_path: Path | None = None
    edge_db_path: Path | None = None
    # 是否把原件备份到 wbsync-backup/<stamp>-purge/（默认不备份）
    backup: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def live(self) -> list[PurgeTarget]:
        return [t for t in self.targets if not t.missing]

    @property
    def cids(self) -> list[str]:
        return [t.cid for t in self.live]

    @property
    def local_count(self) -> int:
        return sum(1 for t in self.live if not t.is_cloud)

    @property
    def cloud_count(self) -> int:
        return sum(1 for t in self.live if t.is_cloud)

    @property
    def orphan_count(self) -> int:
        return sum(1 for t in self.live if not t.is_cloud and not t.body_paths)

    @property
    def total_body_bytes(self) -> int:
        return sum(t.body_bytes for t in self.live)

    @property
    def body_files(self) -> list[Path]:
        return [p for t in self.live for p in t.body_paths]

    @property
    def has_alive(self) -> bool:
        """选中项里是否含「客户端里还活着」的会话（决定确认强度）。"""
        return any(not t.deleted for t in self.live)

    @property
    def empty(self) -> bool:
        return not self.live


@dataclass
class PurgeReport:
    """执行结果。"""

    cids: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup: guard.Backup | None = None
    body_removed: int = 0
    body_failed: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"已删除 {len(self.cids)} 条会话"]
        if self.body_removed:
            parts.append(f"移除正文 {self.body_removed} 个")
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 项")
        if self.body_failed:
            parts.append(f"正文未删 {len(self.body_failed)} 个")
        return "，".join(parts)


# ---------------------------------------------------------------- 只读盘点


def _cid_of(x) -> str:
    return str(getattr(x, "cid", x) or "")


def _scan_all_bodies(base) -> dict[str, list[Path]]:
    """扫全部正文文件：cid -> 所有同名副本。

    ★ 不能用 paths.build_projects_index —— 它对同名 cid 只保留首个，
      物理删除时只删一个副本，别的目录里还留着。
    """
    root = paths.projects_dir(base)
    out: dict[str, list[Path]] = {}
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".jsonl"):
                continue
            cid = fn[: -len(".jsonl")]
            out.setdefault(cid, []).append(Path(dirpath) / fn)
    return out


def _read_sessions_state(conn) -> dict[str, dict]:
    """读 sessions 的最新状态（id -> 行）。列缺失时按空值兜底。"""
    cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
    if not cols or "id" not in cols:
        return {}
    want = [c for c in ("id", "title", "custom_title", "user_id", "transport", "deleted_at") if c in cols]
    out: dict[str, dict] = {}
    for row in conn.execute(f'SELECT {", ".join(want)} FROM "sessions"'):
        d = dict(row)
        out[str(d.get("id") or "")] = d
    return out


def _display_title(row: dict) -> str:
    for key in ("custom_title", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def split_cloud(records) -> tuple[list, list]:
    """把待清除的会话分成「本地可物理删」与「云端只能隐藏」两拨。

    ★ 云端（`transport=cloud`）必须走隐藏，不能删行：本地这行是客户端告诉云端
      「这条我删了」的墓碑，删掉之后客户端重启对账发现「云端有、本地没有」，
      会把它重新下发。实测 26 条一次性回流：rowid 161–186 连续追加在表尾、
      `deleted_at` 仍是 -1、`edge_sync_mapping` 已空 —— 删行本身是成功的，
      回流是客户端行为，本地堵不住。

      真正删除只有一条路：在客户端里删、等云端同步完成，云端才不再下发。
    """
    local: list = []
    cloud: list = []
    for r in records:
        if (str(getattr(r, "transport", "") or "").lower()) == "cloud":
            cloud.append(r)
        else:
            local.append(r)
    return local, cloud


def plan_purge(records, base=None) -> PurgePlan:
    """根据 UI 选中的记录，算出一份删除计划。**只读，不写**。

    records 可以是 SessionRecord，也可以直接是 cid 字符串。
    会以数据库**当前**状态为准：库里已不存在的 cid 标 missing，不参与删除，
    避免基于过期视图删到别的东西。
    """
    plan = PurgePlan()
    plan.db_path = paths.workbuddy_db_path(base)
    plan.edge_db_path = paths.edge_sync_db_path(base)

    if not plan.db_path.is_file():
        raise FileNotFoundError(f"找不到会话数据库：{plan.db_path}")

    seen: set[str] = set()
    ordered: list[object] = []
    for x in records:
        cid = _cid_of(x)
        if not cid or cid in seen:
            continue
        seen.add(cid)
        ordered.append(x)

    bodies = _scan_all_bodies(base)
    with dbcopy.readonly_db_copy(plan.db_path) as conn:
        state = _read_sessions_state(conn)

    for x in ordered:
        cid = _cid_of(x)
        row = state.get(cid)
        if row is None:
            plan.targets.append(
                PurgeTarget(cid=cid, title=str(getattr(x, "title", "") or ""), missing=True)
            )
            continue

        paths_for_cid = tuple(bodies.get(cid, ()))
        size = 0
        for p in paths_for_cid:
            try:
                size += p.stat().st_size
            except OSError:
                continue

        plan.targets.append(
            PurgeTarget(
                cid=cid,
                title=str(getattr(x, "title", "") or _display_title(row)),
                user_id=str(row.get("user_id") or ""),
                transport=str(row.get("transport") or ""),
                deleted=row.get("deleted_at") is not None,
                body_paths=paths_for_cid,
                body_bytes=size,
            )
        )

    if plan.cloud_count:
        plan.warnings.append(
            f"其中 {plan.cloud_count} 条为云端会话：本地删行后，客户端仍可能从云端重新拉回。"
            "建议先在客户端里删除并等同步完成，再物理清除。"
        )
    return plan


# ---------------------------------------------------------------- 执行


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _delete_in(conn, table: str, column: str, cids: list[str]) -> int:
    total = 0
    for chunk in _chunks(cids):
        marks = ", ".join("?" for _ in chunk)
        cur = conn.execute(f'DELETE FROM "{table}" WHERE "{column}" IN ({marks})', tuple(chunk))
        total += max(0, cur.rowcount)
    return total


def _delete_sessions_rows(db_path: Path, cids: list[str]) -> None:
    """删 workbuddy.db 里的会话行与关联行。整库副本改完再原子替换。"""
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到会话数据库：{db_path}")

    with guard.DbEditSession(db_path) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "session_usage" in tables:
            _delete_in(conn, "session_usage", "session_id", cids)
        if "automation_runs" in tables:
            cols = {r[1] for r in conn.execute('PRAGMA table_info("automation_runs")')}
            if "thread_id" in cols:
                _delete_in(conn, "automation_runs", "thread_id", cids)
        _delete_in(conn, "sessions", "id", cids)


def _delete_edge_rows(edge_path: Path, cids: list[str]) -> None:
    """删云端映射。库/表不存在时视为无事可做（fail-open，不拖垮主流程）。"""
    if not edge_path.is_file():
        return
    with guard.DbEditSession(edge_path) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "edge_sync_mapping" in tables:
            _delete_in(conn, "edge_sync_mapping", "session_id", cids)


def _backup_for_purge(plan: PurgePlan, body_files: list[Path], base) -> guard.Backup:
    """备份库与正文。

    正文放到 `bodies/` 子目录并按序号重命名 —— create_backup 是按文件名拍平的，
    同一 cid 在多目录下都有副本时会互相覆盖，备份就少了几份。
    """
    db_files = [p for p in (plan.db_path, plan.edge_db_path) if p is not None and p.is_file()]
    bak = guard.create_backup(db_files, base=base, label="purge")
    if not body_files:
        return bak

    body_root = bak.root / "bodies"
    body_root.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(body_files):
        try:
            dst = body_root / f"{i:04d}-{src.name}"
            shutil.copy2(src, dst)
            bak.files.append((dst, src))
        except OSError:
            continue
    return bak


def execute_purge(plan: PurgePlan, base=None, progress=None) -> PurgeReport:
    """执行删除。会先断言客户端已退出；库写失败时不碰正文。"""
    report = PurgeReport()
    targets = plan.live
    if not targets:
        return report

    # 铁律 1：客户端运行中一律拒写
    guard.assert_safe_to_write()

    db_path = plan.db_path or paths.workbuddy_db_path(base)
    edge_path = plan.edge_db_path or paths.edge_sync_db_path(base)
    cids = [t.cid for t in targets]
    body_files = [p for t in targets for p in t.body_paths]

    report.warnings.extend(plan.warnings)
    if plan.cloud_count and not plan.warnings:
        report.warnings.append(
            f"其中 {plan.cloud_count} 条为云端会话，可能被客户端重新拉回。"
        )
    # 侧边栏快照是客户端自有文件，本工具刻意不改（改错格式会让侧边栏整个渲染不出来）。
    # 客户端下次启动通常自行收敛；若仍看到残留条目，重启一次即可。
    report.warnings.append(
        "侧边栏快照未改动；若客户端仍显示已删除的条目，重启一次 WorkBuddy 即可。"
    )

    # 备份（可选）
    if plan.backup:
        report.backup = _backup_for_purge(plan, body_files, base)

    # ---- 库：失败就停，正文保持原样
    try:
        _delete_sessions_rows(db_path, cids)
        report.cids = list(cids)
    except Exception as exc:  # noqa: BLE001 - 交给 UI 展示
        report.failed.append(("workbuddy.db", f"{type(exc).__name__}: {exc}"))
        return report

    # ---- 云端映射：失败不阻断正文清理（会话行已经删了）
    try:
        _delete_edge_rows(edge_path, cids)
    except Exception as exc:  # noqa: BLE001
        report.failed.append(("edge-sync-mapping", f"{type(exc).__name__}: {exc}"))

    # ---- 正文：逐条删，单条失败可重试
    total = len(body_files)
    for i, p in enumerate(body_files, 1):
        try:
            p.unlink()
            report.body_removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            report.body_failed.append((p, exc.strerror or str(exc)))
        if progress is not None:
            progress(i, total or 1)

    # ---- 记账
    try:
        log = importlog.ImportLog.load(base)
        log.forget(cids)
        log.save()
    except OSError as exc:
        report.warnings.append(f"导入记录更新失败：{exc}")

    # ---- 写后校验
    if not guard.verify_db_readable(db_path, "sessions"):
        report.failed.append(("workbuddy.db", "写后校验失败：库不可读或 sessions 表丢失"))
    if edge_path.is_file() and not guard.verify_db_readable(edge_path, "edge_sync_mapping"):
        report.failed.append(("edge-sync-mapping", "写后校验失败：库不可读或表丢失"))

    return report


# ---------------------------------------------------------------- 自检


def selftest_line(base=None) -> str:
    """供 --selftest 用：只读探测库与关联表，失败时抛异常由上层标 FAILED。"""
    db_path = paths.workbuddy_db_path(base)
    if not db_path.is_file():
        return "purge: 未找到数据库（跳过）"
    with dbcopy.readonly_db_copy(db_path) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        total = conn.execute('SELECT COUNT(*) FROM "sessions"').fetchone()[0]
    extra = " + ".join(
        t for t in ("session_usage", "automation_runs") if t in tables
    ) or "无关联表"
    return f"purge: 接口就绪, 会话 {total} 条, 关联 {extra}"
