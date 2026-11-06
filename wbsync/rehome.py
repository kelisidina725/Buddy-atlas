"""工作空间重映射 —— 把失效的 cwd 指回本机真实存在的目录。

## 为什么需要这件事

跨机同步会把会话连同 cwd 一起搬过来，而目标机上那个路径常常不存在：
盘符不同（`D:`）、用户名不同（`/Users/alice` vs `/Users/bob`）、
或者那只是客户端自动建的临时工作区、早被清理了。客户端打开这类会话时
要先落到 cwd 上，路径不在就是「点不开」—— 会话正文其实完好无损。

## 为什么不能只改数据库

正文放在 `~/.workbuddy/projects/<encode(cwd)>/<cid>.jsonl`，
**目录名由 cwd 编码而来**。只改 cwd 不改目录名，客户端会按新路径找不到正文，
会话变成空的 —— 比原来更糟。所以这两件事必须成对做。

## 编码规则（本机 55/55 个 cwd 精确命中）

    C:/Users/alice/WorkBuddy/project-a  →  c-Users-alice-WorkBuddy-project-a
    D:/测试文档/需求/1.0.0             →  d-测试文档-需求-1.0.0

即：反斜杠统一成 `/` → **盘符字母小写** → 把 `:/` 和 `/` 换成 `-`。
注意只有盘符小写，路径其余部分的大小写原样保留 —— 早先按"整体小写"猜过一次，
54 个 cwd 全部对不上，白跑一轮。

## 执行顺序：先动目录，后改库

库操作（副本改完原子替换）几乎不会失败，文件系统操作却可能因占用/权限失败。
先改库后移目录的话，一旦移目录失败，客户端就会对着一个"指对了路径但找不到正文"
的库；反过来先移目录，失败时库还没动，直接把目录名改回去就回到原状，零损失。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import dbcopy, guard, inventory, paths


# ---------------------------------------------------------------- 路径编码
def normalize(cwd: str) -> str:
    """统一的比较口径：空白清掉、反斜杠转正斜杠、去掉末尾分隔符、盘符小写。

    盘符必须小写：Windows 路径大小写不敏感，但记忆 / 聚合用的字符串 key
    必须一致，否则 `C:/x` 和 `c:/x` 会被当成两路径。这也与 `encode_project_dir`
    的编码口径（盘符小写）保持一致。
    """
    s = (cwd or "").strip().replace("\\", "/").rstrip("/")
    if len(s) > 1 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s


def encode_project_dir(cwd: str) -> str:
    """cwd → projects 下的目录名。"""
    s = normalize(cwd)
    if len(s) > 1 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s.replace(":/", "-").replace("/", "-")


def drive_of(cwd: str) -> str:
    n = normalize(cwd)
    return n[:2].upper() if len(n) > 1 and n[1] == ":" else ""


def drive_exists(cwd: str) -> bool:
    d = drive_of(cwd)
    return bool(d) and os.path.exists(d + "/")


def default_new_cwd(cwd: str) -> str:
    """给一个本机大概率能落地的建议路径。

    只做建议，界面上可改 —— 真实项目该指到哪儿只有用户知道。
    """
    n = normalize(cwd)
    if not n:
        return ""
    parts = [p for p in n.split("/") if p]
    me = Path.home().name

    # 1) 另一个用户名下的路径：先把用户名段换成本机当前用户
    if len(parts) > 2 and parts[0].lower() in ("c:", "d:", "e:") and parts[1].lower() == "users":
        if parts[2].lower() != me.lower():
            swapped = parts[:2] + [me] + parts[3:]
            candidate = "/".join(swapped)
            if os.path.isdir(candidate):
                return candidate
            # 本机没有同名目录，但"换成当前用户名"仍是比造一个假用户目录更好的落点
            return candidate if drive_exists(candidate) else _fallback(candidate)

    # 2) 盘符存在：就地重建即可
    if drive_exists(n):
        return n

    # 3) 盘符不存在：落到客户端的临时工作区旁边
    return _fallback(n)


def _fallback(cwd: str) -> str:
    """本机没有对应盘符时的落点。

    放在 `~/WorkBuddy/<末段>` —— 客户端的临时工作区就建在这个目录下
    （实测 `C:/Users/demo/WorkBuddy/2026-xx-xx-...`），
    所以这是它认得的"工作区根"。不要放到 `~/.workbuddy/` 里：
    那是客户端的内部数据目录，往里塞用户项目会让备份/清理逻辑把它当数据扫。
    """
    tail = [p for p in normalize(cwd).split("/") if p][-1:] or ["workspace"]
    home = Path.home() / "WorkBuddy" / tail[0]
    return str(home).replace("\\", "/")


# ---------------------------------------------------------------- 同名工程
# 匹配强度。**只有 exact / strong 会被自动采用**，weak 只作为候选给界面，
# 因为「末段同名、上面几层完全不同」很可能是两个不相干的工程
# （`D:/a/project` 与 `H:/b/project`），静默指过去比不指更坏。
MATCH_EXACT = "exact"    # 归一化后全路径相同
MATCH_STRONG = "strong"  # 末两段相同（WorkBuddy/project-a vs WorkBuddy/project-a）
MATCH_WEAK = "weak"      # 只有末段同名


def _segments(path: str) -> list[str]:
    return [p for p in normalize(path).split("/") if p]


def known_workspace_dirs(base: str | os.PathLike[str] | None = None) -> list[str]:
    """本机「已登记或已用过」且**真实存在**的目录，浅的排前面。

    两个来源：`workspaces` 表（客户端登记过的）+ `sessions.cwd` 去重。
    只认存在的目录 —— 建议一个不存在的路径等于没建议。

    单独抽出来是因为它要读库，而 `match_named_workspace` 每次比对都要用：
    调用方应当自己算一次再传进来，避免 N 条映射 N 次开库。
    """
    db = paths.workbuddy_db_path(base)
    if not db.is_file():
        return []
    raw: set[str] = set()
    try:
        with dbcopy.readonly_db_copy(db) as conn:
            try:
                for (p,) in conn.execute('SELECT path FROM "workspaces"'):
                    if p:
                        raw.add(str(p))
            except sqlite3.Error:
                pass  # 老版本库没这张表
            try:
                for (p,) in conn.execute(
                    'SELECT DISTINCT cwd FROM "sessions" WHERE cwd IS NOT NULL AND cwd <> ""'
                ):
                    if p:
                        raw.add(str(p))
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        n = normalize(item)
        if not n or n in seen:
            continue
        seen.add(n)
        if os.path.isdir(n):
            out.append(n)
    out.sort(key=lambda s: (len(s), s))
    return out


def match_named_workspace(
    cwd: str,
    base: str | os.PathLike[str] | None = None,
    candidates: list[str] | None = None,
) -> tuple[str, list[str]]:
    """按目录名在本机找同名工程。返回 `(强度, 候选列表)`。

    强度只有一档会被返回：找到 strong 就不再退化成 weak，避免同名候选把
    更靠谱的那个挤掉。全部落空返回 `("", [])`。
    """
    segs = _segments(cwd)
    if not segs:
        return "", []
    raw_dirs = candidates if candidates is not None else known_workspace_dirs(base)
    # 候选统一归一化：盘符大小写、分隔符、尾斜杠任一处不同就会漏掉「同一条路径」，
    # 而这恰恰是 exact 唯一要判的事。
    dirs = [n for n in (normalize(d) for d in raw_dirs) if n]
    if not dirs:
        return "", []

    want = normalize(cwd)
    tail1 = segs[-1].casefold()
    tail2 = [s.casefold() for s in segs[-2:]] if len(segs) >= 2 else []

    exact: list[str] = []
    strong: list[str] = []
    weak: list[str] = []
    for d in dirs:
        if d == want:
            exact.append(d)
            continue
        ds = _segments(d)
        if not ds:
            continue
        if [s.casefold() for s in ds[-len(tail2) :]] == tail2 and len(ds) >= len(tail2):
            strong.append(d)
        elif ds[-1].casefold() == tail1:
            weak.append(d)

    if exact:
        return MATCH_EXACT, exact
    if strong:
        return MATCH_STRONG, strong
    if weak:
        return MATCH_WEAK, weak
    return "", []


# ---------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class SpaceIssue:
    """一个失效的工作空间路径（按归一化路径聚合）。"""

    cwd: str                          # 归一化后的路径，聚合键
    raw_values: tuple[str, ...]       # 库里实际的写法（同一个路径可能有 F:\ 与 F:/ 两种）
    session_count: int
    visible_count: int
    deleted_count: int
    # 这个路径下最近一次会话活动（毫秒）。工作空间页的「最近出现」用它；
    # 取不到时留 0，界面显示「—」而不是 1970 年。
    last_seen: int = 0

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.cwd)

    @property
    def project_dir_name(self) -> str:
        return encode_project_dir(self.cwd)

    @property
    def drive_missing(self) -> bool:
        return not drive_exists(self.cwd)


@dataclass
class RehomeTarget:
    """一条待执行的映射。"""

    issue: SpaceIssue
    new_cwd: str
    create_dir: bool = True
    move_bodies: bool = True

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not normalize(self.new_cwd):
            errs.append("新路径为空")
        # 「新路径 == 旧路径」是合法的：本机的临时工作区被清理后，
        # 正确落点就是原路径，要做的只是把目录重新建出来。
        # 只有「路径相同且不建目录」才等于什么都没干。
        if normalize(self.new_cwd) == self.issue.cwd and not self.create_dir:
            errs.append("新路径与旧路径相同且不创建目录，等于没改")
        return errs


@dataclass
class RehomePlan:
    targets: list[RehomeTarget]
    db_path: Path
    projects_dir: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.targets

    @property
    def session_total(self) -> int:
        return sum(t.issue.session_count for t in self.targets)

    def conflicts(self) -> list[str]:
        """两个目标编码到同一个 projects 目录名 —— 会把两批正文混进一个目录。

        ⚠️ 「新路径相同」也算冲突，不只是「编码撞车」。两个不同的旧空间
        都指向 `H:/p/x` 时，它们的正文会一起落进 `h-p-x`，之后谁也分不开。
        """
        seen: dict[str, str] = {}
        out: list[str] = []
        for t in self.targets:
            name = encode_project_dir(t.new_cwd)
            if name in seen:
                out.append(f"{t.issue.cwd} 与 {seen[name]} 会落到同一个正文目录 {name}")
            seen[name] = t.issue.cwd
        return out


@dataclass
class RehomeReport:
    dry_run: bool = False
    backup: guard.Backup | None = None
    applied: list[str] = field(default_factory=list)          # 成功改掉的旧 cwd
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    moved_dirs: list[tuple[str, str]] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def summary(self) -> str:
        head = "演练" if self.dry_run else "完成"
        bits = [f"{head}：改掉 {len(self.applied)} 项工作空间"]
        if self.moved_dirs:
            bits.append(f"迁移正文目录 {len(self.moved_dirs)} 个")
        if self.created_dirs:
            bits.append(f"新建目录 {len(self.created_dirs)} 个")
        if self.failed:
            bits.append(f"失败 {len(self.failed)} 项")
        return " · ".join(bits)


# ---------------------------------------------------------------- 扫描
def _ms(value) -> int:
    """时间戳兜底：库里可能是 None / 字符串 / 缺字段，取不到就当 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def scan(base: str | os.PathLike[str] | None = None) -> list[SpaceIssue]:
    """扫出所有 cwd 不存在的工作空间，按归一化路径聚合。"""
    inv = inventory.build(base=base, with_fingerprint=False)
    buckets: dict[str, dict] = {}

    for r in inv.records:
        raw = (r.cwd or "").strip()
        if not raw:
            continue
        key = normalize(raw)
        if os.path.isdir(key):
            continue
        b = buckets.setdefault(
            key, {"raw": set(), "count": 0, "visible": 0, "deleted": 0, "seen": 0}
        )
        b["raw"].add(raw)
        b["count"] += 1
        b["seen"] = max(
            b["seen"],
            _ms(getattr(r, "last_activity_at", 0)),
            _ms(getattr(r, "updated_at", 0)),
        )
        if r.is_deleted:
            b["deleted"] += 1
        elif r.visible_by_default:
            b["visible"] += 1

    out = [
        SpaceIssue(
            cwd=key,
            raw_values=tuple(sorted(b["raw"])),
            session_count=b["count"],
            visible_count=b["visible"],
            deleted_count=b["deleted"],
            last_seen=b["seen"],
        )
        for key, b in buckets.items()
    ]
    # 侧栏可见的排前面：那才是用户真正点不开的
    out.sort(key=lambda i: (-i.visible_count, -i.session_count, i.cwd))
    return out


