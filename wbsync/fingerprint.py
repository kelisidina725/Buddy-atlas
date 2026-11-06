"""会话内容指纹 —— 判断两个副本「谁落后 / 谁分叉」的内容级判据。

## 为什么是「指纹」而不是「消息数」或「updatedAt」

`updatedAt` 无法表达「落后 N 条」或「有分歧」——两个副本各自追加过消息时，
时间戳只能告诉你谁最后动过，不能告诉你内容是否同源。只有内容级判据才行。

## 三个必须守住的不变式（全部来自参考项目的实测踩坑）

1. **cid 归一化**：jsonl 每一行都带自己的 `sessionId`，而副本是另一个 cid。
   若直接对原始行做 hash，两个副本**永远没有公共前缀**，
   于是每个会话都会被判成「分叉」——明明内容一致却报冲突（假分歧）。
   ⇒ hash 前必须把本会话的 cid 换成固定占位符。

2. **logical_lines 不能用 split_inclusive('\n')**：末行有没有换行符会让**同一行**
   看起来不同（"a\\n" vs "a"），把「目标末行缺换行」误判成分歧。

3. **只 hash 前 PREFIX_HASH_LIMIT 行**：使响应体不随会话长度线性膨胀。
   代价是超长会话在 512 行之后不可分辨 —— 这是既有取舍，不偷偷放大。

## 为什么用 FNV-1a 而不是内置 hash()

FNV-1a 是**纯函数**：同一份字节在任何进程、任何版本里都得到同一个值，
所以指纹可以安全落盘。Python 的 `hash()` 对 str 是随机加盐的，跨进程不稳，绝不能用。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

# hash 前 512 行（与参考项目 PREFIX_HASH_LIMIT 同口径）
PREFIX_HASH_LIMIT = 512

# 归一化占位符：用 Unit Separator 包裹，正常正文不会撞车
SESSION_ID_PLACEHOLDER = "\x1fbuddy-switch-session-id\x1f"

FNV64_OFFSET_BASIS = 0xCBF29CE484222325
FNV64_PRIME = 0x100000001B3
_MASK64 = 0xFFFFFFFFFFFFFFFF


def fnv1a(data: bytes, state: int = FNV64_OFFSET_BASIS) -> int:
    """64 位 FNV-1a。纯函数，跨进程 / 跨版本稳定。"""
    h = state & _MASK64
    for b in data:
        h ^= b
        h = (h * FNV64_PRIME) & _MASK64
    return h


@dataclass(frozen=True)
class SessionFingerprint:
    """一个会话 jsonl 的内容指纹。

    line_count    —— 完整行数（实测每行一条消息，≈ 消息数）
    prefix_hashes —— 前 PREFIX_HASH_LIMIT 行的逐行 hash（只存 hash，不落正文）
    digest        —— 全文 SHA-256（同样基于归一化后的行）

    ★ digest 是为「超长会话」补的判据。只靠前 512 行的逐行 hash，
      两个 2000 行、内容完全相同的副本也只能确认前 512 行一致，
      之后的关系无从判断，于是被判成「无法判定」而拒绝同步。
      长会话（几百条消息）很常见，这个洞必须堵。
    """

    line_count: int
    prefix_hashes: tuple[int, ...] = field(default_factory=tuple)
    digest: str = ""

    def __len__(self) -> int:  # 便于 sorted / 判空
        return self.line_count

    @property
    def has_digest(self) -> bool:
        return bool(self.digest)


def logical_lines(text: str) -> list[str]:
    """按 '\\n' 切分，并丢掉「文件以换行结尾」产生的那一个空尾元素。

    ⚠️ 不能用 split_inclusive('\\n') 直接比较：末行有没有 '\\n' 会让同一行看起来不同。
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _normalize(line: str, cid: str) -> str:
    if not cid:
        return line
    return line.replace(cid, SESSION_ID_PLACEHOLDER)


