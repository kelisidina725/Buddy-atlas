"""导入前的「关客户端」与导入后的「拉起来」编排。

## 顺序上的一个细节

**必须在关闭之前**取 exe 路径。进程一旦退出，`QueryFullProcessImageNameW`
就拿不到句柄了，重启时只能靠猜——而客户端装在哪、是不是 32 位、有没有多版本，
都无从得知。所以 `prepare_import` 第一步就先读路径。

## 失败语义

- 关不掉（超时仍有进程）→ 返回 ok=False，**调用方必须中止导入**，不能带着
  运行中的客户端去写 SQLite。宁可这次白跑，也不冒写坏库的风险。
- 重启失败 → 只报告，不影响导入结果。数据已经写进去了，启动失败是另一个问题。

## 为什么先优雅后强制

直接 `TerminateProcess` 会让客户端来不及把内存里的状态落盘（草稿、窗口布局、
未同步的标记）。先发 `WM_CLOSE` 让它自己走退出流程，5 秒还不退才强杀。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import winproc


@dataclass
class CloseOutcome:
    """关闭客户端的结果。"""

    ok: bool
    exe_path: str = ""
    message: str = ""
    was_running: bool = False


def prepare_import(
    stems: tuple[str, ...] = winproc.CLIENT_NAMES,
    graceful_timeout: float = 5.0,
    total_timeout: float = 20.0,
    progress=None,
) -> CloseOutcome:
    """关闭客户端以便写库。

    progress(已用秒数, 剩余进程数) 用于界面读秒。
    """
    exe = winproc.primary_exe(stems)  # 趁进程还在，先把路径拿到
    if not winproc.is_running(stems):
        return CloseOutcome(ok=True, exe_path=exe, message="客户端未在运行", was_running=False)

    ok, msg = winproc.close_client(
        stems,
        graceful_timeout=graceful_timeout,
        total_timeout=total_timeout,
        progress=progress,
    )
    return CloseOutcome(ok=ok, exe_path=exe, message=msg, was_running=True)


def restart(exe_path: str) -> bool:
    """以分离进程方式重新拉起客户端。"""
    if not exe_path:
        return False
    return winproc.launch_detached(exe_path)
