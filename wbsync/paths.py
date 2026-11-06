"""WorkBuddy 数据路径解析。

三件套模型（缺一不可）：
  1) 正文   ~/.workbuddy/projects/{workspace}/{cid}.jsonl
  2) 元数据 ~/.workbuddy/workbuddy.db                 -> sessions 表
  3) 云端   ~/.workbuddy/edge-sync-mapping-v{N}.db    -> edge_sync_mapping 表

★ 档位号 N 会随客户端升级往上搬（实测 v2 -> v3 -> v4），必须动态发现最大的 N。
  写死版本号的后果不是报错，而是**静默失败**：新建一个空库、找不到表、一路返回 False。

★ 所有函数都接受可选的 base 参数，单测可注入临时目录，不碰真实 home。
  无参全局路径 + 并行单测 = 假失败，这个坑参考项目踩过四次。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# CN 版客户端数据目录名（国际版为 .workbuddy-ai，本项目只处理 CN）
DATA_DIR_NAME = ".workbuddy"

EDGE_SYNC_DB_PREFIX = "edge-sync-mapping-v"
EDGE_SYNC_DB_SUFFIX = ".db"
# 一个档位库都不存在时回落到它（老客户端形态）
EDGE_SYNC_DB_LEGACY = "edge-sync-mapping-v2.db"

_EDGE_DB_RE = re.compile(r"^edge-sync-mapping-v(\d+)\.db$")


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def session_data_dir(base: str | os.PathLike[str] | None = None) -> Path:
    """客户端会话数据目录（CN: ~/.workbuddy）。"""
    root = Path(base) if base is not None else home_dir()
    return root / DATA_DIR_NAME


def workbuddy_db_path(base: str | os.PathLike[str] | None = None) -> Path:
    """会话元数据数据库路径。"""
    return session_data_dir(base) / "workbuddy.db"


def projects_dir(base: str | os.PathLike[str] | None = None) -> Path:
    """会话正文根目录（按工作空间分子目录）。"""
    return session_data_dir(base) / "projects"


def storage_dir(base: str | os.PathLike[str] | None = None) -> Path:
    """客户端的键值存储根目录（`~/.workbuddy/storage`）。

    ★ 这个目录此前被整体漏掉过，代价是「账号昵称」一直取不到：
      昵称的唯一明文副本在 `storage/skeleton/account-snapshot.json`，
      而它既不在 db 里、也不在 app-config.json 里。
    """
    return session_data_dir(base) / "storage"


def account_snapshot_path(base: str | os.PathLike[str] | None = None) -> Path:
    """首屏账号骨架快照（明文，仅存当前登录账号）。"""
    return storage_dir(base) / "skeleton" / "account-snapshot.json"


def latest_edge_sync_db_version(dirpath: str | os.PathLike[str]) -> int | None:
    """扫目录取 `edge-sync-mapping-v{N}.db` 里**最大**的 N；一个都没有返回 None。

    只认 .db 本体：`-wal` / `-shm` 是 SQLite 边车文件，不参与选档
    （`edge-sync-mapping-v3.db-wal` 正则天然不匹配，自动排除）。

    ⚠️ 刻意不取「最低档位优先」：那会在 v3/v4 并存的机器上选中客户端已不再读的 v3，
       把「静默失效」换个地方重演。
    """
    d = Path(dirpath)
    if not d.is_dir():
        return None
    best: int | None = None
    for entry in d.iterdir():
        m = _EDGE_DB_RE.match(entry.name)
        if not m:
            continue
        v = int(m.group(1))
        if best is None or v > best:
            best = v
    return best


def edge_sync_db_path(base: str | os.PathLike[str] | None = None) -> Path:
    """云端映射库路径（档位号动态发现）。"""
    d = session_data_dir(base)
    v = latest_edge_sync_db_version(d)
    name = f"{EDGE_SYNC_DB_PREFIX}{v}{EDGE_SYNC_DB_SUFFIX}" if v is not None else EDGE_SYNC_DB_LEGACY
    return d / name


def escape_workspace_path(cwd: str) -> str:
    """工作空间绝对路径 -> projects 下的目录名。

    实测样例：
        C:/Users/demo/WorkBuddy/2026-10-29-00-54-11
            -> c-Users-demo-WorkBuddy-2026-10-29-00-54-11
        F:/DEMO_TEST -> f-DEMO_TEST

    规则：统一斜杠 -> 去掉盘符后的冒号 -> 盘符转小写 -> '/' 转 '-'，其余字符原样保留
    （注意只转首字母，路径其余部分的大小写保持不变）。
    """
    s = cwd.replace("\\", "/").rstrip("/")
    if len(s) > 1 and s[1] == ":":
        s = s[0] + s[2:]
    if s:
        s = s[0].lower() + s[1:]
    return s.replace("/", "-")


def build_projects_index(base: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    """建立 `cid -> jsonl 路径` 索引（一次遍历，全程复用）。

    同名 cid 若出现在多个目录下，保留首个（setdefault），不静默覆盖。
    """
    root = projects_dir(base)
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                cid = fn[: -len(".jsonl")]
                index.setdefault(cid, Path(dirpath) / fn)
    return index


def find_project_jsonl(cid: str, base: str | os.PathLike[str] | None = None) -> Path | None:
    """按 cid 找会话正文；找不到返回 None（fail-closed，宁可不做也不猜路径）。"""
    return build_projects_index(base).get(cid)
