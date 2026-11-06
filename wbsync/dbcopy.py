"""安全地读取 WorkBuddy 的 SQLite 数据库。

## 为什么不直接连原库

实测：SQLite 打开 WAL 模式的库需要**写** `-wal` / `-shm` 边车文件；客户端运行时持有锁，
连**只读挂载**都会被拒绝。直接连不仅会失败，还有并发写损坏的风险。

## 做法

把 db 本体连同 `-wal` / `-shm` 一起复制到临时目录，再打开副本。
复制件带 WAL 时，SQLite 打开副本会自动 checkpoint，因此能读到最新数据。

原库全程只读，一个字节都不写。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# 对照参考项目 open_db 的 busy_timeout(5s) / Python sqlite3.connect(timeout=5)
BUSY_TIMEOUT_SECONDS = 5.0

# 需要一起复制的 SQLite 边车文件
_SIDECARS = ("-wal", "-shm")


def copy_db_to(src: Path, dest_dir: Path) -> Path:
    """复制 db 本体与存在的边车文件到 dest_dir，返回副本路径。"""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"数据库不存在：{src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / src.name
    shutil.copy2(src, dst)
    for suffix in _SIDECARS:
        side = src.with_name(src.name + suffix)
        if side.is_file():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return dst


@contextmanager
def readonly_db_copy(
    db_path: str | os.PathLike[str],
    workdir: str | os.PathLike[str] | None = None,
    timeout: float = BUSY_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """在临时副本上打开只读连接；退出时自动清理临时目录。

    用法::

        with readonly_db_copy(paths.workbuddy_db_path()) as conn:
            rows = conn.execute("select id, title from sessions").fetchall()
    """
    tmp = tempfile.mkdtemp(prefix="wbsync-db-", dir=str(workdir) if workdir else None)
    try:
        dst = copy_db_to(Path(db_path), Path(tmp))
        conn = sqlite3.connect(str(dst), timeout=timeout)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """列是否存在。用于防御性读取 —— sessions 表已历经十几次 migration，列会变。"""
    if not table_exists(conn, table):
        return False
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    return column in cols
