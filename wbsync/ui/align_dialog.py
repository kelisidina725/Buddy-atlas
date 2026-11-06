"""本机账号对齐 —— 四步向导：选账号 → 选会话 → 确认附加内容 → 执行。

## 为什么从一个对话框变成四步

v1.6.2 的形态是一张"计划页"：左边账号树（可多选）、右边说明、下面一排按钮。
它把三件事挤在一屏里：**写给谁**、**写哪些会话**、**能不能动手**。用户看到
「新增 60 条会话」时无法判断这 60 条里有几条是重复的、几条是云端拿不到正文的，
只能点执行赌一把 —— 而这是纯加法写盘，赌错的代价是多一份实体正文。

四步把它拆开，每步只问一件事，且每步都能看见自己刚才选了什么。

## 账号为什么是单选（v1.6.11）

一次对齐的语义是「把当前登录账号的数据复制给**一个**账号」。多选时每个接收方
都要占一份独立的正文（N 个账号 ≈ 库总量 × N），而界面上只有一个「预计新增
60.0 MB」的数字 —— 那个数字在多选下永远是错的。所以改成单选框：选一个，
页脚那个数字就是准的。

## 第 3 步为什么是「人格与记忆」而不是 22 项配置

`SOUL / IDENTITY / USER / BOOTSTRAP` 与长期记忆**是按账号存的**
（`user-<uid>-personal/`），所以才谈得上"写给谁"。
技能目录与设置项存在 `~/.workbuddy` 根下，是**这台电脑**的配置，不属于任何账号，
也复制不出第二份 —— 想搬它们请用「导出同步包」。界面里对这一点有一句话说明，
而不是拿一堆勾不动的行凑数。

## 执行放在第 4 步，而不是关掉对话框之后

关客户端、备份、复制、重启是一条连续动作。放在向导内部才能一边跑一边显示
「正在复制第 N 条」，失败了也停在原地让你看见原因，而不是弹一个事后对话框。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import accounts as accounts_mod
from .. import align
from .. import spaces as spaces_mod
from . import icons, theme, tree_util, winsize
from .format import human_bytes

STEPS = ("选择账号", "选择会话", "确认配置", "执行")

PAGE_ACCOUNT, PAGE_SESSIONS, PAGE_EXTRAS, PAGE_RUN = range(4)

# 按账号存的人格文件（`user-<uid>-personal/` 下）。顺序即展示顺序：
# SOUL 决定"你是谁"，IDENTITY 是名字与形态，USER 是关于用户的画像，BOOTSTRAP 是首启引导。
PERSONA_FILES = (
    ("SOUL.md", "SOUL 人格"),
    ("IDENTITY.md", "IDENTITY 身份"),
    ("USER.md", "USER 用户画像"),
    ("BOOTSTRAP.md", "BOOTSTRAP 引导"),
)

_BADGE_STYLE = {
    "ok": (theme.PRIMARY_SOFT, theme.PRIMARY_TEXT),
    "warn": (theme.WARN_SOFT, theme.WARN),
    "muted": (theme.BG_HOVER, theme.TEXT_TERTIARY),
    "bad": (theme.DANGER_SOFT, theme.DANGER),
}


def _badge(text: str, tone: str = "muted") -> QLabel:
    bg, fg = _BADGE_STYLE.get(tone, _BADGE_STYLE["muted"])
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{fg};background:{bg};border-radius:4px;padding:1px 6px;font-size:11px;"
    )
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lbl


def _badge_of(item) -> tuple[str, str]:
    """一条会话在清单里的徽标：(文字, 色调)。"""
    if not item.selectable:
        return ("不可复制", "bad")
    if item.duplicate:
        return ("已复制过", "warn")
    if getattr(item, "body_bytes", 0):
        return ("有正文", "ok")
    return ("缺正文", "muted")


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- 后台线程


class PlanWorker(QThread):
    """后台算计划。只读，不写任何文件。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, params: dict, parent=None) -> None:
        super().__init__(parent)
        self._params = params

    def run(self) -> None:
        try:
            self.done.emit(align.plan_align(**self._params))
        except Exception as exc:  # noqa: BLE001 - 交给界面展示
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AlignWorker(QThread):
    """后台执行对齐。会先关闭客户端，耗时主要在正文复制。"""

    stage = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, plan: align.AlignPlan, base=None, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan
        self._base = base

    def run(self) -> None:
        try:
            rep = align.execute_align(self._plan, base=self._base, progress=self.stage.emit)
            self.done.emit(rep)
        except Exception as exc:  # noqa: BLE001 - 交给界面展示
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 小部件


