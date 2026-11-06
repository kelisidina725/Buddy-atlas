"""分裂按钮 —— 主导航动作 + 低频变体下拉。

## 为什么需要它

「导出同步包」是常态动作，「导出会话包（仅会话）」是偶尔才用的变体。
两者并排成两个按钮会让人分不清该点哪个；塞进「更多」菜单又让常态动作多一层跳转。
分裂按钮给的是：点主体走常态，点右侧箭头才展开变体。

## 箭头为什么要单独一个按钮

`QPushButton.setMenu()` 会在按钮内部画一个系统自带的小三角，和我们自己写的 `▾`
叠在一起变成两个箭头；而且点按钮任意位置都会弹菜单，没法「点主体执行、点箭头选变体」。
所以这里是两个真按钮，靠 QSS 把相邻圆角归零拼成一体。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QSizePolicy, QWidget

ARROW_WIDTH = 26


class SplitButton(QWidget):
    """左侧主体按钮 + 右侧下拉箭头，视觉上是一个控件。"""

    clicked = Signal()

    def __init__(self, text: str, parent=None, primary: bool = False) -> None:
        super().__init__(parent)
        self._primary = primary

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.btn = QPushButton(text)
        self.btn.setObjectName("SplitMain")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self.clicked.emit)
        row.addWidget(self.btn)

        self.btn_arrow = QPushButton("▾")
        self.btn_arrow.setObjectName("SplitArrow")
        self.btn_arrow.setFixedWidth(ARROW_WIDTH)
        # ★ 必须让箭头在纵向撑满：QPushButton 的默认纵向策略是 Fixed，
        #   否则它只按「▾」一个字的行高（约 15px）居中摆放，右侧就出现一块
        #   上下都露白的方块 —— 视觉上像是两个不相关的按钮拼在一起。
        self.btn_arrow.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.btn_arrow.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_arrow.clicked.connect(self._popup)
        row.addWidget(self.btn_arrow)

        # 属性必须在 show 之前设好：QSS 的 [kind="primary"] 只在 polish 时求值
        if primary:
            for b in (self.btn, self.btn_arrow):
                b.setProperty("kind", "primary")

        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._sync_actions)

    # ---------------------------------------------------------- API
    def text(self) -> str:
        return self.btn.text()

    def setText(self, text: str) -> None:  # noqa: N802 - 与 Qt 命名保持一致
        self.btn.setText(text)

    def setToolTip(self, tip: str) -> None:  # noqa: N802
        self.btn.setToolTip(tip)

    def set_arrow_tooltip(self, tip: str) -> None:
        self.btn_arrow.setToolTip(tip)

    def add_action(self, text: str, slot=None):
        """往箭头菜单里加一项。返回 QAction，便于外部改文案/可见性。"""
        act = self._menu.addAction(text)
        if slot is not None:
            act.triggered.connect(slot)
        return act

    def add_separator(self) -> None:
        self._menu.addSeparator()

    def actions(self) -> list:
        return self._menu.actions()

    def menu(self) -> QMenu:
        """下拉菜单本体。写成方法而不是属性，是为了和 QToolButton.menu() 同形。"""
        return self._menu

    def setEnabled(self, on: bool) -> None:  # noqa: N802
        super().setEnabled(on)
        self.btn.setEnabled(on)
        self.btn_arrow.setEnabled(on)

    # ---------------------------------------------------------- 内部
    def _popup(self) -> None:
        if not [a for a in self._menu.actions() if not a.isSeparator()]:
            return
        if not self.btn_arrow.isEnabled():
            return
        pos = self.btn_arrow.mapToGlobal(QPoint(0, self.btn_arrow.height() + 2))
        self._menu.exec(pos)

    def _sync_actions(self) -> None:
        """全部分组都不可见时把菜单项藏起来，避免弹出一个空菜单。"""
        visible = [a for a in self._menu.actions() if not a.isSeparator() and a.isVisible()]
        self.btn_arrow.setEnabled(bool(visible) and self.isEnabled())
