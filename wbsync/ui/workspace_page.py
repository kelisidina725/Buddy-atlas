"""工作空间页 —— 把失效的 cwd 指回本机真实存在的目录。

## 为什么单独一页

它处理的对象不是「某一条会话」，而是「某个工作空间」：一个路径下可能挂着几条、
几十条会话，改一次全都跟着走。放进会话页的「更多」里会让它看起来像单条操作。

## 为什么只剩一张表（v1.6.11）

旧版是上下两张表（待修 / 已记录），同一件事被拆成两半：上面空了用户就问
「我上次设的映射呢」。设计稿把它们合成一张清单，用两段分组带隔开：
「需要修复」在上，「已记录的映射」在下，同一列位置、同一套读法。

## 为什么新路径是可以编辑的输入框

建议值只能按经验给（换用户名、盘符缺失时落到 `~/WorkBuddy/<末段>`），
但真实项目在哪只有用户知道 —— `D:/PycharmProjects/demo` 在本机可能就是
`H:/project/demo`，这个映射关系无法自动推断。所以每一行都得能改，
但未聚焦时它看起来只是一段文字（无边框输入框），不抢「修复」这个主动作。
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import cwd_map, rehome
from .page_base import HeadedPage
from .session_cards import fmt_when
from .theme import BG_FIELD, CONTENT_PADDING, TEXT_SECONDARY, TEXT_TERTIARY

_MEMORY_TAG = "color:#185FA5;background:#E6F1FB;font-size:11px;padding:1px 5px;border-radius:4px;"

C_OLD, C_NEW, C_WHEN, C_OP = range(4)
_COLUMNS = ["旧电脑上的路径", "本机对应", "最近出现", "操作"]


def _fmt_map_time(updated_at: int) -> str:
    """映射的更新时间。cwd_map 存的是秒，界面统一用毫秒口径的 fmt_when。"""
    try:
        return fmt_when(int(updated_at or 0) * 1000)
    except (TypeError, ValueError):
        return "—"


class _EmptyTable(QTableWidget):
    """行数为 0 时在空白区居中写一句「还没有……」的表格。

    ## 为什么在 paintEvent 里画，而不是浮一个 QLabel 上去

    视口尺寸会在「滚动条出现」这类变化里晚一步定稿：Resize 事件送达时视口还是
    850×389，事件跑完才变成 850×368，于是手动 `setGeometry` 会留下 21px 偏差；
    给视口装布局也一样，布局只在那一次 Resize 时算过，之后不再重排（实测）。
    而 `paintEvent` 里的 rect 永远是当下的正确值，画出来就不会错位。

    一张白表既像「界面坏了」，也像「我上次设的映射丢了」—— 空状态必须说话。
    """

    empty_title = ""
    empty_hint = ""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().paintEvent(event)
        if self.rowCount() or not self.empty_title:
            return
        area = self.viewport().rect()
        f_title = QFont(self.font())
        f_title.setPixelSize(13)
        f_hint = QFont(self.font())
        f_hint.setPixelSize(11)
        h_title = QFontMetrics(f_title).height()
        h_hint = QFontMetrics(f_hint).height()
        gap = 6
        top = area.center().y() - (h_title + gap + h_hint) // 2

        p = QPainter(self.viewport())
        try:
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            # 窗口拉窄时按可用宽度省略，而不是让文字在两边被硬切掉
            room = max(60, area.width() - 24)
            p.setFont(f_title)
            p.setPen(QColor(TEXT_SECONDARY))
            p.drawText(
                QRect(area.left(), top, area.width(), h_title),
                Qt.AlignmentFlag.AlignCenter,
                QFontMetrics(f_title).elidedText(
                    self.empty_title, Qt.TextElideMode.ElideRight, room
                ),
            )
            p.setFont(f_hint)
            p.setPen(QColor(TEXT_TERTIARY))
            p.drawText(
                QRect(area.left(), top + h_title + gap, area.width(), h_hint),
                Qt.AlignmentFlag.AlignCenter,
                QFontMetrics(f_hint).elidedText(
                    self.empty_hint, Qt.TextElideMode.ElideRight, room
                ),
            )
        finally:
            p.end()


class WorkspacePage(HeadedPage):
    """失效工作空间清单 + 批量重映射。"""

    fixed = Signal()  # 修完之后让主窗口重新盘点

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._issues: list[rehome.SpaceIssue] = []
        self._mappings: list[tuple[str, dict]] = []
        self._edits: dict[str, QLineEdit] = {}
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

        self.btn_rescan = QPushButton("重新扫描")
        self.btn_rescan.clicked.connect(self.reload)
        tools.addWidget(self.btn_rescan)

        self.btn_fix = QPushButton("修复全部")
        self.btn_fix.setObjectName("Primary")
        self.btn_fix.clicked.connect(self._apply)
        tools.addWidget(self.btn_fix)
        v.addLayout(tools)

        self.table = _EmptyTable(0, len(_COLUMNS))
        self.table.empty_title = "还没有路径映射"
        self.table.empty_hint = (
            "导入同步包时改过的落点、修复成功的工作空间，都会记在这里，下次自动带出"
        )
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setFixedHeight(44)
        hh.setSectionResizeMode(C_OLD, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_NEW, QHeaderView.ResizeMode.Stretch)
        for c in (C_WHEN, C_OP):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(C_WHEN, 130)
        self.table.setColumnWidth(C_OP, 90)
        v.addWidget(self.table, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 10, 0, 0)
        foot.setSpacing(10)
        # 这行先说「这件事是干什么的」，再说操作前提
        self.lbl_foot = QLabel(
            "路径对不上时，会话就找不到内容；「修复」会按文件夹名在本机重新匹配。"
        )
        self.lbl_foot.setObjectName("HintLabel")
        foot.addWidget(self.lbl_foot, 1)
        self.lbl_state = QLabel()
        self.lbl_state.setObjectName("HintLabel")
        self.lbl_state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        foot.addWidget(self.lbl_state)
        v.addLayout(foot)

    # ---------------------------------------------------------- 数据
    def reload(self) -> None:
        self._issues = rehome.scan()
        self._edits = {}
        self._load_mappings()

        total_rows = len(self._issues) + len(self._mappings)
        bands = int(bool(self._issues)) + int(bool(self._mappings))
        self.table.clearSpans()
        self.table.setRowCount(total_rows + bands)

        r = 0
        if self._issues:
            r = self._band(r, f"需要修复 · {len(self._issues)} 个")
            for issue in self._issues:
                self._issue_row(r, issue)
                r += 1
        if self._mappings:
            r = self._band(r, f"已记录的映射 · {len(self._mappings)} 条")
            for src, entry in self._mappings:
                self._mapping_row(r, src, entry)
                r += 1
        self.table.setRowCount(r)
        # 空表说话：行数归零时由 paintEvent 补一句提示，有内容时自然不画
        self.table.viewport().update()

        n_issue = len(self._issues)
        n_map = len(self._mappings)
        sess = sum(i.session_count for i in self._issues)
        if n_issue:
            self._set_sub(f"{n_issue + n_map} 个路径映射 · {n_issue} 个对不上，需要修复")
            self.lbl_summary.setText(
                f"共 {n_issue + n_map} 个路径映射 · {n_issue} 个对不上，需要修复"
                f"（涉及 {sess} 条会话）"
            )
        elif n_map:
            self._set_sub(f"{n_map} 条映射已记录 · 所有会话的路径在本机都存在")
            self.lbl_summary.setText(f"共 {n_map} 个路径映射 · 全部已对上")
        else:
            self._set_sub("所有会话的工作空间都指向本机存在的目录")
            self.lbl_summary.setText("还没有路径映射 · 所有会话的工作空间都指向本机存在的目录")

        self.btn_fix.setEnabled(n_issue > 0)
        self.btn_fix.setText(f"修复全部 {n_issue} 个" if n_issue else "修复全部")
        self.lbl_state.setText(f"共 {n_issue + n_map} 个映射" if (n_issue or n_map) else "")

    def _band(self, row: int, text: str) -> int:
        band = QTableWidgetItem(text)
        band.setFlags(Qt.ItemFlag.NoItemFlags)
        band.setBackground(QColor(BG_FIELD))
        band.setForeground(QColor(TEXT_TERTIARY))
        self.table.setSpan(row, 0, 1, len(_COLUMNS))
        self.table.setItem(row, 0, band)
        self.table.setRowHeight(row, 30)
        return row + 1

    def _issue_row(self, row: int, issue: rehome.SpaceIssue) -> None:
        self.table.setRowHeight(row, 44)
        old = QTableWidgetItem(issue.cwd)
        old.setToolTip("\n".join(issue.raw_values))
        old.setForeground(QColor(TEXT_SECONDARY))
        self.table.setItem(row, C_OLD, old)

        edit = QLineEdit(cwd_map.suggest_new_cwd(issue.cwd))
        edit.setObjectName("InlineEdit")
        edit.setPlaceholderText("留空则不处理")
        edit.setToolTip("可以改成任意本机路径；留空表示这次不修")
        self._edits[issue.cwd] = edit
        cell = QWidget()
        cl = QHBoxLayout(cell)
        cl.setContentsMargins(4, 0, 8, 0)
        cl.setSpacing(6)
        cl.addWidget(edit, 1)
        if cwd_map.has(issue.cwd):
            tag = QLabel("记忆")
            tag.setStyleSheet(_MEMORY_TAG)
            cl.addWidget(tag)
        cell.setStyleSheet("background: transparent;")
        self.table.setCellWidget(row, C_NEW, cell)

        seen = int(getattr(issue, "last_seen", 0) or 0)
        when = QTableWidgetItem(fmt_when(seen) if seen else "—")
        when.setForeground(QColor(TEXT_TERTIARY))
        self.table.setItem(row, C_WHEN, when)

        btn = QPushButton("修复")
        btn.setObjectName("LinkButton")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("把这个路径的所有会话指到左边的本机目录")
        btn.clicked.connect(lambda _c=False, i=issue: self._fix_one(i))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(6, 0, 0, 0)
        hl.addWidget(btn)
        hl.addStretch(1)
        holder.setStyleSheet("background: transparent;")
        self.table.setCellWidget(row, C_OP, holder)

    def _mapping_row(self, row: int, src: str, entry: dict) -> None:
        self.table.setRowHeight(row, 44)
        old = QTableWidgetItem(src)
        old.setToolTip(src)
        old.setForeground(QColor(TEXT_SECONDARY))
        self.table.setItem(row, C_OLD, old)
        target = QTableWidgetItem(entry.get("target", ""))
        target.setToolTip(entry.get("target", ""))
        target.setForeground(QColor(TEXT_SECONDARY))
        self.table.setItem(row, C_NEW, target)
        when = QTableWidgetItem(_fmt_map_time(entry.get("updated_at", 0)))
        when.setForeground(QColor(TEXT_TERTIARY))
        self.table.setItem(row, C_WHEN, when)

        btn = QPushButton("忘记")
        btn.setObjectName("LinkMuted")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("从本地记忆里删掉这条映射（不影响会话数据）")
        btn.clicked.connect(lambda _c=False, s=src: self._forget(s))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(6, 0, 0, 0)
        hl.addWidget(btn)
        hl.addStretch(1)
        holder.setStyleSheet("background: transparent;")
        self.table.setCellWidget(row, C_OP, holder)

    def _load_mappings(self) -> None:
        """把 cwd_map 里记着的映射反显出来（排除上面已经列成待修的那些）。"""
        entries = cwd_map.load_map()
        shown = {i.cwd for i in self._issues}
        rows = [(k, v) for k, v in entries.items() if k not in shown]
        rows.sort(key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
        self._mappings = rows

    def _forget(self, source_cwd: str) -> None:
        cwd_map.forget(source_cwd)
        self.reload()

    # ---------------------------------------------------------- 目标
    def _target_for(self, issue: rehome.SpaceIssue) -> rehome.RehomeTarget | None:
        edit = self._edits.get(issue.cwd)
        if edit is None:
            return None
        new = edit.text().strip().replace("\\", "/")
        if not new:
            return None
        return rehome.RehomeTarget(issue=issue, new_cwd=new)

    def selected_targets(self) -> list[rehome.RehomeTarget]:
        out: list[rehome.RehomeTarget] = []
        for issue in self._issues:
            t = self._target_for(issue)
            if t is not None:
                out.append(t)
        return out

    # ---------------------------------------------------------- 执行
    def _fix_one(self, issue: rehome.SpaceIssue) -> None:
        target = self._target_for(issue)
        if target is None:
            QMessageBox.information(self, "修复工作空间", "这一行的本机路径是空的，先填一个再修。")
            return
        self._run([target])

    def _apply(self) -> None:
        targets = self.selected_targets()
        if not targets:
            QMessageBox.information(self, "修复工作空间", "没有可修的项：本机对应那一列都是空的。")
            return
        self._run(targets)

    def _run(self, targets: list[rehome.RehomeTarget]) -> None:
        plan = rehome.plan_rehome(targets)
        lines = [f"· {t.issue.cwd}  →  {t.new_cwd}（{t.issue.session_count} 条会话）" for t in targets]
        running = bool(__import__("wbsync.winproc", fromlist=["x"]).is_running())

        box = QMessageBox(self)
        box.setWindowTitle("修复工作空间")
        box.setText(f"要重映射 {len(targets)} 个工作空间吗？")
        body = lines[:8] + (["· …"] if len(lines) > 8 else [])
        body.append("")
        body.append("会同时把正文目录改名；写库前会备份会话库。")
        if running:
            body.append("WorkBuddy 正在运行，需要先关闭它。")
        box.setInformativeText("\n".join(body))
        ok = box.addButton("关闭并修复" if running else "修复", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(ok)
        box.exec()
        if box.clickedButton() is not ok:
            return

        if running:
            QMessageBox.information(
                self,
                "请先退出 WorkBuddy",
                "这次修复需要写会话库，必须完全退出客户端后再点一次「修复」。\n"
                "（自动关闭并恢复的流程尚未接上，避免在路径层面出问题时连客户端一起带走。）",
            )
            return

        report = rehome.execute_rehome(plan)
        # 修成功的映射记进本地记忆：下次再遇到同一源路径直接带出
        for t in targets:
            if t.issue.cwd in report.applied:
                cwd_map.remember(t.issue.cwd, t.new_cwd)
        msg = [report.summary, ""]
        if report.moved_dirs:
            msg.append("正文目录迁移：" + "、".join(f"{a} → {b}" for a, b in report.moved_dirs))
        if report.created_dirs:
            msg.append(f"新建目录 {len(report.created_dirs)} 个")
        for name, why in report.failed:
            msg.append(f"失败：{name} —— {why}")
        for w in report.warnings:
            msg.append(f"· {w}")
        (QMessageBox.information if report.ok else QMessageBox.warning)(
            self, "工作空间修复", "\n".join(m for m in msg if m != "")
        )
        self.reload()
        self.fixed.emit()
