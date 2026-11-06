"""同步包导入确认 —— 三节可分别取舍，会话保持三级树。

## 为什么分节开关 + 逐项勾选

包是「全量整包」（导出侧只有一种产物），但**写多少应该由接收方决定**：
有人只想把这台机器的配置搬过来，有人只要那两天的工作记忆。
所以三节各有开关，节内还能逐条挑 —— 勾谁写谁，不勾的连计划都不生成。

## 为什么会话要分三级

会话标题大量重复（"点击没有反应"、"第一条会话"），平铺一列几十条认不出谁是谁。
用户是靠「我那天在哪个目录干活」回忆的：对话区看日期（时间戳目录名），
工作区看空间名，与客户端左栏逐行对得上。三级都带三态勾选，一级就是批量开关。

## 客户端前置条件的不对称

会话必须零进程（SQLite 文件级替换），配置只是文本覆写、技术上不强制。
但客户端退出时会用内存里的配置回写，不关会表现为「同步成功却不生效」。
所以：勾了会话 → 硬性拦截；只导配置/记忆 → 不拦截。

## 为什么没有「仅演练」

演练结果对普通用户是一张写着「将写入 N 条」的表，与导入后的报告没有区别，
却要多点一次、多关一次客户端。引擎的 dry_run 仍然保留，只服务于测试。
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import accounts, guard, importer, memory_pack, spaces
from . import restart_option, theme
from .config_dialog import ConfigActionTable
from .cwd_remap_widget import CwdRemapWidget

_WARNING_ACTIONS = {"diverged", "unknown", "no-cwd"}

# 三节的键，顺序即步骤顺序
SEC_SESSIONS = "sessions"
SEC_CONFIG = "config"
SEC_MEMORY = "memory"


@dataclass
class _Leaf:
    """把 ImportAction 包成 spaces.group_sessions 认识的形状。

    分组只用到 cwd 与最近活动时间，但 ImportAction 上没有这两个字段
    （cwd 在 session 上、时间是 row 里的毫秒戳），在这里摊平一次，
    免得把分组逻辑改成两套。
    """

    action: object

    @property
    def cwd(self) -> str:
        a = self.action
        return str(getattr(a, "remapped_cwd", "") or a.session.cwd or "")

    @property
    def updated_at(self) -> int:
        row = getattr(self.action.session, "row", {}) or {}
        try:
            return int(row.get("updated_at") or 0)
        except (TypeError, ValueError):
            return 0


class SectionCard(QFrame):
    """一张分节卡：整节开关 + 计数 + 一句后果说明。

    勾选框只负责**显示三态**（全选/部分/全不选），不接收鼠标 —— 点击事件
    统一由卡片处理，否则点在小方框上会走 Qt 自己的三态循环，语义变成
    「部分 → 全选 → 部分」，和用户以为的「开/关」对不上。
    """

    toggled = Signal()

    def __init__(self, key: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self._on = True
        self.setObjectName("SectionCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        v = QVBoxLayout(self)
        v.setContentsMargins(11, 9, 11, 9)
        v.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(7)
        self.check = QCheckBox(title)
        self.check.setTristate(True)
        self.check.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(12)
        self.check.setFont(f)
        row.addWidget(self.check)
        row.addStretch(1)
        v.addLayout(row)

        self.lbl_count = QLabel()
        self.lbl_count.setStyleSheet(f"font-size:11px;color:{theme.TEXT_TERTIARY};")
        self.lbl_hint = QLabel()
        self.lbl_hint.setStyleSheet(f"font-size:11px;color:{theme.TEXT_SECONDARY};")
        self.lbl_hint.setWordWrap(True)
        v.addWidget(self.lbl_count)
        v.addWidget(self.lbl_hint)
        self._apply()

    # ---------------------------------------------------------- 状态
    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        if self._on == on:
            return
        self._on = bool(on)
        self._apply()
        self.toggled.emit()

    def toggle(self) -> None:
        self.set_on(not self._on)

    def set_summary(self, count_text: str, hint: str, selected: int, total: int) -> None:
        self.lbl_count.setText(count_text)
        self.lbl_hint.setText(hint)
        if not self._on:
            self.check.setCheckState(Qt.CheckState.Unchecked)
        elif selected <= 0:
            self.check.setCheckState(Qt.CheckState.Unchecked)
        elif selected >= total:
            self.check.setCheckState(Qt.CheckState.Checked)
        else:
            self.check.setCheckState(Qt.CheckState.PartiallyChecked)

    def _apply(self) -> None:
        if self._on:
            self.setStyleSheet(
                "QFrame#SectionCard{background:#F5FCF8;border:1px solid "
                f"{theme.PRIMARY};border-radius:9px;}}"
            )
        else:
            self.setStyleSheet(
                "QFrame#SectionCard{background:#FBFBFC;border:1px dashed #D8DDE4;"
                "border-radius:9px;}"
            )
            self.check.setCheckState(Qt.CheckState.Unchecked)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
        super().mousePressEvent(event)


class SyncPlanDialog(QDialog):
    """package 为 pack.Package；三节的动作都可能为空列表。"""

    def __init__(
        self,
        package,
        session_actions,
        config_actions,
        cwd_remaps=None,
        parent=None,
        memory_actions=None,
    ) -> None:
        super().__init__(parent)
        self._pkg = package
        self._sessions = list(session_actions or [])
        # 这个入口没有取舍界面，所以「拿包内覆盖本机」一律降级为「保留本机」——
        # 与 execute_import 调同一个函数，避免表上写「取包内」而实际没覆盖。
        self._div_downgraded = importer.downgrade_div_overwrite(self._sessions)
        self._config = list(config_actions or [])
        self._memory = list(memory_actions or [])
        self._cwd_remaps = list(cwd_remaps or [])
        self._dry_run = False
        # 目标账号只解析一次：会话归属与「重启后进入哪个账号」是同一个判断，
        # 分两处各调一次 detect_primary_uid 会在用户切号的瞬间给出互相矛盾的文案
        self._labels = accounts.AccountLabels.load()
        self._target_uid = accounts.detect_primary_uid(snapshot=self._labels.snapshot)

        self._on = {
            SEC_SESSIONS: bool(self._sessions),
            SEC_CONFIG: bool(self._config),
            SEC_MEMORY: bool(self._memory),
        }
        self._step_index = 0
        self._steps: list[tuple[str, str, QWidget]] = []

        self.setWindowTitle("导入同步包")
        self.resize(960, 660)
        self._build()
        self._rebuild_steps()
        self._refresh_summary()
        self._refresh_client_state()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(9)

        layout.addWidget(self._build_head())
        layout.addWidget(self._build_rehome_hint())
        layout.addLayout(self._build_cards())

        self._stepper = QWidget()
        self._stepper_layout = QHBoxLayout(self._stepper)
        self._stepper_layout.setContentsMargins(0, 2, 0, 0)
        self._stepper_layout.setSpacing(4)
        layout.addWidget(self._stepper)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        self._page_tools = QHBoxLayout()
        self._page_tools.setSpacing(6)
        self.lbl_pane = QLabel()
        self.lbl_pane.setStyleSheet(f"font-size:11.5px;color:{theme.TEXT_SECONDARY};")
        self._page_tools.addWidget(self.lbl_pane, 1)
        self.btn_all = QPushButton("全选本页")
        self.btn_none = QPushButton("全不选")
        for b in (self.btn_all, self.btn_none):
            b.setFixedHeight(24)
            b.setStyleSheet("font-size:11.5px;padding:1px 10px;")
            self._page_tools.addWidget(b)
        self.btn_all.clicked.connect(lambda: self._set_page_all(True))
        self.btn_none.clicked.connect(lambda: self._set_page_all(False))
        layout.addLayout(self._page_tools)

        # 客户端状态（全局，不随步骤变化）
        state = QHBoxLayout()
        self.lbl_state = QLabel()
        self.lbl_state.setWordWrap(True)
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setToolTip("你已经手动退出 WorkBuddy 的话，点这里刷新状态")
        self.btn_recheck.clicked.connect(self._refresh_client_state)
        state.addWidget(self.lbl_state, 1)
        state.addWidget(self.btn_recheck)
        layout.addLayout(state)

        self.chk_restart = QCheckBox()
        self._apply_restart_option()
        self.chk_restart.toggled.connect(self._refresh_client_state)
        layout.addWidget(self.chk_restart)

        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("font-size: 12px; color: #333;")

        btns = QDialogButtonBox()
        self.btn_prev = btns.addButton("上一步", QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_next = btns.addButton("下一步 →", QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_prev.clicked.connect(self._prev)
        self.btn_next.clicked.connect(self._next)
        btns.rejected.connect(self.reject)
        self.btn_next.setObjectName("Primary")
        self.btn_prev.setVisible(False)

        bar = QHBoxLayout()
        bar.addWidget(self.lbl_summary, 1)
        bar.addWidget(btns)
        layout.addLayout(bar)

    def _build_head(self) -> QWidget:
        pkg = self._pkg
        uid = self._target_uid
        labels = self._labels
        if uid:
            target = labels.display(uid)
        else:
            target = "（未识别到本机账号，将保留源账号归属）"

        src = pkg.source_label or "（未知机器）"
        when = _fmt_ts(pkg.created_at)
        lines = [
            f"来自：{src}    导出时间：{when}",
            f"包内容：{pkg.describe()}",
            "下面选择这次要导入哪些内容：整节可以跳过，节内也能逐条挑。",
        ]
        if pkg.has_sessions:
            # 归属改写必须提前讲清楚，否则用户会以为包里的账号信息丢了
            lines.append(f"会话导入后统一归属到「{target}」，不保留源账号")
        if pkg.has_config:
            lines.append("配置项按逐项判定写入，冲突项需你选择处理方式")
        if pkg.has_memory:
            lines.append("工作空间记忆按更新时间保留最新，覆盖前自动备份")

        head = QLabel("\n".join(lines))
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 12px; color: #444;")
        return head

    def _build_rehome_hint(self) -> QLabel:
        """跨账号导入的代价：归属被改到当前账号，原来那个账号就看不到它了。

        ★ 这是 `REHOME` 的既定语义（「整合到本机当前登录账号」），不是缺陷 ——
          但「在 B 机导一次，A 机那条就没了」对用户是反直觉的，必须动手前说。
          措辞只承诺代码真的会做的事：改 user_id。侧边栏快照本工具不碰，
          所以客户端刷新快照之前那条可能还挂在原账号下面。
        """
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"font-size:12px;color:{theme.WARN};background:{theme.WARN_SOFT};"
            "border-radius:4px;padding:7px 9px;"
        )
        moved = [a for a in self._sessions if a.action == "rehome"]
        if not moved or not self._target_uid:
            lbl.setVisible(False)
            return lbl
        target = self._labels.display(self._target_uid)
        names = "、".join(a.session.title or a.session.cid[:8] for a in moved[:3])
        tail = f" 等 {len(moved)} 条" if len(moved) > 3 else ""
        lbl.setText(
            f"其中 {len(moved)} 条会话已存在于本机「其它账号」（{names}{tail}）。"
            f"导入会把它们的归属改到「{target}」：那是「搬走」不是「复制」——"
            f"原来那个账号里将不再有它们。若想两边都留一份，请改用「本机账号对齐」。"
        )
        return lbl

    def _build_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        self._cards: dict[str, SectionCard] = {}
        specs = [
            (SEC_SESSIONS, "会话"),
            (SEC_CONFIG, "配置"),
            (SEC_MEMORY, "工作空间记忆"),
        ]
        for key, title in specs:
            card = SectionCard(key, title, self)
            card.toggled.connect(self._on_section_toggled)
            self._cards[key] = card
            row.addWidget(card)
        return row

    # ------------------------------------------------------------ 三个页面
    def _build_sessions_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)

        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["标题", "判定"])
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(False)
        tree.setAlternatingRowColors(False)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        # 以 id(动作) 为键：取值时按 self._sessions 的原顺序走，
        # 不受遍历字典顺序影响（导入顺序会写进报告，得稳定）
        self._sess_items: dict[int, QTreeWidgetItem] = {}

        grouped = spaces.group_sessions([_Leaf(a) for a in self._sessions])
        bounds = [
            (spaces.CHAT_LABEL, grouped.chat_groups),
            (spaces.SPACE_ROOT_LABEL, grouped.spaces),
        ]
        for root_label, groups in bounds:
            if not groups:
                continue
            total = sum(g.count for g in groups)
            root = QTreeWidgetItem([f"{root_label}（{total}）", ""])
            self._mark_group(root)
            tree.addTopLevelItem(root)
            for group in groups:
                node = QTreeWidgetItem([f"{group.label}（{group.count}）", ""])
                self._mark_group(node)
                node.setToolTip(0, group.path or "（没有记录工作目录）")
                root.addChild(node)
                for item in group.items:
                    act = getattr(item, "action", None)
                    if act is None:
                        continue
                    leaf = QTreeWidgetItem([act.session.title, act.label])
                    leaf.setToolTip(
                        0,
                        f"{act.session.title}\n{act.session.cid}\n"
                        f"{act.session.cwd or '（没有记录工作目录）'}",
                    )
                    leaf.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    # 默认只勾「真的会写」的：分叉/未知只能降级处理，
                    # 内容重复会在侧栏里多出一份，都得人点头。
                    leaf.setCheckState(
                        0,
                        Qt.CheckState.Checked
                        if act.will_write
                        else Qt.CheckState.Unchecked,
                    )
                    self._paint_action(leaf, act)
                    node.addChild(leaf)
                    self._sess_items[id(act)] = leaf
                # 二级默认展开：勾选是这一屏的主要动作，
                # 收起来只剩组名，用户还得再点一次才知道要导什么。
                node.setExpanded(True)
            root.setExpanded(True)

        self._sync_groups(tree)
        tree.itemChanged.connect(lambda _i, _c: self._refresh_summary())
        v.addWidget(tree, 1)
        self.session_tree = tree

        self.chk_dups = QCheckBox("仍要导入内容重复的会话（导入后侧栏里会有两份）")
        self.chk_dups.setVisible(False)  # 逐条勾选取代了这个总开关
        v.addWidget(self.chk_dups)
        return page

    def _mark_group(self, item: QTreeWidgetItem) -> None:
        """一级/二级节点：三态由 Qt 的 AutoTristate 自动向上汇总。"""
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsAutoTristate
        )
        item.setCheckState(0, Qt.CheckState.Checked)
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)

    def _paint_action(self, item: QTreeWidgetItem, act) -> None:
        if act.action in _WARNING_ACTIONS:
            item.setForeground(1, Qt.GlobalColor.darkRed)
        elif act.overridable:
            item.setForeground(1, Qt.GlobalColor.darkYellow)
        elif act.will_write:
            item.setForeground(1, Qt.GlobalColor.darkGreen)
        else:
            item.setForeground(1, Qt.GlobalColor.gray)

    def _build_memory_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)

        note = QLabel(
            "记忆按「保留最新」写入：目标不存在→新增；内容一致→跳过；"
            "同名但内容不同→比较更新时间，保留较新的那份（覆盖前自动备份，可回溯）。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size:12px;color:{theme.WARN};background:{theme.WARN_SOFT};"
            "border-radius:4px;padding:7px 9px;"
        )
        v.addWidget(note)

        tree = QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels(["工作空间 / 文件", "动作", "目标路径（可改）"])
        tree.setRootIsDecorated(True)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        self._mem_items: dict[int, QTreeWidgetItem] = {}
        self._mem_edits: dict[str, QLineEdit] = {}

        for ws, acts in self._memory_groups():
            node = QTreeWidgetItem([f"{ws.label or ws.wid}（{len(acts)} 个文件）", "", ""])
            self._mark_group(node)
            tree.addTopLevelItem(node)
            root = acts[0].root if acts else ""
            edit = QLineEdit(root)
            edit.setToolTip("导入后写入这个项目目录下的 .workbuddy/memory/")
            edit.textChanged.connect(lambda _t: self._refresh_summary())
            self._mem_edits[ws.wid] = edit
            tree.setItemWidget(node, 2, edit)
            for act in acts:
                leaf = QTreeWidgetItem([act.file.name, act.label, ""])
                leaf.setToolTip(0, f"{act.file.name}\n{act.reason}")
                leaf.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                leaf.setCheckState(0, Qt.CheckState.Checked)
                if act.will_write:
                    leaf.setForeground(1, Qt.GlobalColor.darkGreen)
                else:
                    leaf.setForeground(1, Qt.GlobalColor.gray)
                node.addChild(leaf)
                self._mem_items[id(act)] = leaf
            node.setExpanded(True)

        self._sync_groups(tree)
        tree.itemChanged.connect(lambda _i, _c: self._refresh_summary())
        v.addWidget(tree, 1)
        self.memory_tree = tree
        return page

    def _memory_groups(self) -> list[tuple[object, list]]:
        """按工作空间归组记忆动作（保持出现顺序）。"""
        by_wid: dict[str, tuple[object, list]] = {}
        order: list[str] = []
        for a in self._memory:
            wid = a.workspace.wid
            if wid not in by_wid:
                by_wid[wid] = (a.workspace, [])
                order.append(wid)
            by_wid[wid][1].append(a)
        return [by_wid[w] for w in order]

    # ------------------------------------------------------------ 步骤
    def _ensure_pages(self) -> None:
        if not hasattr(self, "_page_session"):
            self._page_session = self._build_sessions_page()
        if self._cwd_remaps and not hasattr(self, "_page_remap"):
            self.remap_widget = CwdRemapWidget(self._cwd_remaps)
            self._page_remap = self.remap_widget
        if self._config and not hasattr(self, "_page_config"):
            self.config_table = ConfigActionTable(self._config, checkable=True)
            self.config_table.changed.connect(self._refresh_summary)
            self._page_config = self.config_table
        if self._memory and not hasattr(self, "_page_memory"):
            self._page_memory = self._build_memory_page()

    def _rebuild_steps(self) -> None:
        """按当前勾选重建步骤条与页面栈。"""
        self._ensure_pages()
        self._steps = []
        if self._on[SEC_SESSIONS] and self._sessions:
            self._steps.append(("sessions", "会话", self._page_session))
            if getattr(self, "_page_remap", None) is not None:
                self._steps.append(("remap", "工作空间映射", self._page_remap))
        if self._on[SEC_CONFIG] and self._config:
            self._steps.append(("config", "配置", self._page_config))
        if self._on[SEC_MEMORY] and self._memory:
            self._steps.append(("memory", "记忆", self._page_memory))

        while self._stack.count():
            self._stack.removeWidget(self._stack.widget(0))
        for _kind, _title, w in self._steps:
            self._stack.addWidget(w)

        # 步骤条按钮每次重建：节一关就少一颗，按钮上写着条数，重建最省心
        while self._stepper_layout.count():
            item = self._stepper_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._step_buttons: list[QPushButton] = []
        for i, (_kind, title, _w) in enumerate(self._steps):
            btn = QPushButton()
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFlat(True)
            btn.clicked.connect(lambda _=False, idx=i: self._goto_step(idx))
            self._step_buttons.append(btn)
            self._stepper_layout.addWidget(btn)
            if i < len(self._steps) - 1:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setFixedWidth(14)
                line.setStyleSheet("color:#E3E7EC;")
                self._stepper_layout.addWidget(line)
        self._stepper_layout.addStretch(1)
        self._stepper.setVisible(len(self._steps) > 1)

        if self._steps:
            self._step_index = min(self._step_index, len(self._steps) - 1)
            self._stack.setCurrentIndex(self._step_index)
        else:
            self._step_index = 0
        self._sync_page_tools()

    def _sync_page_tools(self) -> None:
        kind = self._steps[self._step_index][0] if self._steps else ""
        # 映射页没有逐项勾选（每行自带勾选框，由控件自己管）
        has_items = kind in ("sessions", "config", "memory")
        self.btn_all.setVisible(has_items)
        self.btn_none.setVisible(has_items)
        self.lbl_pane.setText(_PANE_LEAD.get(kind, ""))

    def _set_page_all(self, on: bool) -> None:
        kind = self._steps[self._step_index][0] if self._steps else ""
        if kind == "sessions":
            for leaf in self._sess_items.values():
                leaf.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        elif kind == "memory":
            for leaf in self._mem_items.values():
                leaf.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        elif kind == "config":
            self.config_table.set_all_checked(on)

    # ------------------------------------------------------------ 计数
    def _checked_session_actions(self) -> list:
        out = []
        for act in self._sessions:
            leaf = self._sess_items.get(id(act))
            if leaf is not None and leaf.checkState(0) == Qt.CheckState.Checked:
                out.append(act)
        return out

    def _checked_memory_actions(self) -> list:
        out = []
        for act in self._memory:
            leaf = self._mem_items.get(id(act))
            if leaf is not None and leaf.checkState(0) == Qt.CheckState.Checked:
                out.append(act)
        return out

    def _sync_groups(self, tree: QTreeWidget) -> None:
        """铺完后按子项算一次父节点状态。

        父节点在子项加进来之前就被设成 Checked，若不等子项铺完再归位，
        第一次打开会看到「父节点是勾、子节点一半没勾」的矛盾画面。
        """
        def walk(item: QTreeWidgetItem) -> Qt.CheckState:
            kids = [item.child(i) for i in range(item.childCount())]
            if not kids:
                return item.checkState(0)
            states = {walk(k) for k in kids}
            if states == {Qt.CheckState.Checked}:
                item.setCheckState(0, Qt.CheckState.Checked)
            elif states == {Qt.CheckState.Unchecked}:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            return item.checkState(0)

        for i in range(tree.topLevelItemCount()):
            walk(tree.topLevelItem(i))

    def _session_writes(self) -> int:
        if not self._on[SEC_SESSIONS]:
            return 0
        return sum(
            1
            for a in self._checked_session_actions()
            if a.will_write or a.overridable
        )

    def _config_writes(self) -> int:
        if not self._on[SEC_CONFIG] or not hasattr(self, "config_table"):
            return 0
        return self.config_table.stats()[0]

    def _memory_writes(self) -> int:
        if not self._on[SEC_MEMORY]:
            return 0
        return sum(1 for a in self._checked_memory_actions() if a.will_write)

    def _refresh_summary(self) -> None:
        """刷新当前步骤摘要、分节卡、步骤条与主按钮。"""
        self._update_cards()
        s_w = self._session_writes()
        c_w = self._config_writes()
        m_w = self._memory_writes()
        total = s_w + c_w + m_w

        self.lbl_summary.setText(self._step_summary_text())
        last = self._step_index >= len(self._steps) - 1
        self.btn_next.setText("执行导入" if last else "下一步 →")
        self.btn_next.setEnabled(total > 0 if last else True)
        self.btn_prev.setVisible(self._step_index > 0)
        self._update_stepper()
        self.chk_restart.setVisible(bool(self._on[SEC_SESSIONS]))

    def _update_cards(self) -> None:
        """刷新三张卡的计数与三态。只画状态，不改用户意图（否则会互相触发）。"""
        s_sel, s_tot = self._session_writes(), len(self._sessions)
        c_sel = (
            self.config_table.selected_count() if hasattr(self, "config_table") else 0
        )
        c_tot = len(self._config)
        m_sel = len(self._checked_memory_actions()) if hasattr(self, "_mem_items") else 0
        m_tot = len(self._memory)
        self._paint_card(
            SEC_SESSIONS,
            f"{s_sel} / {s_tot} 条",
            _counts_text(self._sessions) or "包里没有会话",
            s_sel,
            s_tot,
        )
        self._paint_card(
            SEC_CONFIG,
            f"{c_sel} / {c_tot} 项",
            _counts_text(self._config) or "包里没有配置项",
            c_sel,
            c_tot,
        )
        self._paint_card(
            SEC_MEMORY,
            f"{m_sel} / {m_tot} 个文件",
            f"{len(self._memory_groups())} 个工作空间",
            m_sel,
            m_tot,
        )

    def _paint_card(
        self, key: str, count_text: str, hint: str, selected: int, total: int
    ) -> None:
        card = self._cards[key]
        card.set_summary(count_text, hint, selected, total)
        card.setEnabled(total > 0)

    def _step_summary_text(self) -> str:
        n = len(self._steps)
        if n == 0:
            return "没有勾选任何内容"
        head = f"第 {self._step_index + 1} / {n} 步"
        kind = self._steps[self._step_index][0]
        if kind == "sessions":
            blocked = sum(1 for a in self._checked_session_actions() if a.blocking)
            notes = []
            if blocked:
                notes.append(f"{blocked} 条仍会被拒绝")
            body = f"将写入 {self._session_writes()} 条会话"
            if notes:
                body += "（" + "、".join(notes) + "）"
        elif kind == "config":
            _w, conflicts = self.config_table.stats()
            body = f"将写入 {self._config_writes()} 项配置"
            if conflicts:
                body += f"（冲突 {conflicts} 项）"
        elif kind == "memory":
            body = f"将写入 {self._memory_writes()} 个记忆文件"
        else:
            body = f"{len(self._cwd_remaps)} 条工作空间映射"
        return f"{head}　{body}"

    def _step_count(self, i: int) -> str:
        kind = self._steps[i][0]
        if kind == "sessions":
            return f"{self._session_writes()} / {len(self._sessions)} 条"
        if kind == "config":
            total = len(self._config)
            sel = self.config_table.selected_count() if hasattr(self, "config_table") else 0
            return f"{sel} / {total} 项"
        if kind == "memory":
            return f"{self._memory_writes()} / {len(self._memory)} 文件"
        return f"{len(self._cwd_remaps)} 条"

    def _update_stepper(self) -> None:
        for i, btn in enumerate(self._step_buttons):
            _kind, title, _w = self._steps[i]
            count = self._step_count(i)
            if i == self._step_index:
                mark = "●"
                style = (
                    "QPushButton{background:#E9F6EE;color:#15803D;border:1px solid #CDEBD7;"
                    "border-radius:14px;padding:4px 12px;font-size:12px;font-weight:600;}"
                )
                btn.setEnabled(True)
            elif i < self._step_index:
                mark = "✓"
                style = (
                    "QPushButton{background:#FFFFFF;color:#5B6573;border:1px solid #E3E7EC;"
                    "border-radius:14px;padding:4px 12px;font-size:12px;}"
                )
                btn.setEnabled(True)   # 已完成步骤可点回看
            else:
                mark = str(i + 1)
                style = (
                    "QPushButton{background:#FFFFFF;color:#9AA3AE;border:1px solid #F1F3F6;"
                    "border-radius:14px;padding:4px 12px;font-size:12px;}"
                )
                btn.setEnabled(False)  # 未到达的步骤不可前跳
            btn.setText(f"{mark}  {title} · {count}")
            btn.setStyleSheet(style)

    def _on_section_toggled(self) -> None:
        for key, card in self._cards.items():
            self._on[key] = card.is_on()
        self._rebuild_steps()
        self._refresh_summary()

    def _apply_restart_option(self) -> None:
        restart_option.apply(self.chk_restart, self._labels, self._target_uid)

    # ------------------------------------------------------------ 导航
    def _goto_step(self, idx: int) -> None:
        """只允许回看已到达的步骤；向前跳一律拒绝。"""
        if idx < 0 or idx >= len(self._steps) or idx > self._step_index:
            return
        self._step_index = idx
        self._stack.setCurrentIndex(idx)
        self._sync_page_tools()
        self._refresh_summary()

    def _prev(self) -> None:
        if self._step_index > 0:
            self._step_index -= 1
            self._stack.setCurrentIndex(self._step_index)
            self._sync_page_tools()
            self._refresh_summary()

    def _next(self) -> None:
        if self._step_index < len(self._steps) - 1:
            self._step_index += 1
            self._stack.setCurrentIndex(self._step_index)
            self._sync_page_tools()
            self._refresh_summary()
        else:
            self._on_ok()

    def _refresh_client_state(self) -> None:
        procs = guard.running_client_processes()
        running = bool(procs)
        has_sessions = self._session_writes() > 0

        if has_sessions:
            if running:
                # ★ 不禁用按钮：导入编排（main_window._run_import）本来就会先调
                #   closeflow 关掉客户端，写完再按勾选决定是否重启。在这里要求
                #   用户「先手动退出」，既多余，又与「导入后自动重启」自相矛盾 ——
                #   用户看着已勾选的重启选项，却被一个灰按钮挡住。
                target = self._labels.display(self._target_uid) if self._target_uid else "当前账号"
                tail = (
                    f"写入完成后会自动重启，并回到「{target}」。"
                    if self.chk_restart.isChecked()
                    else "写入完成后不会自动启动，需要你手动打开。"
                )
                self.lbl_state.setText(
                    f"WorkBuddy 正在运行（{'、'.join(procs)}）。点「执行导入」时会先自动关闭它"
                    f"（一般 20 秒内），{tail}"
                )
                self.lbl_state.setStyleSheet("font-size: 12px; color: #8a6d00;")
                self.btn_next.setToolTip(
                    "执行前会先关闭 WorkBuddy：未发送的输入会丢失、正在进行的对话会中断"
                )
            else:
                self.lbl_state.setText("✓ 未检测到 WorkBuddy 进程，可以安全写入")
                self.lbl_state.setStyleSheet("font-size: 12px; color: #1b7f3b;")
                self.btn_next.setToolTip("")
            self._refresh_summary()
        else:
            # 不写会话：不拦截，但把风险说明白
            if running:
                self.lbl_state.setText(
                    "提醒：WorkBuddy 正在运行。配置与记忆是文本覆写，不会损坏数据，"
                    "但客户端可能用内存中的配置回写，导致「同步成功却不生效」。建议退出后再同步。"
                )
                self.lbl_state.setStyleSheet("font-size: 12px; color: #8a6d00;")
            else:
                self.lbl_state.setText("✓ 未检测到 WorkBuddy 进程，可直接写入")
                self.lbl_state.setStyleSheet("font-size: 12px; color: #1b7f3b;")
            self.btn_next.setToolTip("")

    def _on_ok(self) -> None:
        s_w = self._session_writes()
        c_w = self._config_writes()
        m_w = self._memory_writes()
        if s_w + c_w + m_w == 0:
            self.reject()
            return

        msg = []
        if s_w:
            msg.append(f"{s_w} 条会话")
        if c_w:
            msg.append(f"{c_w} 项配置")
        if m_w:
            msg.append(f"{m_w} 个记忆文件")

        text = "即将写入 " + " 和 ".join(msg) + "。"
        blocked = sum(
            1 for a in self._checked_session_actions() if a.blocking
        )
        if blocked:
            text += f"\n另有 {blocked} 条勾选的会话因分叉/无法判定会被拒绝，不会被改动。"
        moved = sum(
            1 for a in self._checked_session_actions() if a.action == "rehome"
        )
        if moved:
            text += f"\n其中 {moved} 条会从其它账号改归当前账号 —— 那是搬走，原账号里将看不到它们。"
        text += "\n\n写入前会自动备份，但导入本身不可撤销。继续？"

        ans = QMessageBox.question(self, "确认导入", text)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._dry_run = False
        self.accept()

    # ------------------------------------------------------------ 结果
    @property
    def dry_run(self) -> bool:
        """对话框不再提供演练入口；保留属性给调用方，恒为 False。"""
        return self._dry_run

    @property
    def auto_restart(self) -> bool:
        return self.chk_restart.isChecked()

    @property
    def include_duplicates(self) -> bool:
        """勾了「内容重复」的条目就允许写入 —— 逐条勾选取代了原来的总开关。"""
        return any(
            a.overridable for a in self._checked_session_actions()
        )

    def session_actions(self) -> list:
        """只返回勾中的会话动作。"""
        if not self._on[SEC_SESSIONS]:
            return []
        return self._checked_session_actions()

    def config_actions(self) -> list:
        if not self._on[SEC_CONFIG] or not hasattr(self, "config_table"):
            return []
        return self.config_table.selected_actions()

    def memory_actions(self) -> list:
        if not self._on[SEC_MEMORY]:
            return []
        return self._checked_memory_actions()

    def config_choices(self) -> dict[str, str]:
        if not hasattr(self, "config_table"):
            return {}
        return self.config_table.choices()

    def cwd_choices(self) -> dict[str, str]:
        """返回勾选且非空的「源 cwd → 本机路径」映射。"""
        if hasattr(self, "remap_widget") and self._on[SEC_SESSIONS]:
            return self.remap_widget.choices()
        return {}

    def memory_path_map(self) -> dict[str, str]:
        """返回「源工作空间路径 → 目标项目目录」映射（沿用编辑框里的值）。"""
        out: dict[str, str] = {}
        for wid, edit in getattr(self, "_mem_edits", {}).items():
            text = edit.text().strip()
            if not text:
                continue
            for ws, _acts in self._memory_groups():
                if ws.wid == wid:
                    out[ws.path] = text
                    break
        return out


_PANE_LEAD = {
    "sessions": "按「对话 / 工作区 → 日期·空间 → 会话」三级展开；勾一级等于勾它下面全部，勾一半显示为半选。",
    "remap": "源工作空间在本机不存在时才需要映射；不勾的条目会落到包里记录的路径。",
    "config": "逐项决定写不写；冲突项要先在右侧选处理方式，否则按「跳过（保持现状）」处理。",
    "memory": "同一工作空间里可以只搬 MEMORY.md，不搬日期日志；目标路径可改。",
}


def _counts_text(actions) -> str:
    """「新增 8 · 追加 1 · 分叉 1」——按判定聚合，一眼看出包里是什么。"""
    counts: dict[str, int] = {}
    for a in actions or ():
        label = getattr(a, "label", "") or ""
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
    return " · ".join(f"{k} {n}" for k, n in sorted(counts.items()))


def _fmt_ts(value: object) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError, TypeError):
        return "（未知时间）"
