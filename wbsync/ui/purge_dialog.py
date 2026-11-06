"""物理删除确认对话框 —— 删除前唯一一次「停下来想清楚」。

## 为什么要分级确认

删除已删除的会话是清理，删活着的会话是破坏。两者不该同样轻松：
全为已删除 → 勾一下「同时备份」即可；只要含一条存活，就必须手打「删除」。
输入框是物理上的摩擦，防止连点两下把正在用的对话删掉。

## 为什么备份默认不勾

备份会把库和正文再复制一份，占用与正文同量级的磁盘。
用户要的往往是「彻底清掉」，默认不备份更贴合意图；
但恢复只能靠它，所以选项要显眼、要写明「不勾选则不可恢复」。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..purge import PurgePlan
from .theme import DANGER, DANGER_SOFT, TEXT_SECONDARY

CONFIRM_WORD = "删除"

_COLUMNS = ["标题", "类型", "正文"]
C_TITLE, C_KIND, C_SIZE = range(3)


def _fmt_size(n: int) -> str:
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _stat_box(label: str, value: str) -> QFrame:
    box = QFrame()
    box.setObjectName("Card")
    v = QVBoxLayout(box)
    v.setContentsMargins(10, 8, 10, 8)
    v.setSpacing(2)
    cap = QLabel(label)
    cap.setStyleSheet(f"font-size:11px;color:{TEXT_SECONDARY};")
    num = QLabel(value)
    num.setStyleSheet("font-size:18px;font-weight:500;")
    v.addWidget(cap)
    v.addWidget(num)
    return box


class PurgeDialog(QDialog):
    """确认物理删除。exec()==Accepted 表示用户已确认，backup 属性是备份选择。"""

    def __init__(self, plan: PurgePlan, parent=None, cloud_only: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("物理删除会话")
        self.resize(640, 540)
        self._plan = plan
        # 云端会话不进 plan（删行会被客户端重新拉回），改由调用方报「将隐藏」的条数
        self._cloud_only = int(cloud_only or 0)
        self._backup = False
        self._build()
        self._sync()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)

        n = len(self._plan.live)
        head = QLabel(f"将从本机永久移除 {n} 条会话的元数据与正文。")
        head.setStyleSheet("font-size:14px;font-weight:500;")
        layout.addWidget(head)

        # ---- 统计
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_stat_box("本地会话", str(self._plan.local_count)))
        row.addWidget(
            _stat_box("云端会话·改为隐藏", str(self._plan.cloud_count + self._cloud_only))
        )
        row.addWidget(_stat_box("正文字节", _fmt_size(self._plan.total_body_bytes)))
        layout.addLayout(row)

        # ---- 风险提示
        tips = [
            "删除后不可撤销：元数据行与关联行一并移除",
            "云端会话删行会被客户端重新拉回，因此不删行、只在本工具里隐藏",
            "不勾选备份时，正文删除后无法恢复",
        ]
        warn = QLabel("<br>".join(f"• {t}" for t in tips))
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"background:{DANGER_SOFT};color:{DANGER};border:1px solid #F0C6C8;"
            "border-radius:8px;padding:9px 12px;font-size:12px;"
        )
        layout.addWidget(warn)

        missing = sum(1 for t in self._plan.targets if t.missing)
        if missing:
            skip = QLabel(f"另有 {missing} 条在数据库里已不存在，将自动跳过。")
            skip.setStyleSheet(f"font-size:11px;color:{TEXT_SECONDARY};")
            layout.addWidget(skip)

        # ---- 明细
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_TITLE, QHeaderView.ResizeMode.Stretch)
        for c in (C_KIND, C_SIZE):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMaximumHeight(190)
        self._fill_table()
        layout.addWidget(self.table, 1)

        # ---- 备份选项
        self.chk_backup = QCheckBox("同时备份到备份区（可恢复，默认不备份）")
        self.chk_backup.setChecked(False)
        self.chk_backup.setToolTip(
            "勾选后先把数据库与正文复制到 wbsync-backup，再执行删除。\n"
            "会占用与正文相近的额外空间；不勾选则删除后不可恢复。"
        )
        layout.addWidget(self.chk_backup)

        # ---- 分级确认
        self.row_confirm = QWidget()
        hl = QHBoxLayout(self.row_confirm)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        self.lbl_confirm = QLabel(f"含存活会话，请输入「{CONFIRM_WORD}」以确认：")
        self.lbl_confirm.setStyleSheet(f"font-size:12px;color:{DANGER};")
        self.txt_confirm = QLineEdit()
        self.txt_confirm.setFixedWidth(120)
        self.txt_confirm.setPlaceholderText(CONFIRM_WORD)
        self.txt_confirm.textChanged.connect(self._sync)
        self.txt_confirm.returnPressed.connect(self._on_ok)
        hl.addWidget(self.lbl_confirm)
        hl.addWidget(self.txt_confirm)
        hl.addStretch(1)
        self.row_confirm.setVisible(self._plan.has_alive)
        layout.addWidget(self.row_confirm)

        layout.addStretch(0)

        # ---- 按钮
        self.buttons = QDialogButtonBox()
        self.btn_ok = self.buttons.addButton("物理删除", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.btn_cancel = self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_ok.setObjectName("Danger")
        self.btn_ok.setAutoDefault(False)
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.setDefault(True)
        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_cancel.clicked.connect(self.reject)
        layout.addWidget(self.buttons)

    def _fill_table(self) -> None:
        live = self._plan.live
        self.table.setRowCount(len(live))
        for i, t in enumerate(live):
            self.table.setItem(i, C_TITLE, QTableWidgetItem(t.title or "(无标题)"))
            self.table.item(i, C_TITLE).setToolTip(t.cid)

            kind = QTableWidgetItem(t.kind_text)
            if t.is_cloud:
                kind.setForeground(Qt.GlobalColor.darkYellow)
            elif t.deleted:
                kind.setForeground(Qt.GlobalColor.gray)
            self.table.setItem(i, C_KIND, kind)

            self.table.setItem(
                i, C_SIZE, QTableWidgetItem(_fmt_size(t.body_bytes) if t.body_paths else "无正文")
            )

    # ------------------------------------------------------------ 校验
    def _typed_ok(self) -> bool:
        if not self._plan.has_alive:
            return True
        return self.txt_confirm.text().strip() == CONFIRM_WORD

    def _sync(self) -> None:
        self.btn_ok.setEnabled(self._typed_ok() and not self._plan.empty)

    def _on_ok(self) -> None:
        if not self._typed_ok():
            self.txt_confirm.setFocus()
            return
        if self._plan.empty:
            return
        self._backup = self.chk_backup.isChecked()
        self.accept()

    # ------------------------------------------------------------ 结果
    @property
    def backup(self) -> bool:
        return self._backup
