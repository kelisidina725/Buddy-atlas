"""本地恢复 —— 把软删除的会话重新变成「活着」。

## 为什么需要它

客户端「删除会话」只是把 `sessions.deleted_at` 写上一个标记：正文 jsonl 还在，
索引行也还在，界面上不显示而已。在此之前，本工具唯一的恢复路径是
「找到导出过它的包 → 导入」—— 而大多数会话根本没导出过，于是用户面对
一条「已删除 · 可恢复」的记录却无路可走，只能去客户端里碰运气。

## 它只做一件事

把 `deleted_at` 置回 NULL，顺带把归属校正到本机当前账号（与导入的 RESTORE
分支同一口径）。**不动正文、不动时间戳、不动云端映射** —— 少改一处就少一种
把数据弄坏的方式。

## 三条铁律照旧

1. 客户端运行中一律拒写（WAL 下外部写会损坏整个库）；
2. 不直接改原库：`guard.DbEditSession` 在副本上改完再原子替换；
3. 整批只备份一次，且备份必须含 `-wal` / `-shm`（否则还原回来是旧库）。

顺序同样是正确性：先备份 → 再改库 → 最后校验。库写失败时数据仍是最新状态。

## 恢复之后能不能立刻看见

侧边栏快照是客户端自有文件，本工具不改（改错格式会让侧边栏整个渲染不出来）。
恢复的会话要等客户端重启后重新扫库才会回到侧边栏 —— 报告里必须写清楚，
否则用户会以为没生效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import dbcopy, guard, importer, paths


# ---------------------------------------------------------------- 数据结构


@dataclass
class RestoreTarget:
    """一条待恢复会话的实测状态。"""

    cid: str
    title: str = ""
    transport: str = ""
    deleted: bool = False
    has_body: bool = False
    # 数据库里已经不存在（视图过期）—— 只记录，不参与写入
    missing: bool = False

    @property
    def kind_text(self) -> str:
        if self.missing:
            return "已不存在"
        if not self.deleted:
            return "未删除"
        if (self.transport or "").lower() == "cloud":
            return "云端"
        return "本地正文" if self.has_body else "缺正文"


@dataclass
class RestorePlan:
    """恢复前算好的范围。plan_restore 只读，不写任何东西。"""

    targets: list[RestoreTarget] = field(default_factory=list)
    db_path: Path | None = None
    local_uid: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def candidates(self) -> list[RestoreTarget]:
        """真正要写库的那些：存在且确实处于已删除状态。"""
        return [t for t in self.targets if not t.missing and t.deleted]

    @property
    def cids(self) -> list[str]:
        return [t.cid for t in self.candidates]

    @property
    def empty(self) -> bool:
        return not self.candidates

    @property
    def bodyless_count(self) -> int:
        return sum(1 for t in self.candidates if not t.has_body)


@dataclass
class RestoreReport:
    applied: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup: guard.Backup | None = None
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "演练：将恢复" if self.dry_run else "已恢复"
        text = f"{head} {len(self.applied)} 条会话"
        if self.failed:
            text += f"，{len(self.failed)} 项失败"
        return text


# ---------------------------------------------------------------- 计划


def plan_restore(records, base: str | Path | None = None) -> RestorePlan:
    """只读探测每条会话的删除状态与正文情况。

    `records` 可以是 `inventory.SessionRecord`，也可以直接是 cid 字符串 ——
    UI 手里有完整记录，单测手里只有 id。
    """
    db_path = paths.workbuddy_db_path(base)
    plan = RestorePlan(db_path=db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到会话数据库：{db_path}")

    # 归属校正要看本机当前账号；判定不出来就只清标记，不改归属
    plan.local_uid = importer.detect_local_uid(base)

    with dbcopy.readonly_db_copy(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            raise RuntimeError("sessions 表不存在，客户端结构可能已变更")
        cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
        if "deleted_at" not in cols:
            # 老版本库没有软删除概念：没有可恢复的东西，别假装能恢复
            raise RuntimeError("这个会话库没有 deleted_at 列，无法判断删除状态")

        seen: set[str] = set()
        for item in records:
            cid = str(getattr(item, "cid", item) or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            row = conn.execute(
                'SELECT title, deleted_at, transport FROM "sessions" WHERE id = ?', (cid,)
            ).fetchone()
            if row is None:
                plan.targets.append(RestoreTarget(cid=cid, missing=True))
                continue
            has_body = getattr(item, "has_body", None)
            plan.targets.append(
                RestoreTarget(
                    cid=cid,
                    title=str(row["title"] or ""),
                    transport=str(row["transport"] or ""),
                    deleted=row["deleted_at"] is not None,
                    # 记录自带判定就信它；只有 cid 时退回查正文索引
                    has_body=bool(has_body) if has_body is not None else _has_body(cid, base),
                )
            )

    if plan.bodyless_count:
        plan.warnings.append(
            f"其中 {plan.bodyless_count} 条本地没有正文：恢复后只是一条空壳，"
            "内容要等客户端从云端拉回。"
        )
    return plan


def _has_body(cid: str, base: str | Path | None) -> bool:
    return paths.find_project_jsonl(cid, base) is not None


# ---------------------------------------------------------------- 执行


def _restore_rows(db_path: Path, targets: list[RestoreTarget], local_uid: str) -> tuple[list[str], list[tuple[str, str]]]:
    """在库副本上把 deleted_at 置空。返回 (成功 cids, 失败项)。"""
    applied: list[str] = []
    failed: list[tuple[str, str]] = []
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到会话数据库：{db_path}")

    with guard.DbEditSession(db_path) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
        if "deleted_at" not in cols:
            raise RuntimeError("这个会话库没有 deleted_at 列，无法恢复")

        for t in targets:
            sets = ["deleted_at = NULL"]
            params: list[object] = []
            # 与 importer 的 RESTORE 分支同口径：恢复即可见，归属统一到本机账号
            if local_uid and "user_id" in cols:
                sets.append("user_id = ?")
                params.append(local_uid)
            params.append(t.cid)
            try:
                cur = conn.execute(
                    f'UPDATE "sessions" SET {", ".join(sets)} WHERE id = ?', tuple(params)
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                failed.append((t.cid, f"{type(exc).__name__}: {exc}"))
                continue
            if cur.rowcount == 0:
                # 计划和执行之间被别的东西删了行；如实报出来，不硬造一条
                failed.append((t.cid, "库里找不到这条会话"))
                continue
            applied.append(t.cid)
    return applied, failed


def execute_restore(
    plan: RestorePlan,
    base: str | Path | None = None,
    dry_run: bool = False,
) -> RestoreReport:
    """执行恢复。会先断言客户端已退出。"""
    report = RestoreReport(dry_run=dry_run)
    targets = plan.candidates
    if not targets:
        return report

    report.warnings.extend(plan.warnings)

    if dry_run:
        report.applied = [t.cid for t in targets]
        return report

    # 铁律 1：客户端运行中一律拒写
    guard.assert_safe_to_write()

    db_path = plan.db_path or paths.workbuddy_db_path(base)

    # 铁律 3：整批只备份一次。只备份库 —— 恢复不碰任何正文文件
    report.backup = guard.create_backup([db_path], base=base, label="restore")

    try:
        applied, failed = _restore_rows(db_path, targets, plan.local_uid)
        report.applied = applied
        report.failed.extend(failed)
    except Exception as exc:  # noqa: BLE001 - 交给 UI 展示，不吞
        report.failed.append(("workbuddy.db", f"{type(exc).__name__}: {exc}"))
        return report

    # 写后校验：库不可读比恢复失败严重得多，必须当场报出来
    if not guard.verify_db_readable(db_path, "sessions"):
        report.failed.append(("workbuddy.db", "写后校验失败：库不可读或 sessions 表丢失"))

    # 侧边栏快照是客户端自有文件，刻意不动；不说清楚用户会以为恢复没生效
    report.warnings.append(
        "侧边栏快照未改动；重启一次 WorkBuddy，恢复的会话就会回到侧边栏。"
    )
    return report


def selftest_line(base: str | Path | None = None) -> str:
    """供 --selftest 用：只读探测库与 deleted_at 列，失败时抛异常由上层标 FAILED。"""
    db_path = paths.workbuddy_db_path(base)
    if not db_path.is_file():
        return "restore: 未找到数据库（跳过）"
    with dbcopy.readonly_db_copy(db_path) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")')}
        if "deleted_at" not in cols:
            return "restore: 库无 deleted_at 列（跳过）"
        dead = conn.execute(
            'SELECT COUNT(*) FROM "sessions" WHERE deleted_at IS NOT NULL'
        ).fetchone()[0]
    return f"restore: 可恢复（已删除）{dead} 条"
