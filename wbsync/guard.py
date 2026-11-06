"""写入安全守卫：进程检测、备份、副本改写、写后校验。

## 三条铁律

1. **客户端运行中一律拒写**。SQLite WAL 模式下，客户端持有锁，外部写入要么失败、
   要么与客户端并发写同一个库 —— 后者会损坏**全部**会话记录。没有中间地带。

2. **不在原库上改，在副本上改完再替换**。
   直接连原库写入，中途异常会留下半改状态；副本方案下，任何失败都只丢临时目录，
   原库一个字节没动。替换是最后一步、且是原子的 `os.replace`。

3. **整批操作只备份一次**。
   备份是流程里最贵的一步（会话正文动辄十几 MB），按会话逐个备份会把一次导入
   拖成几十次全量拷贝。所以备份在批次开始时集中做一次，只备份**本批会动到**的文件。

## 为什么替换前要删 -wal / -shm

副本打开时 SQLite 已自动 checkpoint，副本里包含了原 wal 的全部数据。
此时原库的 -wal 是**已合并的旧数据**，留着反而可能让客户端读到重复或过期内容。
所以替换 .db 之后必须清掉伴生文件。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, winproc

# 备份目录：放在数据目录内，随用户数据一起，不散落到系统临时目录
BACKUP_DIR_NAME = "wbsync-backup"

# 一次备份保留的天数；超出不自动删（删数据的事交给用户，不做隐式清理）
BACKUP_RETENTION_DAYS = 30

BUSY_TIMEOUT = 5.0


# ---------------------------------------------------------------- 进程检测


def running_client_processes() -> list[str]:
    """返回正在运行的客户端进程名（去重保序）。

    ⚠️ 用 ctypes 枚举而不是 `tasklist`：本工具是 console=False 的窗口程序，
       启动控制台子进程会让 Windows 弹一个黑窗出来（用户看到的就是"闪一下"）。
       winproc 全程零子进程，从根上避免这个问题，顺带快十倍。
    """
    names: list[str] = []
    for p in winproc.find_by_stem():
        if p.name not in names:
            names.append(p.name)
    return names


def is_client_running() -> bool:
    return bool(running_client_processes())


class ClientRunningError(RuntimeError):
    """客户端未退出。这是**可恢复**错误 —— 关掉客户端再试一次即可。"""

    def __init__(self, procs: list[str]):
        self.processes = procs
        joined = "、".join(procs)
        super().__init__(
            f"检测到 WorkBuddy 正在运行（{joined}），已拒绝写入。\n"
            "客户端持有数据库锁，写入会损坏全部会话记录。\n"
            "请完全退出 WorkBuddy 后重试 —— 本工具是独立进程，退出客户端不会关闭它。"
        )


def assert_safe_to_write() -> None:
    """写入前的强制检查。不通过直接抛 ClientRunningError。"""
    procs = running_client_processes()
    if procs:
        raise ClientRunningError(procs)


# ------------------------------------------------------------------- 备份


@dataclass
class Backup:
    """一次备份的结果。restore() 可整体回滚。"""

    root: Path
    files: list[tuple[Path, Path]] = field(default_factory=list)  # (原件, 备份件)
    created_at: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.files

    def restore(self) -> int:
        """把备份件拷回原件位置。返回成功还原的文件数。"""
        ok = 0
        for src, dst in self.files:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                ok += 1
            except OSError:
                continue
        return ok

    def size_bytes(self) -> int:
        total = 0
        for src, _dst in self.files:
            try:
                total += src.stat().st_size
            except OSError:
                continue
        return total


def backup_dir(base: str | os.PathLike[str] | None = None) -> Path:
    root = paths.session_data_dir(base)
    return root / BACKUP_DIR_NAME


def _sidecars(p: Path) -> list[Path]:
    out = []
    for suffix in ("-wal", "-shm"):
        side = p.with_name(p.name + suffix)
        if side.is_file():
            out.append(side)
    return out


def create_backup(
    files: list[Path],
    base: str | os.PathLike[str] | None = None,
    label: str = "batch",
) -> Backup:
    """备份一批文件（含 SQLite 伴生文件），整批一次。

    ⚠️ 备份**必须**包含 -wal / -shm：只拷 .db 本体的话，
       还原回来的是一个缺了最近写入的旧库 —— 备份形同虚设。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = backup_dir(base) / f"{stamp}-{label}"
    root.mkdir(parents=True, exist_ok=True)

    bak = Backup(root=root, created_at=time.time())
    for f in files:
        f = Path(f)
        if not f.is_file():
            continue
        try:
            dst = root / f.name
            shutil.copy2(f, dst)
            bak.files.append((dst, f))
        except OSError:
            continue
        for side in _sidecars(f):
            try:
                dside = root / side.name
                shutil.copy2(side, dside)
                bak.files.append((dside, side))
            except OSError:
                continue
    return bak


