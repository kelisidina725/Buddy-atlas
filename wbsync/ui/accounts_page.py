"""账号页 —— 每个账号一张卡片（C 端参考图重做，2026-11-04）。

## 为什么从五列表格改成卡片

表格把「头像 / 名字 / 编号 / 积分 / 会话 / 最近使用 / 操作」压进 48px 行高，
两个账号时只占约 100px，其余全是一张撑满窗口的白卡片空腔。参考图的做法是
**一屏一张外层面板 + 每账号一张卡**：身份信息在卡头，三格统计在卡中，动作在
卡尾，卡片只按内容收高，留白落在浅灰画布上。

## 两个「切换」仍然必须分开

    查看  —— 看谁的会话，纯界面筛选，无损、可反复（点卡片空白处）
    登录  —— 客户端此刻是谁登录着，要关客户端、改认证文件（卡尾「切换账号」）

当前登录身份 = 绿框浅绿底 + 「当前账号」徽标 + 一枚禁用的灰色「切换」；
其余账号 = 白卡 + 绿色描边「切换账号」。默认态下登录账号也是查看中的账号，
两者分离时才在名字旁补一个「查看中」标记，参考图的默认画面不被多出来的元素打乱。

## 积分与会话数都不是猜的

第一格统计从「套餐」改成「可用积分」，数据来自 Buddy Switch 落盘的观察快照
（见 `wbsync/credits.py`）：本机没装、文件坏了、没有对应账号，一律显示「—」，
绝不拿 0 冒充余额。会话数在点卡片「查看」时**按该 uid 单独只读盘一次**
（`main_window.count_visible_for_uid`），不改 `_scan_uid`、不改导出口径，
所以「只看」依然是无损操作；数完把「待盘点」换成真实条数，
计数结果按 uid 记在页面里，重盘时清空。只读盘失败就保留「待盘点」，不写假 0。

## 保留下来的契约

信号、⋯ 菜单、空态、`set_accounts()` 参数全部照旧；
`_COLUMNS` 留作打包自检的构造能力探针（统计数据始终是这三格）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import accounts as accounts_mod, credits
from . import icons
from .format import human_ago
from .page_base import HeadedPage
from .theme import (
    BG_PAGE,
    CONTENT_PADDING,
    TEXT_MUTED,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
    account_avatar_qss,
)

_COLUMNS = ["积分", "会话", "最近使用"]

# 「让所有账号共用同一份数据」暂时收起 —— 它等价于跑一次「本机账号对齐」，
# 两个入口并排放在同一个菜单里，会被当成两个不同的功能；而四步向导已经把
# 选择接收账号、挑会话、确认人格与记忆都摆明了，这个开关只留下"一键"的错觉。
# 逻辑与偏好存储（uistate.load_share_data）都保留，改回 True 即恢复显示。
_SHOW_SHARE_OPTION = False

AVATAR_SIZE = 40
# 卡片高度按视口自适应：上限 282 保证卡内三段不空，下限 256 恰好等于卡片内容的
# 最小高度（实测 minimumSizeHint 255，再低按钮就会被裁）。以前写死 400，
# 在 1114×660 的默认窗口上把页脚整句挤出了视口。
CARD_FLOOR = 256
CARD_MAX_HEIGHT = 282
# 面板底边与视口底之间留一点画布，参考图里也是这样收尾的
PANEL_BOTTOM_GAP = 16


def _rule(css: str = "") -> QFrame:
    line = QFrame()
    line.setObjectName("AccountRule")
    line.setFixedHeight(1)
    if css:
        line.setStyleSheet(css)
    return line


def _esc(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class AccountCard(QFrame):
    """一张账号卡：身份 → 统计 → 动作，三段各有一条细分隔线。"""

    view_clicked = Signal(str)
    switch_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.uid = ""
        self._active = False
        self.setObjectName("AccountCard")
        # QFrame 也要显式打开，否则 QSS 的 background/border 不一定画得出来
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(CARD_FLOOR)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(0)

        # ---- 卡头：头像 / 名字 / 当前账号徽标
        head = QHBoxLayout()
        head.setSpacing(10)
        self.avatar = QLabel()
        self.avatar.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignVCenter)

        self.name = QLabel()
        self.name.setObjectName("AcctName")
        head.addWidget(self.name, 0, Qt.AlignmentFlag.AlignVCenter)
        self.viewing = QLabel("查看中")
        self.viewing.setObjectName("AcctBadge")
        self.viewing.setVisible(False)
        head.addWidget(self.viewing, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.badge = QLabel("当前账号")
        self.badge.setObjectName("AcctBadge")
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(head)

        v.addSpacing(10)
        self.uid_label = QLabel()
        self.uid_label.setObjectName("AcctId")
        self.uid_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        v.addWidget(self.uid_label)

        v.addSpacing(18)
        self.rule1 = _rule()
        v.addWidget(self.rule1)

        # ---- 统计：三格，列名在上、值在下
        v.addStretch(1)
        stats = QGridLayout()
        stats.setHorizontalSpacing(8)
        stats.setVerticalSpacing(8)
        for col in range(3):
            stats.setColumnStretch(col, 1)
        self.stat_keys: list[QLabel] = []
        self.stat_vals: list[QLabel] = []
        for col, key in enumerate(_COLUMNS):
            k = QLabel(key)
            k.setObjectName("StatKey")
            val = QLabel("—")
            val.setObjectName("StatValMuted")
            stats.addWidget(k, 0, col, Qt.AlignmentFlag.AlignLeft)
            stats.addWidget(val, 1, col, Qt.AlignmentFlag.AlignLeft)
            self.stat_keys.append(k)
            self.stat_vals.append(val)
        v.addLayout(stats)

        # 两处伸缩把多余高度分给「身份 → 统计」与「统计 → 卡尾」，
        # 不会把空白全堆在第二条线到按钮之间（参考图是上下均分的）。
        v.addSpacing(18)
        self.rule2 = _rule()
        v.addWidget(self.rule2)
        v.addStretch(1)

        # ---- 卡尾：当前账号给禁用态，其余给绿色描边按钮
        v.addSpacing(12)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.btn_current = QPushButton("  切换")
        self.btn_current.setObjectName("AcctCurrent")
        self.btn_current.setIcon(icons.glyph_icon("minus_circle", TEXT_TERTIARY, 13))
        self.btn_current.setFixedHeight(38)
        self.btn_current.setEnabled(False)
        self.btn_current.setToolTip("客户端此刻已登录这个账号")
        actions.addWidget(self.btn_current)

        self.btn_switch = QPushButton("切换账号")
        self.btn_switch.setObjectName("AcctSwitch")
        self.btn_switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_switch.setFixedHeight(38)
        self.btn_switch.setToolTip(
            "把客户端登录身份切到这个账号：备份认证文件 → 关闭客户端 → "
            "改写登录凭据 → 重新启动"
        )
        self.btn_switch.clicked.connect(
            lambda _checked=False: self.switch_requested.emit(self.uid)
        )
        actions.addWidget(self.btn_switch)
        v.addLayout(actions)

        self.set_active(False)

    # ------------------------------------------------------------ 状态
    def set_active(self, on: bool) -> None:
        self._active = bool(on)
        # 只换 objectName 让全局 QSS 接管两态；给卡片本身 setStyleSheet 会
        # 把下面徽标/统计/按钮的全局样式一起挡掉（实测过一次）。
        self.setObjectName("AccountCardActive" if on else "AccountCard")
        self.avatar.setStyleSheet(account_avatar_qss(on, AVATAR_SIZE))
        self.badge.setVisible(on)
        self.btn_current.setVisible(on)
        self.btn_switch.setVisible(not on)
        line_css = (
            "QFrame{background:#DCEFE3;border:none;}" if on
            else "QFrame{background:#EEF1F4;border:none;}"
        )
        self.rule1.setStyleSheet(line_css)
        self.rule2.setStyleSheet(line_css)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_viewing(self, on: bool) -> None:
        self.viewing.setVisible(bool(on))

    def _stat(self, col: int, text: str) -> None:
        lbl = self.stat_vals[col]
        lbl.setText(text or "—")
        muted = text in ("", "—", "待盘点")
        lbl.setObjectName("StatValMuted" if muted else "StatVal")
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)

    def set_data(self, info: accounts_mod.AccountInfo, *, login: bool,
                 unscanned: bool, session_count: int | None = None) -> None:
        self.uid = info.uid
        label = info.name or info.uid
        self.set_active(login)
        self.avatar.setText(label[:1] or "?")
        self.name.setText(label)
        self.uid_label.setText(f"ID  {_esc(info.uid)}")

        credit = credits.credit_for(info.uid)
        counted = session_count is not None
        if counted:
            count = f"{session_count} 条"
        elif unscanned:
            count = "—"           # 没盘过就没有可信条数，不显示陈旧数字
        else:
            count = f"{info.visible_count} 条" if info.visible_count else "—"
        if unscanned and not counted:
            when = "待盘点"
        else:
            when = "正在使用" if login else (human_ago(info.last_used) or "—")
        self._stat(0, credit.value_text if credit else "—")
        self._stat(1, count)
        self._stat(2, when)

        self.setToolTip(
            "\n".join(
                b for b in (
                    label,
                    f"编号 {info.uid}",
                    f"客户端昵称 {info.nickname}"
                    if info.nickname and info.nickname != label else "",
                    f"可用积分 {credit.value_text}（{credit.time_text}观察）"
                    if credit else "可用积分：本机没有积分快照",
                    "尚未盘点，点卡片查看时会按此账号只读盘点一次" if unscanned and not counted else "",
                ) if b
            )
        )
        self.btn_switch.setToolTip(
            f"把客户端登录身份切到「{label}」：备份认证文件 → 关闭客户端 → "
            "改写登录凭据 → 重新启动"
        )

    # ------------------------------------------------------------ 交互
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            # 点按钮是「切换登录身份」，点卡片其余位置才只是「查看」（无损）
            child = self.childAt(event.position().toPoint())
            on_button = False
            while child is not None and child is not self:
                if isinstance(child, QPushButton):
                    on_button = True
                    break
                child = child.parentWidget()
            if not on_button:
                self.view_clicked.emit(self.uid)
        super().mousePressEvent(event)


class AccountsPage(HeadedPage):
    """账号清单 + 切换入口。"""

    switch_requested = Signal(str)   # 切换客户端登录身份（会重启客户端）
    view_requested = Signal(str)     # 只看某个账号的会话（无损）
    rename_requested = Signal()
    open_dir_requested = Signal()
    add_requested = Signal()
    align_requested = Signal()
    share_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._infos: list[accounts_mod.AccountInfo] = []
        self._cards: list[AccountCard] = []
        self._login_uid = ""
        self._view_uid = ""
        self._share = False
        self._unscanned: set[str] = set()
        # uid -> 最近一次只读数出来的可见会话数；重盘后由主窗口清空
        self._counts: dict[str, int] = {}
        self._fitting = False
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(CONTENT_PADDING, 16, CONTENT_PADDING, 20)
        v.setSpacing(0)

        # 面板按内容收高，不跟着视口拉成一大片白：留白落在 #F9FAFC 画布上。
        # widgetResizable=False + 手动定宽，才能既保持圆角卡片的自然高度，
        # 又在账号多、窗口矮时正常滚动。
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pal = self.scroll.viewport().palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(BG_PAGE))
        self.scroll.viewport().setAutoFillBackground(True)
        self.scroll.viewport().setPalette(pal)
        self.panel = QFrame()
        self.panel.setObjectName("AccountPanel")
        self.panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pv = QVBoxLayout(self.panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)

        # ---- 工具栏：左摘要 + 右动作
        bar = QWidget()
        self._toolbar = bar
        tools = QHBoxLayout(bar)
        tools.setContentsMargins(24, 18, 24, 18)
        tools.setSpacing(10)
        info = QLabel()
        info.setPixmap(icons.glyph("info", TEXT_MUTED, 16))
        tools.addWidget(info, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_summary = QLabel()
        self.lbl_summary.setObjectName("AcctSummary")
        tools.addWidget(self.lbl_summary, 0, Qt.AlignmentFlag.AlignVCenter)
        tools.addStretch(1)

        self.btn_more = QToolButton()
        self.btn_more.setObjectName("IconButtonGhost")
        self.btn_more.setIcon(icons.glyph_icon("more", TEXT_SECONDARY, 16))
        self.btn_more.setToolTip("改名 / 打开数据目录 / 对齐")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_more.setFixedSize(34, 34)
        menu = QMenu(self)
        menu.addAction("修改账号名…").triggered.connect(self.rename_requested.emit)
        menu.addAction("打开数据目录").triggered.connect(self.open_dir_requested.emit)
        menu.addSeparator()
        menu.addAction("本机账号对齐…").triggered.connect(self.align_requested.emit)
        self._sep_share = menu.addSeparator()
        self._act_share = menu.addAction("让所有账号共用同一份数据")
        self._act_share.setCheckable(True)
        self._act_share.toggled.connect(self._on_share_toggled)
        # 收起时连同上面的分隔线一起藏：只藏一项会留一条悬空横线
        for item in (self._sep_share, self._act_share):
            item.setVisible(_SHOW_SHARE_OPTION)
        self.btn_more.setMenu(menu)
        tools.addWidget(self.btn_more)

        self.btn_add = QPushButton("  添加账号")
        self.btn_add.setObjectName("AcctAdd")
        self.btn_add.setIcon(icons.glyph_icon("plus", "#FFFFFF", 14))
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.setFixedHeight(40)
        self.btn_add.setToolTip("把另一个账号收进来：客户端登录过一次即可，或从其它工具的账号库导入")
        self.btn_add.clicked.connect(self.add_requested.emit)
        tools.addWidget(self.btn_add)
        pv.addWidget(bar)
        pv.addWidget(_rule())

        # ---- 卡片区：两列自适应
        self.cards_host = QWidget()
        self.cards_layout = QGridLayout(self.cards_host)
        self.cards_layout.setContentsMargins(24, 20, 24, 0)
        self.cards_layout.setHorizontalSpacing(16)
        self.cards_layout.setVerticalSpacing(16)
        self.cards_layout.setColumnStretch(0, 1)
        self.cards_layout.setColumnStretch(1, 1)
        pv.addWidget(self.cards_host)

        self.lbl_empty = QLabel("还没有识别到账号")
        self.lbl_empty.setObjectName("AcctEmpty")
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty.setVisible(False)
        pv.addWidget(self.lbl_empty)

        # ---- 面板内页脚
        foot = QHBoxLayout()
        foot.setContentsMargins(24, 16, 24, 24)
        foot.setSpacing(10)
        self.lbl_foot = QLabel("切换账号会用该账号的数据重新扫描，原来的不会丢。")
        self.lbl_foot.setObjectName("AcctFoot")
        foot.addWidget(self.lbl_foot, 1)
        self.lbl_share = QLabel("")
        self.lbl_share.setObjectName("HintLabel")
        self.lbl_share.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # 开关收起时这行状态没有可点的入口，留着只会让人四处找那个开关
        self.lbl_share.setVisible(_SHOW_SHARE_OPTION)
        foot.addWidget(self.lbl_share)
        pv.addLayout(foot)

        self.scroll.setWidget(self.panel)
        v.addWidget(self.scroll, 1)
        self._fit_panel()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._fit_panel()

    def _panel_chrome(self) -> int:
        """面板里「卡片之外」的固定开销：工具栏 + 分隔线 + 卡片上边距 + 页脚。

        不能用 `panel.sizeHint() - 卡片高` 反推 —— QLayout 的 sizeHint 有缓存，
        改完卡片固定高度再 activate() 仍可能读到旧值（实测恒为 432），
        算出的开销会偏大 50 多像素，最后表现成「明明够放却出滚动条」。
        这里改成各段真实尺寸相加，结果与缓存无关。
        """
        bar = getattr(self, "_toolbar", None)
        bar_h = bar.sizeHint().height() if bar is not None else 78
        foot_h = self.lbl_foot.sizeHint().height() if hasattr(self, "lbl_foot") else 17
        return (
            bar_h
            + 1                                          # 工具栏下的 1px 分隔线
            + self.cards_layout.contentsMargins().top()  # 卡片区上边距 20
            + foot_h
            + 16 + 24                                    # 页脚上下边距
            + 2                                          # 面板边框
        )

    def _fit_panel(self) -> None:
        """面板宽 = 视口宽，高 = 内容自然高；卡片高度随视口收放。

        三条实测约束（少了任何一条都会回到「页脚看不见」）：
        1. 固定开销按各段真实尺寸相加（`_panel_chrome`），不要读 sizeHint 差值；
        2. 卡片高度 = 视口剩余高度倒推，夹在 [CARD_FLOOR, CARD_MAX_HEIGHT]；
        3. 改卡片高度会再次触发布局，用 _fitting 挡住重入，再补一帧 QTimer。
        """
        scroll = getattr(self, "scroll", None)
        panel = getattr(self, "panel", None)
        if scroll is None or panel is None or self._fitting:
            return
        viewport = scroll.viewport()
        width, vh = viewport.width(), viewport.height()
        if width <= 0:
            return
        self._fitting = True
        try:
            panel.setFixedWidth(width)
            rows = max(1, (len(self._cards) + 1) // 2)
            gap = self.cards_layout.verticalSpacing()
            chrome = self._panel_chrome()

            if not self._cards:
                panel.setFixedHeight(chrome + 96)
                return

            room = vh - PANEL_BOTTOM_GAP - chrome - (rows - 1) * gap
            card_h = max(CARD_FLOOR, min(CARD_MAX_HEIGHT, room // rows))
            for card in self._cards:
                card.setFixedHeight(card_h)
            panel.setFixedHeight(chrome + rows * card_h + (rows - 1) * gap)
        finally:
            self._fitting = False

    # ------------------------------------------------------------ 数据
    def set_accounts(
        self,
        infos: list[accounts_mod.AccountInfo],
        login_uid: str = "",
        view_uid: str = "",
        share: bool = False,
        unscanned: set[str] | None = None,
    ) -> None:
        self._infos = list(infos)
        self._login_uid = login_uid or ""
        self._view_uid = view_uid or self._login_uid
        self._share = bool(share)
        # 这批 uid 尚未盘点：会话数不可知，必须显示「待盘点」而不是陈旧条数。
        self._unscanned = set(unscanned or ())
        # 账号列表变了就丢掉已不在册的计数，避免拿旧账号的数字糊在新卡上
        known = {i.uid for i in self._infos}
        self._counts = {u: n for u, n in self._counts.items() if u in known}
        self._act_share.blockSignals(True)
        self._act_share.setChecked(self._share)
        self._act_share.blockSignals(False)
        self._render()
        self._refresh_texts()

    def _render(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._cards = []
        for i, info in enumerate(self._infos):
            card = AccountCard()
            card.set_data(
                info,
                login=info.uid == self._login_uid,
                unscanned=info.uid in self._unscanned,
                session_count=self._counts.get(info.uid),
            )
            card.view_clicked.connect(self._select_view)
            card.switch_requested.connect(self.switch_requested.emit)
            self.cards_layout.addWidget(card, i // 2, i % 2)
            self._cards.append(card)
        self._update_viewing()
        self.cards_host.setVisible(bool(self._infos))
        self.lbl_empty.setVisible(not self._infos)
        self._fit_panel()
        QTimer.singleShot(0, self._fit_panel)

    def _update_viewing(self) -> None:
        for card in self._cards:
            card.set_viewing(
                card.uid == self._view_uid and card.uid != self._login_uid
            )

    def _select_view(self, uid: str) -> None:
        """点卡片空白处 = 只看这个账号的数据（无损）；登录身份不在这里改。"""
        if uid and uid != self._view_uid:
            self._view_uid = uid
            self._update_viewing()
            self.view_requested.emit(uid)

    def apply_session_count(self, uid: str, count: int) -> None:
        """把只读计数结果落到那张卡上，并记住它（页面重建时仍带得回来）。

        整张卡重刷而不是只改一格：数完之后「最近使用」还写着「待盘点」
        会自相矛盾，三格必须由 `set_data` 一次算齐。
        """
        num = max(0, int(count))
        self._counts[uid] = num
        info = next((i for i in self._infos if i.uid == uid), None)
        if info is None:
            return
        for card in self._cards:
            if card.uid == uid:
                card.set_data(
                    info,
                    login=uid == self._login_uid,
                    unscanned=uid in self._unscanned,
                    session_count=num,
                )
                return

    def clear_session_counts(self) -> None:
        """盘点结果换了，旧的只读计数一律作废。"""
        self._counts.clear()

    def session_count(self, uid: str) -> int | None:
        return self._counts.get(uid)

    def _on_cell_clicked(self, row: int, _col: int = 0) -> None:
        """旧表格口径的兼容入口：行号 → 卡片。"""
        card = self.card(row)
        if card is not None:
            self._select_view(card.uid)

    def _refresh_texts(self) -> None:
        n = len(self._infos)
        # 副标题只报账号口径。「共用同一份数据」的开关已从菜单收起，
        # 副标题再提它就是承诺一个界面上找不到的能力。
        self._set_sub(f"{n}个账号 · 各自独立" if n else "还没有识别到账号")
        self.lbl_summary.setText(
            f"{n}个账号 · 切换后会用该账号的数据重新扫描" if n else "还没有识别到账号"
        )
        self.lbl_share.setText(f"共用同一份数据：{'开' if self._share else '关'}")
        self.btn_add.setEnabled(True)

    def _on_share_toggled(self, on: bool) -> None:
        self._share = bool(on)
        self._refresh_texts()
        self.share_toggled.emit(bool(on))

    # ------------------------------------------------------------ 只读接口
    @property
    def view_uid(self) -> str:
        return self._view_uid

    @property
    def login_uid(self) -> str:
        return self._login_uid

    def infos(self) -> list[accounts_mod.AccountInfo]:
        return list(self._infos)

    def count(self) -> int:
        return len(self._cards)

    def card(self, row: int) -> AccountCard | None:
        return self._cards[row] if 0 <= row < len(self._cards) else None

    def name_text(self, row: int) -> str:
        c = self.card(row)
        return c.name.text() if c is not None else ""

    def uid_text(self, row: int) -> str:
        c = self.card(row)
        return c.uid_label.text() if c is not None else ""

    def count_text(self, row: int) -> str:
        c = self.card(row)
        return c.stat_vals[1].text() if c is not None else ""

    def state_text(self, row: int) -> str:
        c = self.card(row)
        return c.stat_vals[2].text() if c is not None else ""

    def op_text(self, row: int) -> str:
        """卡尾文案：「当前账号」或「切换账号」。给冒烟测试与无障碍读屏共用。"""
        c = self.card(row)
        if c is None:
            return ""
        return "当前账号" if c.uid == self._login_uid else "切换账号"

    def view_at(self, row: int) -> None:
        """模拟点第 row 张卡（不含按钮），用于自动化冒烟。"""
        c = self.card(row)
        if c is not None:
            self._select_view(c.uid)

    def set_share(self, on: bool) -> None:
        """外部改了「共用同一份数据」（例如刚跑完对齐），把勾选状态同步过来。"""
        self._share = bool(on)
        self._act_share.blockSignals(True)
        self._act_share.setChecked(self._share)
        self._act_share.blockSignals(False)
        self._refresh_texts()
