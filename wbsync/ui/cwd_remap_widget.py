"""工作空间映射页签控件 —— 在两个导入对话框里复用。

只负责「展示 + 收集勾选」，不碰任何数据；真正的改路径 / 移正文交给
`importer.execute_import`（它接收这里收集到的映射）。

列：勾选 · 源工作空间（本机不存在）· 会话数 · 改为（本机路径，记忆项带蓝标）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import rehome
from ..importer import CwdRemapPlan

C_CHECK, C_OLD, C_COUNT, C_NEW = range(4)

_MEMORY_TAG_STYLE = (
    "color:#185FA5;background:#E6F1FB;font-size:11px;"
    "padding:2px 6px;border-radius:4px;"
)


class CwdRemapWidget(QWidget):
    """失效源 cwd → 本机落点 的批量勾选表。"""

    def __init__(self, remaps: list[CwdRemapPlan], parent=None) -> None:
        super().__init__(parent)
        self._remaps: list[CwdRemapPlan] = list(remaps)
        self._edits: dict[str, QLineEdit] = {}
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(6)

        if not self._remaps:
            lab = QLabel("包内所有会话的工作空间在本机都存在，无需映射。")
            lab.setObjectName("HintLabel")
            v.addWidget(lab)
            return

        note = QLabel(
            "这些会话的源工作空间在本机不存在；勾选后会直接导入为本机路径，不产生死工作区。"
            "蓝标「记忆」= 上次在此机设过，可直接用也可改。"
        )
        note.setObjectName("HintLabel")
        note.setWordWrap(True)
        v.addWidget(note)

        table = QTableWidget(len(self._remaps), 4)
        self.table = table
        table.setHorizontalHeaderLabels(
            ["", "源工作空间（本机不存在）", "会话", "改为（本机路径）"]
        )
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        hh = table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionResizeMode(C_CHECK, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(C_CHECK, 34)
        hh.setSectionResizeMode(C_COUNT, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(C_COUNT, 64)
        hh.setSectionResizeMode(C_OLD, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_NEW, QHeaderView.ResizeMode.Stretch)

        for r, m in enumerate(self._remaps):
            table.setRowHeight(r, 34)

            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            # 默认全部勾选：导入即修好是主诉求
            check.setCheckState(Qt.CheckState.Checked)
            table.setItem(r, C_CHECK, check)

            old = QLabel(m.source_cwd)
            old.setToolTip(m.source_cwd)
            table.setCellWidget(r, C_OLD, old)

            cnt = QLabel(str(m.session_count))
            cnt.setObjectName("HintLabel")
            table.setCellWidget(r, C_COUNT, cnt)

            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(6)
            edit = QLineEdit(m.suggested_target)
            edit.setPlaceholderText("留空则不映射")
            cl.addWidget(edit, 1)
            if m.from_memory:
                tag = QLabel("记忆")
                tag.setStyleSheet(_MEMORY_TAG_STYLE)
                cl.addWidget(tag)
            table.setCellWidget(r, C_NEW, cell)
            self._edits[m.source_cwd] = edit

        v.addWidget(table, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_all = QPushButton("全选")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none = QPushButton("全不选")
        b_none.clicked.connect(lambda: self._set_all(False))
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch(1)
        v.addLayout(row)

    def _set_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            it = self.table.item(r, C_CHECK)
            if it is not None:
                it.setCheckState(state)

    def choices(self) -> dict[str, str]:
        """返回勾选且非空的映射：归一化源 cwd → 本机 cwd。"""
        out: dict[str, str] = {}
        for r, m in enumerate(self._remaps):
            it = self.table.item(r, C_CHECK)
            edit = self._edits.get(m.source_cwd)
            if it is None or it.checkState() != Qt.CheckState.Checked or edit is None:
                continue
            new = edit.text().strip().replace("\\", "/")
            if not new:
                continue
            out[rehome.normalize(m.source_cwd)] = new
        return out