def create_merge_backup(
    files: list[Path],
    base: str | os.PathLike[str] | None = None,
) -> Backup:
    """分叉取舍覆盖掉的那几份正文，单独备份一份（label=merge）。

    ★ 为什么不并进整批「导入备份」：分叉取舍常常只是顺手确认的一两条，
      用户点「回滚本次」想要的往往只是「把刚才被我丢掉的那半段还回来」。
      混进整批备份里回退，会把同批新增的会话一并撤掉 —— 那不是他要的。
      所以这一份单独成一个目录，回滚粒度 = 本次被覆盖的正文。
    """
    return create_backup(files, base=base, label="merge")


def list_backups(base: str | os.PathLike[str] | None = None) -> list[Path]:
    d = backup_dir(base)
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_dir()), reverse=True)


# ------------------------------------------------------------- 备份盘点/清理

# 备份目录名形如 `20261031-002501-batch`：时间戳 + 标签
_BACKUP_NAME_RE = re.compile(r"^(\d{8}-\d{6})-(.+)$")

# 标签 -> 人话。新增备份来源时在这里补一行，UI 不用改。
_LABEL_TEXT = {
    "batch": "会话备份",
    "config": "配置备份",
    "import": "导入备份",
    "merge": "分叉取舍备份",
    "purge": "物理删除备份",
    "restore": "恢复备份",
    "rehome": "路径修复备份",
    # 配置同步走自己的出口，标签名由 config_sync 里传入，这里补一条人话映射
    "config-sync": "配置备份",
}


@dataclass(frozen=True)
class BackupEntry:
    """一份备份目录的盘点结果。"""

    path: Path
    stamp: str
    label: str
    created_at: float
    size_bytes: int
    file_count: int

    @property
    def kind_text(self) -> str:
        return _LABEL_TEXT.get(self.label, self.label or "未知")

    @property
    def when_text(self) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        except (OSError, ValueError, OverflowError):
            return self.stamp or "未知"


def _dir_stats(p: Path) -> tuple[int, int]:
    """(总字节数, 文件数)。不跟随符号链接，坏链接按 0 计。"""
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(p):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def scan_backups(base: str | os.PathLike[str] | None = None) -> list[BackupEntry]:
    """盘点全部备份，按时间倒序。目录名不合规的也列出，标签取原名。"""
    root = backup_dir(base)
    if not root.is_dir():
        return []

    out: list[BackupEntry] = []
    for p in root.iterdir():
        if not p.is_dir() or p.is_symlink():
            continue
        m = _BACKUP_NAME_RE.match(p.name)
        stamp = m.group(1) if m else ""
        label = m.group(2) if m else p.name
        try:
            ctime = p.stat().st_mtime
        except OSError:
            ctime = 0.0
        size, count = _dir_stats(p)
        out.append(
            BackupEntry(
                path=p,
                stamp=stamp,
                label=label,
                created_at=ctime,
                size_bytes=size,
                file_count=count,
            )
        )
    out.sort(key=lambda e: (e.created_at, e.path.name), reverse=True)
    return out


def _is_backup_child(target: Path, root: Path) -> bool:
    """删除的唯一边界：必须是备份根目录的**直接子目录**。

    三道防线，缺一不可：
      1. 不是符号链接 —— 否则 rmtree 会顺着链接删到别处
      2. 解析后父目录等于 root（resolve 消除 `..`、大小写、8.3 短名等差异）
      3. 目标不等于 root 本身 —— 否则一次误调用清空整个备份区
    """
    try:
        if target.is_symlink():
            return False
        if not target.is_dir():
            return False
        root_r = root.resolve(strict=True)
        target_r = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if target_r == root_r:
        return False
    return target_r.parent == root_r


