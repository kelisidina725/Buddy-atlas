"""三级会话树的勾选工具 —— 三个对话框共用的那一小段状态机。

## 为什么要单独一个模块

「对话 / 工作区 → 日期·空间 → 会话」这套树在切换向导、复制挑选、本机对齐三处
都要铺一遍，勾选规则完全一样：点父节点整棵子树跟着走，点叶子逐级回算三态。
第一版是每个对话框各写一遍两级版本；补上第三级之后，三处的「只算直接子节点」
全部失效（父节点的父节点永远停在半选），只能一处一处改。

所以这里只做四件与业务无关的事：遍历叶子、整棵子树赋值、后序回算三态、
统计可勾选叶子数。各对话框只负责自己的行长得什么样。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem


def iter_descendants(item: QTreeWidgetItem):
    """深度优先遍历所有后代（不含自己）。"""
    for i in range(item.childCount()):
        child = item.child(i)
        yield child
        yield from iter_descendants(child)


def iter_leaves(item: QTreeWidgetItem):
    """所有叶子后代（不含自己）。分组行不参与勾选计数。"""
    for child in iter_descendants(item):
        if child.childCount() == 0:
            yield child


def set_subtree(item: QTreeWidgetItem, state: Qt.CheckState, column: int, checkable) -> None:
    """整棵子树赋值。三态不是「勾选值」，往下传会把叶子也变成半勾 —— 直接不传。"""
    if state == Qt.CheckState.PartiallyChecked:
        return
    for node in iter_descendants(item):
        if checkable(node):
            node.setCheckState(column, state)


def aggregate(item: QTreeWidgetItem, column: int, checkable) -> tuple[int, int]:
    """后序回算整棵树的三态，返回 (勾选叶子数, 可勾选叶子总数)。

    调用方必须先自己压住 `itemChanged`：这里会改分组行的 checkState，
    不挡住的话又会被当成用户点击，`set_subtree` 再往下染一遍。
    """
    on = total = 0
    for i in range(item.childCount()):
        child = item.child(i)
        if child.childCount():
            child_on, child_total = aggregate(child, column, checkable)
        elif checkable(child):
            child_on = 1 if child.checkState(column) == Qt.CheckState.Checked else 0
            child_total = 1
        else:
            child_on = child_total = 0
        on += child_on
        total += child_total
    if item.childCount() and checkable(item):
        if total == 0 or on == 0:
            state = Qt.CheckState.Unchecked
        elif on == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        item.setCheckState(column, state)
    return on, total


def checked_leaves_where(item: QTreeWidgetItem, column: int, checkable) -> list[QTreeWidgetItem]:
    """勾选着的叶子（不含分组行）。"""
    return [
        leaf
        for leaf in iter_leaves(item)
        if checkable(leaf) and leaf.checkState(column) == Qt.CheckState.Checked
    ]