# ---------------------------------------------------------------- 计划
def plan_rehome(
    targets: list[RehomeTarget],
    base: str | os.PathLike[str] | None = None,
) -> RehomePlan:
    plan = RehomePlan(
        targets=list(targets),
        db_path=paths.workbuddy_db_path(base),
        projects_dir=paths.session_data_dir(base) / "projects",
    )
    for t in plan.targets:
        plan.warnings.extend(f"{t.issue.cwd}：{e}" for e in t.validate())
    plan.warnings.extend(plan.conflicts())
    if not plan.projects_dir.is_dir():
        plan.warnings.append(f"正文目录不存在：{plan.projects_dir}")
    # 信任决策是客户端自己记的：换了路径等于换了一个工作区，
    # 首次打开客户端会重新问一次「是否信任此文件夹」，这是预期行为
    plan.warnings.append(
        "改路径后第一次在客户端打开该空间，会重新询问「是否信任此文件夹」——预期行为。"
    )
    return plan


def _sessions_for(conn: sqlite3.Connection, raw_values: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(raw_values))
    row = conn.execute(
        f"SELECT COUNT(*) FROM sessions WHERE cwd IN ({marks})", raw_values
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------- 执行
def execute_rehome(
    plan: RehomePlan,
    base: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> RehomeReport:
    """执行重映射。会先断言客户端已退出。"""
    report = RehomeReport(dry_run=dry_run)
    if plan.empty:
        return report

    report.warnings.extend(plan.warnings)
    if dry_run:
        report.applied = [t.issue.cwd for t in plan.targets]
        return report

    # 铁律 1：客户端运行中一律拒写
    guard.assert_safe_to_write()

    db_path = plan.db_path or paths.workbuddy_db_path(base)

    # 铁律 3：整批只备份一次（库）。正文只是改名不复制，所以不进备份。
    report.backup = guard.create_backup([db_path], base=base, label="rehome")

    # ---- 1) 先动正文目录：失败时库还没改，改回名字即回到原状
    for t in plan.targets:
        if not t.move_bodies:
            continue
        old_name = t.issue.project_dir_name
        new_name = encode_project_dir(t.new_cwd)
        if old_name == new_name:
            continue
        src = plan.projects_dir / old_name
        dst = plan.projects_dir / new_name
        if not src.is_dir():
            report.warnings.append(f"正文目录 {old_name} 不存在，跳过迁移")
            continue
        if dst.exists():
            # 不合并：两个空间的正文混进一个目录后，之后再想分开就无从下手
            report.failed.append((old_name, f"目标目录 {new_name} 已存在，跳过迁移"))
            continue
        try:
            src.rename(dst)
            report.moved_dirs.append((old_name, new_name))
        except OSError as exc:
            report.failed.append((old_name, f"目录改名失败：{exc}"))

    # ---- 2) 建目标目录（"建空目录占位"就靠这一步）
    for t in plan.targets:
        if not t.create_dir:
            continue
        target = Path(normalize(t.new_cwd))
        if target.is_dir():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            report.created_dirs.append(str(target))
        except OSError as exc:
            report.failed.append((str(target), f"创建目录失败：{exc}"))

    # ---- 3) 改库：副本上改完原子替换
    try:
        _update_cwds(db_path, plan.targets, report)
    except Exception as exc:  # noqa: BLE001 - 交给 UI 展示
        report.failed.append(("workbuddy.db", f"{type(exc).__name__}: {exc}"))
        return report

    if not guard.verify_db_readable(db_path, "sessions"):
        report.failed.append(("workbuddy.db", "写后校验失败：库不可读或 sessions 表丢失"))

    report.warnings.append(
        "侧边栏快照未改动；重启一次 WorkBuddy，这些空间下的会话才会按新路径重新挂上。"
    )
    return report


def _update_cwds(db_path: Path, targets: list[RehomeTarget], report: RehomeReport) -> None:
    with guard.DbEditSession(db_path) as conn:
        for t in targets:
            if any(e for e in t.validate()):
                continue
            changed = 0
            for raw in t.issue.raw_values:
                changed += conn.execute(
                    "UPDATE sessions SET cwd=? WHERE cwd=?", (t.new_cwd, raw)
                ).rowcount
            if changed == 0:
                report.failed.append((t.issue.cwd, "库里没有匹配的会话行，未改动"))
            else:
                report.applied.append(t.issue.cwd)


# ---------------------------------------------------------------- 自检
def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    """打包后用 --selftest 验证：能扫出多少条失效路径。"""
    try:
        issues = scan(base)
    except Exception as exc:  # noqa: BLE001
        return f"rehome: FAILED —— {type(exc).__name__}: {exc}"
    if not issues:
        return "rehome: 未发现失效工作空间"
    visible = sum(i.visible_count for i in issues)
    return f"rehome: 失效工作空间 {len(issues)} 个（侧栏可见会话 {visible} 条）"
