"""会话列表 —— 对话区平铺会话，工作区按「空间 → 会话」分组的紧凑列表。

## 为什么长成客户端那样

这个工具最常见的用法是**和 WorkBuddy 客户端并排开着逐行比对**：哪条在、
哪条不在、哪条更新了。比对靠的是「顺序一致 + 行高一致 + 时间写法一致」，
三者缺一就会串行。所以：

- 第一行＝标题 + 行尾体积，与客户端左栏同位置；
- 时间写法照抄客户端（`1小时前` / `昨天` / `10月21日`），不写 `今天 14:32`；
- 行高 48，比客户端略高 —— 因为多了一行时间。

## 为什么体积独占第二行

体积是「这条值不值得搬」的主要依据，不能砍；但塞进第一行右侧会和标题抢宽度，
标题被压到只剩半句时，比对就没法做了。放第二行既保住宽度，
又让第一行与客户端严丝合缝。

## 为什么对话是两级、工作区是三级

客户端左栏分「对话」和「工作区」两个区。工作区的二级是**空间名**，同一项目
几十条会话能一键收起，这层要留。对话区原来的二级是**日期/时间戳目录**
（`2026-11-02-14-14-08`），可它只是客户端给临时会话起的容器名：一条会话
一个组、默认还收起，找会话得先点开一层才看得见标题。2026-11-11 起去掉
这一层，对话区直接平铺会话名称，按最近活动倒序；行内第二行仍写相对时间，
日期信息没有丢。

`spaces.group_sessions` 仍返回对话的日期分组 —— 对齐、逐条选择、同步计划、
切换向导四个界面还在用。归组逻辑不动，只在**本列表**渲染时换成平铺，
改动才不会扩散到那四个界面。

## 缩进是层级的一部分

工作区的三级必须**逐级右移**：一级贴左、二级 +16、三级再 +16。若三级都对齐
在 x=13，一眼看过去就是三排并列的列表，"谁属于谁"只能靠猜。对话区的行虽然
直接挂在一级下，仍沿用 INDENT_ROW —— 两个区的会话标题左缘要落在同一条竖线
（30px）上，否则切来切去会显得错位。

## 选中态只改两行

点一下要重算几十行样式的话会明显卡顿，所以既有的 O(1) 策略保留：
只对「旧选中」和「新选中」两行 unpolish/polish。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import spaces
from ..inventory import SessionRecord
# 路径规则只在 wbsync/spaces.py 定义一次，这里再导出：界面各处与单测历史上都按
# `session_cards.space_label` 取，换引用点属于无意义的批量改动。
from ..spaces import (  # noqa: F401 - 对外兼容导出
    _STAMP_DIR,
    _tail,
    group_key,
    short_space,
    space_label,
)

ROW_HEIGHT = 48
GROUP_HEIGHT = 32
ROOT_HEIGHT = 34
_ROW_PAD = 12
_TIME_GAP = 10

# 三级缩进（px）。二级分组名与三级标题必须落在同一条竖线上：
# 二级 = 16（左边距）+ 12（箭头）+ 2（间距）= 30
# 三级 = 17（行左缘）+ 2（选中竖条）+ 8（间距）+ 3（文字内边距）= 30
INDENT_ROOT = 0
INDENT_GROUP = 16
INDENT_ROW = 17


def fmt_size(n: int | None) -> str:
    if not n:
        return "—"
    mb = n / 1048576
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{max(n / 1024, 1):.0f} KB"


def fmt_when(ms: int) -> str:
    """相对时间 —— 与 WorkBuddy 客户端左栏逐字对齐。

    对齐不是为了好看：两边并排比对时，同一时刻在左边写「1小时前」、
    在右边写「今天 14:32」，眼睛每次都要重新对一遍刻度。
    """
    if not ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(ms / 1000)
    except (OSError, ValueError, OverflowError):
        return "—"

    now = datetime.now()
    seconds = (now - dt).total_seconds()

    if seconds < 0:
        return f"{dt.month}月{dt.day}日"
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    # 跨自然日优先按天算：凌晨 00:10 看昨晚 23:00 的会话，
    # 说「1小时前」不如「昨天」更接近用户对"哪一天"的记忆
    days = (now.date() - dt.date()).days
    if days == 0:
        return f"{int(seconds // 3600)}小时前"
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days}天前"
    if dt.year == now.year:
        return f"{dt.month}月{dt.day}日"
    return f"{dt.year}年{dt.month}月{dt.day}日"


def fmt_when_exact(ms: int) -> str:
    """精确到分的时间 —— 详情面板与表格视图用。

    列表用相对时间是为了扫读；但「到底几点更新的」在核对时是硬需求，
    所以保留这一份带时分的写法，不要两处都用同一种。
    """
    if not ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(ms / 1000)
    except (OSError, ValueError, OverflowError):
        return "—"
    now = datetime.now()
    days = (now.date() - dt.date()).days
    if days == 0:
        return dt.strftime("今天 %H:%M")
    if days == 1:
        return dt.strftime("昨天 %H:%M")
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def short_cid(cid: str) -> str:
    return (cid or "")[:6]


def chevron(expanded: bool, color: str, size: int = 12) -> QPixmap:
    """折叠三角。自绘而不是用 ▸/▾ 字符：字符在不同字体下基线与宽度都会漂。

    比例是踩出来的：第一版把三角画在 0.36~0.68 这段纵向区间里，缩到 9px 后
    实际高度只有 2.6px，屏幕上看就是一个小圆点，完全读不出"可以点开"。
    """
    canvas = size * 3
    pm = QPixmap(canvas, canvas)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    c = float(canvas)
    if expanded:
        pts = [QPointF(c * 0.15, c * 0.28), QPointF(c * 0.85, c * 0.28), QPointF(c * 0.50, c * 0.72)]
    else:
        pts = [QPointF(c * 0.28, c * 0.15), QPointF(c * 0.28, c * 0.85), QPointF(c * 0.72, c * 0.50)]
    p.drawPolygon(QPolygonF(pts))
    p.end()
    pm.setDevicePixelRatio(3)
    return pm


class ElidedLabel(QLabel):
    """按可用宽度自动加省略号的标签。

    直接给 QLabel 长文本时，它的 minimumSizeHint 等于整行文字宽度，
    会把整栏的宽度下限一起顶高 —— 三栏布局里中间栏和右栏都只有几百像素，
    「宽度不够」是常态而不是例外。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 - 与 Qt 命名一致
        self._full = text or ""
        self._apply()
        if self._full:
            self.setToolTip(self._full)

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        fm = QFontMetrics(self.font())
        avail = max(self.width() - 2, 24)
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, avail))


