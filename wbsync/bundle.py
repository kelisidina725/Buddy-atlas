"""会话导出包（.wbsync.zip）—— 跨设备搬运会话的载体。

## 包结构

    manifest.json          版本 + 源机器信息 + 每条会话的元数据/指纹/云端通道
    bodies/<cid>.jsonl     会话正文（保持原文，逐行 JSON）

## 为什么正文放进 zip 而不是裸目录

68 MB 的 JSONL 是可压缩文本的典型：deflate 后通常只剩 1/5 ~ 1/8。
裸目录在云盘里同步慢、还容易被同步客户端拆出冲突副本；单文件 zip 则是一次原子传输。

## 为什么 manifest 里存指纹而不是只存行数

行数是不可靠判据：两端各自删改过内容时行数可能碰巧相等。
指纹（前 512 行逐行 FNV-1a）才是「谁落后 / 谁分叉」的内容级依据。

## 为什么连 sessions 表整行一起打包

导入端要能在侧边栏看到这条会话，就必须重建 sessions 行。
只带正文的导入是**不完整的** —— 那样会话存在但界面里找不到，等于没导。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from . import fingerprint
from .fingerprint import SessionFingerprint, fingerprint_of_file

FORMAT_NAME = "wbsync-bundle"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
BODIES_DIR = "bodies"

# zip 压缩级别：9 对 68MB 文本意义不大却慢很多，6 是实测的甜点
_COMPRESS_LEVEL = 6


# ------------------------------------------------------------ 数据结构


@dataclass
class BundleSession:
    """包里的一条会话。"""

    cid: str
    row: dict[str, object] = field(default_factory=dict)
    cloud_mapped: bool = False
    cloud_channel: str = ""
    cloud_created_at: int = 0
    body_member: str | None = None  # zip 内路径，None 表示无正文
    body_size: int = 0
    fingerprint: SessionFingerprint | None = None

    @property
    def title(self) -> str:
        for key in ("custom_title", "title"):
            v = self.row.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return "(无标题)"

    @property
    def has_body(self) -> bool:
        return self.body_member is not None

    @property
    def cwd(self) -> str:
        return str(self.row.get("cwd") or "")


@dataclass
class Bundle:
    """一个已打开的包。"""

    path: Path
    manifest: dict[str, object] = field(default_factory=dict)
    sessions: list[BundleSession] = field(default_factory=list)
    # 从同步包 v2 里读取时，本节的成员都在 `<prefix>` 下；独立包为空串
    prefix: str = ""

    @property
    def format_version(self) -> int:
        return int(self.manifest.get("version") or 0)

    @property
    def source_host(self) -> str:
        src = self.manifest.get("source")
        return str(src.get("host", "")) if isinstance(src, dict) else ""

    @property
    def total(self) -> int:
        return len(self.sessions)

    def body_bytes(self, s: BundleSession) -> bytes:
        if not s.body_member:
            return b""
        with zipfile.ZipFile(self.path) as z:
            with z.open(self.prefix + s.body_member) as f:
                return f.read()

    def iter_body_lines(self, s: BundleSession):
        """逐行迭代正文（二进制行，保留原始字节，含换行符）。

        用二进制而非文本：正文里可能有客户端写下的非 UTF-8 字节，
        解码再编码会造成**静默改动**，破坏指纹一致性。
        """
        if not s.body_member:
            return
        with zipfile.ZipFile(self.path) as z:
            with z.open(self.prefix + s.body_member) as f:
                for raw in f:
                    yield raw


# 指纹的落盘形态由 fingerprint 模块统一定义：包格式与本地缓存共用一份结构，
# 否则两处各自演进，迟早在「写出去再读回来」时错位。
_fp_to_json = fingerprint.to_json
_fp_from_json = fingerprint.from_json


# ------------------------------------------------------------ 导出


def _session_row(conn, cid: str) -> dict[str, object]:
    """取 sessions 表整行（含全部列，方便导入端原样重建）。"""
    conn.row_factory = _row_factory
    row = conn.execute('SELECT * FROM "sessions" WHERE id = ?', (cid,)).fetchone()
    return dict(row) if row is not None else {}


def _row_factory(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def source_info(base: str | os.PathLike[str] | None = None) -> dict[str, object]:
    return {
        "host": socket.gethostname(),
        "os": platform.platform(),
        "data_dir": str(paths.session_data_dir(base)),
        "edge_db": paths.edge_sync_db_path(base).name,
    }


def collect_sessions(
    records,
    base: str | os.PathLike[str] | None = None,
    with_fingerprint: bool = True,
    progress=None,
) -> list[BundleSession]:
    """把一批会话整理成包内条目：整行元数据 + 云端映射 + 指纹。

    ★ 不负责写入 zip —— 独立包与同步包 v2 都复用这一步，
      两者的差别只在成员路径前缀，由写入方决定。
    """
    from . import dbcopy

    db_path = paths.workbuddy_db_path(base)
    edge_path = paths.edge_sync_db_path(base)

    # 云端映射与整行元数据各读一次，避免逐会话重复开库
    rows: dict[str, dict[str, object]] = {}
    if db_path.is_file():
        with dbcopy.readonly_db_copy(db_path) as conn:
            conn.row_factory = _row_factory
            for row in conn.execute('SELECT * FROM "sessions"'):
                d = dict(row)
                cid = str(d.get("id") or "")
                if cid:
                    rows[cid] = d

    cloud: dict[str, tuple[str, int]] = {}
    if edge_path.is_file():
        with dbcopy.readonly_db_copy(edge_path) as conn:
            conn.row_factory = _row_factory
            if dbcopy.table_exists(conn, "edge_sync_mapping"):
                for row in conn.execute("SELECT * FROM edge_sync_mapping"):
                    d = dict(row)
                    cloud[str(d.get("session_id") or "")] = (
                        str(d.get("msg_channel") or ""),
                        int(d.get("created_at") or 0),
                    )

    sessions: list[BundleSession] = []
    total = len(records)
    for i, rec in enumerate(records):
        ch, cat = cloud.get(rec.cid, ("", 0))
        bs = BundleSession(
            cid=rec.cid,
            row=rows.get(rec.cid, {}),
            cloud_mapped=rec.cid in cloud,
            cloud_channel=ch,
            cloud_created_at=cat,
        )
        if rec.has_body:
            bs.body_member = f"{BODIES_DIR}/{rec.cid}.jsonl"
            bs.body_size = rec.jsonl_size or 0
            bs.fingerprint = (
                rec.fingerprint
                if rec.fingerprint is not None
                else (fingerprint_of_file(rec.jsonl_path, rec.cid) if with_fingerprint else None)
            )
        sessions.append(bs)
        if progress is not None:
            progress(i + 1, total)
    return sessions


def sessions_manifest(sessions: list[BundleSession]) -> list[dict[str, object]]:
    """包内 sessions 节的清单。成员路径一律相对**本节根**。"""
    return [
        {
            "cid": s.cid,
            "row": s.row,
            "cloud": {
                "mapped": s.cloud_mapped,
                "channel": s.cloud_channel,
                "created_at": s.cloud_created_at,
            },
            "body": s.body_member,
            "body_size": s.body_size,
            "fingerprint": _fp_to_json(s.fingerprint),
        }
        for s in sessions
    ]


def write_session_bodies(
    z: zipfile.ZipFile,
    sessions: list[BundleSession],
    base: str | os.PathLike[str] | None = None,
    prefix: str = "",
) -> int:
    """把会话正文写进已打开的 zip，返回实际写入的文件数。"""
    written = 0
    for s in sessions:
        if not s.body_member:
            continue
        src = paths.find_project_jsonl(s.cid, base)
        if src is None:
            continue
        z.write(src, prefix + s.body_member)
        written += 1
    return written


def export_bundle(
    out_path: str | os.PathLike[str],
    records,  # list[inventory.SessionRecord]
    base: str | os.PathLike[str] | None = None,
    with_fingerprint: bool = True,
    progress=None,
    extra: dict[str, object] | None = None,
) -> Path:
    """把一批会话导出成**独立会话包**（v1，仅会话）。

    records 来自 inventory.build()；无正文的会话也会进包（元数据仍在，标 has_body=False），
    由导入端决定是否跳过。

    `extra` 合并进 manifest 顶层 —— 用来标注这个包的用途（例如回传包的
    `{"kind": "back-pack"}`）。读端不认识未知键会忽略，所以不涉及格式版本。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sessions = collect_sessions(records, base, with_fingerprint, progress)

    manifest: dict[str, object] = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": int(time.time()),
        "source": source_info(base),
        "sessions": sessions_manifest(sessions),
    }
    if extra:
        manifest.update(extra)

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=_COMPRESS_LEVEL) as z:
            z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))
            write_session_bodies(z, sessions, base)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, out_path)
    return out_path


