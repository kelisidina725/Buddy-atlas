"""检查更新 —— 程序里唯一会联网的地方。

其余功能都是本机文件读写。把网络访问收口在一个模块里，是为了让
「什么时候联网、访问哪个地址、会不会上传东西」在代码里一眼能查清：
只有用户点开「检查更新」才调用 fetch_latest，只读 GitHub 的 Release
元数据；下载要用户自己选保存位置，不自动安装，也不发送本机数据。

打包配置 wbsync.spec 原先为了「纯离线」排掉了 urllib.request、http、
email 和 libssl/libcrypto 两个 DLL。现在要放行它们，否则源码里能跑、
打包出的 exe 会在点开检查更新那一刻 ImportError。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = "kelisidina725/Buddy-atlas"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_USER_AGENT = "Buddy-atlas-update-check"
_CHUNK = 64 * 1024


class UpdateError(Exception):
    """能直接显示给用户的失败原因。"""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: tuple[int, int, int] | None
    title: str
    page_url: str
    notes: str
    published_at: str
    asset_name: str
    asset_url: str
    asset_size: int
    asset_digest: str

    @property
    def has_asset(self) -> bool:
        return bool(self.asset_url)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """从 `v1.14.0`、`1.14.0` 这类标签里取三段数字。

    GitHub 的 tag 可能带前缀或后缀，版本号也可能写成 `1.14`。取不到
    三段时返回 None，由调用方按「无法比较」处理，不猜。
    """
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def is_newer(remote: tuple[int, int, int] | None, local: tuple[int, int, int]) -> bool:
    if remote is None:
        return False
    return remote > local


def format_size(size: int) -> str:
    if size <= 0:
        return "大小未知"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )


def _urlopen(url: str, timeout: float):
    """先走系统代理；代理把 TLS 掐了就再试一次直连。

    有些机器装了抓包代理，系统代理能连上但对 api.github.com 的证书
    校验会失败。重试一次直连即可，两次都不通时抛第一次的原因，避免
    把「直连也不通」误报成代理问题。
    """
    req = _request(url)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, ssl.SSLError, OSError) as first:
        if not urllib.request.getproxies():
            raise
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            return opener.open(req, timeout=timeout)
        except Exception:
            raise first from None


def fetch_latest(timeout: float = 10.0) -> ReleaseInfo:
    """读取最新 Release 的元数据。只查询，不下载任何东西。"""
    try:
        with _urlopen(LATEST_API, timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("这个仓库还没有发布过 Release。") from None
        raise UpdateError(f"GitHub 返回 HTTP {exc.code}，稍后再试。") from None
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"连不上 GitHub：{exc}") from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError(f"GitHub 返回的内容读不懂：{exc}") from None

    if not isinstance(data, dict):
        raise UpdateError("GitHub 返回的格式不对。")

    assets = data.get("assets") or []
    exe = next(
        (a for a in assets if isinstance(a, dict)
         and str(a.get("name", "")).lower().endswith(".exe")),
        None,
    )
    if exe is None:
        exe = {}

    tag = str(data.get("tag_name") or "")
    return ReleaseInfo(
        tag=tag,
        version=parse_version(tag),
        title=str(data.get("name") or tag or "最新版本"),
        page_url=str(data.get("html_url") or RELEASES_PAGE),
        notes=str(data.get("body") or ""),
        published_at=str(data.get("published_at") or ""),
        # 附件名固定为 Buddy-atlas.exe（版本由 tag 标识）。这里只是 Release
        # 没有 exe 附件时的兜底名，供保存对话框预填，不再拼版本号。
        asset_name=str(exe.get("name") or "Buddy-atlas.exe"),
        asset_url=str(exe.get("browser_download_url") or ""),
        asset_size=int(exe.get("size") or 0),
        asset_digest=str(exe.get("digest") or ""),
    )


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download_file(
    info: ReleaseInfo,
    dest: Path,
    progress=None,
    cancelled=None,
    timeout: float = 30.0,
) -> Path:
    """把新版 exe 下载到 dest。先写 `.part`，全部校验通过才改名。

    progress(done, total) 用于刷新进度条；cancelled() 返回 True 时中止并
    删掉半成品。GitHub 在 asset 上给了 sha256 digest，有就逐字节核对，
    没给就核对声明的文件大小，两者都没有才只按「能写完」放行。
    """
    if not info.has_asset:
        raise UpdateError("这个 Release 里没有可下载的 exe，请到 Release 页面看看。")
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    try:
        with _urlopen(info.asset_url, timeout) as resp:
            total = int(resp.headers.get("Content-Length") or info.asset_size or 0)
            done = 0
            with part.open("wb") as out:
                while True:
                    if cancelled is not None and cancelled():
                        raise UpdateError("已取消下载。")
                    block = resp.read(_CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress is not None:
                        progress(done, total)
        if info.asset_size and part.stat().st_size != info.asset_size:
            raise UpdateError("下载不完整，文件长度对不上，已删除半成品。")
        if info.asset_digest.startswith("sha256:"):
            want = info.asset_digest.split(":", 1)[1].lower()
            if sha256_of(part) != want:
                raise UpdateError("校验不通过，SHA256 与 Release 上登记的不一致，已删除半成品。")
        os.replace(part, dest)
        return dest
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, ssl.SSLError,
            TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：{exc}") from None