class StepBar(QWidget):
    """右上角的四步指示器：已完成/当前＝绿字，未到＝灰字，之间连一条细线。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self._chips: list[QLabel] = []
        for i, name in enumerate(STEPS):
            if i:
                line = QLabel()
                line.setFixedSize(16, 1)
                line.setStyleSheet(f"background:{theme.BORDER};")
                h.addWidget(line)
            chip = QLabel(f"● {i + 1} {name}")
            h.addWidget(chip)
            self._chips.append(chip)
        self.set_current(0)

    def set_current(self, index: int) -> None:
        for i, chip in enumerate(self._chips):
            if i == index:
                chip.setStyleSheet(
                    f"color:{theme.PRIMARY_TEXT};font-size:11px;font-weight:600;"
                )
            elif i < index:
                chip.setStyleSheet(f"color:{theme.PRIMARY_TEXT};font-size:11px;")
            else:
                chip.setStyleSheet(f"color:{theme.TEXT_TERTIARY};font-size:11px;")


class Banner(QFrame):
    """浅色提示条：左边一个圆形图标，右边一行字。"""

    def __init__(self, text: str = "", tone: str = "ok", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner" if tone == "ok" else "WarnBanner")
        self.setMinimumHeight(44)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(10)
        self.mark = QLabel()
        self.mark.setFixedSize(18, 18)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl = QLabel(text)
        self.lbl.setObjectName("BannerDesc")
        self.lbl.setWordWrap(True)
        h.addWidget(self.lbl, 1)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        if tone == "ok":
            self.setObjectName("Banner")
            self.mark.setStyleSheet(f"background:{theme.PRIMARY};border-radius:9px;")
            self.mark.setPixmap(icons.glyph("check", "#FFFFFF", 11))
        else:
            self.setObjectName("WarnBanner")
            self.mark.setStyleSheet(f"background:{theme.WARN};border-radius:9px;")
            self.mark.setPixmap(icons.glyph("plus", "#FFFFFF", 11))
        self.style().unpolish(self)
        self.style().polish(self)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self.lbl.setText(text)


# ---------------------------------------------------------------- 向导


class AlignWizard(QDialog):
    """四步对齐向导。前三步只收集意图；写盘发生在第 4 步。"""

    def __init__(
        self,
        account_list: list[accounts_mod.AccountInfo],
        labels: accounts_mod.AccountLabels,
        login_uid: str,
        base=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._infos = list(account_list or [])
        self._labels = labels
        self._login_uid = login_uid or ""
        self._base = base

        self._plan: align.AlignPlan | None = None
        self._target_uid = ""
        self._picked: set[str] | None = None      # None = 用计划里的默认勾选
        self._extras: dict[str, bool] = {}        # 文件名 -> 是否勾选
        self._do_memory = True
        self._diff_only = True

        self._worker: PlanWorker | None = None
        self._seq = 0
        self._planning = False
        self._run_worker: AlignWorker | None = None
        self._started_at = 0.0
        self._elapsed = 0.0
        self.report: align.AlignReport | None = None
        self._extra_rows: dict[int, tuple[str, str]] = {}

        self.setWindowTitle("本机账号对齐")
        # 与主窗同一套夹取：1010×700 在 1280×720 或 150% 缩放的屏上装不下，
        # 对话框一旦比屏幕高，底部的「下一步」就点不到了。
        winsize.apply(self, (1010, 700), (880, 620), center=False)
        self._build()
        self._goto(PAGE_ACCOUNT)

    # ------------------------------------------------------------ 外壳
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget()
        head.setObjectName("WizardHead")
        h = QHBoxLayout(head)
        h.setContentsMargins(24, 16, 24, 14)
        h.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.lbl_title = QLabel("本机账号对齐")
        self.lbl_title.setObjectName("PageTitle")
        text.addWidget(self.lbl_title)
        self.lbl_sub = QLabel("")
        self.lbl_sub.setObjectName("PageSub")
        text.addWidget(self.lbl_sub)
        h.addLayout(text, 1)
        self.steps = StepBar()
        h.addWidget(self.steps, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(head)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_accounts())
        self.stack.addWidget(self._build_sessions())
        self.stack.addWidget(self._build_extras())
        self.stack.addWidget(self._build_run())
        root.addWidget(self.stack, 1)

        foot = QWidget()
        foot.setObjectName("WizardFoot")
        f = QHBoxLayout(foot)
        f.setContentsMargins(24, 10, 24, 10)
        f.setSpacing(8)
        self.lbl_foot = QLabel()
        self.lbl_foot.setObjectName("StatusLine")
        self.lbl_foot.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        f.addWidget(self.lbl_foot, 1)
        self.lbl_foot_right = QLabel("")
        self.lbl_foot_right.setObjectName("HintLabel")
        f.addWidget(self.lbl_foot_right, 0)

        self.btn_back = QPushButton("上一步")
        self.btn_back.clicked.connect(self._on_back)
        f.addWidget(self.btn_back)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        f.addWidget(self.btn_cancel)
        self.btn_next = QPushButton("下一步")
        self.btn_next.setObjectName("Primary")
        self.btn_next.setDefault(True)
        self.btn_next.clicked.connect(self._on_next)
        f.addWidget(self.btn_next)
        root.addWidget(foot)

    def _page(self, title: str, sub: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("Content")
        v = QVBoxLayout(page)
        v.setContentsMargins(24, 18, 24, 12)
        v.setSpacing(12)
        page._title = title          # type: ignore[attr-defined]
        page._sub = sub              # type: ignore[attr-defined]
        return page, v

    def _goto(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.steps.set_current(index)
        page = self.stack.currentWidget()
        self.lbl_title.setText(getattr(page, "_title", "本机账号对齐"))
        self.lbl_sub.setText(getattr(page, "_sub", ""))
        self.btn_back.setVisible(index in (PAGE_SESSIONS, PAGE_EXTRAS))
        self.btn_cancel.setVisible(not (index == PAGE_RUN and self.report is None))
        self.btn_next.setText("完成" if index == PAGE_RUN else "下一步")
        self.btn_next.setEnabled(self._can_next(index))
        self._refresh_footer()

    # ------------------------------------------------------------ 第 1 步
    def _build_accounts(self) -> QWidget:
        page, v = self._page("先选清楚：要把数据写给哪个账号", "只写 1 个账号，多选会让磁盘翻倍")

        self.banner_acc = Banner(
            "选 1 个接收账号：它会拿到当前账号的全部会话；人格与记忆在第 3 步确认。"
            "当前登录的账号只作为数据来源，不会被写入。"
        )
        v.addWidget(self.banner_acc)

        self.tbl_acc = QTableWidget(0, 4)
        self.tbl_acc.setHorizontalHeaderLabels(["账号", "套餐", "现有会话", "将要执行"])
        self.tbl_acc.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tbl_acc.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_acc.verticalHeader().setVisible(False)
        self.tbl_acc.verticalHeader().setDefaultSectionSize(54)
        hh = self.tbl_acc.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setFixedHeight(44)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
        self.tbl_acc.setColumnWidth(1, 120)
        self.tbl_acc.setColumnWidth(2, 140)
        self.tbl_acc.setColumnWidth(3, 220)
        v.addWidget(self.tbl_acc, 1)

        self._radios: dict[str, QRadioButton] = {}
        self._fill_accounts()
        return page

    def _fill_accounts(self) -> None:
        """重画账号表。

        ★ 单选钮**每次都新建**，不复用旧对象：`setCellWidget` 会把上一轮的
          单元格控件删掉，缓存的按钮再塞进新单元格时可能已经是个野指针。
          旧的一整组按钮也随之销毁 —— 否则 QButtonGroup 会越攒越多。
        """
        old_group = getattr(self, "_radio_group", None)
        if old_group is not None:
            for btn in list(old_group.buttons()):
                old_group.removeButton(btn)
            old_group.deleteLater()
        self._radio_group = QButtonGroup(self)
        self._radio_group.setExclusive(True)
        self._radios = {}

        infos = self._ordered_infos()
        self.tbl_acc.setRowCount(len(infos))
        for r, info in enumerate(infos):
            self.tbl_acc.setRowHeight(r, 54)
            is_login = info.uid == self._login_uid
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(8, 0, 8, 0)
            cl.setSpacing(8)
            if is_login:
                # 数据来源那一行没有单选钮：它不是"可以写给谁"，而是"从哪里读"
                tag = QLabel("来源")
                tag.setStyleSheet(f"color:{theme.TEXT_TERTIARY};font-size:11px;")
                cl.addWidget(tag)
            else:
                rb = QRadioButton()
                self._radio_group.addButton(rb)
                self._radios[info.uid] = rb
                rb.toggled.connect(
                    lambda on, u=info.uid: self._pick_account(u) if on else None
                )
                rb.setChecked(info.uid == self._target_uid)
                cl.addWidget(rb)
            name = QLabel(self._name_cell(info))
            name.setStyleSheet("background:transparent;")
            name.setTextFormat(Qt.TextFormat.RichText)
            cl.addWidget(name, 1)
            cell.setStyleSheet("background:transparent;")
            self.tbl_acc.setCellWidget(r, 0, cell)

            self.tbl_acc.setItem(r, 1, self._item(info.edition or "—"))
            self.tbl_acc.setItem(
                r, 2, self._item(f"{info.visible_count} 条" if info.visible_count else "0 条")
            )
            self.tbl_acc.setItem(r, 3, self._item(self._action_text(info)))
            if info.uid == self._target_uid:
                for c in range(4):
                    it = self.tbl_acc.item(r, c)
                    if it is not None:
                        it.setBackground(QColor(theme.PRIMARY_TINT))
                cell.setStyleSheet(f"background:{theme.PRIMARY_TINT};")

    def _ordered_infos(self) -> list[accounts_mod.AccountInfo]:
        out = [a for a in self._infos if a.uid == self._login_uid]
        out += [a for a in self._infos if a.uid != self._login_uid]
        return out

    def _name_cell(self, info: accounts_mod.AccountInfo) -> str:
        name = info.name or info.uid
        return (
            f'<span style="font-size:13px;color:{theme.TEXT_PRIMARY};">{_esc(name)}</span>'
            f'&nbsp;&nbsp;<span style="font-size:11px;color:{theme.TEXT_TERTIARY};">'
            f"{_esc(info.short)}</span>"
        )

    def _action_text(self, info: accounts_mod.AccountInfo) -> str:
        if info.uid == self._login_uid:
            return "数据来源"
        if info.uid == self._target_uid:
            return "复制全部会话"
        if not info.visible_count:
            return "尚无会话，将全部写入"
        return f"写入后共 {info.visible_count + self._source_count()} 条"

    def _source_count(self) -> int:
        for a in self._infos:
            if a.uid == self._login_uid:
                return a.visible_count
        return 0

    def _item(self, text: str) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setForeground(QColor(theme.TEXT_SECONDARY))
        return it

    def _pick_account(self, uid: str) -> None:
        if uid == self._target_uid:
            return
        self._target_uid = uid
        # 换接收方 = 整份计划作废：会话清单与"目标里已有什么"全都变了
        self._plan = None
        self._picked = None
        self._extras = {}
        self._refresh_footer()
        # ★ 重画延后一拍：这个回调正处在单选钮自己的 toggled 信号里，
        #   当场把那个按钮连单元格一起换掉，等于删掉正在发信号的对象。
        QTimer.singleShot(0, self._fill_accounts)

    # ------------------------------------------------------------ 第 2 步
    def _build_sessions(self) -> QWidget:
        page, v = self._page("选择要写入的会话", "勾掉的不会复制，源账号不受影响")

        strip = QFrame()
        strip.setObjectName("WizardStrip")
        s = QHBoxLayout(strip)
        s.setContentsMargins(14, 0, 14, 0)
        s.setSpacing(10)
        self.lbl_target = QLabel("")
        self.lbl_target.setObjectName("ValLabel")
        s.addWidget(self.lbl_target, 1)
        self.lbl_skip = QLabel("")
        self.lbl_skip.setObjectName("HintLabel")
        s.addWidget(self.lbl_skip, 0)
        self.btn_change = QPushButton("更改账号")
        self.btn_change.setObjectName("LinkButton")
        self.btn_change.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_change.clicked.connect(lambda: self._goto(PAGE_ACCOUNT))
        s.addWidget(self.btn_change, 0)
        v.addWidget(strip)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.lbl_pick = QLabel("选择要写入的会话")
        self.lbl_pick.setObjectName("SectionTitle")
        tools.addWidget(self.lbl_pick)
        tools.addStretch(1)
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("搜索会话标题…")
        self.txt_filter.setFixedWidth(240)
        self.txt_filter.addAction(
            icons.glyph_icon("search", theme.TEXT_TERTIARY, 14),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.txt_filter.textChanged.connect(self._apply_filter)
        tools.addWidget(self.txt_filter)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("HintLabel")
        tools.addWidget(self.lbl_count)
        v.addLayout(tools)

        self.tree = QTreeWidget()
        self.tree.setObjectName("PickTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        # ★ 徽标列必须钉死在最右：Stretch 的第一列若不重算，徽标会落在标题
        #   后面一百多像素处，看着像"标签没对齐"（第一版就是这样）。
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(1, 120)
        self.tree.itemChanged.connect(self._on_tree_changed)
        v.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("LinkButton")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("全不选")
        self.btn_none.setObjectName("LinkButton")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        row.addStretch(1)
        v.addLayout(row)
        return page

    def _plan_params(self) -> dict:
        return dict(
            uids=[a.uid for a in self._infos],
            labels=self._labels,
            base=self._base,
            do_sessions=True,
            do_persona=True,
            do_memory=True,
            primary_uid=self._login_uid,
            target_uids=[self._target_uid] if self._target_uid else [],
        )

    def _start_plan(self) -> None:
        if not self._target_uid:
            return
        self._seq += 1
        seq = self._seq
        self._planning = True
        self.lbl_pick.setText("选择要写入的会话 · 正在计算…")
        self.lbl_count.setText("")
        w = PlanWorker(self._plan_params(), parent=self)
        self._worker = w
        w.done.connect(lambda plan, s=seq: self._on_plan(plan, s))
        w.failed.connect(lambda msg, s=seq: self._on_plan_failed(msg, s))
        w.start()

    def _on_plan(self, plan, seq: int) -> None:
        if seq != self._seq:
            return
        self._plan = plan
        self._planning = False
        self.lbl_pick.setText("选择要写入的会话")
        self._fill_sessions()
        self._fill_extras()
        self._refresh_footer()

    def _on_plan_failed(self, msg: str, seq: int) -> None:
        if seq != self._seq:
            return
        self._planning = False
        self.lbl_pick.setText("选择要写入的会话")
        self.lbl_count.setText("计划计算失败")
        QMessageBox.warning(self, "计算计划失败", msg)
        self._refresh_footer()

    def _target(self):
        if self._plan is None:
            return None
        for t in self._plan.targets:
            if t.uid == self._target_uid:
                return t
        return None

    def _fill_sessions(self) -> None:
        """铺「对话 / 工作区 → 日期·空间 → 会话」三级树。

        与客户端左栏同一套口径：临时目录里的对话归「对话」，二级是那天的目录名；
        有工作目录的归「工作区」，二级是空间名。第一版只按 cwd 平铺一层，
        40 条会话在对话框里是一排同名文件夹，既分不出哪几天聊过，
        也看不出哪些其实属于同一个项目。
        """
        target = self._target()
        if target is None:
            self.tree.clear()
            self.lbl_target.setText("没有可写入的目标账号")
            return
        self.lbl_target.setText(
            f"写入到 {target.name} · 现有 {self._info_of(target.uid).visible_count} 条会话"
        )
        dup = target.dup_content
        self.lbl_skip.setText(f"同名会话跳过 {dup} 条" if dup else "没有同名重复")

        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            grouped = spaces_mod.group_sessions(target.items)

            picked = self._picked if self._picked is not None else (
                {i.cid for i in target.copies} | {s.cid for s in target.syncs if s.actionable}
            )

            if grouped.chat_groups:
                root = self._root_row(spaces_mod.CHAT_LABEL, len(grouped.chats))
                for group in grouped.chat_groups:
                    node = self._node_row(root, group)
                    for it in group.items:
                        self._leaf_row(node, it, picked)
                # ★ 默认展开一级：不展开的话第 2 步只能看到两行组名，
                #   用户会以为"没得选"（第一版就踩了这个）。
                root.setExpanded(True)

            if grouped.spaces:
                root = self._root_row(
                    spaces_mod.SPACE_ROOT_LABEL, sum(g.count for g in grouped.spaces)
                )
                for group in grouped.spaces:
                    node = self._node_row(root, group)
                    for it in group.items:
                        self._leaf_row(node, it, picked)
                root.setExpanded(True)
        finally:
            self.tree.blockSignals(False)
        self.tree.header().resizeSection(1, 120)
        self._refresh_count()

    def _root_row(self, label: str, count: int) -> QTreeWidgetItem:
        """一级：对话 / 工作区。括号里是会话条数，两区相加等于总数。"""
        row = QTreeWidgetItem([f"{label}（{count}）", ""])
        row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        row.setCheckState(0, Qt.CheckState.Checked)
        self.tree.addTopLevelItem(row)
        return row

    def _node_row(self, parent: QTreeWidgetItem, group) -> QTreeWidgetItem:
        """二级：日期（时间戳目录）或空间名。默认收起会话。

        名字里不写条数 —— 右侧那一列已经是「N 条」，同一行说两遍等于没说。
        """
        row = QTreeWidgetItem(parent, [group.label, f"{group.count} 条"])
        row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        row.setCheckState(0, Qt.CheckState.Checked)
        row.setToolTip(0, group.path or group.key)
        row.setForeground(1, QColor(theme.TEXT_TERTIARY))
        row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.setExpanded(False)
        return row

    def _leaf_row(self, parent: QTreeWidgetItem, it, picked: set[str]) -> None:
        text, tone = _badge_of(it)
        leaf = QTreeWidgetItem(parent, [it.title or "（无标题）", text])
        leaf.setData(0, Qt.ItemDataRole.UserRole, it.cid)
        leaf.setForeground(1, QColor(_BADGE_STYLE[tone][1]))
        leaf.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        leaf.setCheckState(
            0,
            Qt.CheckState.Checked
            if (it.selectable and it.cid in picked)
            else Qt.CheckState.Unchecked,
        )
        if not it.selectable:
            leaf.setFlags(Qt.ItemFlag.ItemIsEnabled)
        if it.block_reason:
            leaf.setToolTip(0, it.block_reason)

    @staticmethod
    def _checkable(item: QTreeWidgetItem) -> bool:
        return bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def _sync_tree(self) -> None:
        """任何一处勾选变化都整棵回算 —— 三级下"只回算父节点"会让一级永远半选。"""
        for i in range(self.tree.topLevelItemCount()):
            tree_util.aggregate(self.tree.topLevelItem(i), 0, self._checkable)

    def _on_tree_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        self.tree.blockSignals(True)
        try:
            # 三态不是「勾选值」：往下传会把叶子也变成半勾
            if item.childCount():
                tree_util.set_subtree(item, item.checkState(0), 0, self._checkable)
            self._sync_tree()
        finally:
            self.tree.blockSignals(False)
        self._picked = self._picked_cids()
        self._refresh_count()
        self._refresh_footer()

    def _picked_cids(self) -> set[str]:
        out: set[str] = set()
        for i in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(i)
            for leaf in tree_util.checked_leaves_where(root, 0, self._checkable):
                cid = leaf.data(0, Qt.ItemDataRole.UserRole)
                if cid:
                    out.add(str(cid))
        return out

    def _total_rows(self) -> int:
        return sum(
            sum(1 for _ in tree_util.iter_leaves(self.tree.topLevelItem(i)))
            for i in range(self.tree.topLevelItemCount())
        )

    def _refresh_count(self) -> None:
        self.lbl_count.setText(f"已选 {len(self._picked_cids())} / {self._total_rows()}")

    def _apply_filter(self, text: str) -> None:
        """按标题过滤：三级树要自下而上收，叶子全不匹配的分组整行隐藏。"""
        key = (text or "").strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            self._filter_node(self.tree.topLevelItem(i), key)

    def _filter_node(self, node: QTreeWidgetItem, key: str) -> int:
        if node.childCount() == 0:
            hit = not key or key in node.text(0).lower()
            node.setHidden(not hit)
            return 1 if hit else 0
        shown = sum(self._filter_node(node.child(i), key) for i in range(node.childCount()))
        node.setHidden(shown == 0)
        return shown

    def _set_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.tree.blockSignals(True)
        try:
            for i in range(self.tree.topLevelItemCount()):
                for leaf in tree_util.iter_leaves(self.tree.topLevelItem(i)):
                    if self._checkable(leaf):
                        leaf.setCheckState(0, state)
            self._sync_tree()
        finally:
            self.tree.blockSignals(False)
        self._picked = self._picked_cids()
        self._refresh_count()
        self._refresh_footer()

    # ------------------------------------------------------------ 第 3 步
    def _build_extras(self) -> QWidget:
        page, v = self._page("确认要带过去的人格与记忆", "只补不覆盖：目标已有的同文件不会被改写")

        head = QHBoxLayout()
        self.lbl_extra_head = QLabel("")
        self.lbl_extra_head.setObjectName("SectionTitle")
        head.addWidget(self.lbl_extra_head)
        head.addStretch(1)
        self.btn_diff = QPushButton("仅差异")
        self.btn_diff.setObjectName("Tab")
        self.btn_diff.setCheckable(True)
        self.btn_diff.setChecked(True)
        self.btn_diff.clicked.connect(lambda: self._set_diff_only(True))
        self.btn_all_items = QPushButton("全部")
        self.btn_all_items.setObjectName("Tab")
        self.btn_all_items.setCheckable(True)
        self.btn_all_items.clicked.connect(lambda: self._set_diff_only(False))
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        grp.addButton(self.btn_diff)
        grp.addButton(self.btn_all_items)
        head.addWidget(self.btn_diff)
        head.addWidget(self.btn_all_items)
        v.addLayout(head)

        self.tbl_extra = QTableWidget(0, 4)
        self.tbl_extra.setHorizontalHeaderLabels(["名称", "本机", "写入后", "状态"])
        self.tbl_extra.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tbl_extra.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_extra.setWordWrap(False)
        self.tbl_extra.verticalHeader().setVisible(False)
        self.tbl_extra.verticalHeader().setDefaultSectionSize(44)
        hh = self.tbl_extra.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setFixedHeight(44)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
        self.tbl_extra.setColumnWidth(1, 150)
        self.tbl_extra.setColumnWidth(2, 150)
        self.tbl_extra.setColumnWidth(3, 160)
        self.tbl_extra.itemChanged.connect(self._on_extra_changed)
        v.addWidget(self.tbl_extra, 1)

        self.lbl_extra_note = QLabel(
            "技能与设置项属于这台电脑（~/.workbuddy 根目录），不按账号复制 —— "
            "要搬它们请用「导出同步包」。"
        )
        self.lbl_extra_note.setObjectName("HintLabel")
        self.lbl_extra_note.setWordWrap(True)
        v.addWidget(self.lbl_extra_note)
        return page

    def _set_diff_only(self, on: bool) -> None:
        self._diff_only = bool(on)
        self.btn_diff.setChecked(bool(on))
        self.btn_all_items.setChecked(not on)
        self._fill_extras()

    def _persona_rows(self) -> list[tuple[str, str, str, str]]:
        """(文件名, 标题, 本机大小, 状态) —— 状态: add / same / conflict / missing"""
        if not self._target_uid or not self._login_uid:
            return []
        src_dir = align.persona_dir_of(self._login_uid, self._base)
        dst_dir = align.persona_dir_of(self._target_uid, self._base)
        rows: list[tuple[str, str, str, str]] = []
        for fname, label in PERSONA_FILES:
            src = src_dir / fname
            dst = dst_dir / fname
            if not src.is_file():
                rows.append((fname, label, "", "missing"))
                continue
            try:
                src_size = src.stat().st_size
            except OSError:
                src_size = 0
            if not dst.is_file():
                rows.append((fname, label, human_bytes(src_size), "add"))
                continue
            try:
                same = src.read_bytes() == dst.read_bytes()
            except OSError:
                same = False
            rows.append((fname, label, human_bytes(src_size), "same" if same else "conflict"))
        return rows

    def _fill_extras(self) -> None:
        rows = self._persona_rows()
        self._extra_rows = {}
        pending = [r for r in rows if r[3] in ("add", "conflict")]
        show = [r for r in rows if not self._diff_only or r[3] in ("add", "conflict")]
        self.lbl_extra_head.setText(
            f"{len(rows)} 项人格文件中，{len(pending)} 项与目标账号不同"
            if rows else "还没有可确认的内容"
        )

        self.tbl_extra.blockSignals(True)
        self.tbl_extra.clearSpans()
        rows_needed = 1 + len(show) + 2
        self.tbl_extra.setRowCount(rows_needed)
        r = 0
        if show:
            self._band(r, f"人格文件 · {len(rows)} 项 · 决定 AI 记住你什么")
            r += 1
            for fname, label, size, state in show:
                self._extra_row(r, fname, label, size, state)
                r += 1
        else:
            # 全部一致时不留一条空分组带：那看起来像"加载失败"
            self._band(
                r,
                f"人格文件 · {len(rows)} 项全部与本机一致，无需写入"
                "（切到「全部」可逐项查看）",
            )
            r += 1
        self._band(r, "长期记忆 · 1 项 · 跨会话积累的偏好与事实")
        r += 1
        self._memory_row(r)
        r += 1
        self.tbl_extra.setRowCount(r)
        self.tbl_extra.blockSignals(False)
        self._refresh_footer()

    def _band(self, row: int, text: str) -> None:
        band = QTableWidgetItem(text)
        band.setFlags(Qt.ItemFlag.NoItemFlags)
        band.setBackground(QColor(theme.BG_FIELD))
        band.setForeground(QColor(theme.TEXT_TERTIARY))
        self.tbl_extra.setSpan(row, 0, 1, 4)
        self.tbl_extra.setItem(row, 0, band)
        self.tbl_extra.setRowHeight(row, 30)

    def _extra_row(self, row: int, fname: str, label: str, size: str, state: str) -> None:
        self.tbl_extra.setRowHeight(row, 44)
        name = QTableWidgetItem(label)
        name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        if state in ("add", "conflict"):
            checked = self._extras.get(fname, state == "add")
            name.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        else:
            name.setCheckState(Qt.CheckState.Checked)
        if state == "missing":
            name.setForeground(QColor(theme.TEXT_TERTIARY))
        self.tbl_extra.setItem(row, 0, name)
        self._extra_rows[row] = (fname, state)

        self.tbl_extra.setItem(row, 1, self._item(size or "—"))
        self.tbl_extra.setItem(row, 2, self._item(size if state == "add" else "不变" if state == "same" else (size or "—")))
        text, tone = {
            "add": ("新增", "ok"),
            "same": ("一致", "muted"),
            "conflict": ("已有不同·不覆盖", "warn"),
            "missing": ("本机没有", "muted"),
        }.get(state, ("—", "muted"))
        cell = QWidget()
        cl = QHBoxLayout(cell)
        cl.setContentsMargins(8, 0, 0, 0)
        cl.addWidget(_badge(text, tone))
        cl.addStretch(1)
        cell.setStyleSheet("background:transparent;")
        self.tbl_extra.setCellWidget(row, 3, cell)

    def _memory_row(self, row: int) -> None:
        self.tbl_extra.setRowHeight(row, 44)
        item = QTableWidgetItem("长期记忆（跨会话积累的偏好与事实）")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if self._do_memory else Qt.CheckState.Unchecked
        )
        self.tbl_extra.setItem(row, 0, item)
        self._extra_rows[row] = ("__memory__", "memory")
        self.tbl_extra.setItem(row, 1, self._item("—"))
        self.tbl_extra.setItem(row, 2, self._item("合并"))
        cell = QWidget()
        cl = QHBoxLayout(cell)
        cl.setContentsMargins(8, 0, 0, 0)
        cl.addWidget(_badge("只补不覆盖", "muted"))
        cl.addStretch(1)
        cell.setStyleSheet("background:transparent;")
        self.tbl_extra.setCellWidget(row, 3, cell)

    def _on_extra_changed(self, item: QTableWidgetItem) -> None:
        if item is None or item.column() != 0:
            return
        entry = self._extra_rows.get(self.tbl_extra.row(item))
        if entry is None:
            return
        on = item.checkState() == Qt.CheckState.Checked
        if entry[0] == "__memory__":
            self._do_memory = on
        else:
            self._extras[entry[0]] = on
        self._refresh_footer()

    def _chosen_persona(self) -> list[str]:
        out: list[str] = []
        for fname, label, size, state in self._persona_rows():
            if state == "add" and self._extras.get(fname, True):
                out.append(fname)
        return out

    # ------------------------------------------------------------ 第 4 步
    def _build_run(self) -> QWidget:
        page, v = self._page("正在对齐", "关客户端 → 备份 → 写入 → 重启，一条流水线")

        self.banner_run = Banner("准备开始…")
        v.addWidget(self.banner_run)

        self.tbl_run = QTableWidget(0, 4)
        self.tbl_run.setHorizontalHeaderLabels(["账号", "写入内容", "耗时", "状态"])
        self.tbl_run.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tbl_run.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_run.setWordWrap(False)
        self.tbl_run.verticalHeader().setVisible(False)
        self.tbl_run.verticalHeader().setDefaultSectionSize(52)
        hh = self.tbl_run.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setFixedHeight(44)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tbl_run.setColumnWidth(0, 240)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
        self.tbl_run.setColumnWidth(2, 100)
        self.tbl_run.setColumnWidth(3, 110)
        v.addWidget(self.tbl_run, 1)

        self.bar_run = QFrame()
        self.bar_run.setObjectName("WizardStrip")
        b = QHBoxLayout(self.bar_run)
        b.setContentsMargins(14, 0, 14, 0)
        b.setSpacing(10)
        self.lbl_backup = QLabel("")
        self.lbl_backup.setObjectName("HintLabel")
        self.lbl_backup.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        b.addWidget(self.lbl_backup, 1)
        self.btn_open_backup = QPushButton("打开目录")
        self.btn_open_backup.setObjectName("LinkButton")
        self.btn_open_backup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_backup.clicked.connect(self._open_backup)
        self.btn_open_backup.setVisible(False)
        b.addWidget(self.btn_open_backup, 0)
        self.bar_run.setVisible(False)
        v.addWidget(self.bar_run)
        return page

    def _start_run(self) -> None:
        plan = self._build_final_plan()
        if plan is None or not plan.ready:
            QMessageBox.information(
                self, "没有可执行的内容", (plan.headline() if plan else "还没有算出计划。")
            )
            self._goto(PAGE_EXTRAS)
            return
        self._started_at = time.monotonic()
        self.banner_run.set_tone("ok")
        self.banner_run.setText("正在对齐：会先关闭 WorkBuddy，完成后自动重新启动…")
        self.tbl_run.setRowCount(0)
        self.bar_run.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_back.setVisible(False)
        self.btn_next.setEnabled(False)
        self._run_worker = AlignWorker(plan, base=self._base, parent=self)
        self._run_worker.stage.connect(self._on_run_stage)
        self._run_worker.done.connect(self._on_run_done)
        self._run_worker.failed.connect(self._on_run_failed)
        self._run_worker.start()

    def _on_run_stage(self, text: str) -> None:
        self.banner_run.setText(text)

    def _on_run_done(self, report) -> None:
        self.report = report
        self.stack.widget(PAGE_RUN)._title = "对齐完成" if report.ok else "对齐未完成"
        self.lbl_title.setText(self.stack.widget(PAGE_RUN)._title)
        self._elapsed = max(0.0, time.monotonic() - self._started_at)
        self.btn_cancel.setVisible(False)
        self.btn_next.setEnabled(True)
        self.btn_next.setText("完成")
        self._fill_run(report)
        self._refresh_footer()

    def _on_run_failed(self, msg: str) -> None:
        self.stack.widget(PAGE_RUN)._title = "对齐未完成"
        self.lbl_title.setText("对齐未完成")
        self._elapsed = max(0.0, time.monotonic() - self._started_at)
        self.banner_run.set_tone("warn")
        self.banner_run.setText(f"对齐失败：{msg}")
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setText("关闭")
        self.btn_next.setEnabled(True)
        self.btn_next.setText("关闭")
        self._refresh_footer()

    def _fill_run(self, report: align.AlignReport) -> None:
        target = self._target()
        rows: list[tuple[str, str, str, str]] = []
        n = report.copied.get(self._target_uid, 0)
        m = report.appended.get(self._target_uid, 0)
        if n or m or report.persona_added or report.memory_merged:
            bits: list[str] = []
            if n:
                bits.append(f"{n} 条会话")
            if m:
                bits.append(f"追加 {m} 条增量")
            if report.persona_added:
                bits.append(f"{report.persona_added} 个人格文件")
            if report.memory_merged:
                bits.append("合并长期记忆")
            rows.append(
                (
                    target.name if target else "目标账号",
                    " · ".join(bits) or "无改动",
                    f"{self._elapsed:.0f} 秒",
                    "已完成",
                )
            )
        else:
            rows.append(
                (target.name if target else "目标账号", "跳过，保持原样", "—", "未变更")
            )
        src = self._info_of(self._login_uid)
        rows.append((src.name if src else "当前账号", "数据来源，未写入", "—", "未变更"))

        self.tbl_run.setRowCount(len(rows))
        for r, (name, content, took, state) in enumerate(rows):
            self.tbl_run.setRowHeight(r, 52)
            self.tbl_run.setItem(r, 0, self._item(name))
            self.tbl_run.setItem(r, 1, self._item(content))
            self.tbl_run.setItem(r, 2, self._item(took))
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(8, 0, 0, 0)
            cl.addWidget(_badge(state, "ok" if state == "已完成" else "muted"))
            cl.addStretch(1)
            cell.setStyleSheet("background:transparent;")
            self.tbl_run.setCellWidget(r, 3, cell)

        ok = report.ok
        self.banner_run.set_tone("ok" if ok else "warn")
        summary = " · ".join(
            b
            for b in (
                f"{sum(report.copied.values())} 条会话" if report.copied else "",
                f"{report.persona_added} 个人格文件" if report.persona_added else "",
                human_bytes(report.body_bytes) if report.body_bytes else "",
            )
            if b
        )
        if ok:
            self.banner_run.setText(
                f"全部完成，用时 {self._elapsed:.0f} 秒" + (f"　|　{summary}" if summary else "")
            )
        else:
            self.banner_run.setText(report.error or "对齐未完成")

        if report.backup is not None:
            self.bar_run.setVisible(True)
            self.lbl_backup.setText(f"已生成回滚备份：{report.backup.root}")
            self.btn_open_backup.setVisible(True)
        else:
            self.bar_run.setVisible(bool(report.error))
            self.lbl_backup.setText(report.error or "")
            self.btn_open_backup.setVisible(False)

    def _open_backup(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if self.report is not None and self.report.backup is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report.backup.root)))

    # ------------------------------------------------------------ 计划拼装
    def _build_final_plan(self) -> align.AlignPlan | None:
        """把三步的选择落到计划对象上：会话取勾选、人格取勾选、记忆取开关。"""
        plan = self._plan
        target = self._target()
        if plan is None or target is None:
            return None
        picked = self._picked
        if picked is not None:
            target.copies = [i for i in target.items if i.cid in picked and i.selectable]
            # 被勾掉的追加项必须移出计划 —— execute 按 sync_actions 写盘
            target.syncs = [s for s in target.syncs if s.cid in picked]
        allowed = set(self._chosen_persona())
        target.persona.add = [f for f in target.persona.add if f in allowed]
        target.memory = bool(self._do_memory)
        return plan

    def _info_of(self, uid: str):
        return next((a for a in self._infos if a.uid == uid), None)

    # ------------------------------------------------------------ 导航
    def _on_back(self) -> None:
        index = self.stack.currentIndex()
        if index <= PAGE_ACCOUNT:
            return
        self._goto(index - 1)

    def _on_next(self) -> None:
        index = self.stack.currentIndex()
        if index == PAGE_RUN:
            if self.report is not None:
                self.accept()
            return
        if index == PAGE_ACCOUNT:
            if not self._target_uid:
                QMessageBox.information(self, "先选一个账号", "请选择一个接收账号（单选）。")
                return
            self._goto(PAGE_SESSIONS)
            if self._plan is None:
                self._start_plan()
            return
        if index == PAGE_SESSIONS:
            if not self._picked_cids():
                QMessageBox.information(
                    self, "没有选会话", "至少要勾一条会话，或者点「更改账号」回上一步。"
                )
                return
            self._goto(PAGE_EXTRAS)
            return
        if index == PAGE_EXTRAS:
            plan = self._build_final_plan()
            if plan is None or not plan.ready:
                QMessageBox.information(
                    self, "没有可执行的内容", plan.headline() if plan else "还没有算出计划。"
                )
                return
            box = QMessageBox(self)
            box.setWindowTitle("确认本机账号对齐")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(plan.headline())
            steps = [f"  {i}. {t}" for i, t in enumerate(plan.steps(), 1)]
            body = [
                "关闭客户端会导致：",
                "· 未发送的消息输入丢失",
                "· 正在进行的对话中断（包括当前这个对话）",
                "",
                *steps,
            ]
            if plan.warnings:
                body += ["", "注意："] + [f"  · {w}" for w in plan.warnings]
            body += ["", "会话与人格文件在改写前都会备份到 wbsync-backup。"]
            box.setInformativeText("\n".join(body))
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
            )
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            self._goto(PAGE_RUN)
            self._start_run()

    # ------------------------------------------------------------ 页脚
    def _refresh_footer(self) -> None:
        index = self.stack.currentIndex()
        src = self._info_of(self._login_uid)
        if index == PAGE_ACCOUNT:
            size = human_bytes(src.body_bytes) if src else ""
            if self._target_uid:
                tgt = self._info_of(self._target_uid)
                self.lbl_foot.setText(
                    f"将向 1 个账号（{tgt.name if tgt else ''}）写入"
                    + (f"，预计新增 {size}" if size else "")
                )
            else:
                self.lbl_foot.setText("还没选接收账号")
            self.lbl_foot_right.setText("执行期间请先退出 WorkBuddy")
        elif index == PAGE_SESSIONS:
            target = self._target()
            picked = self._picked_cids()
            size = human_bytes(
                sum(i.body_bytes for i in target.items if i.cid in picked)
            ) if target is not None else ""
            self.lbl_foot.setText(
                f"将复制 {len(picked)} 条会话" + (f"，约 {size}" if size else "")
            )
            self.lbl_foot_right.setText("同名会话不会覆盖，重复项默认不勾")
        elif index == PAGE_EXTRAS:
            chosen = self._chosen_persona()
            self.lbl_foot.setText(
                (f"已选 {len(chosen)} 个人格文件" if chosen else "不写入人格文件")
                + ("，并合并长期记忆" if self._do_memory else "，不合并记忆")
            )
            self.lbl_foot_right.setText("只补不覆盖")
        else:
            if self.report is None:
                self.lbl_foot.setText("正在写入，请不要关闭本窗口…")
                self.lbl_foot_right.setText("")
            else:
                self.lbl_foot.setText("点「完成」后会重新启动 WorkBuddy")
                self.lbl_foot_right.setText(
                    "全部完成" if self.report.ok else "有失败项"
                )
        self.btn_next.setEnabled(self._can_next(index))

    def _can_next(self, index: int) -> bool:
        if index == PAGE_ACCOUNT:
            return bool(self._target_uid)
        if index == PAGE_SESSIONS:
            return bool(self._picked_cids())
        if index == PAGE_EXTRAS:
            return bool(self._plan is not None and self._plan.ready)
        return self.report is not None

    # ------------------------------------------------------------ 收尾
    def _wait_worker(self) -> None:
        """测试与关闭窗口时用：把两个后台线程收干净，别留在那里访问已删除的控件。"""
        for w in (self._worker, self._run_worker):
            if w is not None and w.isRunning():
                w.wait(30000)

    def reject(self) -> None:  # noqa: D401 - Qt 接口
        if self.stack.currentIndex() == PAGE_RUN and self.report is None:
            QMessageBox.information(self, "正在写入", "对齐已经开始，等它跑完再关。")
            return
        self._wait_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._wait_worker()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 首帧把页脚算一次：不然「下一步」的可用性要等到用户点一下才正确
        self._refresh_footer()

    # ------------------------------------------------------------ 兼容旧调用
    @property
    def plan(self) -> align.AlignPlan | None:
        """旧主窗口按 `dlg.plan` 取计划 —— 保留一个只读出口。"""
        return self._plan


# 旧的类名保留：主窗口与历史调用方按 AlignDialog 引用
AlignDialog = AlignWizard


def align_qss() -> str:
    """（样式已并入 `theme.app_qss()`；保留此函数只为在测试里断言"样式已装"。）"""
    qss = theme.app_qss()
    required = ("QWidget#WizardHead", "QWidget#WizardFoot", "QFrame#WizardStrip")
    return "" if all(k in qss for k in required) else "未装"
