"""备份与包 —— 导航里的整页，不再是藏在菜单里的弹窗。

## 为什么从弹窗改成整页

备份管理是「清理空间」这类需要慢慢看的事：要看时间、看类型、看大小，
还要跨几次导入对比。弹窗天生是「看一眼就走」的形态，塞进表格 + 勾选 + 删除
之后，它已经不像弹窗了。放进左侧导航后，它的位置也才和「会话」「配置」对等 ——
备份不是附属功能，是这条数据链路上唯一可回退的地方。

## 删除的边界（与 guard 保持一致，这里不重复实现）

只允许删除 `wbsync-backup` 的**直接子目录**，拒绝符号链接、拒绝根目录本身。
页面只负责把「要删哪些、多大、在哪」摊开，让人确认。

## 为什么行尾是「打开」而不是「恢复」

恢复整份备份需要把库文件写回原位，那是写路径，必须走 guard 的三条铁律
（关客户端 → 备份当前 → 原子替换 → 校验可读）。目前「恢复」只发生在导入/修复
流程内部（`guard.Backup.restore()`），没有单独的「用这份备份覆盖现在」入口；
这里给一个说得清的「打开」，也不给一个点了不知道会发生什么的「恢复」。

## 全选为什么在表头

备份是一次导入留一份，攒到几十份时逐行点勾选是折磨。表头那一格空着也是空着，
于是把三态勾选框放在那儿：点一下全勾、再点一下全清、勾了一部分显示横杠。
它只在**这一列**响应点击，点「时间」「类型」这些列头不会误触。

判定「部分选中」必须自己数一遍 —— `QTableWidget` 不会替你把表头推到中间态，
所以每次勾选变化都由 `_sync_header()` 重新算，而不是让表头盯着 model。
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import guard
from .page_base import HeadedPage
from .theme import (
    BG_CARD,
    BORDER_STRONG,
    CONTENT_PADDING,
    PRIMARY,
    TEXT_SECONDARY,
)

# 第一列专门放勾选框（表头也在这列画全选框），列名留空。
# ★ 不再把勾选框塞进「备份」那一格：表头的全选框要画在列名左边，
#   两个控件挤在同一格只能靠缩进空格凑，字号一变就错位。
_COLUMNS = ["", "备份", "时间", "类型", "大小", "操作"]
C_CHECK, C_NAME, C_WHEN, C_KIND, C_SIZE, C_OP = range(6)

# 勾选列宽：14px 方框 + 两侧留白
_CHECK_COL_W = 40


class _CheckHeader(QHeaderView):
    """表头首列的三态勾选框 —— 全选 / 部分 / 未选。

    ## 为什么自己画，而不是塞一个 QCheckBox 进去

    `QTableWidget` 的表头没有 `setCellWidget`。把 QCheckBox 作为子控件浮在表头
    上，就得跟着列宽变化、横向滚动、DPI 缩放反复挪位置，而且会压住列名。
    这里只画一个 14px 的方框，位置由 `paintSection` 拿到的矩形直接算，
    缩放与滚动都跟着走，没有一处需要手动同步。

    背景与下边线仍交给 `super().paintSection()` —— 那样才和别的列头一样
    吃全局 QSS（底色、字号、下边框）。
    """

    toggled = Signal(bool)      # True = 请求全选，False = 请求全不选

    # 行内勾选框在 QSS 里是「14px 内容 + 1px 描边」= 16px 视觉。表头这边是自绘，
    # 方框尺寸要把那 1px 描边也算进去，否则并排看会小一圈。
    BOX = 15

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._state = Qt.CheckState.Unchecked
        self._hover = False
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        self.setMouseTracking(True)

    # ---------------------------------------------------------- 状态
    def check_state(self) -> Qt.CheckState:
        return self._state

    def set_check_state(self, state: Qt.CheckState) -> None:
        if state == self._state:
            return
        self._state = state
        self.updateSection(0)

    # ---------------------------------------------------------- 绘制
    def paintSection(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:  # noqa: N802
        super().paintSection(painter, rect, logicalIndex)
        if logicalIndex != 0:
            return
        box = QRect(0, 0, self.BOX, self.BOX)
        # 居中而不是左对齐：这一列只有 40px、列名是空的，居中后与下面每行的
        # 勾选框落在同一条竖线上。+2 是实测补差：行内框由 style 画在单元格
        # 内容左侧（列内 13–28px），而 `rect.center()` 在偶数宽度上会左偏半格。
        box.moveCenter(rect.center())
        box.adjust(0, 0, -1, -1)          # 右/下各让 1px，1px 描边才不被裁掉
        box.translate(2, 0)

        painter.save()
        # ★ 必须显式把裁剪区设成这一格：套了全局 QSS 之后，Qt 交到 paintSection
        #   手里的 painter 带着一个**空裁剪区**（clipBoundingRect 实测是 0×0），
        #   在这种 painter 上画什么都会被丢掉。表头全选框「看不见」不是颜色太淡，
        #   是根本没落到画布上 —— 2026-11-04 用离屏渲染逐像素核对确认。
        painter.setClipRect(rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._state == Qt.CheckState.Unchecked:
            # 悬停时描边换成主色：一个空心方框本身不说明「能点」
            painter.setPen(
                QPen(QColor(PRIMARY if self._hover else BORDER_STRONG), 1)
            )
            painter.setBrush(QColor(BG_CARD))
            painter.drawRoundedRect(box, 3, 3)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(PRIMARY))
            painter.drawRoundedRect(box, 3, 3)
            if self._state == Qt.CheckState.PartiallyChecked:
                # 只有「部分选中」画一道白杠。全选用纯绿块 —— 与行内勾选框
                # 保持同一套画法（QSS 的 indicator 没有对勾，表头也不该多一个）
                pen = QPen(QColor("#FFFFFF"), 2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                cy = box.center().y()
                painter.drawLine(box.left() + 4, cy, box.right() - 4, cy)
        painter.restore()

    # ---------------------------------------------------------- 悬停
    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        hover = self.logicalIndexAt(event.position().toPoint()) == 0
        if hover != self._hover:
            self._hover = hover
            self.updateSection(0)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._hover:
            self._hover = False
            self.updateSection(0)
        super().leaveEvent(event)

    # ---------------------------------------------------------- 点击
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            # 整格可点，不只是那 14px：点列头习惯了整格响应
            if self.logicalIndexAt(event.position().toPoint()) == 0:
                self.toggled.emit(self._state != Qt.CheckState.Checked)
                return
        super().mouseReleaseEvent(event)


def _fmt_size(n: int) -> str:
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _fmt_when(ts: float) -> str:
    """「今天 21:43 / 昨天 21:38 / 10-28 10:20」。

    精确到秒在这里没有意义：两份备份的差别靠「哪一天哪一刻」就够分辨了。
    """
    import datetime

    try:
        dt = datetime.datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return "时间未知"
    days = (datetime.date.today() - dt.date()).days
    if days == 0:
        return f"今天 {dt.strftime('%H:%M')}"
    if days == 1:
        return f"昨天 {dt.strftime('%H:%M')}"
    return dt.strftime("%m-%d %H:%M")


class BackupPage(HeadedPage):
    """备份盘点与清理。"""

    def __init__(self, base=None, parent=None) -> None:
        super().__init__(parent)
        self._base = base
        self._entries: list[guard.BackupEntry] = []
        # 建表/批量改勾选时用来挡住 itemChanged 的连锁重算
        self._loading = False
        self._build()
        self.reload()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(CONTENT_PADDING, 20, CONTENT_PADDING, 14)
        v.setSpacing(12)

        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(8)
        self.lbl_summary = QLabel()
        self.lbl_summary.setObjectName("StatusLine")
        tools.addWidget(self.lbl_summary)
        tools.addStretch(1)

        self.btn_open = QPushButton("打开备份目录")
        self.btn_open.clicked.connect(self._open_dir)
        tools.addWidget(self.btn_open)

        self.btn_refresh = QPushButton("重新扫描")
        self.btn_refresh.clicked.connect(self.reload)
        tools.addWidget(self.btn_refresh)

        self.btn_delete = QPushButton("清理选中备份")
        self.btn_delete.setObjectName("Primary")
        self.btn_delete.clicked.connect(self._delete)
        tools.addWidget(self.btn_delete)
        v.addLayout(tools)

        self.table = QTableWidget(0, len(_COLUMNS))
        # 表头换成自带全选框的版本 —— 必须在 setHorizontalHeaderLabels 之前装。
        self._head = _CheckHeader(self.table)
        self.table.setHorizontalHeader(self._head)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        # 纯白行叠在一起分不出行 —— 隔行底色是让「一堵白墙」变成列表最省的办法
        self.table.setAlternatingRowColors(True)
        hh = self._head
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setFixedHeight(44)
        hh.setSectionResizeMode(C_CHECK, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        for c in (C_WHEN, C_KIND, C_SIZE, C_OP):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(C_CHECK, _CHECK_COL_W)
        self.table.setColumnWidth(C_WHEN, 150)
        self.table.setColumnWidth(C_KIND, 140)
        self.table.setColumnWidth(C_SIZE, 110)
        self.table.setColumnWidth(C_OP, 100)
        self._head.toggled.connect(self._select_all_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.table, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(10)
        self.lbl_foot = QLabel("恢复前会先把当前状态再存一份，不会丢数据。")
        self.lbl_foot.setObjectName("HintLabel")
        foot.addWidget(self.lbl_foot, 1)
        self.lbl_foot_right = QLabel("")
        self.lbl_foot_right.setObjectName("HintLabel")
        self.lbl_foot_right.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        foot.addWidget(self.lbl_foot_right)
        v.addLayout(foot)

    # ------------------------------------------------------------ 数据
    def reload(self) -> None:
        self._entries = guard.scan_backups(self._base)
        # 建表期间会触发 itemChanged（每行一次），不挡住的话表头会被反复重算，
        # 还会在「还没建完」的状态下算出一个错的全选态
        self._loading = True
        try:
            self.table.setRowCount(len(self._entries))

            for i, e in enumerate(self._entries):
                self.table.setRowHeight(i, 44)

                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                check.setCheckState(Qt.CheckState.Unchecked)
                check.setToolTip("勾选后可清理；表头的方框是全选")
                self.table.setItem(i, C_CHECK, check)

                cell = QTableWidgetItem(e.path.name)
                cell.setToolTip(f"{e.path}\n{e.file_count} 个文件")
                self.table.setItem(i, C_NAME, cell)

                when = QTableWidgetItem(_fmt_when(e.created_at))
                when.setForeground(QColor(TEXT_SECONDARY))
                when.setToolTip(e.when_text)
                self.table.setItem(i, C_WHEN, when)

                kind = QTableWidgetItem(e.kind_text)
                kind.setForeground(QColor(TEXT_SECONDARY))
                self.table.setItem(i, C_KIND, kind)

                size = QTableWidgetItem(_fmt_size(e.size_bytes))
                size.setForeground(QColor(TEXT_SECONDARY))
                size.setToolTip(f"{e.file_count} 个文件")
                self.table.setItem(i, C_SIZE, size)

                btn = QPushButton("打开")
                btn.setObjectName("LinkButton")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip("在资源管理器里打开这份备份")
                btn.clicked.connect(lambda _c=False, p=e.path: self._open_one(p))
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(6, 0, 0, 0)
                hl.addWidget(btn)
                hl.addStretch(1)
                holder.setStyleSheet("background: transparent;")
                self.table.setCellWidget(i, C_OP, holder)
        finally:
            self._loading = False

        self._head.set_check_state(Qt.CheckState.Unchecked)
        total = guard.backup_total_bytes(self._entries)
        self._set_sub(
            "每次导入或修复前都会自动留一份，随时可以退回去" if self._entries
            else "还没有备份 —— 导入与修复前会自动留一份"
        )
        self.lbl_summary.setText(
            f"共 {len(self._entries)} 份备份 · 合计 {_fmt_size(total)}" if self._entries
            else "还没有备份"
        )
        self.lbl_foot_right.setText(
            f"备份目录：{guard.backup_dir(self._base)}" if self._entries else ""
        )
        has = bool(self._entries)
        self.table.setEnabled(has)
        self._refresh_selection()

    # ------------------------------------------------------------ 勾选
    def _set_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._loading = True
        try:
            for i in range(self.table.rowCount()):
                cell = self.table.item(i, C_CHECK)
                if cell is not None:
                    cell.setCheckState(state)
        finally:
            self._loading = False
        self._head.set_check_state(state)
        self._refresh_selection()

    def _select_all_clicked(self, on: bool) -> None:
        """表头方框被点：要么全勾，要么全清。"""
        self._set_all(on)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != C_CHECK:
            return
        self._sync_header()
        self._refresh_selection()

    def _sync_header(self) -> None:
        """按实际勾选数把表头推到三态之一。

        ★ 三态必须由这里算，不能让表头自己盯着 model：
          「部分选中」是 QTableWidget 不会自动给出的状态，只有数一遍才知道。
        """
        total = self.table.rowCount()
        checked = len(self._checked_rows())
        if checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked >= total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self._head.set_check_state(state)

    def _checked_rows(self) -> list[int]:
        rows = []
        for i in range(self.table.rowCount()):
            cell = self.table.item(i, C_CHECK)
            if cell is not None and cell.checkState() == Qt.CheckState.Checked:
                rows.append(i)
        return rows

    def _selected(self) -> list[guard.BackupEntry]:
        out: list[guard.BackupEntry] = []
        for i in self._checked_rows():
            if i < len(self._entries):
                out.append(self._entries[i])
        return out

    def _refresh_selection(self) -> None:
        """按钮跟着勾选数走：没勾就点不动，勾了就报出条数。"""
        n = len(self._checked_rows())
        if not self._entries:
            self.btn_delete.setText("清理选中备份")
            self.btn_delete.setEnabled(False)
            return
        self.btn_delete.setText(f"清理选中的 {n} 份" if n else "清理选中备份")
        self.btn_delete.setEnabled(n > 0)

    # ------------------------------------------------------------ 动作
    def _open_dir(self) -> None:
        root = guard.backup_dir(self._base)
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _open_one(self, path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _delete(self) -> None:
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "清理备份", "没有勾选任何备份。")
            return

        total = sum(e.size_bytes for e in sel)
        lines = [
            f"· {e.when_text}  {e.kind_text}  {_fmt_size(e.size_bytes)}\n  {e.path}"
            for e in sel
        ]
        detail = "\n".join(lines[:12])
        if len(lines) > 12:
            detail += f"\n…另有 {len(lines) - 12} 份"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认删除备份")
        box.setText(f"将永久删除 {len(sel)} 份备份，共 {_fmt_size(total)}。")
        all_of_them = len(sel) == len(self._entries)
        box.setInformativeText(
            f"{detail}\n\n删除后无法恢复。这些备份是导入前自动留下的还原点，"
            "只有在确定当前数据正常时才该删。"
            + ("\n\n⚠ 这一下会清空所有备份，一份都不剩。" if all_of_them else "")
        )
        b_yes = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        b_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_no)
        box.exec()
        if box.clickedButton() is not b_yes:
            return

        deleted, failed = guard.delete_backups([e.path for e in sel], self._base)
        self.reload()

        if failed:
            detail = "\n".join(f"· {p.name} — {why}" for p, why in failed[:10])
            QMessageBox.warning(
                self,
                "部分备份未能删除",
                f"已删除 {len(deleted)} 份，{len(failed)} 份失败：\n\n{detail}",
            )
        elif deleted:
            self.lbl_foot.setText(f"已删除 {len(deleted)} 份备份，释放 {_fmt_size(total)}。")
