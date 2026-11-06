"""整壳页头 —— 绿色渐变横幅（页面标题 + 副标题 + 运行状态 + 账号入口）。

## 为什么只剩一根

参考图把「我在哪一页」和「这台机器怎么样」压进同一块绿色横幅：左边大标题
加一行副标题（口径由各页自己算），右边运行状态胶囊与账号入口。原先的白色
64px 产品条已经移除 —— 产品名在左侧栏顶部，再重复一次只是白吃 64px 高度，
而默认窗口客户区只有 660px。

## 账号胶囊仍是唯一入口

点它 = 进「账号」页。这里只展示身份首字与套餐，不在横幅里塞切换菜单。

## 渐变是画出来的

QSS 的 `qlineargradient` 只有两三个硬停点，做不出参考图那种「斜向一道柔光」
的过渡；所以底色与高光都由 `paintEvent` 画，QSS 只负责文字与胶囊。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .theme import HEADER_HEIGHT, HEADER_GREEN_FROM, HEADER_GREEN_MID, HEADER_GREEN_TO

AVATAR = 30
CHIP_HEIGHT = 36
CHIP_TEXT_MAX = 120

# 状态胶囊：白底绿字（运行中）/ 半透明白底白字（离线）。两态高度一致，
# 避免切换时右边整簇控件左右跳动。
PILL_HEIGHT = 30


class AccountChip(QFrame):
    """页头右侧：圆形头像 + 套餐文字 + 下拉箭头。"""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("AccountChip")
        self.setFixedHeight(CHIP_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("账号与切换")

        h = QHBoxLayout(self)
        h.setContentsMargins(3, 3, 8, 3)
        h.setSpacing(8)

        self.avatar = QLabel()
        self.avatar.setObjectName("ChipAvatar")
        self.avatar.setFixedSize(AVATAR, AVATAR)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignVCenter)

        self.meta = QLabel("")
        self.meta.setObjectName("ChipMeta")
        self.meta.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        h.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

        self.chev = QLabel()
        self.chev.setObjectName("ChipChev")
        self.chev.setPixmap(icons.glyph("chevron_down", "#E6F6EC", 12))
        h.addWidget(self.chev, 0, Qt.AlignmentFlag.AlignVCenter)

        self._main = "未识别账号"
        self._meta = "免费版"
        self.set_account("", "")

    def set_account(self, main: str, meta: str) -> None:
        """主行给人名（只取首字画头像），副行给套餐 —— 不显示裸 uuid。"""
        self._main = main or "未识别账号"
        self._meta = meta or "免费版"
        self.avatar.setText(self._main[:1] or "?")
        fm = QFontMetrics(self.meta.font())
        shown = fm.elidedText(self._meta, Qt.TextElideMode.ElideRight, CHIP_TEXT_MAX)
        self.meta.setText(shown)
        self.meta.setToolTip(self._meta)
        self.setToolTip(f"{self._main}\n点击进入「账号」页")
        self.setFixedWidth(
            fm.horizontalAdvance(shown) + AVATAR + 8 + 8 + 12 + 18
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PageHeader(QWidget):
    """绿色渐变页头：左标题 + 副标题，右状态 + 账号。切页只换这一行的文字。"""

    account_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PageHeader")
        self.setFixedHeight(HEADER_HEIGHT)

        h = QHBoxLayout(self)
        h.setContentsMargins(24, 0, 20, 0)
        h.setSpacing(12)

        # ---- 左：标题块（垂直居中，和右侧胶囊同一中线）
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        col.addStretch(1)
        self.title = QLabel("")
        self.title.setObjectName("IntroTitle")
        col.addWidget(self.title)
        self.sub = QLabel("")
        self.sub.setObjectName("IntroSub")
        self.sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        col.addWidget(self.sub)
        col.addStretch(1)
        h.addLayout(col, 1)

        # ---- 右：运行状态胶囊 + 账号胶囊
        self.pill = QLabel()
        self.pill.setObjectName("StatusPill")
        self.pill.setFixedHeight(PILL_HEIGHT)
        self.pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)

        self.chip = AccountChip()
        self.chip.clicked.connect(self.account_clicked.emit)
        h.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)

        self._sub_text = ""
        self.set_running(False, "未运行")

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        base = QLinearGradient(QPointF(0, 0), QPointF(w, h))
        base.setColorAt(0.0, QColor(HEADER_GREEN_FROM))
        base.setColorAt(0.55, QColor(HEADER_GREEN_MID))
        base.setColorAt(1.0, QColor(HEADER_GREEN_TO))
        p.fillRect(self.rect(), base)

        # 斜向柔光：从右上往左下扫过的一道浅色带，参考图里那道高光
        sheen = QLinearGradient(QPointF(w * 0.30, 0), QPointF(w * 0.66, h))
        sheen.setColorAt(0.0, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.5, QColor(255, 255, 255, 34))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(self.rect(), sheen)
        p.end()

    # ------------------------------------------------------------ 数据
    def set_page(self, title: str, sub: str = "") -> None:
        self.title.setText(title or "")
        self._sub_text = sub or ""
        self._elide_sub()

    def set_running(self, running: bool, label: str = "") -> None:
        text = label or ("运行中" if running else "未运行")
        self.pill.setText(text)
        self.pill.setObjectName("StatusPill" if running else "StatusPillIdle")
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)

    def set_account(self, main: str, meta: str) -> None:
        self.chip.set_account(main, meta)

    # ------------------------------------------------------------ 布局
    def _elide_sub(self) -> None:
        fm = QFontMetrics(self.sub.font())
        avail = max(80, self.sub.width())
        self.sub.setText(fm.elidedText(self._sub_text, Qt.TextElideMode.ElideRight, avail))
        self.sub.setToolTip(self._sub_text)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._elide_sub()
