"""切换登录账号 —— 三步向导：目标与来源 / 迁移内容 / 确认。

## 为什么是三步而不是一个确认框

切换账号会**关闭客户端、改掉登录身份、再把它拉起来**。这三件事都不轻：
一旦写错，用户下次启动看到的是别人（或没人）的账号。所以要点名目标、
逐项列出要搬什么、最后把「客户端会关掉」摆到明面上再让他点执行。

## 为什么「目标账号」要标凭据状态

能切到哪个账号，取决于本机是否存过那个账号的登录凭据。没存过的账号
（用户从没在这台机器上登录过）不是灰掉不解释 —— 那样用户会以为程序坏了；
而是明确写出「需先用该账号登录一次」，并在底部给一条「从其它工具导入」的出口。

## 为什么复制会话是可选、默认勾选

用户说「同步数据后直接切过去」，那默认就该把会话带过去。但复制确实会
在目标账号里造出一批新会话（正文按新 id 落盘），有人只想换身份、不想多一份，
所以给一个明确的勾选项，默认开、可关。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import accounts as accounts_mod
from .. import copysession, memorymerge, spaces as spaces_mod, vault
from . import icons, theme

_STEPS = ("目标与来源", "迁移内容", "确认")

# 树的列：名字列（带勾选框与层级）+ 判定列
C_NAME, C_KIND = 0, 1


class StepBadge(QLabel):
    """步骤徽标：①②③ + 标题，当前步高亮。"""

    def __init__(self, index: int, text: str, parent=None) -> None:
        super().__init__(parent)
        self._index = index
        self._text = text
        self.setObjectName("StepBadge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_active(self, active: bool, done: bool = False) -> None:
        num = "✓" if done else str(self._index + 1)
        self.setText(f"{num}  {self._text}")
        if active:
            self.setStyleSheet(
                f"background:{theme.PRIMARY};color:#FFFFFF;border-radius:11px;"
                "padding:4px 12px;font-size:12px;"
            )
        elif done:
            self.setStyleSheet(
                f"background:{theme.PRIMARY_SOFT};color:{theme.PRIMARY_TEXT};"
                "border-radius:11px;padding:4px 12px;font-size:12px;"
            )
        else:
            self.setStyleSheet(
                f"background:{theme.BG_HOVER};color:{theme.TEXT_SECONDARY};"
                "border-radius:11px;padding:4px 12px;font-size:12px;"
            )


class SwitchAccountDialog(QDialog):
    """选择目标账号并勾选要搬的内容。对话框只收集意图，不执行任何写入。"""

    def __init__(
        self,
        account_list: list[accounts_mod.AccountInfo],
        labels: accounts_mod.AccountLabels,
        current_uid: str,
        base=None,
        parent=None,
        preselect_uid: str = "",
    ) -> None:
        super().__init__(parent)
        self._accounts = list(account_list or [])
        self._labels = labels
        self._current_uid = current_uid
        self._base = base
        self._preselect = preselect_uid or ""
        self._store = vault.Vault.load(base)
        self._plan: copysession.CopyPlan | None = None
        self._target_uid = ""
        self._memory_available = False
        self._memory_init = False
        self._step = 0
        # 首次布局之后禁止缩高（见 _fit_to_step）：缩高会在屏幕上留下旧底栏残影
        self._height_locked = False
        # 树上的勾选聚合由代码统一重算，重算期间必须屏蔽 itemChanged，
        # 否则「设置父节点 → 触发信号 → 再改子节点」会递归打转。
        self._tree_guard = False
        self._cid_items: dict[str, QTreeWidgetItem] = {}

        self.setWindowTitle("切换登录账号")
        # 宽度定值；高度由当前步骤的内容决定（见 _fit_to_step）——
        # 原来写死 580，三步里只有账号选择步最短，那一屏就空了一大截。
        self.resize(760, 520)
        self._build()
        self._goto(0)
        # 从抽屉进来时目标已经定了，替用户选中 —— 否则要在列表里再找一遍自己刚点的那个
        if self._preselect:
            self._preselect_row()

    def _chrome_height(self) -> int:
        """除了步骤内容之外，窗口还要占多高（标题 / 副标题 / 步骤条 / 底栏 + 间距）。

        ★ 不能写 `self.sizeHint().height() - self.stack.sizeHint().height()`：
          QStackedWidget 的 sizeHint 取的是**所有页里最大的那个**，不是当前页。
          用它相减会把「迁移内容」那一页的高度算进固定区，于是切到账号选择步
          时窗口仍然那么高，下面空出一大块 —— 正是要修的那个毛病。
        """
        lay = self.layout()
        if lay is None:
            return 0
        total = lay.contentsMargins().top() + lay.contentsMargins().bottom()
        for i in range(lay.count()):
            item = lay.itemAt(i)
            w = item.widget()
            if w is self.stack:
                continue
            if w is not None:
                total += w.sizeHint().height()
            elif item.layout() is not None:
                total += item.layout().sizeHint().height()
        if lay.count() > 1:
            total += lay.spacing() * (lay.count() - 1)
        return total

    def _fit_to_step(self) -> None:
        """窗口高度 = 固定区 + 当前步骤内容高度，上限屏高 86%，下限 320。

        下限只防「内容极短时窗口缩成一条」，不负责撑场面 —— 撑出来的空白
        就是这个对话框原来「空荡荡」的来源。

        ★ 只允许**长高、不允许缩矮**（首次布局除外）。窗口在可见状态下缩高，
          Windows 不会重画刚腾出来的那块桌面，上一帧较高的底栏会残留在屏幕上，
          看起来就是「确认页下面还叠着一组上一步/取消/下一步」——2026-11-05
          用户截图里的多余部分。宁可让较矮的确认页多留一点空白，也不能留残影。
          每次改高度后同步 repaint 自身与父窗口，作为第二道保险。
        """
        page = self.stack.currentWidget()
        if page is None:
            return
        # 固定区优先用**实测值**（showEvent 里量的），没有才按 sizeHint 估。
        # ★ 估的值不可靠：带 wordWrap 的副标题在 760 宽下是一行，sizeHint 却
        #   按两行算，于是每步都多留一行空白。
        chrome = getattr(self, "_chrome_measured", 0) or self._chrome_height()
        screen = self.screen()
        cap = int(screen.availableGeometry().height() * 0.86) if screen else 760
        page_h = page.sizeHint().height()

        # ★ QStackedWidget 的最小高度取**所有页里最大的那个**：迁移内容页有棵树，
        #   它有 225px 的最小值，于是账号选择步（只需 144）也被顶着下不来，
        #   下面白出一大块。按当前页显式给一个最小高度，才能真的跟着步骤收放。
        self.stack.setMinimumHeight(page_h)

        need = chrome + page_h
        target = max(300, min(need, cap))
        if self._height_locked and target < self.height():
            target = self.height()
        self._height_locked = True
        if target == self.height():
            return
        self.resize(self.width(), target)
        # update() 只是把重绘排进队列，嵌套的 exec() 事件循环里可能一直不执行；
        # repaint() 是同步的。父窗口也要重画，否则腾出来的那块会留着旧像素。
        self.repaint()
        parent = self.parentWidget()
        if parent is not None:
            parent.repaint()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """显示后量一次真实固定区，再按当前步收紧。

        构造期拿不到真实字体与表格行高，`sizeHint` 估出来的固定区偏高，
        每步都会白留一行；这里用「窗口高 − 步骤区高」量真值。
        """
        super().showEvent(event)
        measured = self.height() - self.stack.geometry().height()
        if measured > 0:
            self._chrome_measured = measured
        self._fit_to_step()

    def _preselect_row(self) -> None:
        for i, info in enumerate(self._account_rows):
            if info.uid == self._preselect:
                self.tbl_accounts.selectRow(i)
                return

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        self.lbl_title = QLabel("切换到另一个账号")
        self.lbl_title.setFont(_bold(16))
        root.addWidget(self.lbl_title)

        self.lbl_sub = QLabel()
        self.lbl_sub.setWordWrap(True)
        self.lbl_sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        root.addWidget(self.lbl_sub)

        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.badges: list[StepBadge] = []
        for i, name in enumerate(_STEPS):
            b = StepBadge(i, name)
            badges.addWidget(b)
            self.badges.append(b)
            if i < len(_STEPS) - 1:
                line = QLabel("—")
                line.setStyleSheet(f"color:{theme.BORDER_STRONG};")
                badges.addWidget(line)
        badges.addStretch(1)
        root.addLayout(badges)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_step_target())
        self.stack.addWidget(self._build_step_content())
        self.stack.addWidget(self._build_step_confirm())
        root.addWidget(self.stack, 1)

        foot = QHBoxLayout()
        self.btn_import = QPushButton("从其它工具导入凭据…")
        self.btn_import.setToolTip("读取 ~/.buddy-switch/accounts.json（只读，不改动对方文件）")
        self.btn_import.clicked.connect(self._on_import)
        foot.addWidget(self.btn_import)
        foot.addStretch(1)
        self.btn_back = QPushButton("上一步")
        self.btn_back.clicked.connect(self._on_back)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_next = QPushButton("下一步")
        self.btn_next.setObjectName("Primary")
        self.btn_next.clicked.connect(self._on_next)
        foot.addWidget(self.btn_back)
        foot.addWidget(self.btn_cancel)
        foot.addWidget(self.btn_next)
        root.addLayout(foot)

    # ---- 第一步：目标与来源
    def _build_step_target(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(8)

        cur = self._labels.display(self._current_uid) if self._current_uid else "未识别"
        self.lbl_source = QLabel(f"当前登录：{cur}")
        self.lbl_source.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:13px;")
        v.addWidget(self.lbl_source)

        hint = QLabel("选择一个目标账号。只能切到「本机存有登录凭据」的账号。")
        hint.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        v.addWidget(hint)

        self.tbl_accounts = QTableWidget(0, 4)
        self.tbl_accounts.setHorizontalHeaderLabels(["目标账号", "编号", "会话", "凭据"])
        self.tbl_accounts.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_accounts.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_accounts.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_accounts.verticalHeader().setVisible(False)
        self.tbl_accounts.setAlternatingRowColors(True)
        hh = self.tbl_accounts.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_accounts.itemSelectionChanged.connect(self._on_target_changed)
        # 不加 stretch 系数：表格自己按行数定高，否则 3 个账号也会被拉成一整屏
        v.addWidget(self.tbl_accounts, 0)
        v.addStretch(1)

        self.lbl_target_note = QLabel()
        self.lbl_target_note.setWordWrap(True)
        self.lbl_target_note.setStyleSheet(f"color:{theme.WARN};font-size:12px;")
        v.addWidget(self.lbl_target_note)

        self._fill_accounts()
        return page

    def _fill_accounts(self) -> None:
        rows = [a for a in self._accounts if a.uid != self._current_uid]
        self.tbl_accounts.setRowCount(len(rows))
        for i, info in enumerate(rows):
            self.tbl_accounts.setItem(i, 0, QTableWidgetItem(info.name))
            self.tbl_accounts.setItem(i, 1, QTableWidgetItem(info.short))
            count = QTableWidgetItem(str(info.session_count or 0))
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tbl_accounts.setItem(i, 2, count)

            cred = self._store.get(info.uid)
            if cred is None:
                cell = QTableWidgetItem("未存凭据")
                cell.setForeground(Qt.GlobalColor.darkYellow)
                cell.setToolTip("该账号还没在这台机器上登录过，无法切换")
            else:
                cell = QTableWidgetItem(f"就绪 · {cred.expiry_text()}")
                cell.setForeground(Qt.GlobalColor.darkGreen)
                cell.setToolTip(f"{cred.source_text}；凭据形态 {cred.token_form}")
            self.tbl_accounts.setItem(i, 3, cell)
            # 把 uid 挂在第一列，避免另建一份映射表
            self.tbl_accounts.item(i, 0).setData(Qt.ItemDataRole.UserRole, info.uid)
        self._account_rows = rows
        self._fit_account_table()

    def _fit_account_table(self) -> None:
        """表格高度 = 表头 + 行数 × 行高，最多 5 行。

        ★ 原来它被 addWidget(..., 1) 拉伸填满整步，三个账号也占掉 400px，
          于是弹窗显得空荡荡。这里是「尺寸假设」那个反模式的直接修法：
          高度从内容算，不是按「可能有十几个账号」预留。
        """
        rows = self.tbl_accounts.rowCount()
        header = self.tbl_accounts.horizontalHeader().height() or 30
        row_h = self.tbl_accounts.verticalHeader().defaultSectionSize() or 30
        visible = min(max(rows, 1), 5)
        self.tbl_accounts.setFixedHeight(header + visible * row_h + 2)

    # ---- 第二步：迁移内容
    def _build_step_content(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(8)

        self.chk_copy = QCheckBox("复制会话到目标账号")
        self.chk_copy.setChecked(True)
        self.chk_copy.setStyleSheet("font-size:13px;")
        self.chk_copy.setToolTip(
            "每条会话生成一个新的 id 复制过去（原账号的那份不动）。\n"
            "这样两个账号都能看到同一段对话；不复制则目标账号是空的。"
        )
        self.chk_copy.toggled.connect(self._on_copy_toggled)
        v.addWidget(self.chk_copy)

        self.lbl_copy_note = QLabel()
        self.lbl_copy_note.setWordWrap(True)
        self.lbl_copy_note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        self.lbl_copy_note.setContentsMargins(22, 0, 0, 0)
        v.addWidget(self.lbl_copy_note)

        self.tree_sessions = QTreeWidget()
        self.tree_sessions.setObjectName("SwitchTree")
        self.tree_sessions.setColumnCount(2)
        self.tree_sessions.setHeaderLabels(["会话 / 日期 · 空间", "判定"])
        self.tree_sessions.setRootIsDecorated(True)
        self.tree_sessions.setIndentation(14)
        self.tree_sessions.setUniformRowHeights(True)
        self.tree_sessions.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree_sessions.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.tree_sessions.header()
        hh.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_KIND, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_sessions.itemChanged.connect(self._on_tree_changed)
        v.addWidget(self.tree_sessions, 1)

        hint = QLabel(
            "按客户端左栏归组：对话（临时目录，二级是日期）／工作区（二级是空间名）。"
            "默认展开到二级，点日期或空间名才展开里面的会话。"
        )
        hint.setStyleSheet(f"color:{theme.TEXT_TERTIARY};font-size:11px;")
        hint.setContentsMargins(2, 0, 0, 0)
        v.addWidget(hint)

        row = QHBoxLayout()
        self.btn_all = QPushButton("全选可复制")
        self.btn_none = QPushButton("全不选")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        row.addStretch(1)
        self.lbl_copy_count = QLabel()
        self.lbl_copy_count.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        row.addWidget(self.lbl_copy_count)
        v.addLayout(row)

        self.chk_memory = QCheckBox("合并长期记忆")
        self.chk_memory.setStyleSheet("font-size:13px;")
        self.chk_memory.setToolTip(
            "把当前账号的长期记忆按内容并入目标账号：目标已有的小节不动，"
            "只补它没有的小节与条目。改写前会备份原文。"
        )
        v.addWidget(self.chk_memory)
        self.lbl_memory_note = QLabel()
        self.lbl_memory_note.setWordWrap(True)
        self.lbl_memory_note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        self.lbl_memory_note.setContentsMargins(22, 0, 0, 0)
        v.addWidget(self.lbl_memory_note)
        return page

    # ---- 第三步：确认
    def _build_step_confirm(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(10)

        self.lbl_confirm_head = QLabel()
        self.lbl_confirm_head.setFont(_bold(13))
        v.addWidget(self.lbl_confirm_head)

        self.lbl_steps = QLabel()
        self.lbl_steps.setWordWrap(True)
        self.lbl_steps.setStyleSheet("font-size:13px;")
        v.addWidget(self.lbl_steps)

        warn = QLabel(
            "执行时会关闭 WorkBuddy（未发送的输入会丢失、正在进行的对话会中断，包括当前这个）。"
            "\n执行前会把认证文件与数据库各备份一份到 wbsync-backup。"
        )
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"background:{theme.WARN_SOFT};color:{theme.WARN};border-radius:6px;"
            "padding:8px 10px;font-size:12px;"
        )
        v.addWidget(warn)

        self.lbl_confirm_note = QLabel()
        self.lbl_confirm_note.setWordWrap(True)
        self.lbl_confirm_note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        v.addWidget(self.lbl_confirm_note)
        v.addStretch(1)
        return page

    # ------------------------------------------------------------ 数据
    def _recompute_plan(self) -> None:
        # 来回翻步骤时保留用户已经取消勾选的那些 —— 重算计划是为了反映目标账号
        # 的最新状态，不是为了把他的选择重置掉
        keep = set(self.checked_cids()) if self._cid_items else set()
        reselect = bool(keep) and self._plan is not None and self._plan.target_uid == self._target_uid

        plan = copysession.plan_copy(
            target_uid=self._target_uid,
            source_uid=self._current_uid,
            base=self._base,
        )
        self._plan = plan
        self._fill_sessions(plan, keep if reselect else None)
        self.lbl_copy_note.setText(
            f"源账号共 {len(plan.items)} 条会话，其中 {len(plan.copyable)} 条可复制"
            + (f"，{len(plan.duplicates)} 条之前已复制过" if plan.duplicates else "")
            + (f"，{len(plan.blocked)} 条不可复制" if plan.blocked else "")
            + "。"
        )
        self._refresh_copy_count()
        self._refresh_memory_note()

    def _fill_sessions(self, plan: copysession.CopyPlan, keep: set[str] | None = None) -> None:
        """按「对话 / 工作区 → 日期·空间 → 会话」铺树。

        40 条会话平铺成 40 行时，用户看不出这批会话分属哪几个项目、也没有
        "哪几天聊过"的概念，还没法成批勾选，只能逐行点。三级之后默认展开到
        二级（日期/空间名可见、会话收起）：先看清要搬的是哪几天哪几个项目，
        需要时再点开某一组确认单条。
        """
        self._cid_items = {}
        self.tree_sessions.blockSignals(True)
        try:
            self.tree_sessions.clear()
            grouped = spaces_mod.group_sessions(plan.items)

            if grouped.chat_groups:
                chat_root = self._group_row(
                    spaces_mod.CHAT_LABEL,
                    len(grouped.chats),
                    "没有工作目录、或工作目录是客户端自动建的临时目录（纯对话，"
                    "不属于任何项目）；二级是那天的目录名",
                )
                for group in grouped.chat_groups:
                    node = self._node_row(
                        chat_root, group.label, group.count, group.path or group.key
                    )
                    for item in group.items:
                        self._leaf(node, item, keep)
                chat_root.setExpanded(True)

            if grouped.spaces:
                space_root = self._group_row(
                    spaces_mod.SPACE_ROOT_LABEL,
                    sum(g.count for g in grouped.spaces),
                    "按工作空间归类；二级是空间名，点它展开里面的会话",
                )
                for group in grouped.spaces:
                    node = self._node_row(
                        space_root, group.label, group.count, group.path or group.key
                    )
                    for item in group.items:
                        self._leaf(node, item, keep)
                # 展开到「空间名」这一层就停 —— 会话默认收起，界面才读得清
                space_root.setExpanded(True)

            self._sync_checks()
        finally:
            self.tree_sessions.blockSignals(False)

    def _group_row(self, label: str, count: int, tip: str = "") -> QTreeWidgetItem:
        """一级（对话 / 工作区）。括号里是**会话条数**：两区相加等于总条数。"""
        row = QTreeWidgetItem([f"{label} ({count})", ""])
        row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        row.setCheckState(C_NAME, Qt.CheckState.Checked)
        row.setFont(C_NAME, _semi(13))
        if tip:
            row.setToolTip(C_NAME, tip)
        self.tree_sessions.addTopLevelItem(row)
        return row

    def _node_row(
        self, parent: QTreeWidgetItem, label: str, count: int, tip: str = ""
    ) -> QTreeWidgetItem:
        """二级：对话区的日期/时间戳目录，或工作区的空间名。默认收起会话。"""
        row = QTreeWidgetItem(parent, [label, f"{count} 条"])
        row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        row.setCheckState(C_NAME, Qt.CheckState.Checked)
        row.setIcon(C_NAME, icons.glyph_icon("workspace", theme.TEXT_SECONDARY, 14))
        # 悬停给原始大小写的完整路径：两个同名空间（本机确实有两个 WorkBuddy
        # 目录）靠它分辨；对话则能看出是哪个临时目录
        if tip:
            row.setToolTip(C_NAME, tip)
        row.setForeground(C_KIND, QColor(theme.TEXT_TERTIARY))
        row.setExpanded(False)
        return row

    def _leaf(self, parent: QTreeWidgetItem, item, keep: set[str] | None) -> None:
        title = item.title or f"（无标题 · {accounts_mod.short_uid(item.cid)}）"
        leaf = QTreeWidgetItem(parent, [title, item.kind_text])
        # 列宽不够时 Qt 会省略标题；补一条 tooltip，至少悬停能看到全称
        leaf.setToolTip(0, title)
        leaf.setData(C_NAME, Qt.ItemDataRole.UserRole, item.cid)
        leaf.setToolTip(C_NAME, f"{title}\n{accounts_mod.short_uid(item.cid)}")
        if item.copyable:
            leaf.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            checked = (item.cid in keep) if keep is not None else True
            leaf.setCheckState(
                C_NAME, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        else:
            # 云端 / Claw / 缺正文：勾了也复制不出来，干脆不给勾选框
            leaf.setFlags(Qt.ItemFlag.ItemIsEnabled)
            leaf.setForeground(C_NAME, QColor(theme.TEXT_TERTIARY))
        if item.block_reason or item.duplicate_of:
            leaf.setForeground(C_KIND, QColor(theme.TEXT_TERTIARY))
        self._cid_items[item.cid] = leaf

    # ---- 勾选聚合：父节点三态由子节点算出，点子节点也要回写父节点
    def _selectable_leaves(self, node: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        stack = [node.child(i) for i in range(node.childCount())]
        while stack:
            cur = stack.pop()
            if cur.childCount():
                stack.extend(cur.child(i) for i in range(cur.childCount()))
            elif cur.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                out.append(cur)
        return out

    def _sync_checks(self) -> None:
        """自底向上重算三态。只由代码调用，调用方负责屏蔽 itemChanged。"""
        for i in range(self.tree_sessions.topLevelItemCount()):
            self._sync_node(self.tree_sessions.topLevelItem(i))

    def _sync_node(self, node: QTreeWidgetItem) -> Qt.CheckState | None:
        states: list[Qt.CheckState] = []
        for i in range(node.childCount()):
            child = node.child(i)
            if child.childCount():
                sub = self._sync_node(child)
                if sub is not None:
                    states.append(sub)
            elif child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                states.append(child.checkState(C_NAME))

        if not states:
            # 整组都没有可勾选的会话：去掉勾选框，别给一个点了没反应的框
            node.setFlags(Qt.ItemFlag.ItemIsEnabled)
            return None
        node.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        if all(s == Qt.CheckState.Checked for s in states):
            agg = Qt.CheckState.Checked
        elif all(s == Qt.CheckState.Unchecked for s in states):
            agg = Qt.CheckState.Unchecked
        else:
            agg = Qt.CheckState.PartiallyChecked
        node.setCheckState(C_NAME, agg)
        return agg

    def _on_tree_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """用户点了勾选框：父节点向下整组生效，任何节点改动都向上重算三态。"""
        if self._tree_guard or column != C_NAME:
            return
        self._tree_guard = True
        try:
            leaves = self._selectable_leaves(item)
            if leaves:
                target = (
                    Qt.CheckState.Checked
                    if item.checkState(C_NAME) != Qt.CheckState.Unchecked
                    else Qt.CheckState.Unchecked
                )
                for leaf in leaves:
                    leaf.setCheckState(C_NAME, target)
            # 叶子被点也要让父节点的三态跟着变，所以无条件重算一遍
            self._sync_checks()
        finally:
            self._tree_guard = False
        self._refresh_copy_count()

    def _refresh_copy_count(self) -> None:
        picked = self.checked_cids()
        total = len(self._plan.copyable) if self._plan else 0
        self.lbl_copy_count.setText(f"已勾选 {len(picked)} / {total} 条")
        self._sync_next_enabled()

    def _refresh_memory_note(self) -> None:
        self._memory_available = bool(memorymerge.available(self._base))
        if not self._memory_available:
            self.chk_memory.setEnabled(False)
            self.chk_memory.setChecked(False)
            self.lbl_memory_note.setText("本机没有长期记忆文件，此项不可用。")
            return
        self.chk_memory.setEnabled(True)
        # 只在**第一次**可用时默认勾上：来回翻步骤不能把用户手动的取消改回去
        if not self._memory_init:
            self._memory_init = True
            self.chk_memory.setChecked(True)
        self.lbl_memory_note.setText(
            "目标账号已有的记忆小节会保留，只补入它没有的内容；改写前自动备份。"
        )

    # ------------------------------------------------------------ 交互
    def _goto(self, step: int) -> None:
        self._step = max(0, min(step, len(_STEPS) - 1))
        self.stack.setCurrentIndex(self._step)
        self._fit_to_step()
        # 切页后 sizeHint 要等一次布局才准（新页的控件还没被赋过几何），
        # 这里再排一次收紧，否则从「迁移内容」退回别的步骤会停在上一页的高度。
        QTimer.singleShot(0, self._fit_to_step)
        for i, badge in enumerate(self.badges):
            badge.set_active(i == self._step, done=i < self._step)

        if self._step == 0:
            self.lbl_title.setText("切换到哪个账号")
            self.lbl_sub.setText(
                "切换会关闭并重启 WorkBuddy，登录身份由认证文件决定 —— "
                "不需要重新扫码，但需要本机存过目标账号的凭据。"
            )
        elif self._step == 1:
            self.lbl_title.setText(
                f"要一并迁移哪些内容到「{self._labels.name_of(self._target_uid)}」"
            )
            self.lbl_sub.setText("会话会以新 id 复制一份过去，源账号的那份保持不动。")
        else:
            self.lbl_title.setText("确认切换")
            self.lbl_sub.setText("下面列出即将发生的每一步，确认后开始执行。")

        self.btn_back.setVisible(self._step > 0)
        self.btn_next.setText("开始切换" if self._step == len(_STEPS) - 1 else "下一步")
        self.btn_next.setObjectName("Danger" if self._step == len(_STEPS) - 1 else "Primary")
        self.btn_next.style().unpolish(self.btn_next)
        self.btn_next.style().polish(self.btn_next)
        self.btn_import.setVisible(self._step == 0)
        self._sync_next_enabled()

        if self._step == 1 and self._target_uid:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._recompute_plan()
            finally:
                QApplication.restoreOverrideCursor()
        if self._step == 2:
            self._fill_confirm()

    def _on_target_changed(self) -> None:
        rows = self.tbl_accounts.selectionModel().selectedRows() if self.tbl_accounts.selectionModel() else []
        if not rows:
            self._target_uid = ""
            self._sync_next_enabled()
            return
        idx = rows[0].row()
        item = self.tbl_accounts.item(idx, 0)
        self._target_uid = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        cred = self._store.get(self._target_uid)
        if cred is None:
            self.lbl_target_note.setText(
                "该账号在本机没有登录凭据，无法切换。请先用它登录一次 WorkBuddy"
                "（本工具会自动采集），或用下方的「从其它工具导入凭据」。"
            )
        elif cred.token_form == "信封":
            self.lbl_target_note.setText(f"凭据就绪（{cred.source_text}，{cred.expiry_text()}）。")
            self.lbl_target_note.setStyleSheet(f"color:{theme.PRIMARY_TEXT};font-size:12px;")
        else:
            self.lbl_target_note.setText(
                f"凭据就绪（{cred.source_text}，{cred.expiry_text()}）。"
                "若客户端提示登录失效，需要重新登录该账号。"
            )
            self.lbl_target_note.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:12px;")
        self._sync_next_enabled()

    def _on_copy_toggled(self, _checked: bool) -> None:
        self._refresh_copy_count()

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._tree_guard = True
        try:
            for leaf in self._cid_items.values():
                if leaf.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    leaf.setCheckState(C_NAME, state)
            self._sync_checks()
        finally:
            self._tree_guard = False
        self._refresh_copy_count()

    def _on_import(self) -> None:
        added, updated, skipped = self._store.import_from_buddy_switch()
        from PySide6.QtWidgets import QMessageBox

        if not added and not updated:
            QMessageBox.information(
                self,
                "没有可导入的账号",
                "未找到其它工具的账号库，或里面的账号都已存在。\n\n" + "\n".join(skipped[:5]),
            )
            return
        self._store = vault.Vault.load(self._base)
        self._fill_accounts()
        QMessageBox.information(
            self,
            "导入完成",
            f"新增 {added} 个账号，更新 {updated} 个。\n现在可以直接切换到它们。",
        )

    def _sync_next_enabled(self) -> None:
        if self._step == 0:
            ok = bool(self._target_uid) and self._store.get(self._target_uid) is not None
            self.btn_next.setEnabled(ok)
            if not self._account_rows:
                self.btn_next.setEnabled(False)
        elif self._step == 1:
            ok = True
            if self.chk_copy.isChecked() and self._plan is not None:
                ok = True  # 允许 0 条（此时只切身份），在确认页如实说明
            self.btn_next.setEnabled(ok)
        else:
            self.btn_next.setEnabled(True)

    def _fill_confirm(self) -> None:
        name = self._labels.name_of(self._target_uid)
        self.lbl_confirm_head.setText(f"即将切换到「{name}」")
        lines: list[str] = []

        if self.chk_copy.isChecked():
            picked = self.checked_cids()
            skipped = len(self._plan.duplicates) if self._plan else 0
            if picked:
                lines.append(f"· 把 {len(picked)} 条会话复制到目标账号（新 id，原账号不动）")
            else:
                lines.append("· 不复制任何会话（目标账号的会话列表保持原样）")
            if skipped:
                lines.append(f"· {skipped} 条之前已复制过，本次跳过")
        else:
            lines.append("· 不复制会话")

        if self.chk_memory.isChecked() and self._memory_available:
            lines.append("· 合并长期记忆（只补目标没有的小节与条目）")
        else:
            lines.append("· 不合并长期记忆")

        lines.append("· 备份认证文件与数据库到 wbsync-backup")
        lines.append(f"· 关闭 WorkBuddy，把登录身份写成「{name}」")
        lines.append("· 重新启动 WorkBuddy")
        self.lbl_steps.setText("\n".join(lines))

        cred = self._store.get(self._target_uid)
        notes = []
        if cred is not None and cred.expiry_text().startswith("登录已过期"):
            notes.append("目标账号的登录凭据已过期，重启后很可能需要在客户端重新登录一次。")
        notes.append(
            "重启后若仍在原账号，说明认证文件被客户端写回 —— 请先完全退出客户端再切一次。"
        )
        self.lbl_confirm_note.setText("\n".join(notes))

    # ------------------------------------------------------------ 结果
    def _on_back(self) -> None:
        self._goto(self._step - 1)

    def _on_next(self) -> None:
        if self._step < len(_STEPS) - 1:
            self._goto(self._step + 1)
            return
        self.accept()

    def checked_cids(self) -> list[str]:
        """已勾选的会话 id。顺序沿用计划里的原顺序，方便确认页按空间复述。"""
        out: list[str] = []
        for cid, leaf in self._cid_items.items():
            if not (leaf.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if leaf.checkState(C_NAME) == Qt.CheckState.Checked:
                out.append(cid)
        return out

    # ------------------------------------------------------------ 输出
    @property
    def target_uid(self) -> str:
        return self._target_uid

    @property
    def do_copy(self) -> bool:
        return self.chk_copy.isChecked()

    @property
    def do_memory(self) -> bool:
        return self.chk_memory.isChecked() and self._memory_available

    def copy_cids(self) -> list[str] | None:
        """None = 不复制；列表（可能为空）= 复制勾选的这些。"""
        if not self.chk_copy.isChecked():
            return None
        return self.checked_cids()


def _bold(size: int) -> QFont:
    f = QFont()
    f.setPointSize(size)
    f.setWeight(QFont.Weight.DemiBold)
    return f


def _semi(size: int) -> QFont:
    """分组行用的小号半粗 —— 比正文重一点就够，整行加粗会把层级压过标题。"""
    f = QFont()
    f.setPointSizeF(size * 0.85)
    f.setWeight(QFont.Weight.DemiBold)
    return f
