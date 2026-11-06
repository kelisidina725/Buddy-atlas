"""详情面板 —— 右栏，只谈「当前选中的这一条」。

## 三个动作按危险程度从上往下排

恢复（浅绿，主）→ 导出（普通）→ 物理清除（红色文字）。
顺序不是审美问题：用户的手指是从上往下扫的，越危险的动作越该在最后。

## 未选中时整条危险区不渲染

旧版把「物理清除选中会话…」放在页头的通用「更多」菜单里，没选中任何会话时
它照样可点，点下去才提示「请先选中一条」—— 一个点了才知道是空壳的入口，
迟早会被人当成「清除全部」。现在它只活在详情面板里：没有选中对象，
就没有这个面板内容，也就没有这个按钮。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import accounts as accounts_mod
from ..inventory import SessionRecord
from .session_cards import ElidedLabel, fmt_size, fmt_when_exact, short_cid, space_label

PANEL_WIDTH = 300


class DetailPanel(QWidget):
    """会话详情 + 三个动作。"""

    closed = Signal()
    restore_requested = Signal(object)
    export_requested = Signal(object)
    purge_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DetailPanel")
        self.setFixedWidth(PANEL_WIDTH)
        self._record: SessionRecord | None = None
        self._labels = accounts_mod.AccountLabels.load()
        self._build()
        self.clear()

    # ---------------------------------------------------------- 构建
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("会话详情")
        title.setObjectName("PanelTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_close = QPushButton("×")
        self.btn_close.setObjectName("IconButton")
        self.btn_close.setToolTip("收起详情面板")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self._on_close)
        head.addWidget(self.btn_close)
        outer.addLayout(head)

        # ---- 空态
        self._empty = QWidget()
        ev = QVBoxLayout(self._empty)
        ev.setContentsMargins(0, 40, 0, 0)
        ev.setSpacing(6)
        hint = QLabel("未选中会话")
        hint.setObjectName("RowTitle")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(hint)
        sub = QLabel("在左侧列表里点一条，这里显示它的详情与可执行动作。")
        sub.setObjectName("HintLabel")
        sub.setWordWrap(True)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(sub)
        ev.addStretch(1)
        outer.addWidget(self._empty, 1)

        # ---- 正常内容
        self._body = QWidget()
        bv = QVBoxLayout(self._body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(10)

        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("DetailTitle")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        bv.addWidget(self.lbl_title)

        self._pills = QHBoxLayout()
        self._pills.setSpacing(6)
        self.pill_state = QLabel()
        self.pill_state.setObjectName("PillOk")
        self.pill_cid = QLabel()
        self.pill_cid.setObjectName("PillMuted")
        self.pill_account = QLabel()
        self.pill_account.setObjectName("PillMuted")
        self._pills.addWidget(self.pill_state)
        self._pills.addWidget(self.pill_cid)
        self._pills.addWidget(self.pill_account)
        self._pills.addStretch(1)
        bv.addLayout(self._pills)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #EEF1EF;")
        line.setFixedHeight(1)
        bv.addWidget(line)

        self._kv = QGridLayout()
        self._kv.setContentsMargins(0, 0, 0, 0)
        self._kv.setHorizontalSpacing(10)
        self._kv.setVerticalSpacing(8)
        self._kv.setColumnStretch(1, 1)
        self._vals: dict[str, QLabel] = {}
        # 只留五行主信息：云端映射已经是状态胶囊里的一句话（未注册云端），
        # 归属账号改成胶囊 —— 每一行 KV 都是右栏的一块固定开销，
        # 七行会把动作区往下推，而动作区才是这个面板的正文。
        for row, key in enumerate(("工作空间", "会话包", "体积", "行数", "最近更新")):
            k = QLabel(key)
            k.setObjectName("KeyLabel")
            # 值一律单行省略：这里宽度只有两百多像素，换行会把 5 行撑成一屏
            v = ElidedLabel()
            v.setObjectName("ValLabel")
            v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._kv.addWidget(k, row, 0)
            self._kv.addWidget(v, row, 1)
            self._vals[key] = v
        bv.addLayout(self._kv)

        bv.addSpacing(2)

        # ---- 动作区
        self.btn_restore = QPushButton("恢复此会话")
        self.btn_restore.setObjectName("SoftPrimary")
        self.btn_restore.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_restore.setToolTip("清掉删除标记，重启客户端后回到侧边栏")
        self.btn_restore.clicked.connect(self._on_restore)
        bv.addWidget(self.btn_restore)

        # 「导出会话包」已下线：导出只剩主窗口那一个整包入口，
        # 免得这里又造出一个只含一条会话的残包。按钮留着但不进布局，
        # 老代码里引用 btn_export 的地方不会因此炸。
        self.btn_export = QPushButton("导出会话包")
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.clicked.connect(self._on_export)
        self.btn_export.setVisible(False)

        self.btn_purge = QPushButton("物理清除选中会话…")
        self.btn_purge.setObjectName("LinkDanger")
        self.btn_purge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_purge.setToolTip("把这条会话从库里和磁盘上彻底移除，不可撤销")
        self.btn_purge.clicked.connect(self._on_purge)
        bv.addWidget(self.btn_purge)

        # ---- 恢复效果说明
        # 三段式：做了什么（标题）/ 一句话结论（摘要）/ 细节（灰字）。
        # 旧的「标题 + 两段长文」在 300px 宽的栏里会堆成六行灰字，
        # 用户扫一眼看不出结论，只会觉得这里是免责声明。
        self.callout = QFrame()
        self.callout.setObjectName("Callout")
        cv = QVBoxLayout(self.callout)
        cv.setContentsMargins(12, 10, 12, 10)
        cv.setSpacing(3)
        self.lbl_callout_head = QLabel("恢复效果")
        self.lbl_callout_head.setObjectName("CalloutStrong")
        cv.addWidget(self.lbl_callout_head)
        self.lbl_callout_sum = QLabel()
        self.lbl_callout_sum.setObjectName("SectionTitle")
        self.lbl_callout_sum.setWordWrap(True)
        cv.addWidget(self.lbl_callout_sum)
        self.lbl_callout_body = QLabel()
        self.lbl_callout_body.setObjectName("HintLabel")
        self.lbl_callout_body.setWordWrap(True)
        cv.addWidget(self.lbl_callout_body)
        bv.addWidget(self.callout)

        bv.addStretch(1)

        self.lbl_foot = QLabel(
            "删除的只是客户端会话，包体仍完整；导入包含它的同步包即可恢复。"
        )
        self.lbl_foot.setObjectName("HintLabel")
        self.lbl_foot.setWordWrap(True)
        bv.addWidget(self.lbl_foot)

        outer.addWidget(self._body, 1)

    # ---------------------------------------------------------- 数据
    def set_labels(self, labels) -> None:
        """账号别名表换了（改名后）要能立刻反映到「归属账号」上。"""
        self._labels = labels
        if self._record is not None:
            self._fill_account(self._record)

    def set_record(self, record: SessionRecord | None) -> None:
        """灌一条记录。

        ★ 有记录就把自己显出来：面板在没选中时不占位（设计稿），
          而「谁负责显形」如果交给调用方，总有一处会忘 —— 忘了的后果是
          用户点了一行却什么都没发生。收起仍由 `closed` 信号驱动。
        """
        self._record = record
        if record is None:
            self.clear()
            return

        self.setVisible(True)
        self._empty.setVisible(False)
        self._body.setVisible(True)

        title = record.title or "(未命名会话)"
        self.lbl_title.setText(title)
        self.lbl_title.setToolTip(title)
        self.pill_cid.setText(f"会话包 {short_cid(record.cid)}")
        self.pill_cid.setToolTip(record.cid)

        state_text, state_style = _state_of(record)
        self.pill_state.setText(state_text)
        self.pill_state.setObjectName(state_style)
        self.pill_state.style().unpolish(self.pill_state)
        self.pill_state.style().polish(self.pill_state)

        space = record.sidebar_space or record.cwd or ""
        self._vals["工作空间"].setText(space_label(space) or "—")
        self._vals["工作空间"].setToolTip(space)
        self._vals["会话包"].setText(short_cid(record.cid))
        self._vals["会话包"].setToolTip(record.cid)
        self._vals["体积"].setText(fmt_size(record.jsonl_size))
        lines = record.fingerprint.line_count if record.fingerprint else None
        self._vals["行数"].setText(str(lines) if lines is not None else ("—" if not record.has_body else "未计算"))
        # 列表用相对时间好扫读，但「到底几点更新的」在核对时是硬需求
        self._vals["最近更新"].setText(
            fmt_when_exact(record.last_activity_at or record.updated_at)
        )
        self._fill_account(record)

        # ---- 动作可用性
        self.btn_restore.setVisible(bool(record.is_deleted and record.has_body))
        # 单条导出入口已下线（导出统一走主窗口的整包），这里恒不显示
        self.btn_export.setVisible(False)

        if record.is_deleted:
            self.callout.setVisible(True)
            if record.has_body:
                self.lbl_callout_head.setText("可以就地恢复")
                self.lbl_callout_sum.setText(
                    f"清删除标记 · 正文 {fmt_size(record.jsonl_size)} 原样保留"
                )
                self.lbl_callout_body.setText(
                    "重启 WorkBuddy 后回到侧边栏。写入前先备份会话库，可整体回滚。"
                )
            else:
                self.lbl_callout_head.setText("只能等云端")
                self.lbl_callout_sum.setText("本地没有这条会话的正文")
                self.lbl_callout_body.setText(
                    "恢复出来只是一条空壳，内容要等客户端重新从云端拉回。"
                )
        else:
            self.callout.setVisible(False)

    def _fill_account(self, record: SessionRecord) -> None:
        uid = record.user_id or ""
        name = self._labels.name_of(uid) if uid else ""
        self.pill_account.setText(name or "未知归属")
        self.pill_account.setVisible(True)
        self.pill_account.setToolTip(f"归属账号 {uid}" if uid else "库里没有归属字段")

    def clear(self) -> None:
        self._record = None
        self._body.setVisible(False)
        self._empty.setVisible(True)

    @property
    def record(self) -> SessionRecord | None:
        return self._record

    # ---------------------------------------------------------- 事件
    def _on_close(self) -> None:
        self.clear()
        self.closed.emit()

    def _on_restore(self) -> None:
        if self._record is not None:
            self.restore_requested.emit(self._record)

    def _on_export(self) -> None:
        if self._record is not None:
            self.export_requested.emit(self._record)

    def _on_purge(self) -> None:
        if self._record is not None:
            self.purge_requested.emit(self._record)


def _state_of(record: SessionRecord) -> tuple[str, str]:
    if record.is_deleted:
        return ("已删除 · 可恢复" if record.has_body else "已删除 · 无正文"), "PillBad"
    if not record.has_body:
        return "缺正文", "PillBad"
    if not record.has_cloud_mapping:
        return "未注册云端", "PillWarn"
    return "正常", "PillOk"
