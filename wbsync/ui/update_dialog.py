"""「检查更新」对话框。

两个动作按用户实际想做的事分开：想直接拿安装包就点「下载新版」，
想先看发布说明或翻历史版本就点「打开 Release 页面」。下载只写到用户
自己选的位置，不替换正在运行的 exe，也不自动重启。

查询和下载都放在 QThread 里。网络慢或者连不上时，主界面不能跟着卡住；
失败时始终留着「打开 Release 页面」这条手动兜底。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .. import __version__, updater
from .theme import TEXT_PRIMARY, TEXT_SECONDARY

_NOTES_LIMIT = 1600


class _CheckWorker(QThread):
    ok = Signal(object)
    bad = Signal(str)

    def run(self) -> None:  # noqa: D102 - QThread 入口
        try:
            self.ok.emit(updater.fetch_latest())
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成界面上的一句话
            self.bad.emit(str(exc) or type(exc).__name__)


class _DownloadWorker(QThread):
    progressed = Signal(int, int)
    ok = Signal(str)
    bad = Signal(str)

    def __init__(self, info: updater.ReleaseInfo, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.info = info
        self.dest = dest
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D102 - QThread 入口
        try:
            path = updater.download_file(
                self.info,
                self.dest,
                progress=lambda done, total: self.progressed.emit(done, total),
                cancelled=lambda: self._cancel,
            )
            self.ok.emit(str(path))
        except Exception as exc:  # noqa: BLE001
            self.bad.emit(str(exc) or type(exc).__name__)


class UpdateDialog(QDialog):
    """查一次最新版本，然后决定要不要下载。

    只在用户点开这个对话框时才联网，启动程序、盘点、导入导出都不触发。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("检查更新")
        self.resize(560, 440)
        self._info: updater.ReleaseInfo | None = None
        self._checker: _CheckWorker | None = None
        self._downloader: _DownloadWorker | None = None
        self._saved: Path | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        self.lbl_current = QLabel(f"当前版本  v{__version__}")
        self.lbl_current.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        root.addWidget(self.lbl_current)

        self.lbl_state = QLabel("正在检查更新…")
        self.lbl_state.setWordWrap(True)
        self.lbl_state.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 15px;")
        root.addWidget(self.lbl_state)

        self.lbl_detail = QLabel("")
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        root.addWidget(self.lbl_detail)

        self.notes = QTextBrowser()
        self.notes.setReadOnly(True)
        self.notes.setOpenExternalLinks(False)
        self.notes.setPlainText("")
        self.notes.setVisible(False)
        root.addWidget(self.notes, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)
        root.addWidget(self.progress)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_download = QPushButton("下载新版")
        self.btn_download.setObjectName("Primary")
        self.btn_download.setVisible(False)
        self.btn_download.clicked.connect(self._start_download)
        row.addWidget(self.btn_download)

        self.btn_page = QPushButton("打开 Release 页面")
        self.btn_page.clicked.connect(self._open_page)
        row.addWidget(self.btn_page)

        # 国内拉 GitHub 附件常常只有几十 KB/s。程序内下载慢的时候，
        # 把直链交给浏览器往往更快，也不至于让人对着进度条干等。
        self.btn_browser = QPushButton("用浏览器下载")
        self.btn_browser.setVisible(False)
        self.btn_browser.clicked.connect(self._open_asset)
        row.addWidget(self.btn_browser)

        self.btn_folder = QPushButton("打开所在文件夹")
        self.btn_folder.setVisible(False)
        self.btn_folder.clicked.connect(self._open_folder)
        row.addWidget(self.btn_folder)

        self.btn_retry = QPushButton("重试")
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(self._check)
        row.addWidget(self.btn_retry)

        self.btn_cancel = QPushButton("取消下载")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel_download)
        row.addWidget(self.btn_cancel)

        row.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_close)
        root.addLayout(row)

        self._check()

    # ------------------------------------------------------------ 检查

    def _check(self) -> None:
        if self._checker is not None and self._checker.isRunning():
            return
        self._info = None
        self._saved = None
        self.lbl_state.setText("正在检查更新…")
        self.lbl_detail.setText(f"正在访问 {updater.LATEST_API}")
        self.notes.setVisible(False)
        self.progress.setVisible(False)
        self.btn_download.setVisible(False)
        self.btn_browser.setVisible(False)
        self.btn_folder.setVisible(False)
        self.btn_retry.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_close.setEnabled(True)
        self._checker = _CheckWorker(self)
        self._checker.ok.connect(self._on_checked)
        self._checker.bad.connect(self._on_check_failed)
        self._checker.start()

    def _on_checked(self, info: object) -> None:
        if not isinstance(info, updater.ReleaseInfo):  # pragma: no cover - 防御
            self._on_check_failed("检查结果格式不对。")
            return
        self._info = info
        self.lbl_detail.setText("")
        self.notes.setPlainText(info.notes[:_NOTES_LIMIT].strip() or "这个版本没有写发布说明。")
        self.notes.setVisible(True)

        local = updater.parse_version(__version__) or (0, 0, 0)
        if updater.is_newer(info.version, local):
            label = info.version and ".".join(str(x) for x in info.version)
            self.lbl_state.setText(f"发现新版本 v{label or info.tag}")
            self.btn_download.setVisible(True)
            self.btn_download.setEnabled(True)
            self.btn_download.setText(
                f"下载新版（{updater.format_size(info.asset_size)}）"
                if info.asset_size else "下载新版"
            )
        elif info.version is None:
            self.lbl_state.setText(f"最新版本：{info.tag}")
            self.lbl_detail.setText("这个标签不是数字版本号，无法自动比较，可以到 Release 页面确认。")
        else:
            self.lbl_state.setText(f"已是最新版本 v{__version__}")
            self.lbl_detail.setText("想看历史版本或发布说明，打开 Release 页面。")

    def _on_check_failed(self, reason: str) -> None:
        self.lbl_state.setText("检查更新没成功")
        self.lbl_detail.setText(reason)
        self.notes.setVisible(False)
        self.btn_retry.setVisible(True)

    # ------------------------------------------------------------ 下载

    def _start_download(self) -> None:
        info = self._info
        if info is None or not info.has_asset:
            self._open_page()
            return
        downloads = Path.home() / "Downloads"
        base = downloads if downloads.is_dir() else Path.home()
        dest, _ = QFileDialog.getSaveFileName(
            self, "保存新版", str(base / info.asset_name), "可执行文件 (*.exe)"
        )
        if not dest:
            return
        self._saved = None
        self.lbl_state.setText("正在下载…")
        self.lbl_detail.setText(
            "下载完成后会核对 SHA256，不会替换正在运行的程序。太慢就点「用浏览器下载」。"
        )
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.btn_download.setEnabled(False)
        self.btn_page.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.btn_browser.setVisible(True)
        self.btn_retry.setVisible(False)
        self._downloader = _DownloadWorker(info, Path(dest), self)
        self._downloader.progressed.connect(self._on_progress)
        self._downloader.ok.connect(self._on_downloaded)
        self._downloader.bad.connect(self._on_download_failed)
        self._downloader.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, int(done * 100 / total)))
            self.lbl_state.setText(
                f"正在下载… {updater.format_size(done)} / {updater.format_size(total)}"
            )
        else:
            self.progress.setRange(0, 0)
            self.lbl_state.setText(f"正在下载… {updater.format_size(done)}")

    def _finish_download_ui(self) -> None:
        self.progress.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setEnabled(True)
        self.btn_download.setEnabled(True)
        self.btn_page.setEnabled(True)
        self.btn_close.setEnabled(True)

    def _on_downloaded(self, path: str) -> None:
        self._finish_download_ui()
        self._saved = Path(path)
        self.lbl_state.setText("下载完成")
        self.lbl_detail.setText(f"已保存到 {path}，关掉程序后手动替换即可。")
        self.btn_folder.setVisible(True)
        self.btn_download.setVisible(False)
        self.btn_browser.setVisible(False)
        QMessageBox.information(
            self, "下载完成",
            f"新版已保存到：\n{path}\n\n下载前核对过 Release 上登记的 SHA256。"
            "程序不会自动替换正在运行的 exe，请自行关闭后替换。",
        )

    def _on_download_failed(self, reason: str) -> None:
        self._finish_download_ui()
        self.lbl_state.setText("下载没完成")
        self.lbl_detail.setText(f"{reason} 也可以重试，或交给浏览器下载。")
        self.btn_retry.setVisible(False)
        self.btn_browser.setVisible(True)

    def _cancel_download(self) -> None:
        if self._downloader is not None:
            self._downloader.cancel()
        self.lbl_state.setText("正在取消…")
        self.btn_cancel.setEnabled(False)

    # ------------------------------------------------------------ 外部入口

    def _open_page(self) -> None:
        url = self._info.page_url if self._info else updater.RELEASES_PAGE
        QDesktopServices.openUrl(QUrl(url))

    def _open_asset(self) -> None:
        info = self._info
        url = info.asset_url if info and info.has_asset else updater.RELEASES_PAGE
        QDesktopServices.openUrl(QUrl(url))

    def _open_folder(self) -> None:
        if self._saved is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._saved.parent)))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        for worker in (self._downloader, self._checker):
            if worker is not None and worker.isRunning():
                if isinstance(worker, _DownloadWorker):
                    worker.cancel()
                worker.wait(3000)
        super().closeEvent(event)