def delete_backups(
    targets: list[Path],
    base: str | os.PathLike[str] | None = None,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """删除指定的备份目录，返回 (已删, 失败原因)。

    ★ 拒绝删除备份根目录本身、拒绝越界路径、拒绝符号链接。
      这些不是"防御性冗余"，而是删数据功能的必要边界：
      路径一旦来自 UI 勾选，就不能假设它一定是干净的。
    """
    root = backup_dir(base)
    deleted: list[Path] = []
    failed: list[tuple[Path, str]] = []

    if not root.is_dir():
        return deleted, [(root, "备份目录不存在")]

    for t in targets:
        t = Path(t)
        if not _is_backup_child(t, root):
            failed.append((t, "不是备份目录的直接子目录，已拒绝"))
            continue
        try:
            shutil.rmtree(t)
            deleted.append(t)
        except OSError as e:
            failed.append((t, f"删除失败：{e.strerror or e}"))
    return deleted, failed


def backup_total_bytes(entries: list[BackupEntry]) -> int:
    return sum(e.size_bytes for e in entries)


# --------------------------------------------------- 副本改写 + 替换原库


@dataclass
class DbEditSession:
    """在**临时副本**上修改数据库，成功后替换原库。

    用法::

        with db_edit_session(paths.workbuddy_db_path()) as conn:
            conn.execute("INSERT ...", params)
        # 退出 with 即完成校验 + 替换

    任何异常都会让原库保持原样。
    """

    db_path: Path
    workdir: Path | None = None
    conn: sqlite3.Connection | None = None
    tmpdir: Path | None = None
    copy_path: Path | None = None

    def __enter__(self) -> sqlite3.Connection:
        import tempfile

        self.db_path = Path(self.db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(f"数据库不存在：{self.db_path}")

        self.tmpdir = Path(tempfile.mkdtemp(prefix="wbsync-edit-"))
        dst = self.tmpdir / self.db_path.name
        shutil.copy2(self.db_path, dst)
        # wal/shm 一起带上：副本打开时会自动 checkpoint，从而包含最新数据
        for side in _sidecars(self.db_path):
            shutil.copy2(side, dst.with_name(dst.name + side.name[len(self.db_path.name):]))
        self.copy_path = dst

        conn = sqlite3.connect(str(dst), timeout=BUSY_TIMEOUT)
        conn.row_factory = sqlite3.Row
        self.conn = conn
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        conn, tmp = self.conn, self.tmpdir
        self.conn = None
        try:
            if conn is not None:
                if exc_type is None:
                    conn.commit()
                else:
                    conn.rollback()
                conn.close()

            if exc_type is None and self.copy_path is not None:
                self._replace_original()
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def _replace_original(self) -> None:
        """用改好的副本替换原库，并清掉已合并的伴生文件。"""
        assert self.copy_path is not None
        src = self.copy_path
        dst = self.db_path

        # 先清伴生文件：副本已 checkpoint，原 wal 属于过期数据
        for side in _sidecars(dst):
            try:
                side.unlink()
            except OSError:
                pass

        # 同分区 os.replace 是原子的；跨分区回退到 copy2
        try:
            os.replace(src, dst)
        except OSError:
            shutil.copy2(src, dst)

        for suffix in ("-wal", "-shm"):
            stale = dst.with_name(dst.name + suffix)
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass


def verify_db_readable(db_path: Path, expect_table: str | None = None) -> bool:
    """写后校验：库能被打开、目标表还在。防止把库写坏还报成功。"""
    db_path = Path(db_path)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return False
    # ⚠️ 不能写成 `with sqlite3.connect(...) as conn`：
    #    Connection 的上下文管理器只管事务（commit/rollback），**不关闭连接**，
    #    会留下 unclosed database 的 ResourceWarning 和悬空文件句柄。
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
        conn.execute("PRAGMA integrity_check").fetchone()
        if expect_table:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (expect_table,),
            ).fetchone()
            if row is None:
                return False
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()
    return True