def _digest_of(lines: list[str] | None, cid: str, stream=None) -> str:
    """全文 SHA-256（基于归一化后的内容 + 固定分隔符）。

    ⚠️ 必须归一化：副本的 cid 不同，直接对原始字节取 hash 会得到不同结果，
       digest 就永远不相等，白算。
    ⚠️ 行间加 '\\n' 再 update：否则 ["ab","c"] 与 ["a","bc"] 会得出同一个 hash。
    """
    h = hashlib.sha256()
    if lines is not None:
        for line in lines:
            h.update(_normalize(line, cid).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()
    if stream is not None:
        for raw in stream:
            line = raw[:-1] if raw.endswith("\n") else raw
            h.update(_normalize(line, cid).encode("utf-8", "replace"))
            h.update(b"\n")
        return h.hexdigest()
    return ""


def fingerprint_of_text(text: str, cid: str = "") -> SessionFingerprint:
    """内存版指纹。cid 为空表示不做归一化（调用方保证两侧 cid 一致）。"""
    lines = logical_lines(text)
    hashes = [
        fnv1a(_normalize(line, cid).encode("utf-8"))
        for line in lines[:PREFIX_HASH_LIMIT]
    ]
    return SessionFingerprint(
        line_count=len(lines),
        prefix_hashes=tuple(hashes),
        digest=_digest_of(lines, cid),
    )


def fingerprint_of_file(
    path: str | os.PathLike[str], cid: str = ""
) -> SessionFingerprint | None:
    """文件版指纹（流式读取，大文件不整体入内存）。

    读不到返回 None —— **fail-closed**：宁可不算，也不把缺失当成「空会话」。

    ⚠️ 迭代文件对象与 logical_lines 的行切分语义一致：
       文件以 '\\n' 结尾时不会产生额外空行，末行无换行时仍计为一行。
    """
    hashes: list[int] = []
    line_count = 0
    h = hashlib.sha256()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw[:-1] if raw.endswith("\n") else raw
                line_count += 1
                norm = _normalize(line, cid).encode("utf-8")
                if len(hashes) < PREFIX_HASH_LIMIT:
                    hashes.append(fnv1a(norm))
                # digest 要把全文都喂进去，这是超长会话唯一的判等依据
                h.update(norm)
                h.update(b"\n")
    except OSError:
        return None
    return SessionFingerprint(
        line_count=line_count, prefix_hashes=tuple(hashes), digest=h.hexdigest()
    )


def lf_size(path: str | os.PathLike[str]) -> int | None:
    """正文的「LF 口径」字节数 = 原始字节数 − CR 个数。

    ## 为什么判重的预筛不能用 `st_size`

    `digest` 是**文本模式**读出来的：universal newlines 会把 `\\r\\n` 归一成 `\\n`，
    所以「内容是否相同」这件事本来对换行符不敏感。可字节数预筛用的是原始 `st_size`，
    两端口径一不一致，同一段对话的 LF 版与 CRLF 版就永远撞不上车（正好差「行数」
    个字节），判重**静默失效**。

    2026-11-03 实测：旧版本复制会话写盘时做了换行转换，本机因此混着两种写法的副本；
    导入/对齐都把它们当新内容又各复制一份，用户看到的就是「导入后重复了」。

    读不到返回 None（fail-closed，与 `fingerprint_of_file` 同语义）。
    """
    total = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                total += len(chunk) - chunk.count(b"\r")
    except OSError:
        return None
    return total


def to_json(fp: SessionFingerprint | None) -> dict[str, object] | None:
    """指纹落盘/入包的统一形态。

    存 16 进制串而非 int：JSON 对 >2^53 的整数不保证精度，
    int 化会让指纹在「写出去再读回来」之后悄悄变样。
    包格式与本地缓存共用这一份，避免两处演进出不同结构。
    """
    if fp is None:
        return None
    return {
        "line_count": fp.line_count,
        "prefix_hashes": [format(h, "016x") for h in fp.prefix_hashes],
        # 全文摘要：让超长会话在导入端也能被判为「一致」而不是「无法判定」
        "digest": fp.digest,
    }


def from_json(d: object) -> SessionFingerprint | None:
    """to_json 的逆运算。结构不对一律返回 None（fail-closed，不猜）。"""
    if not isinstance(d, dict):
        return None
    hashes = d.get("prefix_hashes")
    if not isinstance(hashes, list):
        return None
    try:
        parsed = tuple(int(h, 16) for h in hashes)
    except (TypeError, ValueError):
        return None
    return SessionFingerprint(
        line_count=int(d.get("line_count") or 0),
        prefix_hashes=parsed,
        # 旧包没有 digest：留空，比较时自动退回前缀判据
        digest=str(d.get("digest") or ""),
    )


def full_line_hashes_of_file(
    path: str | os.PathLike[str], cid: str = ""
) -> list[int] | None:
    """**全量**逐行 hash（不受 PREFIX_HASH_LIMIT 限制）。

    ★ 为什么需要它：PREFIX_HASH_LIMIT=512 是为「指纹要进包、响应体不能随会话
      长度膨胀」设的。但拿它判「短的那份是不是长的那份的前缀」时，超过 512 行
      的会话公共前缀必然停在 512，于是**永远判不出谁包含谁** —— 要么判成分叉
      （参考实现就这么干，长会话恒报分歧），要么判成无法判定。

      这里算全量，只用于**本机**的同族判定，不进包、不落盘。

    ★ 为什么这里不用 `fnv1a`：`prefix_hashes` 要落盘、要跨机比对，算法一变
      旧包里的 hash 就与本地算出来的对不上，全部会话会被判成「分叉」。
      而本函数的结果只在本进程内两两比较（两侧都由它算），换算法没有兼容问题。
      实测纯 Python 的 fnv1a 在 9 千行上耗 3.3 秒（占整套计划 77%），
      换成 C 实现的 blake2b-64 后降到 0.1 秒级。

    读不到返回 None（fail-closed，与 fingerprint_of_file 同语义）。
    """
    def line_hash(norm: bytes) -> int:
        return int.from_bytes(
            hashlib.blake2b(norm, digest_size=8).digest(), "big"
        )

    hashes: list[int] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw[:-1] if raw.endswith("\n") else raw
                hashes.append(line_hash(_normalize(line, cid).encode("utf-8")))
    except OSError:
        return None
    return hashes


def common_prefix_of(a: list[int], b: list[int]) -> int:
    """两组逐行 hash 的公共前缀长度（全量，无上限）。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def common_prefix_len(a: SessionFingerprint, b: SessionFingerprint) -> int:
    """两个指纹的公共前缀长度 = 「共同旧版」的行号。

    受 PREFIX_HASH_LIMIT 限制：若两边都超过 512 行，最大只能返回 512。
    """
    n = min(len(a.prefix_hashes), len(b.prefix_hashes))
    i = 0
    while i < n and a.prefix_hashes[i] == b.prefix_hashes[i]:
        i += 1
    return i


SYNCED = "synced"      # 完全一致
PROBABLY_SYNCED = "probably-synced"  # 行数相同 + 前缀一致，依据不足但按一致处理
BEHIND = "behind"      # 一方落后，可安全追加
DIVERGED = "diverged"  # 双方在共同旧版之后各自改过
UNKNOWN = "unknown"    # 公共前缀触到指纹精度上限，无法判定


@dataclass(frozen=True)
class CompareResult:
    """两个副本的比对结论。"""

    verdict: str
    common: int
    a_lines: int
    b_lines: int
    note: str = ""

    @property
    def delta(self) -> int:
        """落后方比领先方少多少行（负数表示 b 比 a 多）。"""
        return self.a_lines - self.b_lines


def compare(a: SessionFingerprint, b: SessionFingerprint) -> CompareResult:
    """判定两个副本的关系。

    ★ 分歧判据：公共前缀 < **较短的一方**。
      （不能写成 `< 目标行数`：目标比源长且源是其前缀时，那是「目标更新」，不是分歧。）

    ★ 先看全文 digest：它不受 512 行精度限制。两个 2000 行的副本内容完全一致时，
      只靠前缀会被判成「无法判定」而拒绝，有了 digest 才能正确定为一致。
    """
    common = common_prefix_len(a, b)
    shorter = min(a.line_count, b.line_count)

    if a.digest and b.digest and a.digest == b.digest:
        return CompareResult(
            SYNCED, min(common, shorter) if common else shorter, a.line_count, b.line_count
        )

    if a.line_count == b.line_count and common == a.line_count:
        return CompareResult(SYNCED, common, a.line_count, b.line_count)

    if common >= PREFIX_HASH_LIMIT:
        if a.line_count == b.line_count:
            # 行数完全相同 + 前 PREFIX_HASH_LIMIT 行逐行一致。
            # 严格说无法证明第 512 行之后也一致，但**行数相同**本身就是强证据
            # （分叉总会伴随行数变化，除非恰好抵消）。
            # 调用方只在这个结论上做「校正归属」这类不碰正文的动作，风险可控。
            return CompareResult(
                PROBABLY_SYNCED,
                common,
                a.line_count,
                b.line_count,
                f"行数相同且前 {PREFIX_HASH_LIMIT} 行一致（更远处依据不足）",
            )
        return CompareResult(
            UNKNOWN,
            common,
            a.line_count,
            b.line_count,
            f"公共前缀已达指纹上限 {PREFIX_HASH_LIMIT} 行，无法判定更远处是否分叉",
        )

    if common < shorter:
        return CompareResult(
            DIVERGED,
            common,
            a.line_count,
            b.line_count,
            f"双方在共同旧版（{common} 行）之后各自有改动",
        )

    return CompareResult(BEHIND, common, a.line_count, b.line_count)
