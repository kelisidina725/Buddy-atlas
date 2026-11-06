"""「选择要复制的会话」—— 三级树（对话/工作区 → 日期·空间 → 会话）、逐条勾选。

## 为什么要有这个对话框

对齐计划页只给「新增 60 条会话」这样的汇总。可 60 条里有正文的、有内容重复的、
有云端拿不到正文的，混在一个数字里用户没法判断该不该执行。把清单摊开、让他
自己挑，是唯一诚实的方式。

## 为什么重复项要显示而不是藏起来

藏起来会让用户以为「源账号只有这么多会话」。实际是「这几条你那边已经有了」——
这两句话的意思完全不同。所以重复项照样列出来，打上徽标、默认不勾，
但允许他执意勾上（有时确实想要两份独立的副本）。

## 为什么按「对话 / 工作区 → 日期·空间」分组

会话标题大量重复（"点击没有反应"、"第一条会话"），平铺一列几十条根本认不出
哪条是哪条。三级归组之后，用户是靠「我那天在哪个目录干活」来回忆的：
对话区看日期（时间戳目录名），工作区看空间名，两边与客户端左栏一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import copysession
from .. import spaces as spaces_mod
from ..align import FAMILY_DIVERGE, FAMILY_NEWER
from . import theme, tree_util

# 徽标：文字 → (底色, 字色)
_BADGE = {
    "inline": (theme.BG_HOVER, theme.TEXT_SECONDARY),
    "dup": (theme.WARN_SOFT, theme.WARN),
    "blocked": (theme.DANGER_SOFT, theme.DANGER),
    "bulk": (theme.PRIMARY_SOFT, theme.PRIMARY_TEXT),
}


def _bold(size: float) -> QFont:
    f = QFont()
    f.setPointSizeF(size)
    f.setBold(True)
    return f


def cwd_label(cwd: str) -> str:
    """cwd → 文件夹名。末段为空时给出可读的兜底。"""
    tail = (cwd or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    return tail or "未指定工作区"


def _badge_of(item: copysession.CopyItem) -> tuple[str, str, str]:
    """(样式键, 文字, 提示)。"""
    if item.block_reason:
        return "blocked", item.block_reason, "复制过去会是空壳或不可用，因此不可勾选"
    if item.duplicate_of:
        return "dup", "已复制过", f"目标账号里已有这条会话的副本（{item.duplicate_of[:8]}），默认不勾选"
    if item.same_content_of:
        return "dup", "已有相同内容", (
            f"与 {item.same_content_of[:8]} 内容完全相同，只是 id 不同"
            "（可能是同一段对话的另一份副本，也可能是目标账号里已有的那条）。\n"
            "两条都复制过去，同一段对话会在侧边栏里出现两次。"
        )
    return "inline", "有正文", item.kind_text


def _sync_badge(s) -> tuple[str, str, str, bool, bool]:
    """追加项的徽标：(样式, 文字, 提示, 可否勾选, 默认勾选)。"""
    if s.status == FAMILY_DIVERGE:
        return (
            "blocked",
            "内容已分叉",
            f"目标里那份（{s.dst_lines} 行）与这份（{s.src_lines} 行）在共同部分之后"
            "各自长过，自动合并会丢消息，因此不写入。\n需要你自己判断保留哪一份。",
            False,
            False,
        )
    if s.status == FAMILY_NEWER:
        return (
            "inline",
            "目标里更新",
            f"目标里那份（{s.dst_lines} 行）比这份（{s.src_lines} 行）新，"
            "把旧的复制过去等于回退，因此跳过。",
            False,
            False,
        )
    return (
        "bulk",
        f"可追加 {s.add_lines} 行",
        f"目标里那份是旧版本（{s.dst_lines} 行）。勾选后把它缺的 {s.add_lines} 行"
        f"追加进去，变成 {s.src_lines} 行；不新增副本。",
        True,
        s.selected,
    )


def _badge_cell(style: str, text: str, tip: str) -> QWidget:
    """右对齐的徽标单元格。

    ★ 不能把 QLabel 直接交给 setItemWidget：单元格会把 widget 拉满，
      浅底被撑成一整块色带，看起来像行背景而不是标签。
    """
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 2, 6, 2)
    lay.setSpacing(0)
    lay.addStretch(1)
    badge = QLabel(text)
    bg, fg = _BADGE[style]
    badge.setStyleSheet(
        f"background:{bg};color:{fg};border-radius:5px;"
        "padding:1px 7px;font-size:11px;"
    )
    badge.setToolTip(tip)
    badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    lay.addWidget(badge)
    return wrap


@dataclass
class _Row:
    """清单里的一行。复制项与追加项共用同一套渲染。"""

    kind: str          # "copy" | "sync"
    cid: str
    title: str
    cwd: str
    updated_at: int
    selectable: bool
    checked: bool
    style: str
    badge: str
    tip: str


class CopyPickDialog(QDialog):
    """勾选要复制/追加的会话。只收集意图，不写任何东西。"""

    def __init__(
        self,
        items: list[copysession.CopyItem],
        account_name: str = "",
        source_name: str = "",
        parent=None,
        syncs: list | None = None,
    ) -> None:
        super().__init__(parent)
        self._items = list(items or [])
        self._syncs = list(syncs or [])
        self._account = account_name or ""
        self._source = source_name or ""
        self._copy_rows: dict[str, QTreeWidgetItem] = {}
        self._sync_rows: dict[str, QTreeWidgetItem] = {}
        self._syncing = False

        self.setWindowTitle("选择要复制/追加的会话")
        self.resize(680, 640)
        self._build()
        self._fill()
        self._refresh_count()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("选择要复制 / 追加的会话")
        title.setFont(_bold(15))
        head.addWidget(title)
        head.addStretch(1)
        self.lbl_count = QLabel()
        self.lbl_count.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        head.addWidget(self.lbl_count)
        root.addLayout(head)

        if self._account:
            sub = QLabel(
                f"复制到「{self._account}」"
                + (f"（来自「{self._source}」）" if self._source else "")
                + "。勾选后：新增会话＝复制一份（新 id，源账号不动）；"
                "可追加＝把缺的行补进目标里那份旧版本（不新增副本）。"
            )
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
            root.addWidget(sub)

        self.tree = QTreeWidget()
        self.tree.setObjectName("PickTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # 右侧只放「可追加 / 重复」这类两三个字：按内容定宽即可。
        # 写死 132px 是「用表格承载树」的残留 —— 它白占着宽度，标题只能被省略。
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._on_changed)
        root.addWidget(self.tree, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_all = QPushButton("全选")
        self.btn_all.setToolTip("勾选全部可复制的会话（含标注为重复的）")
        self.btn_none = QPushButton("全不选")
        self.btn_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        self.btn_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        row.addStretch(1)
        root.addLayout(row)

        foot = QHBoxLayout()
        foot.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        foot.addWidget(cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("PrimaryButton")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        foot.addWidget(self.btn_ok)
        root.addLayout(foot)

    # ------------------------------------------------------------ 数据
    def _rows_to_show(self) -> list[_Row]:
        out: list[_Row] = []
        for it in self._items:
            style, text, tip = _badge_of(it)
            out.append(
                _Row(
                    "copy", it.cid, it.title or "（无标题）", it.cwd, it.updated_at,
                    it.selectable, it.recommended, style, text, tip,
                )
            )
        for s in self._syncs:
            style, text, tip, selectable, checked = _sync_badge(s)
            out.append(
                _Row(
                    "sync", s.cid, s.title or "（无标题）", s.cwd, s.updated_at,
                    selectable, checked, style, text, tip,
                )
            )
        return out

    def _all_leaves(self) -> list[QTreeWidgetItem]:
        return list(self._copy_rows.values()) + list(self._sync_rows.values())

    def _fill(self) -> None:
        """铺「对话 / 工作区 → 日期·空间 → 会话」三级树。

        第一版是「工作区 → cwd → 会话」两级，且所有会话都挂在同一个「工作区」
        根下 —— 临时目录里的对话（客户端左栏归「对话」区）也被塞进工作区，
        和图一（客户端左栏）对不上。三级之后两边的分区、日期、空间名逐行一致。
        """
        self._syncing = True
        try:
            rows = self._rows_to_show()
            # 组内与组间都按最近活动倒序：用户是靠「我那天什么时候聊的」回忆的
            rows.sort(key=lambda r: r.updated_at, reverse=True)
            grouped = spaces_mod.group_sessions(rows)

            if grouped.chat_groups:
                root = self._root_row(spaces_mod.CHAT_LABEL, len(grouped.chats))
                for group in grouped.chat_groups:
                    node = self._node_row(
                        root, group.label, group.count, group.path or "（没有记录工作目录）"
                    )
                    for spec in group.items:
                        self._leaf_row(node, spec)
                root.setExpanded(True)

            if grouped.spaces:
                total = sum(g.count for g in grouped.spaces)
                root = self._root_row(spaces_mod.SPACE_ROOT_LABEL, total)
                for group in grouped.spaces:
                    node = self._node_row(root, group.label, group.count, group.path or group.key)
                    for spec in group.items:
                        self._leaf_row(node, spec)
                root.setExpanded(True)
        finally:
            self._syncing = False
        self._sync_tree()

    def _root_row(self, label: str, count: int) -> QTreeWidgetItem:
        """一级：对话 / 工作区。括号里是**会话条数**，两个数相加等于总条数。"""
        item = QTreeWidgetItem([f"{label}（{count}）", ""])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked)
        self.tree.addTopLevelItem(item)
        return item

    def _node_row(self, parent: QTreeWidgetItem, label: str, count: int, tip: str) -> QTreeWidgetItem:
        """二级：日期（时间戳目录）或空间名。默认收起，先让人看清有几天/几个项目。"""
        item = QTreeWidgetItem(parent, [f"{label}（{count}）", ""])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked)
        item.setToolTip(0, tip)
        item.setExpanded(False)
        return item

    def _leaf_row(self, parent: QTreeWidgetItem, spec: _Row) -> None:
        row = QTreeWidgetItem(parent, [spec.title, ""])
        row.setToolTip(0, f"{spec.title}\n{spec.cid}\n{spec.cwd or '（没有记录工作目录）'}")
        # ★ 必须先 addChild 再设 checkbox 状态：QTreeWidgetItem 还没
        #   挂进树时设置的 checkState 会被入树时的默认值覆盖。
        if spec.selectable:
            row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(
                0,
                Qt.CheckState.Checked if spec.checked else Qt.CheckState.Unchecked,
            )
        else:
            # 不可执行：列出来但勾不动，免得用户以为“漏了”
            row.setFlags(Qt.ItemFlag.NoItemFlags)
            row.setForeground(0, Qt.GlobalColor.darkGray)

        self.tree.setItemWidget(row, 1, _badge_cell(spec.style, spec.badge, spec.tip))
        if spec.kind == "copy":
            self._copy_rows[spec.cid] = row
        else:
            self._sync_rows[spec.cid] = row

    # ------------------------------------------------------------ 勾选
    def _checkable(self, item: QTreeWidgetItem) -> bool:
        return bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def _roots(self) -> list[QTreeWidgetItem]:
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _on_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._syncing or column != 0:
            return
        self._syncing = True
        try:
            # 向下：父节点决定全部后代；向上：整棵树重新回算三态
            if item.childCount():
                tree_util.set_subtree(item, item.checkState(0), 0, self._checkable)
            self._sync_tree()
        finally:
            self._syncing = False
            self._refresh_count()

    def _sync_tree(self) -> None:
        """自下而上回算三态：三个层级（一级/二级/会话）都要算到。

        ★ 这里必须自己压住 _syncing：它改的是父节点的 checkState，同样会发
          itemChanged；不挡住的话 _on_changed 会把「部分勾选」当成用户点击，
          再往下铺一遍，叶子全被染成半勾（实战踩过）。
        """
        was = self._syncing
        self._syncing = True
        try:
            for root in self._roots():
                tree_util.aggregate(root, 0, self._checkable)
        finally:
            self._syncing = was

    def _set_all(self, state: Qt.CheckState) -> None:
        self._syncing = True
        try:
            for row in self._all_leaves():
                if self._checkable(row):
                    row.setCheckState(0, state)
            self._sync_tree()
        finally:
            self._syncing = False
        self._refresh_count()

    def _refresh_count(self) -> None:
        picked = len(self.picked_cids()) + len(self.picked_sync_cids())
        total = sum(1 for row in self._all_leaves() if self._checkable(row))
        self.lbl_count.setText(f"已选 {picked} / {total}")
        self.btn_ok.setEnabled(picked > 0)
        self.btn_ok.setToolTip("" if picked else "至少选一条")

    # ------------------------------------------------------------ 结果
    @staticmethod
    def _checked(rows: dict[str, QTreeWidgetItem], checkable) -> set[str]:
        return {
            cid
            for cid, row in rows.items()
            if checkable(row) and row.checkState(0) == Qt.CheckState.Checked
        }

    def picked_cids(self) -> set[str]:
        """勾选的「复制」项（源会话 cid）。"""
        return self._checked(self._copy_rows, self._checkable)

    def picked_sync_cids(self) -> set[str]:
        """勾选的「追加」项（源会话 cid —— 它的目标那份由计划里记着）。"""
        return self._checked(self._sync_rows, self._checkable)


__all__ = ["CopyPickDialog", "cwd_label"]
