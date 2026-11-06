"""页面基类 —— 页面只负责内容，标题与副标题交给顶栏。

## 为什么标题上移

设计稿里标题固定画在顶栏（64px）左侧，右侧挂状态与账号。这带来两个好处：
切页时标题不会跟着滚动，且「我在哪一页」与「这台机器什么状态」在同一行读完。

代价是页面不能再自己画标题，副标题也得能被顶栏读到 —— 所以有了这个基类：
页面把副标题写进 `_set_sub()`，顶栏通过信号取最新值，不互相持有引用。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget


class HeadedPage(QWidget):
    """带副标题的页面。子类只需在数据变化时调 `self._set_sub(text)`。"""

    sub_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Content")
        # ★ 必须显式打开「样式表画背景」。
        #   QWidget 默认不画 QSS 里的 background，`QWidget#Content{background:#F4F5F7}`
        #   在各页面上其实一直没生效 —— 页面底色一直是 QPalette 的窗口色（白）。
        #   于是白卡片、#FAFBFC 数据带叠在白底上，只剩 1px 描边能看出块，
        #   这就是「卡片/表格不明显」的根：不是卡片画得淡，是画布没颜色。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._sub = ""

    def subtitle(self) -> str:
        return self._sub

    def _set_sub(self, text: str) -> None:
        text = text or ""
        if text == self._sub:
            return
        self._sub = text
        self.sub_changed.emit(text)
