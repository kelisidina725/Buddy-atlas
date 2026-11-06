"""概览页 —— 一屏看清「本机有什么、上次做了什么」。

## 这一页只回答三个问题

1. 本机这端的数据有多少（会话 / 可找回 / 配置 / 备份）；
2. 客户端现在能不能写（运行中就不能写库）；
3. 接下来该去哪儿（看已删除的、去配置、去备份、直接导包）。

刻意不做图表：这四个数字之间没有可比性，画成饼或柱只会增加误读。
也不做「健康分」之类的合成指标 —— 把「缺正文 3 条」藏进一个 87 分里，等于什么都没说。

## 数据从哪儿来

全部由主窗口在盘点结束后调用 `refresh()` 灌进来，页面自己不去碰数据库：
盘点有缓存、有范围（当前账号 / 全部账号），页内各自取数必然口径不一致。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
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

from . import icons
from .page_base import HeadedPage
from .session_cards import fmt_size
from .theme import (
    CONTENT_PADDING,
    PRIMARY,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
)

BANNER_HEIGHT = 72
STAT_HEIGHT = 100


class StatCard(QFrame):
    """一个数字 + 一句说明。顺序与设计稿一致：标签在上、数字在下。

    整张卡可点：四张统计卡各自是「去那一页看这批数据」的入口。
    """

    clicked = Signal()

    def __init__(self, label: str, glyph: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setFixedHeight(STAT_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(2)

        # 标签行 = 小图标 + 文字。图标只做区分，不承载信息，所以统一灰色。
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.icon = QLabel()
        self.icon.setFixedSize(15, 15)
        if glyph:
            self.icon.setPixmap(icons.glyph(glyph, TEXT_TERTIARY, 14))
        else:
            self.icon.hide()
        head.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.caption = QLabel(label)
        self.caption.setObjectName("StatLabel")
        head.addWidget(self.caption, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        v.addLayout(head)
        self.value = QLabel("—")
        self.value.setObjectName("StatValue")
        v.addWidget(self.value)
        self.hint = QLabel("")
        self.hint.setObjectName("StatHint")
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        v.addWidget(self.hint)
        # 子控件若接收鼠标事件，点击会打在标签上、frame 收不到 ——
        # 让它们对鼠标透明，整张卡才是一块可点区域（:hover 也才正常）。
        for w in (self.caption, self.value, self.hint, self.icon):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set(self, value: str, hint: str = "") -> None:
        self.value.setText(value)
        self.hint.setText(hint)
        self.hint.setToolTip(hint)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _ActionRow(QFrame):
    """灰底动作行：左文字 + 右箭头。整行可点。"""

    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ActionRow")
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(icons.glyph("check", "#2E9E63", 13))
        h.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self._label(text), 1)
        chev = QLabel()
        chev.setPixmap(icons.glyph("chevron", TEXT_TERTIARY, 12))
        h.addWidget(chev)

    @staticmethod
    def _label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("ActionText")
        return lbl

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# 最近备份每行的固定高度：卡片按可用高度决定显示几行时用它换算
RECENT_ROW_H = 30


class _BackupRow(QWidget):
    """最近备份的一行：时间 · 类型（左）+ 体积（右）。"""

    clicked = Signal()

    def __init__(self, when: str, kind: str, size: str, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(RECENT_ROW_H)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        left = QLabel(f"{when} · {kind}")
        left.setObjectName("ValLabel")
        h.addWidget(left, 1)
        right = QLabel(size)
        right.setObjectName("HintLabel")
        h.addWidget(right)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class OverviewPage(HeadedPage):
    """统计概览。"""

    goto_sessions = Signal(str)     # 传筛选档，例如 "deleted"
    goto_config = Signal()
    goto_backup = Signal()
    goto_workspace = Signal()
    import_package = Signal()
    export_package = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._recent_rows: list[_BackupRow] = []
        self._fitting_rows = False
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(CONTENT_PADDING, 20, CONTENT_PADDING, 14)
        # 12 而不是 16：默认窗口 1114×660 里，16 的段间距会让「最近备份」卡片
        # 只剩 160px（连三行都放不下），收紧 4 个间隙后能多放一整行。
        v.setSpacing(12)

        # ---- 问候行：一句话交代「今天做过什么」+ 两个入口动作
        greet = QHBoxLayout()
        greet.setContentsMargins(0, 0, 0, 0)
        greet.setSpacing(12)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        self.lbl_greet = QLabel("你好")
        self.lbl_greet.setObjectName("GreetTitle")
        col.addWidget(self.lbl_greet)
        self.lbl_greet_sub = QLabel("")
        self.lbl_greet_sub.setObjectName("StatusLine")
        col.addWidget(self.lbl_greet_sub)
        greet.addLayout(col, 1)

        self.btn_import = QPushButton("  导入同步包")
        self.btn_import.setIcon(icons.glyph_icon("inbox_down", TEXT_SECONDARY, 14))
        self.btn_import.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_import.clicked.connect(self.import_package.emit)
        greet.addWidget(self.btn_import, 0, Qt.AlignmentFlag.AlignBottom)

        self.btn_export = QPushButton("  导出同步包")
        self.btn_export.setObjectName("Primary")
        self.btn_export.setIcon(icons.glyph_icon("outbox_down", "#FFFFFF", 14))
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.clicked.connect(self.export_package.emit)
        greet.addWidget(self.btn_export, 0, Qt.AlignmentFlag.AlignBottom)
        v.addLayout(greet)

        # ---- 状态横幅
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        self.banner.setFixedHeight(BANNER_HEIGHT)
        b = QHBoxLayout(self.banner)
        b.setContentsMargins(16, 0, 14, 0)
        b.setSpacing(12)
        self.ban_mark = QLabel()
        self.ban_mark.setFixedSize(28, 28)
        self.ban_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ban_mark.setStyleSheet(f"background:{PRIMARY};border-radius:14px;")
        self.ban_mark.setPixmap(icons.glyph("check", "#FFFFFF", 15))
        b.addWidget(self.ban_mark, 0, Qt.AlignmentFlag.AlignVCenter)

        btext = QVBoxLayout()
        btext.setContentsMargins(0, 0, 0, 0)
        btext.setSpacing(2)
        self.ban_title = QLabel("一切正常，正在自动保护")
        self.ban_title.setObjectName("BannerTitle")
        btext.addWidget(self.ban_title)
        self.ban_desc = QLabel("")
        self.ban_desc.setObjectName("BannerDesc")
        self.ban_desc.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        btext.addWidget(self.ban_desc)
        b.addLayout(btext, 1)

        self.btn_banner = QPushButton("查看备份")
        self.btn_banner.setObjectName("SoftPrimary")
        self.btn_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_banner.setFixedHeight(30)
        self.btn_banner.clicked.connect(self.goto_backup.emit)
        b.addWidget(self.btn_banner, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addWidget(self.banner)

        # ---- 四张统计卡
        self.card_sessions = StatCard("我的会话", "bubble")
        self.card_deleted = StatCard("可找回", "minus_circle")
        self.card_config = StatCard("同步配置", "sliders_square")
        self.card_backup = StatCard("自动备份", "briefcase")

        # 四张卡各自跳到对应功能页。会话两张卡复用了会话页的筛选档：
        # 「我的会话」= 本地可见那批，「可找回」= 已删除那批，点进去数字能对上。
        self.card_sessions.setToolTip("去会话页看本地可见的会话")
        self.card_sessions.clicked.connect(lambda: self.goto_sessions.emit("visible"))
        self.card_deleted.setToolTip("去会话页看可恢复的会话")
        self.card_deleted.clicked.connect(lambda: self.goto_sessions.emit("deleted"))
        self.card_config.setToolTip("去配置页核对设置、文件与技能")
        self.card_config.clicked.connect(self.goto_config.emit)
        self.card_backup.setToolTip("去备份与包页管理备份")
        self.card_backup.clicked.connect(self.goto_backup.emit)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(0)
        for i, card in enumerate(
            (self.card_sessions, self.card_deleted, self.card_config, self.card_backup)
        ):
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        v.addLayout(grid)

        # ---- 最近备份 + 快捷操作
        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        recent = QFrame()
        recent.setObjectName("Card")
        self._recent_card = recent
        rv = QVBoxLayout(recent)
        rv.setContentsMargins(16, 14, 16, 12)
        rv.setSpacing(4)
        rhead = QHBoxLayout()
        rhead.setSpacing(8)
        rtitle = QLabel("最近备份")
        self._recent_title = rtitle
        rtitle.setObjectName("CardTitle")
        rhead.addWidget(rtitle)
        rhead.addStretch(1)
        self.btn_all_backups = QPushButton("全部记录")
        self.btn_all_backups.setObjectName("LinkButton")
        self.btn_all_backups.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_all_backups.clicked.connect(self.goto_backup.emit)
        rhead.addWidget(self.btn_all_backups)
        rv.addLayout(rhead)
        self._recent_box = QVBoxLayout()
        self._recent_box.setContentsMargins(0, 2, 0, 0)
        self._recent_box.setSpacing(0)
        rv.addLayout(self._recent_box)
        self.lbl_recent_empty = QLabel("还没有备份。导入与恢复前会自动留一份，这里能看到它。")
        self.lbl_recent_empty.setObjectName("HintLabel")
        rv.addWidget(self.lbl_recent_empty)
        rv.addStretch(1)
        self.lbl_recent_note = QLabel("每次导入或修复前都会自动留一份备份，放心操作。")
        self.lbl_recent_note.setObjectName("HintLabel")
        rv.addWidget(self.lbl_recent_note)
        bottom.addWidget(recent, 2)

        quick = QFrame()
        quick.setObjectName("Card")
        qv = QVBoxLayout(quick)
        qv.setContentsMargins(16, 14, 16, 14)
        qv.setSpacing(10)
        qtitle = QLabel("快捷操作")
        qtitle.setObjectName("CardTitle")
        qv.addWidget(qtitle)
        for text, slot in (
            ("找回已删除的会话", lambda: self.goto_sessions.emit("deleted")),
            ("核对同步的配置", self.goto_config.emit),
            ("管理备份与恢复", self.goto_backup.emit),
            ("查看我的工作空间", self.goto_workspace.emit),
        ):
            row = _ActionRow(text)
            row.clicked.connect(slot)
            qv.addWidget(row)
        qv.addStretch(1)
        bottom.addWidget(quick, 1)
        v.addLayout(bottom, 1)

        self.lbl_foot = QLabel()
        self.lbl_foot.setObjectName("HintLabel")
        self.lbl_foot.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.lbl_foot.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.lbl_foot)

    # ---------------------------------------------------------- 灌数据
    def refresh(
        self,
        *,
        greeting_name: str = "",
        today_backup_count: int = 0,
        last_backup_text: str = "",
        visible_count: int,
        total_count: int,
        deleted_count: int,
        bodyless_count: int,
        config_count: int,
        backup_count: int,
        backup_bytes: int,
        client_running: bool,
        account_text: str,
        scope_text: str,
        snapshot_text: str = "",
        baseline_text: str = "",
        sessions_bytes: int = 0,
        recent_backups: list[tuple[str, str, str]] | None = None,
    ) -> None:
        hour = __import__("datetime").datetime.now().hour
        part = "早上好" if hour < 9 else "上午好" if hour < 12 else "下午好" if hour < 18 else "晚上好"
        # 只有「真名字」才配进问候语：昵称常常就是一串手机号或 uuid，
        # 「晚上好，15680662901」比不写名字更生硬。
        human = greeting_name if any(not ch.isdigit() for ch in greeting_name or "") else ""
        self.lbl_greet.setText(f"{part}，{human}" if human else part)

        bits: list[str] = []
        if today_backup_count:
            bits.append(f"今天已经自动备份 {today_backup_count} 次")
        if last_backup_text:
            bits.append(f"最近一次在 {last_backup_text}")
        if not bits:
            bits.append("还没有自动备份记录")
        self.lbl_greet_sub.setText("，".join(bits))

        self._set_sub(f"{scope_text} · {account_text}")

        size_text = fmt_size(sessions_bytes) if sessions_bytes else "—"
        # 横幅只有一种颜色：绿色 = 「一切照常」。客户端在跑不是异常（本工具就是
        # 为它设计的），所以把「写库前会自动关掉它」写进副行，而不是把横幅染黄。
        self.ban_title.setText("一切正常，正在自动保护")
        self.ban_desc.setText(
            f"{visible_count} 条会话 · {config_count} 项配置 · 共 {size_text}。"
            + ("你正在聊天时也不会打断：写库前会自动关掉客户端，写完再打开。"
               if client_running
               else "导入与恢复会先备份、再写库，出问题随时可以退回去。")
        )
        self.ban_mark.setStyleSheet(f"background:{PRIMARY};border-radius:14px;")
        self.ban_mark.setPixmap(icons.glyph("check", "#FFFFFF", 15))

        self.card_sessions.set(str(visible_count), "条条都有正文" if bodyless_count == 0 else f"缺正文 {bodyless_count} 条")
        self.card_deleted.set(
            str(deleted_count),
            "到备份里找回来" if deleted_count else "没有需要恢复的",
        )
        self.card_config.set(str(config_count), "设置、文件与技能")
        self.card_backup.set(
            f"{backup_count} 份",
            f"共 {fmt_size(backup_bytes)}，可随时恢复" if backup_count else "还没有备份",
        )

        # 最近备份：清空后重建（条数少，重建比增量更新代码少一半）
        while self._recent_box.count():
            item = self._recent_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        rows = recent_backups or []
        self._recent_rows = []
        for when, kind, size in rows[:6]:
            row = _BackupRow(when, kind, size)
            row.clicked.connect(self.goto_backup.emit)
            self._recent_box.addWidget(row)
            self._recent_rows.append(row)
        self.lbl_recent_empty.setVisible(not rows)
        self.lbl_recent_note.setVisible(bool(rows))
        # 此刻卡片还没被布局定高，先算一次、再排一帧兜住真正的尺寸
        self._fit_recent_rows()
        QTimer.singleShot(0, self._fit_recent_rows)

        foot = [f"侧边栏快照：{snapshot_text}", f"配置基线：{baseline_text}"]
        foot.append(f"已盘点 {total_count} 条")
        self.lbl_foot.setText("　·　".join(b for b in foot if b and not b.endswith("：")))

    # -------------------------------------------------- 最近备份的自适应行数
    def _fit_recent_rows(self) -> None:
        """按「最近备份」卡片的真实高度决定显示几行，避免页脚说明被卡片底边裁掉。

        默认 1114×660 的窗口里，这一块只剩约 160px：标题 20 + 5 行 × 30 + 说明 14
        已经超了 60px，硬排的结果就是说明压在边框上。行数由高度换算，隐藏的行
        仍可用右上角「全部记录」查看，信息没有丢。
        """
        if not self._recent_rows or self._fitting_rows:
            return
        card = getattr(self, "_recent_card", None)
        title = getattr(self, "_recent_title", None)
        if card is None or title is None or card.height() <= 0:
            return
        self._fitting_rows = True
        try:
            margins = card.layout().contentsMargins()
            chrome = (
                margins.top() + margins.bottom()
                + title.sizeHint().height()
                + RECENT_ROW_H // 2                      # 标题与列表之间
                + (self.lbl_recent_note.sizeHint().height() + 4
                   if self.lbl_recent_note.isVisible() else 0)
            )
            room = card.height() - chrome
            fit = max(1, room // RECENT_ROW_H)
            for index, row in enumerate(self._recent_rows):
                row.setVisible(index < fit)
        finally:
            self._fitting_rows = False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._fit_recent_rows()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # show 之后才拿得到卡片的真实高度，且首帧布局可能还在排队
        QTimer.singleShot(0, self._fit_recent_rows)
