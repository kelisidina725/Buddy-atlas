"""通用格式化 —— 账号页与概览页共用同一套口径。

同一个字节数在账号页写 `56.0 MB`、在概览页写 `58,720,256 B`，用户会以为
两个数不是一回事；时间同理。所以这两个函数只留一份实现。
"""
from __future__ import annotations

import time


def human_bytes(n: int) -> str:
    """字节数 -> 「56.0 MB」这类短串（1024 进制、一位小数）。"""
    n = int(n or 0)
    if n <= 0:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1073741824:.2f} GB"


def human_ago(ts_ms: int, now_ms: int | None = None) -> str:
    """毫秒时间戳 -> 「刚刚 / 3 天前 / 10-20」。

    ★ 时间戳可能混着秒级和毫秒级（客户端的 `deleted_at` 就有 -1 这类哨兵值），
      所以先做量级归一：小于 10^11 的按秒算，否则按毫秒。
    """
    ts = int(ts_ms or 0)
    if ts <= 0:
        return ""
    if ts < 100_000_000_000:  # 秒级
        ts *= 1000
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    delta = max(0, now - ts) / 1000.0
    if delta < 90:
        return "刚刚使用"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    try:
        return time.strftime("%m-%d", time.localtime(ts / 1000))
    except (OSError, ValueError, OverflowError):
        return ""
