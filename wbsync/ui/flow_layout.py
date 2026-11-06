"""流式布局与自适应工具栏 —— 窄了就换行，绝不把按钮压到文字被裁。

## 为什么需要

会话页工具栏一行里塞了 6 个筛选页签 + 搜索框 + 5 个动作。窗口 1080 宽、
右侧详情面板展开时，列表区只剩约 530px，而这一行自身在理想宽度下要 1000px 出头。
`QHBoxLayout` 在空间不足时会把每个控件压到 `minimumSizeHint` 以下 ——
文字于是被裁成「地可见」「已删除(F 侧栏)」这种半截，正是「被挤压」的来源。

三条规则：

1. **页签走 FlowLayout**：放不下就整颗挪到下一行，胶囊永远是完整的；
2. **动作组整组换行**：页签与动作挤不进同一行时，动作组整体落到第二行，
   而不是各自被削掉几像素；
3. **搜索框只在窄行里放开**：宽屏保持设计稿的 212 定宽（工具栏不会无谓地跳），
   窄行里放下限、允许伸缩，把剩余宽度吃满。

## 为什么自己写 FlowLayout

Qt 没有内置的流式布局，官方示例就是这段；区别在于：

- 过滤不可见项 —— 诊断档页签 0 条时会被 `setVisible(False)`，
  空的 `QWidgetItem` 仍会占一格，导致明明只有 3 个胶囊却排成 6 列；
- 暴露 `single_line_width()`：上层要靠它判断「页签和动作还能不能同处一行」，
  而 `sizeHint()` 给的是最宽那颗的宽度，判断不了整行。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLayout,
    QLayoutItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_SPACING = 8


class FlowLayout(QLayout):
    """从左到右排，放不下就换行；行高取该行最高的一项。"""

    def __init__(self, parent=None, spacing: int = _SPACING) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = int(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # ---- QLayout 接口
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        """理想宽度 = 所有可见项排成一行。

        不能像 Qt 官方示例那样返回 `minimumSize()`（只有最宽那项）：
        父布局会真的只给这么宽，于是宽屏下也会被压成多行。
        窄的时候由 `heightForWidth()` 决定换成几行，宽的时候就该是一行。
        """
        items = self.visible_items()
        if not items:
            return QSize(0, 0)
        m = self.contentsMargins()
        height = max(it.sizeHint().height() for it in items)
        return QSize(
            self.single_line_width() + m.left() + m.right(),
            height + m.top() + m.bottom(),
        )

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize(0, 0)
        for item in self.visible_items():
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # ---- 自用
    def spacing(self) -> int:
        return self._spacing

    def visible_items(self) -> list[QLayoutItem]:
        """只算真正会画出来的项：隐藏的页签不该继续占位。"""
        return [it for it in self._items if not it.isEmpty()]

    def single_line_width(self) -> int:
        """所有可见项排成一行所需的宽度 —— 判断要不要整组换行的依据。"""
        items = self.visible_items()
        if not items:
            return 0
        return sum(it.sizeHint().width() for it in items) + self._spacing * (len(items) - 1)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self.visible_items():
            hint = item.sizeHint()
            # 一行都放不下一颗时也要摆上去，否则宽 0 的极端情形会无限换行
            if x + hint.width() > area.right() + 1 and line_h > 0:
                x = area.x()
                y += line_h + self._spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class FlowBox(QWidget):
    """FlowLayout 的容器。

    纵向策略必须是 `Preferred` + `heightForWidth`：流式布局的高度由宽度决定，
    告诉父布局「我随时会变高」，换行时才不会被裁掉第二行。
    """

    def __init__(self, spacing: int = _SPACING, parent=None) -> None:
        super().__init__(parent)
        self.flow = FlowLayout(self, spacing)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def single_line_width(self) -> int:
        return self.flow.single_line_width()


class ResponsiveBar(QWidget):
    """页签区 + 动作区：同处一行放不下时，动作区整组落到第二行。

    `narrow_changed` 让上层把搜索框从定宽切成可伸缩 —— 布局本身不该知道
    某个按钮叫什么，它只负责报告「现在是不是窄行」。
    """

    narrow_changed = Signal(bool)

    def __init__(
        self,
        chips: QWidget,
        actions: QWidget,
        spacing: int = _SPACING,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._chips = chips
        self._actions = actions
        self._spacing = int(spacing)
        self._narrow = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(self._spacing)

        self._row_top = QWidget()
        top = QHBoxLayout(self._row_top)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(self._spacing)
        # 页签吃掉剩余宽度，动作组自然被顶到最右
        top.addWidget(chips, 1)
        top.addWidget(actions)

        self._row_bottom = QWidget()
        bottom = QHBoxLayout(self._row_bottom)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(self._spacing)
        bottom.addStretch(1)
        self._row_bottom.setVisible(False)

        outer.addWidget(self._row_top)
        outer.addWidget(self._row_bottom)

    # ---------------------------------------------------------- 对外
    def sync(self) -> None:
        """页签增减（诊断档 0 条被收起）后重算一次，显隐变化不会自动通知到外层。"""
        layout = self._chips.layout()
        if layout is not None:
            layout.invalidate()
        self._chips.updateGeometry()
        self._apply(self._decide_narrow())

    def is_narrow(self) -> bool:
        return self._narrow

    # ---------------------------------------------------------- 内部
    def resizeEvent(self, event) -> None:  # noqa: N802
        if event is not None:
            super().resizeEvent(event)
        self._apply(self._decide_narrow())

    def _decide_narrow(self) -> bool:
        width = self.width()
        if width <= 0:  # 尚未布局，先按宽屏算，避免首帧闪一次换行
            return self._narrow
        chip_need = self._chips_single_line()
        act_need = self._actions.sizeHint().width()
        return width < chip_need + act_need + self._spacing

    def _chips_single_line(self) -> int:
        getter = getattr(self._chips, "single_line_width", None)
        if callable(getter):
            return int(getter())
        return int(self._chips.sizeHint().width())

    def _apply(self, narrow: bool) -> None:
        if narrow == self._narrow:
            return
        self._narrow = narrow
        top = self._row_top.layout()
        bottom = self._row_bottom.layout()
        if narrow:
            top.removeWidget(self._actions)
            # 插在最前：bottom 里已经有一个 stretch，追加会把它顶到最右
            bottom.insertWidget(0, self._actions)
        else:
            bottom.removeWidget(self._actions)
            top.addWidget(self._actions)
        self._row_bottom.setVisible(narrow)
        self.narrow_changed.emit(narrow)
        self.updateGeometry()