class GroupHeader(QWidget):
    """分组标题：折叠三角 + 名字 + 条数。整条可点。

    `level=0` 是一级（对话 / 工作区），`level=1` 是二级（工作区的空间名）。
    两者只差缩进、字号与写法，交互完全一样 —— 一级也能收起整区。

    多选模式下右侧额外出现「全选本组」勾选框（三态：全选/半选/不选）。
    """

    toggled = Signal(str, bool)              # (节点键, 是否折叠)
    group_check_toggled = Signal(str, bool)  # (节点键, 是否全选)

    def __init__(
        self,
        key: str,
        label: str,
        count: int,
        collapsed: bool,
        level: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.space = key          # 旧名保留：信号载荷与折叠偏好都按它走
        self.level = level
        self.collapsed = collapsed
        self.setObjectName("GroupHead")
        self.setProperty("level", "root" if level == 0 else "sub")
        self.setFixedHeight(ROOT_HEIGHT if level == 0 else GROUP_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # 一级写「对话 (6)」（客户端口径），二级写「WS1 · 3 条」：
        # 括号留给固定分区，圆点留给容器，两种写法一眼能分出层级
        self._label = label
        self._text = f"{label} ({count})" if level == 0 else f"{label} · {count} 条"
        self.setToolTip(f"{self._text}（点击{'展开' if collapsed else '收起'}）\n{key}")

        row = QHBoxLayout(self)
        # 缩进只加在左边距上：箭头与名字一起右移，二级的名字才会落在三级
        # 标题的左边，层级看起来才成立
        row.setContentsMargins(INDENT_GROUP if level else INDENT_ROOT, 0, _ROW_PAD, 0)
        row.setSpacing(2)

        self._arrow = QLabel()
        self._arrow.setFixedSize(12, 12)
        self._arrow.setPixmap(chevron(not collapsed, "#5F645F"))
        row.addWidget(self._arrow)

        self._name = ElidedLabel()
        self._name.setObjectName("GroupTitle")
        self._name.setProperty("level", "root" if level == 0 else "sub")
        self._name.setText(self._text)
        row.addWidget(self._name, 1)

        # 多选模式才出现：放在最右，不与左侧的折叠热区抢点击
        self._gcheck = QCheckBox("全选本组")
        self._gcheck.setObjectName("GroupCheck")
        self._gcheck.setStyleSheet("QCheckBox{spacing:4px;font-size:11px;color:#5B6573;}")
        self._gcheck.setTristate(True)
        self._gcheck.setVisible(False)
        # 子控件自己吞掉点击事件，不会冒泡到 header 触发折叠
        self._gcheck.toggled.connect(lambda on: self.group_check_toggled.emit(self.space, on))
        row.addWidget(self._gcheck)

    def name(self) -> str:
        """裸名字（不带条数）：日期/空间名。查节点、写提示都按它。"""
        return self._label

    def label(self) -> str:
        """显示文本（带条数）。"""
        return self._text

    def toggle(self) -> None:
        """切换折叠。鼠标事件与测试共用这一条路径，不各自实现一套。"""
        self.set_collapsed(not self.collapsed, emit=True)

    def set_collapsed(self, collapsed: bool, emit: bool = False) -> None:
        self.collapsed = bool(collapsed)
        self._arrow.setPixmap(chevron(not self.collapsed, "#5F645F"))
        if emit:
            self.toggled.emit(self.space, self.collapsed)

    def set_check_visible(self, on: bool) -> None:
        self._gcheck.setVisible(on)

    def set_check_state(self, state: Qt.CheckState) -> None:
        """由列表统一计算三态，不走用户点击的反馈回路（避免信号回环）。"""
        self._gcheck.blockSignals(True)
        self._gcheck.setCheckState(state)
        self._gcheck.blockSignals(False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
        super().mousePressEvent(event)


class SessionRow(QFrame):
    """单条会话的列表行（对话区第二级 / 工作区第三级）：标题 + 体积，第二行时间。

    多选模式下行首出现勾选框，点击整行即勾选（不再送进详情面板）。
    `indent` 是三级缩进，由列表按层级注入。
    """

    clicked = Signal(object)
    double_clicked = Signal(object)
    check_toggled = Signal(object, bool)  # (record, 是否勾选)

    def __init__(self, record: SessionRecord, indent: int = INDENT_ROW, parent=None) -> None:
        super().__init__(parent)
        self.record = record
        self.setObjectName("SessionRow")
        self.setFixedHeight(ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", "false")
        self._build(indent)

    def _build(self, indent: int) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(indent, 0, _ROW_PAD, 0)
        root.setSpacing(8)

        # 选中时的左侧竖条。⚠️ 必须常驻：靠 setVisible 切换会让隐藏时的行
        # 少占 2px，选中瞬间整行文字右移 —— 肉眼看得见的抖动。
        # 所以宽度固定 2px，只换背景色。
        self._accent = QFrame()
        self._accent.setObjectName("RowAccent")
        self._accent.setFixedWidth(2)
        self._accent.setStyleSheet("background: transparent;")
        root.addWidget(self._accent)

        # 多选模式的勾选框：常态隐藏，出现时紧挨左侧竖条。无文字、固定 18px，
        # 与全局 QSS 的 14px 方块指示符对齐。
        self._check = QCheckBox(self)
        self._check.setObjectName("RowCheck")
        self._check.setFixedSize(18, 18)
        self._check.setStyleSheet("QCheckBox{spacing:0;}")
        self._check.setVisible(False)
        self._check.toggled.connect(lambda on: self.check_toggled.emit(self.record, on))
        root.addWidget(self._check)

        text = QVBoxLayout()
        # 左内边距 3：让标题左边缘与二级分组名落在同一条竖线（见 INDENT_* 说明）
        text.setContentsMargins(3, 6, 0, 6)
        text.setSpacing(0)

        # ---- 第一行：标题
        self._title = ElidedLabel()
        self._title.setObjectName("RowTitle")
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        text.addWidget(self._title)

        # ---- 第二行：相对时间（与客户端左栏写法一致，行内不重复空间名/日期）
        self._time_full = fmt_when(self.record.last_activity_at or self.record.updated_at)
        self._meta_text = self._time_full
        self._meta = QLabel(self._meta_text)
        self._meta.setObjectName("RowVol")
        self._meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        text.addWidget(self._meta)

        root.addLayout(text, 1)

        # ---- 右侧：体积垂直居中（设计稿把体积放在行尾，不占标题宽度）
        self._vol = QLabel(fmt_size(self.record.jsonl_size))
        self._vol.setObjectName("RowVol")
        self._vol.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._vol.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        root.addWidget(self._vol)

        self._apply_text()
        # cid 与完整路径不再占位，但比对时常常要抄，所以留在一处可查的地方
        self.setToolTip(
            f"{self.record.title or '(未命名会话)'}\n"
            f"会话包 {self.record.cid}\n"
            f"{self.record.cwd or self.record.sidebar_space or ''}\n"
            f"{fmt_size(self.record.jsonl_size)} · {self._time_full}"
        )

    def _apply_text(self) -> None:
        full = self.record.title or "(未命名会话)"
        self._title.setText(full)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_text()

    def changeEvent(self, event) -> None:
        # 字体/配色变化会改变文字宽度，省略阈值必须跟着重算
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._apply_text()
        super().changeEvent(event)

    # ---------------------------------------------------------- 选中
    def set_selected(self, on: bool) -> None:
        want = "true" if on else "false"
        if self.property("selected") == want:
            return
        self.setProperty("selected", want)
        self._accent.setStyleSheet(
            "background: #0F6E56;" if on else "background: transparent;"
        )
        # 改 property 后必须重新求值样式表，否则不生效
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    # ---------------------------------------------------------- 多选
    def set_check_visible(self, on: bool) -> None:
        self._check.setVisible(on)

    def set_checked(self, on: bool) -> None:
        if self._check.isChecked() != bool(on):
            self._check.setChecked(bool(on))

    def is_checked(self) -> bool:
        return self._check.isChecked()

    def mousePressEvent(self, event) -> None:
        # 多选模式：点行即勾选（点在勾选框上的不算，避免一次按下既勾又取消）
        if self._check.isVisible():
            if not self._check.geometry().contains(event.pos()):
                self.set_checked(not self.is_checked())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.record)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # 多选模式下双击也不弹详情，避免误触
        if not self._check.isVisible():
            if event.button() == Qt.MouseButton.LeftButton:
                self.double_clicked.emit(self.record)
        super().mouseDoubleClickEvent(event)


class SessionList(QScrollArea):
    """会话列表：对话区平铺会话，工作区按「空间 → 会话」分组。

    节点键（写进折叠偏好文件，必须稳定）：
    - 一级：`对话` / `工作区`
    - 二级：`工作区|<小写空间路径>`
    - 旧偏好里的 `对话|<时间戳目录或日期>` 已无对应节点，加载后自然被忽略。
    """

    selection_changed = Signal(object)      # SessionRecord | None
    card_activated = Signal(object)         # 双击
    group_toggled = Signal(str, bool)       # (节点键, 是否折叠) —— 交给上层持久化
    check_count_changed = Signal(int)       # 勾选条数变化 —— 驱动底部动作条

    ROOT_CHAT = spaces.CHAT_LABEL
    ROOT_SPACE = spaces.SPACE_ROOT_LABEL

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("ListColumn")

        self._host = QWidget()
        self._host.setObjectName("ListColumn")
        self._box = QVBoxLayout(self._host)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)
        self._box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._host)

        self._cards: list[SessionRow] = []
        self._by_cid: dict[str, SessionRow] = {}
        self._headers: dict[str, GroupHeader] = {}
        self._members: dict[str, list[SessionRow]] = {}
        self._children: dict[str, list[str]] = {}
        self._parent: dict[str, str] = {}
        self._owner: dict[str, str] = {}        # cid → 所属二级节点键
        self._roots: list[str] = []
        self._collapsed: set[str] = set()
        self._expanded: set[str] = set()        # 二级默认收起，所以记"展开过的"
        self._selected_cid: str | None = None
        self._checked: set[str] = set()
        self._selmode: bool = False
        self._bulk: bool = False

    # ---------------------------------------------------------- 数据
    def set_collapse_state(self, collapsed: set[str]) -> None:
        """只注入收起名单（旧接口，二级仍按默认收起）。须在 set_records 之前调。"""
        self._collapsed = set(collapsed or ())

    def set_state(self, collapsed: set[str], expanded: set[str] | None = None) -> None:
        """注入折叠偏好。

        一级默认**展开**、二级默认**收起**（"展开到二级"）：二级是几十个
        空间，全开就是铺满标题的墙；全收又什么都看不见。存「收起的」与
        「展开过的」两套名单才分得清「用户展开过」和「从没碰过」。
        """
        self._collapsed = set(collapsed or ())
        self._expanded = set(expanded or ())

    def set_records(self, records: list[SessionRecord]) -> None:
        for row in self._cards:
            row.setParent(None)
            row.deleteLater()
        for head in self._headers.values():
            head.setParent(None)
            head.deleteLater()
        self._cards = []
        self._by_cid = {}
        self._headers = {}
        self._members = {}
        self._children = {}
        self._parent = {}
        self._owner = {}
        self._roots = []

        grouped = spaces.group_sessions(records)

        # 对话区不铺日期二级：grouped.chat_groups 仍供四个向导使用，本列表只取
        # 平铺的 chats，直接挂到一级节点下；工作区照旧按空间分组。
        if grouped.chats:
            self._build_chat_root(self.ROOT_CHAT, len(grouped.chats), grouped.chats)
        if grouped.spaces:
            self._build_root(
                self.ROOT_SPACE,
                sum(g.count for g in grouped.spaces),
                grouped.spaces,
                "工作区的二级是工作空间名（同名的两个目录会在名字前补上级路径）",
            )

        self._apply_visibility()

        # 重建后只补亮选中的那一行；选中项已不在列表里就清掉，避免留下悬空 cid
        if self._selected_cid:
            if self._selected_cid in self._by_cid:
                self._apply_selection(None, self._selected_cid)
            else:
                self._selected_cid = None

        # 勾选是「这一屏」的临时态，重建即清空；多选模式继续开着则把勾选框重新亮出来
        self._checked.clear()
        for head in self._headers.values():
            head.set_check_state(Qt.CheckState.Unchecked)
            head.set_check_visible(self._selmode)
        for row in self._cards:
            row.set_check_visible(self._selmode)

    def _build_chat_root(self, key: str, count: int, chats) -> None:
        """对话区：一级下面直接列会话，不插日期/时间戳二级节点。

        日期目录名只是客户端给临时会话起的容器名，一条会话一个组还得逐组点开，
        找会话时反而是障碍。行直接挂在一级键下即可：折叠、全选、选中都靠节点键
        递归，平铺不需要另写一套逻辑。排序按最近活动，与客户端左栏口径一致。
        """
        self._add_header(
            key, key, count, level=0, parent_key="",
            tip="对话区按最近活动倒序直接列出会话",
        )
        self._roots.append(key)
        for record in sorted(chats, key=spaces._activity, reverse=True):
            self._add_row(record, key)

    def _build_root(self, key: str, count: int, groups, tip: str) -> None:
        """铺一级（工作区）及其下所有空间分组与会话行。"""
        self._add_header(key, key, count, level=0, parent_key="", tip=tip)
        self._roots.append(key)
        for group in groups:
            node_key = f"{key}|{group.key}"
            self._add_header(
                node_key,
                group.label,
                group.count,
                level=1,
                parent_key=key,
                tip=group.path or group.key,
            )
            for record in group.items:
                self._add_row(record, node_key)

    def _add_header(
        self, key: str, label: str, count: int, level: int, parent_key: str, tip: str = ""
    ) -> GroupHeader:
        collapsed = key in self._collapsed
        if level == 1 and not collapsed:
            # 二级要看「用户是否展开过」：默认收起，只有记在展开名单里才打开
            collapsed = key not in self._expanded
        head = GroupHeader(key, label, count, collapsed, level)
        if tip:
            head.setToolTip(head.toolTip() + f"\n{tip}")
        head.toggled.connect(self._on_group_toggled)
        head.group_check_toggled.connect(self._on_group_check_toggled)
        head.setParent(self._host)
        self._box.addWidget(head)
        self._headers[key] = head
        self._members[key] = []
        self._children.setdefault(key, [])
        self._parent[key] = parent_key
        if parent_key:
            self._children.setdefault(parent_key, []).append(key)
        return head

    def _add_row(self, record: SessionRecord, owner: str) -> None:
        row = SessionRow(record, INDENT_ROW)
        row.clicked.connect(self._on_click)
        row.double_clicked.connect(self.card_activated.emit)
        row.check_toggled.connect(self._on_check_toggled)
        row.setParent(self._host)
        self._box.addWidget(row)
        self._cards.append(row)
        self._by_cid[record.cid] = row
        self._members[owner].append(row)
        self._owner[record.cid] = owner

    # ---------------------------------------------------------- 折叠
    def _visible_key(self, key: str, include_self: bool = True) -> bool:
        """自身与所有祖先都没被收起才算可见。

        ★ 判据是分组头自己的 `collapsed`，不是 `_collapsed` 集合：二级默认收起，
          它**不在**收起名单里（名单只记"用户点过收起"）。拿集合判会让默认
          收起的二级看起来是展开的，行全飘在外面。

        ★ 判**分组头自己**要不要显示时 `include_self=False`：它收起来的是自己的
          内容，不是自己。第一版没区分，结果整屏只剩一个二级节点 —— 其余全被
          自己的 collapsed 藏掉了（2026-11-03 截图复核时发现）。
        """
        walk = key if include_self else self._parent.get(key, "")
        while walk:
            head = self._headers.get(walk)
            if head is not None and head.collapsed:
                return False
            walk = self._parent.get(walk, "")
        return True

    def _apply_visibility(self) -> None:
        for key, head in self._headers.items():
            head.setVisible(self._visible_key(key, include_self=False))
        for row in self._cards:
            row.setVisible(self._visible_key(self._owner.get(row.record.cid, "")))

    def _on_group_toggled(self, key: str, collapsed: bool) -> None:
        # 分组头是折叠状态的唯一权威：它记着"默认收起"，而 `_collapsed` 只记
        # 用户点过收起的那几个。两处都要同步，否则从代码里折叠（select_cid、
        # 展开全部）会出现"集合说展开、头还收着"的错位。
        head = self._headers.get(key)
        if head is not None:
            head.set_collapsed(collapsed)
        if collapsed:
            self._collapsed.add(key)
            self._expanded.discard(key)
        else:
            self._collapsed.discard(key)
            self._expanded.add(key)
        self._apply_visibility()
        # 收起时把选中项一并清掉：详情面板对着一条看不见的会话做删除是危险的
        if collapsed and self._selected_cid:
            owner = self._owner.get(self._selected_cid, "")
            if not self._visible_key(owner):
                self._selected_cid = None
                self.selection_changed.emit(None)
        self.group_toggled.emit(key, collapsed)

    def collapsed_state(self) -> set[str]:
        return set(self._collapsed)

    def expanded_state(self) -> set[str]:
        return set(self._expanded)

    def clear_selection(self) -> None:
        prev = self._selected_cid
        self._selected_cid = None
        if prev:
            self._apply_selection(prev, None)
        self.selection_changed.emit(None)

    def select_cid(self, cid: str) -> None:
        """按 cid 点亮某一行（表格视图双击时用）。"""
        row = self._by_cid.get(cid)
        if row is None:
            return
        # 目标在收起的组里就先展开（含祖先一级），否则「选中了但看不见」
        walk = self._owner.get(cid, "")
        opened = False
        while walk:
            head = self._headers.get(walk)
            if head is not None and head.collapsed:
                self._collapsed.discard(walk)
                self._expanded.add(walk)
                head.set_collapsed(False)
                opened = True
            walk = self._parent.get(walk, "")
        if opened:
            self._apply_visibility()
        self._on_click(row.record)

    # ---------------------------------------------------------- 选中
    def _on_click(self, record: SessionRecord) -> None:
        prev = self._selected_cid
        self._selected_cid = record.cid
        # 点同一行不必再 polish 一遍
        if prev != record.cid:
            self._apply_selection(prev, record.cid)
        self.selection_changed.emit(record)

    def _apply_selection(self, prev_cid: str | None, new_cid: str | None) -> None:
        """只动「旧选中」和「新选中」两行，与总行数无关。"""
        if prev_cid and prev_cid != new_cid:
            row = self._by_cid.get(prev_cid)
            if row is not None:
                row.set_selected(False)
        if new_cid:
            row = self._by_cid.get(new_cid)
            if row is not None:
                row.set_selected(True)

    @property
    def selected_record(self) -> SessionRecord | None:
        row = self._by_cid.get(self._selected_cid) if self._selected_cid else None
        return row.record if row is not None else None

    @property
    def group_count(self) -> int:
        """二级分组个数（工作区空间；对话区已平铺）。"""
        return sum(1 for head in self._headers.values() if head.level == 1)

    def node(self, key: str) -> GroupHeader | None:
        """按名字或节点键取分组头。

        三种写法都接受：完整节点键（`工作区|d:/work/ws1`）、二级裸键、二级**显示名**
        （`WS1`）。键是路径（小写、带盘符），显示名是人看的那半截 —— 冒烟与单测
        按显示名取，让人自己去拼 `工作区|` 前缀太容易写错。
        """
        if key in self._headers:
            return self._headers[key]
        for root in (self.ROOT_CHAT, self.ROOT_SPACE):
            hit = self._headers.get(f"{root}|{key}")
            if hit is not None:
                return hit
        for head in self._headers.values():
            if head.level == 1 and head.name() == key:
                return head
        return None

    # ---------------------------------------------------------- 多选
    def set_select_mode(self, on: bool) -> None:
        """进入/退出多选模式：统一显隐所有勾选框，退出时清掉勾选。"""
        self._selmode = bool(on)
        for row in self._cards:
            row.set_check_visible(self._selmode)
        for head in self._headers.values():
            head.set_check_visible(self._selmode)
        if not self._selmode:
            self.clear_checks()
        self.check_count_changed.emit(len(self._checked))

    def _on_check_toggled(self, record, on: bool) -> None:
        cid = record.cid
        if on:
            self._checked.add(cid)
        else:
            self._checked.discard(cid)
        if not self._bulk:
            self._recompute_roots()
        self.check_count_changed.emit(len(self._checked))

    def _on_group_check_toggled(self, key: str, on: bool) -> None:
        """勾整个节点：包含子孙与收起组里的成员（展开后它们就是勾着的）。"""
        self._bulk = True
        try:
            for row in self._descendant_rows(key):
                row.set_checked(on)
        finally:
            self._bulk = False
        self._recompute_roots()
        self.check_count_changed.emit(len(self._checked))

    def _descendant_rows(self, key: str) -> list[SessionRow]:
        rows = list(self._members.get(key, []))
        for child in self._children.get(key, []):
            if self._headers.get(child) is not None:
                rows.extend(self._descendant_rows(child))
        return rows

    def _recompute_roots(self) -> None:
        for root in self._roots:
            self._recompute(root)

    def _recompute(self, key: str) -> tuple[int, int]:
        """自下而上算三态：返回 (勾选数, 总数)。"""
        on = total = 0
        for row in self._members.get(key, []):
            total += 1
            if row.is_checked():
                on += 1
        for child in self._children.get(key, []):
            if self._headers.get(child) is None:
                continue
            c_on, c_total = self._recompute(child)
            on += c_on
            total += c_total
        head = self._headers.get(key)
        if head is not None:
            if total == 0 or on == 0:
                state = Qt.CheckState.Unchecked
            elif on == total:
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
            head.set_check_state(state)
        return on, total

    def clear_checks(self) -> None:
        self._bulk = True
        try:
            for row in self._cards:
                row.set_checked(False)
        finally:
            self._bulk = False
        self._checked.clear()
        for head in self._headers.values():
            head.set_check_state(Qt.CheckState.Unchecked)
        self.check_count_changed.emit(0)

    def select_all_visible(self, on: bool) -> None:
        """「全选当前列表」：只选当前可见的行（收起的分组先展开再勾）。"""
        self._bulk = True
        try:
            for row in self._cards:
                if self._visible_key(self._owner.get(row.record.cid, "")):
                    row.set_checked(on)
        finally:
            self._bulk = False
        self._recompute_roots()
        self.check_count_changed.emit(len(self._checked))

    def checked_records(self) -> list[SessionRecord]:
        return [self._by_cid[c].record for c in self._checked if c in self._by_cid]


# 旧名字保留：外部（主窗口、冒烟测试）仍按 CardGrid 引用这个控件
CardGrid = SessionList