# ------------------------------------------------------------ 读取


def parse_sessions(
    manifest: dict[str, object],
    members: set[str],
) -> list[BundleSession]:
    """从 manifest 的 sessions 节解析条目。members 为**去掉前缀后**的成员名集合。"""
    sessions: list[BundleSession] = []
    for item in manifest.get("sessions") or []:
        body = item.get("body")
        if body is not None and body not in members:
            # 正文缺失：降级为无正文，让导入端跳过而不是中途炸掉
            body = None
        cloud = item.get("cloud") or {}
        sessions.append(
            BundleSession(
                cid=str(item.get("cid") or ""),
                row=dict(item.get("row") or {}),
                cloud_mapped=bool(cloud.get("mapped")),
                cloud_channel=str(cloud.get("channel") or ""),
                cloud_created_at=int(cloud.get("created_at") or 0),
                body_member=body,
                body_size=int(item.get("body_size") or 0),
                fingerprint=_fp_from_json(item.get("fingerprint")),
            )
        )
    return [s for s in sessions if s.cid]


def read_bundle(path: str | os.PathLike[str], prefix: str = "") -> Bundle:
    """打开并解析一个会话包。格式不对直接抛 ValueError（fail-closed，不猜）。

    prefix 非空时，从同步包 v2 的 `<prefix>` 子目录里读本节（如 "sessions/"）。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"包不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是有效的会话包（非 zip）：{path}")

    with zipfile.ZipFile(path) as z:
        try:
            with z.open(prefix + MANIFEST_NAME) as f:
                manifest = json.loads(f.read().decode("utf-8"))
        except KeyError as e:
            raise ValueError(f"包内缺少 {prefix}{MANIFEST_NAME}") from e
        names = z.namelist()
    members = {n[len(prefix):] for n in names if n.startswith(prefix)}

    if manifest.get("format") != FORMAT_NAME:
        raise ValueError(f"不是 wbsync 会话包：format={manifest.get('format')!r}")
    if int(manifest.get("version") or 0) > FORMAT_VERSION:
        raise ValueError(
            f"包版本 {manifest.get('version')} 高于本工具支持的 {FORMAT_VERSION}，请升级工具"
        )

    return Bundle(
        path=path,
        manifest=manifest,
        prefix=prefix,
        sessions=parse_sessions(manifest, members),
    )


def bundle_fingerprint(path: str | os.PathLike[str]) -> str:
    """包的 SHA-256（前 16 位），用于报告里标识「导出的到底是哪个包」。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
