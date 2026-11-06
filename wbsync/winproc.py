"""Windows 进程控制（ctypes 直调 Win32 API）。

## 为什么不用 tasklist / wmic / PowerShell

本工具打包成 `console=False` 的窗口程序。在这种进程里启动**控制台子程序**，
Windows 会为子进程分配一个新的控制台窗口 —— 于是界面上一闪而过一个黑框。
用户看到的「像 cmd 窗口关闭的效果」就是这个。

加 `CREATE_NO_WINDOW` 能挡住，但还有别的问题：
  - `tasklist` 一次要 100~300 ms，而 ctypes 枚举约 20 ms
  - `wmic` 在新版 Windows 已被移除
  - PowerShell 启动要 1~2 秒，本身也可能闪窗

所以这里直接用 Win32 API：`CreateToolhelp32Snapshot` 遍历进程、
`QueryFullProcessImageNameW` 取可执行路径、`PostMessage(WM_CLOSE)` 请求优雅退出。
**全程零子进程**，从根上消除闪窗。

## 优雅关闭 vs 强制结束

先对所有可见顶层窗口发 `WM_CLOSE`，让客户端走自己的退出流程（落盘、清理）。
等 5 秒还没退干净，才 `TerminateProcess`。
直接强杀会让客户端来不及把内存里的状态写回，可能丢草稿。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Iterator

IS_WINDOWS = os.name == "nt"

# 客户端进程名（不含 .exe，比较时统一小写去扩展名）
CLIENT_NAMES = ("workbuddy", "codebuddy")

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WM_CLOSE = 0x0010

_CTYPES_READY = False
_kernel32 = None
_user32 = None
_PROCESSENTRY32W = None


def _ensure_ctypes():
    """惰性初始化 ctypes 结构体，避免非 Windows 平台 import 就炸。"""
    global _CTYPES_READY, _kernel32, _user32, _PROCESSENTRY32W
    if _CTYPES_READY or not IS_WINDOWS:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _kernel32 = kernel32
    _user32 = user32
    _PROCESSENTRY32W = PROCESSENTRY32W
    _CTYPES_READY = True


def normalize(name: str) -> str:
    """`WorkBuddy.exe` -> `workbuddy`，便于比较。"""
    n = (name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str

    @property
    def stem(self) -> str:
        return normalize(self.name)


def iter_processes() -> Iterator[ProcInfo]:
    """枚举当前所有进程。非 Windows 或失败时返回空。"""
    if not IS_WINDOWS:
        yield from _iter_posix()
        return
    _ensure_ctypes()
    if not _CTYPES_READY:
        return

    import ctypes

    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            yield ProcInfo(pid=int(entry.th32ProcessID), name=str(entry.szExeFile))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)


def _iter_posix() -> Iterator[ProcInfo]:
    """macOS/Linux 兜底：读 /proc 或 ps 输出。仅用于本地开发，不影响 Windows 行为。"""
    try:
        import subprocess

        out = subprocess.run(
            ["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001
        return
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            yield ProcInfo(pid=int(parts[0]), name=parts[1].strip())
        except ValueError:
            continue


def find_by_stem(stems: tuple[str, ...] = CLIENT_NAMES) -> list[ProcInfo]:
    """按进程名（去扩展名、小写）找进程。"""
    wanted = {normalize(s) for s in stems}
    return [p for p in iter_processes() if p.stem in wanted]


def is_running(stems: tuple[str, ...] = CLIENT_NAMES) -> bool:
    return bool(find_by_stem(stems))


def exe_path(pid: int) -> str:
    """取进程的完整可执行路径。拿不到返回空串。"""
    if not IS_WINDOWS:
        return ""
    _ensure_ctypes()
    if not _CTYPES_READY:
        return ""
    import ctypes

    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = ctypes.c_ulong(2048)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
    except Exception:  # noqa: BLE001
        return ""
    finally:
        _kernel32.CloseHandle(handle)
    return ""


def primary_exe(stems: tuple[str, ...] = CLIENT_NAMES) -> str:
    """取客户端可执行文件路径 —— 重启时要用同一个。

    多个进程（Electron 类客户端常见 8~10 个）指向同一个 exe，
    优先选路径最短的那个（通常是主进程，子进程带 --type=xxx 参数但路径相同，所以随便取一个即可）。

    **只认正在运行的进程**：客户端没开就一定返回空串。要「客户端关着也能拉起来」，
    请用 `resolve_client_exe()`。
    """
    for p in find_by_stem(stems):
        path = exe_path(p.pid)
        if path:
            return path
    return ""


# ---------------------------------------------------------------- 客户端定位（关着也要能找到）
#
# 「导入完成后自动重启」的真正难点不是启动，而是**关掉之后就找不着了**：
# 关客户端是导入的前置动作，关完再靠进程映像取路径必然落空。
# 所以这里的顺序是：进程映像 → 上次记住的路径 → 注册表/默认安装目录。

_UNINSTALL_ROOTS = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

_EXE_NAMES = {"workbuddy": "WorkBuddy.exe", "codebuddy": "CodeBuddy.exe"}

_ENV_DIRS = (
    r"%LOCALAPPDATA%\Programs\WorkBuddy",
    r"%LOCALAPPDATA%\Programs\workbuddy",
    r"%LOCALAPPDATA%\WorkBuddy",
    r"%PROGRAMFILES%\WorkBuddy",
    r"%PROGRAMFILES(X86)%\WorkBuddy",
)


def _strip_icon_suffix(value: str) -> str:
    """`D:\\workbuddy\\WorkBuddy.exe,0` -> `D:\\workbuddy\\WorkBuddy.exe`。"""
    text = (value or "").strip().strip('"')
    if not text:
        return ""
    head, sep, tail = text.rpartition(",")
    if sep and tail.strip().isdigit():
        return head.strip().strip('"')
    return text


def known_install_candidates() -> Iterator[str]:
    """客户端没在运行时，去注册表与默认安装目录找 exe。只读，不修改任何东西。"""
    exe_names = tuple(_EXE_NAMES.get(normalize(s), f"{normalize(s)}.exe") for s in CLIENT_NAMES)

    if IS_WINDOWS:
        try:
            import winreg
        except ImportError:  # pragma: no cover - 非 Windows
            winreg = None  # type: ignore[assignment]
        if winreg is not None:
            # 1) App Paths：安装器写了这一项就能直接命中
            for stem in CLIENT_NAMES:
                key = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{stem}.exe"
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, key) as k:
                            value, _ = winreg.QueryValueEx(k, "")
                    except OSError:
                        continue
                    path = _strip_icon_suffix(str(value))
                    if path:
                        yield path
            # 2) 卸载项：DisplayIcon / InstallLocation 都记着装在哪
            for root in _UNINSTALL_ROOTS:
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, root) as k:
                            # 枚举必须留在 with 里：句柄一旦关掉，后面的 EnumKey 全抛 OSError
                            for i in range(winreg.QueryInfoKey(k)[0]):
                                try:
                                    sub = winreg.EnumKey(k, i)
                                    with winreg.OpenKey(k, sub) as item:
                                        name, _ = winreg.QueryValueEx(item, "DisplayName")
                                        if not any(
                                            n in str(name).lower() for n in ("workbuddy", "codebuddy")
                                        ):
                                            continue
                                        for field in ("DisplayIcon", "InstallLocation"):
                                            try:
                                                raw, _ = winreg.QueryValueEx(item, field)
                                            except OSError:
                                                continue
                                            text = _strip_icon_suffix(str(raw))
                                            if text.lower().endswith(".exe"):
                                                yield text
                                            elif text:
                                                for exe_name in exe_names:
                                                    yield os.path.join(text, exe_name)
                                except OSError:
                                    continue
                    except OSError:
                        continue

    # 3) 默认安装目录：注册表被清理过（绿色版/手工解压）时的最后兜底
    for pattern in _ENV_DIRS:
        root = os.path.expandvars(pattern)
        if "%" in root:  # 展开失败的占位符，跳过
            continue
        for exe_name in exe_names:
            yield os.path.join(root, exe_name)


def resolve_client_exe(
    process_lookup: Callable[[], str] | None = None,
    remembered: Callable[[], str] | None = None,
    candidates: Callable[[], Iterator[str]] | None = None,
) -> str:
    """定位客户端 exe，**关着也能找到**。

    顺序：正在运行的进程 → 上次成功定位并记住的路径 → 注册表/默认安装目录。
    三个来源都可以注入，便于在不真实触碰进程与注册表的前提下测这条链路。
    """
    exe = (process_lookup or primary_exe)()
    if exe:
        return exe

    saved = (remembered or (lambda: ""))()
    if saved and os.path.isfile(saved):
        return saved

    for path in (candidates or known_install_candidates)():
        if path and os.path.isfile(path):
            return path
    return ""



def request_close(pid: int) -> bool:
    """给该进程的所有可见顶层窗口发 WM_CLOSE，请求它自己退出。"""
    if not IS_WINDOWS:
        return False
    _ensure_ctypes()
    if not _CTYPES_READY:
        return False
    import ctypes
    from ctypes import wintypes

    targets: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd):
            targets.append(hwnd)
        return True

    try:
        _user32.EnumWindows(enum_proc(_cb), 0)
    except Exception:  # noqa: BLE001
        return False

    for hwnd in targets:
        try:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            continue
    return bool(targets)


def terminate(pid: int) -> bool:
    """强制结束进程。"""
    if not IS_WINDOWS:
        return False
    _ensure_ctypes()
    if not _CTYPES_READY:
        return False
    handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


def wait_until_gone(
    stems: tuple[str, ...] = CLIENT_NAMES,
    timeout: float = 20.0,
    interval: float = 0.3,
    progress: Callable[[float, int], None] | None = None,
) -> bool:
    """轮询直到目标进程全部消失，返回是否成功。

    progress(已用秒数, 剩余进程数) 用于界面读秒 ——
    关客户端可能要十几秒，没有反馈会被当成卡死。
    """
    started = time.monotonic()
    while True:
        remaining = len(find_by_stem(stems))
        elapsed = time.monotonic() - started
        if progress is not None:
            try:
                progress(elapsed, remaining)
            except Exception:  # noqa: BLE001 - 回调出错不能影响关闭流程
                pass
        if remaining == 0:
            return True
        if elapsed >= timeout:
            return False
        time.sleep(interval)


def close_client(
    stems: tuple[str, ...] = CLIENT_NAMES,
    graceful_timeout: float = 5.0,
    total_timeout: float = 20.0,
    progress: Callable[[float, int], None] | None = None,
) -> tuple[bool, str]:
    """关闭客户端。返回 (是否成功, 说明)。

    1. 对所有可见窗口发 WM_CLOSE
    2. 等 graceful_timeout 秒
    3. 仍有残留 -> 强制结束所有同名进程
    4. 再等到 total_timeout
    """
    procs = find_by_stem(stems)
    if not procs:
        return True, "客户端未在运行"

    for p in procs:
        request_close(p.pid)

    if wait_until_gone(stems, timeout=graceful_timeout, progress=progress):
        return True, "已正常退出"

    for p in find_by_stem(stems):
        terminate(p.pid)

    if wait_until_gone(stems, timeout=max(total_timeout - graceful_timeout, 5.0), progress=progress):
        return True, "已强制结束（客户端未能正常退出）"

    left = len(find_by_stem(stems))
    return False, f"仍有 {left} 个进程未退出"


def launch_detached(exe_path: str, args: list[str] | None = None) -> bool:
    """以分离进程方式启动客户端，确保本工具退出后它仍在。

    DETACHED_PROCESS 是关键：否则子进程与本工具共享控制台/进程组，
    工具一关可能把它一起带走。
    """
    if not exe_path:
        return False
    try:
        import subprocess

        flags = 0
        if IS_WINDOWS:
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [exe_path] + (args or []),
            close_fds=True,
            creationflags=flags,
            cwd=os.path.dirname(exe_path) or None,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
