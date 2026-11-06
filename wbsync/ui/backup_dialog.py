"""备份管理对话框 —— 页面形态的薄壳。

备份管理的主场是导航里的「备份与包」整页（`backup_page.BackupPage`）。
这个对话框只为「从别处临时看一眼备份」保留：内容与页面共用同一套实现，
避免两份表格各自演进出现口径差异。
"""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

from .backup_page import BackupPage


class BackupDialog(QDialog):
    def __init__(self, base=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("备份管理")
        self.resize(880, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        self.page = BackupPage(base=base, parent=self)
        # 页面自带页头，放进对话框时把标题留白收掉一点，避免上下双重标题感
        layout.addWidget(self.page, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.accept)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)

    @property
    def table(self):
        """转发给页面，保持既有调用可用。"""
        return self.page.table

    def reload(self):
        return self.page.reload()
