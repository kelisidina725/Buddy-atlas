"""WorkBuddy 会话与配置同步工具 —— 入口。

会话：盘点只读，导入写三件套，需先退出客户端。
配置：设置项 / 人格文件 / 技能包，三方比对，冲突需人工拍板。

打包成 exe 后没有控制台，任何未捕获异常都会表现为「窗口一闪就没了」。
所以这里挂了崩溃日志钩子，并提供 `--selftest` 用于验证打包完整性。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from wbsync import __version__


def _log_dir() -> Path:
    return Path.home() / ".workbuddy"


def _write_log(name: str, text: str) -> Path:
    out = _log_dir() / name
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return out


def _install_crash_log() -> None:
    """未捕获异常写日志 + 弹窗。没有这一步，exe 崩溃时用户拿不到任何线索。"""

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        path = _write_log("wbsync-crash.log", detail)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    "程序异常",
                    f"{exc_type.__name__}: {exc}\n\n详细信息已写入：\n{path}",
                )
        except Exception:  # noqa: BLE001 - 崩溃处理本身不能再崩
            pass

    sys.excepthook = hook


def _run_version() -> int:
    """打印版本号。

    打包成 windowed exe 后 sys.stdout 可能为 None，直接 print 会抛
    AttributeError（表现为窗口一闪而过），所以先判空再兜异常，
    同时把版本写一份文件，保证在任何启动方式下都能查到。
    """
    text = f"Buddy-atlas {__version__}"
    path = _write_log("wbsync-version.txt", text + "\n")
    out = sys.stdout
    if out is not None:
        try:
            print(text)
            # 这行刻意用 ASCII：windowed exe 的 stdout 按 UTF-8 写出，
            # 而 Windows 控制台按 GBK 解码，中文提示在此处必然乱码。
            print(f"written: {path}")
        except (OSError, ValueError, AttributeError):
            pass
    return 0


def _run_selftest() -> int:
    """验证打包后关键依赖是否齐全。结果写文件（exe 无控制台）。"""
    lines: list[str] = []
    ok = True

    try:
        import sqlite3
        import zipfile  # noqa: F401

        lines.append(f"sqlite3: {sqlite3.sqlite_version}")
    except Exception:
        ok = False
        lines.append("sqlite3 FAILED:\n" + traceback.format_exc())

    # 线程池：会话复制靠它并行搬正文。wbsync.spec 的 excludes 里一旦把
    # concurrent 排掉，这里会立刻 FAILED —— 而不是等到用户点「复制会话」才炸。
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as _pool:
            _total = sum(_pool.map(len, ["ab", "cde"]))
        if _total != 5:
            raise AssertionError(f"线程池结果异常：{_total}")
        lines.append("threads: 线程池可用（会话复制依赖）")
    except Exception:
        ok = False
        lines.append("threads FAILED:\n" + traceback.format_exc())

    try:
        from wbsync import config_items, importlog, inventory, sidebar

        inv = inventory.build(with_fingerprint=False)
        lines.append(f"inventory: {inv.total} 条会话, 数据库 {inv.db_path}")
        lines.append(f"config: {len(config_items.discover())} 项配置")
        lines.append(f"sidebar: {len(inv.sidebar_info.snapshots)} 份快照, 可见 {len(inv.visible)} 条")
        # 单独验一次导入记录：它是「导入后能否立刻看到」的关键，缺了会表现成导入失败
        lines.append(f"importlog: {len(importlog.pending_cids())} 条待确认")

        # 进程控制走 ctypes，打包环境最容易在这里出岔子（漏 DLL / 结构体对齐）
        from wbsync import winproc

        procs = winproc.find_by_stem()
        exe = winproc.primary_exe()
        lines.append(f"winproc: 枚举正常, 客户端进程 {len(procs)} 个")
        lines.append(f"winproc: exe={'（未运行）' if not exe else exe}")

        # 备份扫描：清理入口依赖它，目录遍历在打包环境里必须能跑
        from wbsync import guard as _guard

        backups = _guard.scan_backups()
        lines.append(
            f"backup: {len(backups)} 份, "
            f"{_guard.backup_total_bytes(backups) / 1048576:.1f} MB"
        )

        # 指纹缓存：命中判据靠 st_mtime_ns，打包环境里若 stat 不可用会静默退化
        from wbsync import fpcache as _fpcache

        cache = _fpcache.open_cache()
        lines.append(
            f"fpcache: {cache.total} 条, 文件 {cache.path.name}"
            + ("（可写）" if cache.path.parent.parent.is_dir() else "（目录缺失）")
        )

        # 物理删除：只读探测库与关联表，确认接口在打包环境里能跑
        from wbsync import purge as _purge

        lines.append(_purge.selftest_line())

        # 本地恢复：只读探测 sessions.deleted_at，确认「可恢复 N 条」算得出来
        from wbsync import restore as _restore

        lines.append(_restore.selftest_line())

        # 折叠偏好：只读不写 —— 自检不应该改用户的界面状态
        from wbsync import uistate as _uistate

        _collapsed = _uistate.load_collapsed()
        lines.append(f"uistate: 折叠偏好可读（当前收起 {len(_collapsed)} 组）")

        # 工作空间重映射：只读扫描，不碰任何数据
        from wbsync import rehome as _rehome

        lines.append(_rehome.selftest_line())

        # 映射记忆：只读探测文件是否存在 / 能否解析，不写记忆
        from wbsync import cwd_map as _cwd_map

        m = _cwd_map.load_map()
        lines.append(f"cwd_map: 记忆文件 {_cwd_map._map_path().name}, 已存 {len(m)} 条")

        # 账号切换链路：认证文件 / 凭据库 / 会话复制 / 记忆合并
        # 全部**只读探测** —— 自检绝不能改登录态，那是最不能碰的东西
        from wbsync import authfile as _authfile
        from wbsync import copysession as _copysession
        from wbsync import memorymerge as _memorymerge
        from wbsync import switch as _switch
        from wbsync import vault as _vault

        lines.append(_authfile.selftest_line())
        lines.append(_vault.selftest_line())
        lines.append(_copysession.selftest_line())
        lines.append(_memorymerge.selftest_line())
        lines.append(_switch.selftest_line())

        from wbsync import memory_pack as _memory_pack

        lines.append(_memory_pack.selftest_line())

        # 本机账号对齐（并集）与 settings.json 行为开关
        from wbsync import align as _align
        from wbsync import config_items as _ci

        lines.append(_align.selftest_line())
        _items = _ci.discover()
        _settings = [i for i in _items if i.container == _ci.CONTAINER_SETTINGS]
        lines.append(
            f"config_items: 共 {len(_items)} 项，其中 settings.json 行为开关 {len(_settings)} 项"
        )

        # 账号页与对齐页的构造能力：打包漏掉 ui 模块时，
        # 只有用户点到那一刻才会 ImportError —— 这里先建一次
        from wbsync.ui import accounts_page as _accounts
        from wbsync.ui import align_dialog as _align_dialog

        lines.append(
            f"ui: 账号页卡片式（两列自适应）/ 对齐页 "
            f"{'样式就绪' if not _align_dialog.align_qss() else '样式缺失'}"
        )

        # 同步包 v2 的解析链路（zipfile 双前缀 + 版式校验），
        # 用内存里造的最小包验证，不碰用户数据
        import json as _json
        import tempfile as _tempfile
        import zipfile as _zipfile

        from wbsync import pack as _pack

        probe_dir = Path(_tempfile.mkdtemp(prefix="wbsync-probe-"))
        probe_zip = probe_dir / "probe.wbsync.zip"
        with _zipfile.ZipFile(probe_zip, "w") as _z:
            _z.writestr(
                "manifest.json",
                _json.dumps(
                    {
                        "format": _pack.FORMAT_NAME,
                        "version": _pack.FORMAT_VERSION,
                        "sections": {},
                    }
                ),
            )
        try:
            _pack.read_package(probe_zip)
            lines.append("pack: FAILED —— 空包未被拒绝")
            ok = False
        except ValueError:
            lines.append(f"pack: v{_pack.FORMAT_VERSION} 解析链路正常（空包被拒）")
        finally:
            import shutil as _shutil

            _shutil.rmtree(probe_dir, ignore_errors=True)
    except Exception:
        ok = False
        lines.append("wbsync 核心 FAILED:\n" + traceback.format_exc())

    # 只建一个空 QWidget 证明不了什么 —— 真正要验的是两个界面能不能搭起来、
    # 表格和后台线程在裁剪过的 Qt 里是否仍然可用。
    try:
        import time as _time

        from PySide6.QtWidgets import QApplication, QWidget

        app = QApplication.instance() or QApplication([])
        probe = QWidget()
        probe.resize(20, 20)
        probe.show()
        app.processEvents()
        probe.close()
        lines.append("qt: 平台插件加载正常")

        from wbsync.ui.config_dialog import ConfigSyncDialog
        from wbsync.ui.main_window import MainWindow

        win = MainWindow()
        deadline = _time.time() + 25
        while win._inv is None and _time.time() < deadline:
            app.processEvents()
            _time.sleep(0.05)
        app.processEvents()
        lines.append(f"ui: 主界面 {win.table.rowCount()} 行，导航 {win.pages.count()} 页")
        lines.append(
            f"ui: 会话列表 {win.card_view.group_count} 组 / {len(win.card_view._cards)} 行"
        )

        cfg = ConfigSyncDialog(win)
        lines.append(f"ui: 配置界面 {cfg.table.rowCount()} 行")

        # 逐条选择器：打包漏掉 copy_pick_dialog 时，只有用户点下「逐条选择…」
        # 那一刻才会 ImportError —— 这里真建一次，代价一个空对话框
        from wbsync import copysession as _cs
        from wbsync.ui.copy_pick_dialog import CopyPickDialog as _Pick

        sample = _cs.CopyItem(
            cid="00000000-0000-4000-8000-000000000000",
            title="自检会话", cwd="C:/selftest", body_bytes=1, updated_at=1,
        )
        pick = _Pick([sample], "自检账号")
        # 用公开方法而不是内部字段：这里只验「建得起来、默认勾得上」，
        # 内部结构改名不该把自检打断（v1.6.4 就因为 _rows 改名红过一次）。
        pick_rows = len(pick.picked_cids())
        pick.close()
        lines.append(
            f"ui: 会话选择器 默认勾选 {pick_rows} 条 / "
            f"{'就绪' if pick_rows == 1 else '异常'}"
        )

        win.close()
        cfg.close()
    except Exception:
        ok = False
        lines.append("Qt/UI FAILED:\n" + traceback.format_exc())

    lines.insert(0, f"version: {__version__}")
    lines.insert(0, "RESULT: " + ("OK" if ok else "FAILED"))
    path = _write_log("wbsync-selftest.log", "\n".join(lines))
    if not ok:
        _write_log("wbsync-crash.log", "\n".join(lines))
    print("\n".join(lines))
    print(f"\n已写入 {path}")
    return 0 if ok else 1


def _set_taskbar_identity() -> None:
    """给进程一个独立的任务栏身份。

    不设 AppUserModelID 时，任务栏按 exe 路径分组：源码运行时全都归到
    python.exe 名下，图标也是 Python 的，看起来像「一个脚本」而不是程序。
    设了之后任务栏、跳转列表、固定到开始菜单都用应用自己的图标和名字。
    失败静默 —— 这只是观感，不该拦住启动。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Buddy-atlas")
    except Exception:  # noqa: BLE001 - 拿不到身份只影响观感
        pass


def main() -> int:
    _install_crash_log()
    # 放在最前：查询版本的场景不该去加载 Qt
    if "--version" in sys.argv or "-V" in sys.argv:
        return _run_version()
    if "--selftest" in sys.argv:
        return _run_selftest()

    from PySide6.QtWidgets import QApplication

    from wbsync.ui import icons
    from wbsync.ui.main_window import APP_NAME, MainWindow
    from wbsync.ui.theme import app_qss

    _set_taskbar_identity()

    app = QApplication(sys.argv)
    # 应用级图标：QMessageBox、文件对话框等自建窗口都从这里继承。
    # 只给主窗口 setWindowIcon 的话，那些对话框的标题栏会是空白的。
    app.setWindowIcon(icons.app_icon())
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyleSheet(app_qss())  # 全局样式，对话框一并生效
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
