"""检查更新 —— 程序里唯一会联网的地方。

其余功能都是本机文件读写。把网络访问收口在一个模块里，是为了让
「什么时候联网、访问哪个地址、会不会上传东西」在代码里一眼能查清：
只有用户点开「检查更新」才调用 fetch_latest，只读 GitHub 的 Release
元数据；下载要用户自己选保存位置，不自动安装，也不发送本机数据。

两个只读数据源：
  - api.github.com 的 releases/latest：字段全、带附件大小与 SHA256，优先用；
    但未认证请求按出口 IP 限额 60 次/小时，共享代理（Clash 等）的出口
    很容易撞满 → HTTP 403。
  - github.com 的 releases.atom：网页 feed，不占 API 配额；API 报错时
    降级读它，拿得到版本号与发布说明，但没有附件元数据。
下载链接在两个数据源里都是 GitHub 官方地址，只写用户自己选的位置。

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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPO = "kelisidina725/Buddy-atlas"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
ATOM_FEED = f"https://github.com/{REPO}/releases.atom"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_USER_AGENT = "Buddy-atlas-update-check"
_CHUNK = 64 * 1024
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


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
    # "api" = 来自 releases/latest，字段齐全；"atom" = API 失败后降级读
    # releases.atom 的兜底结果，没有附件元数据，无法程序内下载。
    source: str = "api"

    @property
    def has_asset(self) -> bool:
        return bool(self.asset_url)

    @property
    def from_fallback(self) -> bool:
        return self.source == "atom"


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


def _fetch_latest_api(timeout: float) -> ReleaseInfo:
    """从 releases/latest 读取。字段最全，但 403 限额时会抛 UpdateError。"""
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
        source="api",
    )


def _notes_to_text(html: str) -> str:
    """把 Atom 里 HTML 转义过的发布说明压成界面能直接显示的纯文本。

    Atom 的 <content type="html"> 实体解码后是一段 HTML。这里不引入 html
    模块（spec 的排除清单里有它），用最小实现去掉常见块级/行内标签，
    保留换行和列表层次即可 —— 只用于 QTextBrowser 展示，不解析语义。
    """
    text = html or ""
    # 块级标签与 <br> 转成换行，<li> 前补一个短横线
    text = re.sub(r"(?is)<li[^>]*>", "\n- ", text)
    text = re.sub(r"(?is)<br\s*/?>|</(?:p|div|h[1-6]|ul|ol|pre|blockquote)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)  # 去掉剩余标签
    # html.unescape 不在可用清单里，手工还原常见实体
    for ent, ch in (
        ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&"),
    ):
        text = text.replace(ent, ch)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip()).strip()


def _parse_atom(raw: bytes) -> ReleaseInfo:
    """把 releases.atom 的 XML 解析成 ReleaseInfo。纯函数，便于离线自检。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UpdateError(f"备用来源返回的内容读不懂：{exc}") from None

    entry = root.find(f"{_ATOM_NS}entry")
    if entry is None:
        raise UpdateError("这个仓库还没有发布过 Release。")

    def field(name: str) -> str:
        node = entry.find(f"{_ATOM_NS}{name}")
        return (node.text or "") if node is not None else ""

    # 页面链接用 <link rel="alternate" type="text/html">，比自行拼更可靠
    page_url = RELEASES_PAGE
    for link in entry.findall(f"{_ATOM_NS}link"):
        if link.get("rel") == "alternate":
            page_url = link.get("href") or page_url
            break

    # <id> 形如 tag:github.com,2008:Repository/<repo_id>/v1.2.3，取末段即 tag
    raw_id = field("id")
    tag = raw_id.rsplit("/", 1)[-1] if "/" in raw_id else ""
    return ReleaseInfo(
        tag=tag,
        version=parse_version(tag),
        title=field("title") or tag or "最新版本",
        page_url=page_url,
        notes=_notes_to_text(field("content")),
        published_at=field("updated"),
        asset_name="Buddy-atlas.exe",
        asset_url="",  # Atom 不提供附件直链，交给界面引导到 Release 页面
        asset_size=0,
        asset_digest="",
        source="atom",
    )


def _fetch_latest_atom(timeout: float) -> ReleaseInfo:
    """API 失败时的兜底数据源：读 github.com 的 releases.atom。

    网页 feed 不占 api.github.com 的 60 次/小时配额，和 API 撞 403 的
    共享出口也能读到。取不到附件元数据，只给出版本号 + 说明 + 页面链接，
    由界面引导用户到 Release 页面手动下载。
    """
    try:
        with _urlopen(ATOM_FEED, timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"备用来源返回 HTTP {exc.code}。") from None
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"连不上备用来源：{exc}") from None
    return _parse_atom(raw)


def fetch_latest(timeout: float = 10.0) -> ReleaseInfo:
    """读取最新 Release 的元数据。只查询，不下载任何东西。

    API 优先（字段齐全、可程序内下载）；一旦失败，自动降级读 releases.atom，
    保证共享出口 IP 撞上 GitHub 403 时仍能查到版本。两条路都不通才报错。
    """
    try:
        return _fetch_latest_api(timeout)
    except UpdateError:
        return _fetch_latest_atom(timeout)


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
