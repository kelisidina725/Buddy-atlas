"""导入 · 增量合并面板 —— 单页决策，取代原来的「计划表格 + 映射页签 + 二次确认」。

## 为什么不是表格

三件事都不适合用列来表达：

1. **路径映射**：源路径与本机路径都是长串，一旦放进固定列必然截断，
   只能靠 tooltip 补；这里给它独占一整行，可换行、不截断。
2. **行数差**：`42 → 57 +15` 是三个数挤在一个语义里，占三列太贵，压成一段尾注。
3. **分叉对比**：要并排看两份的「共同旧版 / 各自加了多少 / 末条摘要」，
   表格列头对不齐任何一行，只能用卡片。

所以整页是「分组 + 条目行」，没有列头，也没有内嵌横向滚动。

## 与导入器分工

只做展示与收集，不碰数据：
- 分叉单选 → `importer.apply_div_choice`（就地改这一条，不重新 plan）
- 落点 → `cwd_choices()`，交给 `execute_import(cwd_remaps=...)`
- 回传包 → 只读导出，可以直接在这里生成（不需要关客户端）

## 为什么回滚单独一份备份

「取包内」会覆盖本地正文。这份覆盖单独备份（`guard.create_merge_backup`），
备份页只提供「打开目录」不提供一键还原（还原是写路径，得走关客户端那套铁律），
所以这里的文案只说「备份在哪、怎么手动放回去」，不承诺点一下就回滚。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import accounts, guard, importer, pack, rehome
from . import restart_option, theme

# 分叉取舍的三个选项，值必须与 importer.apply_div_choice 认的字符串一致
_KEEP_SRC, _KEEP_LOCAL, _KEEP_BOTH = "src", "local", "both"


# ---------------------------------------------------------------- 小部件


class _StatChip(QFrame):
    """统计项：内容宽度，不做等分。点一下滚到对应分组。"""

    clicked = Signal(str)

    def __init__(self, key: str, number: int, label: str, note: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self._key = key
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("StatChip")
        self.setStyleSheet(
            f"#StatChip:hover{{background:{theme.BG_BAND};border-radius:6px;}}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(5)
        self._num = QLabel(str(number))
        self._num.setStyleSheet(f"font-size:19px;font-weight:600;color:{color};")
        t = QLabel(label)
        t.setStyleSheet(f"font-size:12px;color:{theme.TEXT_SECONDARY};")
        top.addWidget(self._num)
        top.addWidget(t)
        top.addStretch(1)
        lay.addLayout(top)

        d = QLabel(note)
        d.setStyleSheet(f"font-size:11px;color:{theme.TEXT_TERTIARY};")
        lay.addWidget(d)

    def set_number(self, number: int) -> None:
        self._num.setText(str(number))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.clicked.emit(self._key)
        super().mousePressEvent(event)


class _Group(QWidget):
    """可折叠分组：标题行 + 主体。标题行的说明文字会自动省略，不挤按钮。"""

    def __init__(self, key: str, tag: str, tag_color: str, title: str, desc: str, parent=None):
        super().__init__(parent)
        self._key = key
        self._body_rows = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._frame = QFrame()
        self._frame.setObjectName("GroupFrame")
        self._frame.setStyleSheet(
            f"#GroupFrame{{border:1px solid {theme.BORDER};border-radius:9px;}}"
        )
        fl = QVBoxLayout(self._frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        # ---- 标题行
        self._head = QWidget()
        self._head.setCursor(Qt.CursorShape.PointingHandCursor)
        self._head.setObjectName("GroupHead")
        self._head.setStyleSheet(f"#GroupHead{{background:{theme.BG_BAND};}}")
        hl = QHBoxLayout(self._head)
        hl.setContentsMargins(11, 7, 11, 7)
        hl.setSpacing(8)

        self._caret = QLabel("▼")
        self._caret.setStyleSheet(f"color:{theme.TEXT_TERTIARY};font-size:9px;")
        hl.addWidget(self._caret)

        chip = QLabel(tag)
        chip.setStyleSheet(
            f"background:{tag_color}22;color:{tag_color};font-size:11px;font-weight:600;"
            "border-radius:9px;padding:1px 7px;"
        )
        hl.addWidget(chip)

        tt = QLabel(title)
        tt.setStyleSheet(f"font-size:12.5px;font-weight:600;color:{theme.TEXT_PRIMARY};")
        hl.addWidget(tt)

        self._desc = QLabel(desc)
        self._desc.setStyleSheet(f"font-size:11px;color:{theme.TEXT_TERTIARY};")
        self._desc.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._desc.setMinimumWidth(0)
        hl.addWidget(self._desc, 1)

        self._action_slot = QHBoxLayout()
        self._action_slot.setContentsMargins(0, 0, 0, 0)
        self._action_slot.setSpacing(6)
        hl.addLayout(self._action_slot)

        fl.addWidget(self._head)

        # ---- 主体
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(0)
        fl.addWidget(self._body)

        outer.addWidget(self._frame)
        self._head.mousePressEvent = self._toggle  # type: ignore[assignment]

    # -- 折叠
    def _toggle(self, _event) -> None:
        visible = not self._body.isVisible()
        self.set_body_visible(visible)

    def set_body_visible(self, visible: bool) -> None:
        self._body.setVisible(visible)
        self._caret.setText("▼" if visible else "▶")

    def collapse(self) -> None:
        self.set_body_visible(False)

    def head_action(self, widget: QWidget) -> None:
        self._action_slot.addWidget(widget)

    def set_desc(self, text: str) -> None:
        self._desc.setText(text)

    # -- 主体内容
    def add_widget(self, w: QWidget, top_hairline: bool = True) -> None:
        if top_hairline and self._body_rows:
            line = QFrame()
            line.setFixedHeight(1)
            line.setStyleSheet(f"background:{theme.BORDER_HAIR};border:none;")
            self._body_lay.addWidget(line)
        self._body_lay.addWidget(w)
        self._body_rows += 1

    @property
    def body_rows(self) -> int:
        return self._body_rows


class _Row(QWidget):
    """一主一辅两行：主行标题 + 右侧尾注，辅行可放整行宽度的内容。"""

    def __init__(self, title: str, tail: str = "", parent=None) -> None:
        super().__init__(parent)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(11, 8, 11, 8)
        self._v.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title = QLabel(title or "（无标题）")
        self.title.setStyleSheet(
            f"font-size:12px;font-weight:600;color:{theme.TEXT_PRIMARY};"
        )
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setToolTip(title)
        self.title.setMinimumWidth(0)
        top.addWidget(self.title, 1)
        self.tail = QLabel(tail)
        self.tail.setStyleSheet(
            f"font-size:11px;color:{theme.TEXT_TERTIARY};font-family:{theme.TABULAR};"
        )
        top.addWidget(self.tail)
        self._v.addLayout(top)

    def add_line(self, widget: QWidget) -> None:
        self._v.addWidget(widget)


class _MapLine(QWidget):
    """源路径 → 本机落点。路径独占整行，不截断、不靠 tooltip。"""

    def __init__(self, source: str, target: str, origin: str, candidates: list[str], parent=None):
        super().__init__(parent)
        self.source = source
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        src = QLabel(source)
        src.setStyleSheet(
            f"font-size:10.5px;color:{theme.TEXT_SECONDARY};font-family:{theme.TABULAR};"
        )
        src.setWordWrap(True)
        src.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        src.setMaximumWidth(330)
        lay.addWidget(src)

        arrow = QLabel("→")
        arrow.setStyleSheet(f"color:{theme.TEXT_TERTIARY};")
        lay.addWidget(arrow)

        self.edit = QLineEdit(target)
        self.edit.setStyleSheet(
            f"font-family:{theme.TABULAR};font-size:10.5px;border:1px solid {theme.BORDER};"
            f"border-radius:6px;padding:3px 7px;"
        )
        self.edit.setMinimumWidth(200)
        self.edit.setPlaceholderText("留空则不映射（保留源路径）")
        lay.addWidget(self.edit, 1)

        self.combo = None
        if candidates:
            self.combo = QComboBox()
            self.combo.addItem("改用同名候选…", "")
            for c in candidates[:8]:
                self.combo.addItem(c, c)
            self.combo.setStyleSheet("font-size:11px;")
            self.combo.currentIndexChanged.connect(self._on_pick)
            lay.addWidget(self.combo)

        self.tag = QLabel(self._tag_text(origin))
        self.tag.setStyleSheet(self._tag_style(origin))
        lay.addWidget(self.tag)

    @staticmethod
    def _tag_text(origin: str) -> str:
        return {
            "memory": "记忆",
            "exact": "同路径命中",
            "strong": "同名命中",
            "weak": "同名候选",
        }.get(origin, "未命中 · 将新建")

    @staticmethod
    def _tag_style(origin: str) -> str:
        color = {
            "memory": "#185FA5",
            "exact": theme.PRIMARY_TEXT,
            "strong": theme.PRIMARY_TEXT,
            "weak": theme.WARN,
        }.get(origin, theme.WARN)
        bg = {
            "memory": "#E6F1FB",
            "exact": theme.PRIMARY_SOFT,
            "strong": theme.PRIMARY_SOFT,
            "weak": theme.WARN_SOFT,
        }.get(origin, theme.WARN_SOFT)
        return f"color:{color};background:{bg};font-size:10.5px;border-radius:9px;padding:1px 7px;"

    def _on_pick(self, _idx: int) -> None:
        if self.combo is None:
            return
        picked = self.combo.currentData()
        if not picked:
            return
        self.edit.setText(picked)
        self.tag.setText("同名候选")
        self.tag.setStyleSheet(self._tag_style("strong"))

    def value(self) -> str:
        return self.edit.text().strip()


class _DivItem(QWidget):
    """一条分叉：并排两份 + 取舍单选。切单选只改这一条，不重新 plan。"""

    def __init__(self, action, base, on_change, parent=None) -> None:
        super().__init__(parent)
        self.action = action
        self._base = base
        self._on_change = on_change
        s = action.session
        line_count = s.fingerprint.line_count if s.fingerprint else 0

        v = QVBoxLayout(self)
        v.setContentsMargins(11, 9, 11, 9)
        v.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        title = QLabel(s.title or s.cid[:8])
        title.setStyleSheet(f"font-size:12px;font-weight:600;color:{theme.TEXT_PRIMARY};")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip(s.title)
        title.setMinimumWidth(0)
        top.addWidget(title, 1)
        meta = QLabel(f"共同旧版 {action.div_common} 行")
        meta.setStyleSheet(f"font-size:10.5px;color:{theme.TEXT_TERTIARY};")
        top.addWidget(meta)
        v.addLayout(top)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.card_local = self._card(
            "本机副本",
            importer._ts_label(action.local_ts),
            action.div_local_new,
            "本机续写",
        )
        self.card_src = self._card(
            "包内副本（对方）",
            importer._ts_label(action.src_ts),
            action.div_src_new,
            "对方续写",
        )
        cards.addWidget(self.card_local, 1)
        cards.addWidget(self.card_src, 1)
        v.addLayout(cards)

        pick = QHBoxLayout()
        pick.setSpacing(14)
        self._group = QButtonGroup(self)
        self._buttons: dict[str, QRadioButton] = {}
        for key, text in (
            (_KEEP_SRC, "保留包内"),
            (_KEEP_LOCAL, "保留本机"),
            (_KEEP_BOTH, "保留两份"),
        ):
            rb = QRadioButton(text)
            rb.setStyleSheet("font-size:11px;")
            rb.setChecked(key == action.div_keep)
            rb.toggled.connect(self._changed)
            self._group.addButton(rb)
            self._buttons[key] = rb
            pick.addWidget(rb)
        pick.addStretch(1)
        self.drop = QLabel()
        self.drop.setStyleSheet(f"font-size:10.5px;color:{theme.DANGER};")
        pick.addWidget(self.drop)
        v.addLayout(pick)

        self._paint()

    @staticmethod
    def _card(who: str, when: str, new_lines: int, caption: str) -> QFrame:
        f = QFrame()
        f.setObjectName("BranchCard")
        f.setStyleSheet(
            f"#BranchCard{{border:1px solid {theme.BORDER};border-radius:8px;"
            f"background:{theme.BG_BAND};}}"
        )
        lay = QVBoxLayout(f)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        head = QHBoxLayout()
        w = QLabel(who)
        w.setStyleSheet(f"font-size:10.5px;font-weight:600;color:{theme.TEXT_SECONDARY};")
        t = QLabel(f"续写 {when}")
        t.setStyleSheet(f"font-size:10.5px;color:{theme.TEXT_TERTIARY};")
        head.addWidget(w)
        head.addStretch(1)
        head.addWidget(t)
        lay.addLayout(head)
        n = QLabel(f"{caption} +{new_lines} 行")
        n.setStyleSheet(f"font-size:11.5px;font-weight:600;font-family:{theme.TABULAR};")
        lay.addWidget(n)
        return f

    def _changed(self, _checked: bool) -> None:
        pick = self._pick()
        if pick:
            importer.apply_div_choice(self.action, pick, self._base)
            self._paint()
            self._on_change()

    def _pick(self) -> str:
        for key, rb in self._buttons.items():
            if rb.isChecked():
                return key
        return ""

    def _paint(self) -> None:
        """把「哪一份会被丢掉」画到卡片上 —— 这是本页唯一会丢数据的地方。"""
        keep = self.action.div_keep or "local"
        losing_src = keep == _KEEP_LOCAL
        losing_local = keep == _KEEP_SRC
        self.card_local.setStyleSheet(
            f"#BranchCard{{border:1px solid {theme.DANGER if losing_local else theme.BORDER};"
            f"border-radius:8px;background:{theme.DANGER_SOFT if losing_local else theme.BG_BAND};}}"
        )
        self.card_src.setStyleSheet(
            f"#BranchCard{{border:1px solid {theme.DANGER if losing_src else theme.BORDER};"
            f"border-radius:8px;background:{theme.DANGER_SOFT if losing_src else theme.BG_BAND};}}"
        )
        drop = self.action.dropped_lines
        if keep == _KEEP_BOTH:
            self.drop.setText("两份都留")
            self.drop.setStyleSheet(f"font-size:10.5px;color:{theme.TEXT_TERTIARY};")
        else:
            side = "本机" if losing_local else "包内"
            self.drop.setText(f"丢弃{side} +{drop} 行（将单独备份）")
            self.drop.setStyleSheet(f"font-size:10.5px;color:{theme.DANGER};")


# ---------------------------------------------------------------- 主对话框


class MergeDialog(QDialog):
    """导入 · 增量合并。对外接口与原 PlanDialog 对齐，多一个 div_choices()。"""

    def __init__(
        self,
        bundle,
        actions,
        cwd_remaps=None,
        parent=None,
        base: str | None = None,
    ) -> None:
        super().__init__(parent)
        # base 透传到底：分叉取舍要 stat 本地正文，测试里必须能指向临时 home
        self._base = base
        self._bundle = bundle
        self._actions = list(actions)
        self._remaps = list(cwd_remaps or [])
        self._dry_run = False
        self._back_pack_path = ""
        self._map_lines: list[_MapLine] = []
        self._div_items: list[_DivItem] = []

        self.setWindowTitle("导入 · 增量合并")
        self.setStyleSheet(f"QDialog{{background:{theme.BG_CARD};}}")
        self._build()
        self._fit_to_content()

    # ------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_head())
        root.addWidget(self._build_stats())
        root.addWidget(self._build_banner())
        self._rehome_hint = self._build_rehome_hint()
        root.addWidget(self._rehome_hint)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self._inner = QVBoxLayout(inner)
        self._inner.setContentsMargins(18, 10, 18, 8)
        self._inner.setSpacing(10)
        self._scroll.setWidget(inner)
        root.addWidget(self._scroll, 1)

        self._groups: dict[str, _Group] = {}
        self._build_group_add()
        self._build_group_append()
        self._build_group_diverged()
        self._build_group_back()
        self._build_group_same()
        self._inner.addStretch(1)

        self._result = self._build_result()
        root.addWidget(self._result)

        root.addWidget(self._build_footer())

    def _build_head(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background:{theme.BG_CARD};")
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 14, 18, 10)
        v.setSpacing(6)

        row = QHBoxLayout()
        t = QLabel("导入 · 增量合并")
        t.setStyleSheet(f"font-size:14px;font-weight:600;color:{theme.TEXT_PRIMARY};")
        row.addWidget(t)
        sub = QLabel("与包比对，只写需要写的那部分")
        sub.setStyleSheet(f"font-size:11.5px;color:{theme.TEXT_TERTIARY};")
        row.addWidget(sub)
        row.addStretch(1)
        v.addLayout(row)

        src = (self._bundle.manifest.get("source") or {}) if self._bundle else {}
        host = src.get("host") or "未知机器"
        when = _fmt_time(self._bundle.manifest.get("created_at")) if self._bundle else "—"
        info = QLabel(
            f"来源设备 {host}　·　导出 {when}　·　包内 {self._bundle.total if self._bundle else 0} 条会话"
        )
        info.setStyleSheet(f"font-size:11.5px;color:{theme.TEXT_SECONDARY};")
        v.addWidget(info)
        return w

    def _build_stats(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background:{theme.BG_CARD};border-top:1px solid {theme.BORDER};")
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 8, 18, 8)
        h.setSpacing(0)
        self._chips: dict[str, _StatChip] = {}

        self._n_add = len([a for a in self._actions if a.action == importer.ADD])
        self._n_app = len([a for a in self._actions if a.action == importer.APPEND])
        self._n_div = len(importer.diverged_actions(self._actions))
        self._n_back = len(importer.back_candidates(self._actions))
        # ★ 不能用减法凑：一条会话可能既算「分叉」又算「本地更新」（分叉后取本地），
        #   相减会把「无需处理」算成负数再被 max 抹平，数字对不上。
        self._n_same = _same_count(self._actions)

        for key, num, label, note, color in (
            ("add", self._n_add, "新增", "写入本机", theme.PRIMARY_TEXT),
            ("app", self._n_app, "追加", "本机落后", "#1D4ED8"),
            ("div", self._n_div, "分叉", "双方续写", theme.WARN),
            ("back", self._n_back, "本地更新", "可回传", "#7C3AED"),
            ("same", self._n_same, "无需处理", "一致/跳过", theme.TEXT_TERTIARY),
        ):
            chip = _StatChip(key, num, label, note, color)
            chip.clicked.connect(self._scroll_to)
            self._chips[key] = chip
            h.addWidget(chip)
            if key != "same":
                line = QFrame()
                line.setFixedWidth(1)
                line.setStyleSheet(f"background:{theme.BORDER_HAIR};border:none;")
                h.addWidget(line)
        h.addStretch(1)
        return w

    def _build_banner(self) -> QWidget:
        w = QLabel()
        w.setWordWrap(True)
        w.setStyleSheet(
            f"font-size:11.5px;color:{theme.WARN};background:{theme.WARN_SOFT};"
            f"border-top:1px solid {theme.WARN_SOFT};padding:8px 18px;"
        )
        div = importer.diverged_actions(self._actions)
        if div:
            w.setText(
                f"分叉 {len(div)} 条：默认保留「续写更晚」的那一份（按正文末行时间戳判定，"
                "不看 updated_at）。被覆盖的那份会单独备份，路径在执行完成的提示里给出。"
            )
        else:
            w.setText("本次没有分叉：两台机器没有各自续写过同一条会话。")
        w.setTextFormat(Qt.TextFormat.PlainText)
        return w

    def _build_rehome_hint(self) -> QLabel:
        """跨账号导入是「搬走」不是「复制」。

        ★ 这段文案与「同步包」那条链路口径必须一致：两处入口说两套话，
          用户会以为自己点错了东西。文字里必须出现「搬走」二字 —— 只说
          「校正归属」，用户会理解成复制了一份。
        """
        lbl = QLabel()
        lbl.setWordWrap(True)
        moved = [a for a in self._actions if a.action == importer.REHOME]
        if not moved:
            lbl.setVisible(False)
            return lbl
        labels = accounts.AccountLabels.load(self._base)
        target_uid = accounts.detect_primary_uid(base=self._base)
        if not target_uid:
            lbl.setVisible(False)
            return lbl
        names = "、".join(
            (a.session.title or a.session.cid[:8]) for a in moved[:3]
        )
        tail = f" 等 {len(moved)} 条" if len(moved) > 3 else ""
        lbl.setText(
            f"其中 {len(moved)} 条会话已存在于本机「其它账号」（{names}{tail}）。"
            f"导入会把它们的归属改到「{labels.display(target_uid)}」：那是「搬走」不是「复制」——"
            "原来那个账号里将不再有它们。若想两边都留一份，请改用「本机账号对齐」。"
        )
        lbl.setStyleSheet(
            f"font-size:11.5px;color:{theme.WARN};background:{theme.WARN_SOFT};"
            "border-radius:4px;padding:7px 18px;margin:0 18px;"
        )
        return lbl

    # ------------------------------------------------------------ 分组
    def _new_group(self, key: str, tag: str, color: str, title: str, desc: str) -> _Group:
        g = _Group(key, tag, color, title, desc)
        self._groups[key] = g
        self._inner.addWidget(g)
        return g

    def _build_group_add(self) -> None:
        adds = [a for a in self._actions if a.action == importer.ADD or a.action == importer.DIVERGED_BOTH]
        g = self._new_group(
            "add", f"新增 {len(adds)}", theme.PRIMARY_TEXT, "写入本机", "落点决定它挂在哪个工程下"
        )
        if not adds:
            g.add_widget(_hint("没有新增会话。"), top_hairline=False)
            g.collapse()
            return

        # 需要落点决策的源目录（按 cwd 聚合，不按会话：同目录的会话共用一个落点）
        hits = sum(1 for m in self._remaps if m.confident)
        note = _hint(
            f"同名工程命中 {hits} / {len(self._remaps)}，其余新建到设备目录下。"
            if self._remaps
            else "这些会话的源目录在本机都存在，直接导入。"
        )
        if self._remaps:
            btn = QPushButton("全部套用同名命中")
            btn.setStyleSheet(_btn_style())
            btn.clicked.connect(self._apply_all_matches)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(11, 6, 11, 6)
            rl.addWidget(note, 1)
            rl.addWidget(btn)
            g.add_widget(row, top_hairline=False)
        else:
            g.add_widget(note, top_hairline=False)

        by_cwd = {m.source_cwd: m for m in self._remaps}
        seen: set[str] = set()
        for a in adds:
            key = rehome.normalize(a.session.cwd)
            plan = by_cwd.get(key)
            if plan is None:
                continue  # 源目录本机存在，无需映射行
            if key in seen:
                continue
            seen.add(key)
            titles = [x.session.title for x in adds if rehome.normalize(x.session.cwd) == key]
            row = _Row(
                titles[0] if titles else plan.source_cwd,
                f"{plan.session_count} 条 · {sum(1 for x in adds if rehome.normalize(x.session.cwd) == key)} 条待写",
            )
            line = _MapLine(plan.source_cwd, plan.suggested_target, plan.origin, plan.candidates)
            row.add_line(line)
            self._map_lines.append(line)
            g.add_widget(row)

    def _build_group_append(self) -> None:
        apps = [a for a in self._actions if a.action == importer.APPEND]
        total = sum((a.session.fingerprint.line_count if a.session.fingerprint else 0) - a.local_line_count for a in apps)
        g = self._new_group(
            "app", f"追加 {len(apps)}", "#1D4ED8", "补到本地末尾", "本机是包的前缀，只补差额行"
        )
        if not apps:
            g.add_widget(_hint("没有需要追加的会话。"), top_hairline=False)
            g.collapse()
            return
        g.set_desc(f"本机是包的前缀，只补差额行（共 +{total} 行）")
        for a in apps:
            src_n = a.session.fingerprint.line_count if a.session.fingerprint else 0
            delta = src_n - a.local_line_count
            row = _Row(a.session.title, f"{a.local_line_count} → {src_n}  +{delta}")
            g.add_widget(row)

    def _build_group_diverged(self) -> None:
        div = importer.diverged_actions(self._actions)
        total = importer.dropped_lines(div)
        g = self._new_group(
            "div", f"分叉 {len(div)}", theme.WARN, "双方各自续写", "默认留续写更晚的一份"
        )
        if not div:
            g.add_widget(_hint("没有分叉。"), top_hairline=False)
            g.collapse()
            return
        self._drop_label = QLabel()
        self._drop_label.setStyleSheet(f"font-size:11px;color:{theme.TEXT_SECONDARY};")
        g.head_action(self._drop_label)
        for a in div:
            item = _DivItem(a, self._base, self._refresh_summary)
            self._div_items.append(item)
            g.add_widget(item)

    def _build_group_back(self) -> None:
        back = importer.back_candidates(self._actions)
        g = self._new_group(
            "back", f"本地更新 {len(back)}", "#7C3AED", "可回传给对方", "本机比包新，本次不动它"
        )
        if not back:
            g.add_widget(_hint("本机没有比包更新的会话。"), top_hairline=False)
            g.collapse()
            return
        btn = QPushButton(f"生成回传包（{len(back)} 条）")
        btn.setStyleSheet(_btn_style(primary=True))
        btn.clicked.connect(self._on_back_pack)
        g.head_action(btn)
        for a in back[:12]:
            sink = a.session.fingerprint.line_count if a.session.fingerprint else 0
            row = _Row(a.session.title, f"本机 {a.local_line_count} ← 包 {sink}  +{max(0, a.local_line_count - sink)}")
            if a.div_keep == "local":
                row.add_line(
                    _hint("分叉后取本地：对方那几行不要了，本机这几行回传过去即可。")
                )
            g.add_widget(row)
        if len(back) > 12:
            g.add_widget(_hint(f"另有 {len(back) - 12} 条同类会话未逐条列出，回传包会包含全部 {len(back)} 条。"))

    def _build_group_same(self) -> None:
        same = [
            a
            for a in self._actions
            if a.action
            not in (
                importer.ADD,
                importer.APPEND,
                importer.DIVERGED_BOTH,
                importer.LOCAL_NEWER,
            )
            and not a.div_keep
        ]
        g = self._new_group(
            "same", f"无需处理 {len(same)}", theme.TEXT_SECONDARY, "一致 / 跳过", "不逐条列出"
        )
        counts: dict[str, int] = {}
        for a in same:
            counts[a.label] = counts.get(a.label, 0) + 1
        text = "、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无"
        g.add_widget(_hint(f"{text}。列表越长，真正要看的那几条越难找，所以这里只给汇总。"), top_hairline=False)
        g.collapse()

    # ------------------------------------------------------------ 尾注与动作
    def _build_result(self) -> QWidget:
        w = QFrame()
        w.setVisible(False)
        w.setStyleSheet(
            f"QFrame{{background:{theme.PRIMARY_SOFT};border:1px solid #CDEBD7;"
            "border-radius:9px;margin:10px 18px 0;}}"
        )
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(4)
        self._result_text = QLabel()
        self._result_text.setWordWrap(True)
        self._result_text.setStyleSheet(f"font-size:11.5px;color:{theme.PRIMARY_TEXT};")
        v.addWidget(self._result_text)
        return w

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background:{theme.BG_BAND};border-top:1px solid {theme.BORDER};")
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 10, 18, 12)
        v.setSpacing(8)

        # 客户端状态 + 两个选项
        state_row = QHBoxLayout()
        procs = guard.running_client_processes()
        self._lbl_state = QLabel(
            f"WorkBuddy 正在运行（{'、'.join(procs)}），执行前会自动关闭它"
            if procs
            else "✓ 未检测到 WorkBuddy 进程，可以安全写入"
        )
        self._lbl_state.setStyleSheet(
            f"font-size:11.5px;color:{theme.WARN if procs else theme.PRIMARY_TEXT};"
        )
        state_row.addWidget(self._lbl_state, 1)
        v.addLayout(state_row)

        self.chk_restart = QCheckBox()
        restart_option.apply(
            self.chk_restart,
            accounts.AccountLabels.load(self._base),
            accounts.detect_primary_uid(base=self._base),
        )
        v.addWidget(self.chk_restart)

        dups = [a for a in self._actions if a.overridable]
        self.chk_dups = QCheckBox(
            f"仍要导入这 {len(dups)} 条内容重复的会话（导入后侧边栏里会有两份）"
        )
        self.chk_dups.setVisible(bool(dups))
        self.chk_dups.toggled.connect(self._refresh_summary)
        v.addWidget(self.chk_dups)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self._lbl_summary = QLabel()
        self._lbl_summary.setStyleSheet(f"font-size:11.5px;color:{theme.TEXT_SECONDARY};")
        self._lbl_summary.setWordWrap(True)
        bottom.addWidget(self._lbl_summary, 1)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setStyleSheet(_btn_style())
        self.btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(self.btn_cancel)

        self.btn_dry = QPushButton("仅演练")
        self.btn_dry.setStyleSheet(_btn_style())
        self.btn_dry.clicked.connect(self._on_dry)
        bottom.addWidget(self.btn_dry)

        self.btn_ok = QPushButton("执行导入")
        self.btn_ok.setStyleSheet(_btn_style(primary=True, big=True))
        self.btn_ok.clicked.connect(self._on_ok)
        bottom.addWidget(self.btn_ok)
        v.addLayout(bottom)

        self._refresh_summary()
        return w

    # ------------------------------------------------------------ 交互
    def _scroll_to(self, key: str) -> None:
        g = self._groups.get(key)
        if g is None:
            return
        g.set_body_visible(True)
        self._scroll.ensureWidgetVisible(g)

    def _apply_all_matches(self) -> None:
        """把「同名命中/记忆」的落点批量写进输入框，未命中的保持原样（不猜）。"""
        for m in self._remaps:
            if not m.confident:
                continue
            for line in self._map_lines:
                if line.source == m.source_cwd:
                    line.edit.setText(m.suggested_target)
                    line.tag.setText(_MapLine._tag_text(m.origin))
                    line.tag.setStyleSheet(_MapLine._tag_style(m.origin))
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        self._update_stats()
        will = len([a for a in self._actions if a.will_write])
        if self.include_duplicates:
            will += len([a for a in self._actions if a.overridable])
        div = importer.diverged_actions(self._actions)
        drop = importer.dropped_lines(self._actions)
        parts = [f"将写入 {will} 项"]
        if div:
            parts.append(f"分叉取舍 {len(div)}")
            parts.append(f"丢弃 {drop} 行（将备份）")
        if self._n_back:
            parts.append(f"回传包 {self._n_back} 条")
        self._lbl_summary.setText("　·　".join(parts))
        if hasattr(self, "_drop_label"):
            self._drop_label.setText(f"丢弃 +{drop} 行" if drop else "不丢弃")
        self.btn_ok.setEnabled(will > 0)

    def _update_stats(self) -> None:
        """统计数字必须跟着取舍走：切到「保留两份」会让新增 +1、分叉不变。

        停在构建时的快照上，用户会看到「新增 0」却写着「将写入 1 项」。
        """
        chips = getattr(self, "_chips", None)
        if not chips:
            return
        acts = self._actions
        add = len([a for a in acts if a.action in (importer.ADD, importer.DIVERGED_BOTH)])
        app = len([a for a in acts if a.action == importer.APPEND])
        div = len(importer.diverged_actions(acts))
        back = len(importer.back_candidates(acts))
        chips["add"].set_number(add)
        chips["app"].set_number(app)
        chips["div"].set_number(div)
        chips["back"].set_number(back)
        chips["same"].set_number(_same_count(acts))

    def _on_back_pack(self) -> None:
        cids = [a.session.cid for a in importer.back_candidates(self._actions)]
        if not cids:
            return
        default = pack.suggest_back_pack_name()
        path, _ = QFileDialog.getSaveFileName(
            self, "保存回传包", default, "会话包 (*.wbsync *.zip);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            out = pack.export_back_pack(path, cids, base=self._base)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "生成回传包失败", str(exc))
            return
        self._back_pack_path = str(out)
        # 内联反馈而不是弹窗：路径要能一直看得见（弹窗关掉就没了），
        # 而且用户可能想连续生成两次。
        self._result_text.setText(
            f"回传包已生成（{len(cids)} 条）：{out}\n"
            "拷到另一台电脑，用「导入会话包」读入即可 —— 那边会把这些会话的新内容追加进来。"
            "官方「数据导入」遇到已存在的会话会直接跳过，这一步只有这里能做。"
        )
        self._result.setVisible(True)

    def _on_dry(self) -> None:
        self._dry_run = True
        self.accept()

    def _on_ok(self) -> None:
        will = len([a for a in self._actions if a.will_write])
        if self.include_duplicates:
            will += len([a for a in self._actions if a.overridable])
        if will <= 0:
            self.reject()
            return
        drop = importer.dropped_lines(self._actions)
        msg = f"即将写入 {will} 项会话。"
        if drop:
            msg += (
                f"\n其中分叉取舍会丢弃 {drop} 行正文（本机或包内的一侧）。"
                "\n被丢弃的那份会单独备份一份，完成提示里会给出目录。"
            )
        blocked = [a for a in self._actions if a.blocking]
        if blocked:
            msg += f"\n另有 {len(blocked)} 条无法判定，本次不动。"
        msg += "\n\n写入前会自动备份，但导入本身不可撤销。继续？"
        if (
            QMessageBox.question(self, "确认导入", msg)
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._dry_run = False
        self.accept()

    # ------------------------------------------------------------ 对外接口
    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def auto_restart(self) -> bool:
        return self.chk_restart.isChecked()

    @property
    def include_duplicates(self) -> bool:
        return self.chk_dups.isChecked()

    @property
    def back_pack_path(self) -> str:
        return self._back_pack_path

    def div_choices(self) -> dict[str, str]:
        return {a.session.cid: a.div_keep for a in importer.diverged_actions(self._actions)}

    def cwd_choices(self) -> dict[str, str]:
        """勾选且非空的「源 cwd → 本机路径」。空格子视为「沿用默认落点」。"""
        out: dict[str, str] = {}
        for line in self._map_lines:
            value = line.value()
            if value:
                out[rehome.normalize(line.source)] = value.replace("\\", "/")
        return out

    # ------------------------------------------------------------ 布局自适应
    def _fit_to_content(self) -> None:
        """宽度定值、高度随内容 —— 但不超过屏幕可用高度的 86%。

        原来的导入对话框按「最多十几个账号」定高，实际只列三行就空一大截；
        这里让内容决定高度，只在内容真的放不下时才出现内部滚动。
        """
        # 宽度定值但不死板：125% 缩放下小屏的可用宽度可能不到 880 逻辑像素，
        # 固定宽度会直接溢到屏幕外（与 winsize.py 踩过的是同一个坑）。
        screen = self.screen()
        avail = screen.availableGeometry() if screen else None
        width = min(880, int(avail.width() * 0.92)) if avail else 880
        self.setFixedWidth(max(640, width))
        self._scroll.setMinimumHeight(120)
        self.adjustSize()

        # ★ 不能直接用 self.sizeHint()：QScrollArea 的 sizeHint 与内部内容无关
        #   （默认两百多像素），照它定高就会出现「内容明明不长却要滚」。
        #   要的是 非滚动区高度 + 内容实际高度。
        inner = self._scroll.widget()
        inner_h = inner.sizeHint().height() if inner is not None else 0
        chrome = max(0, self.height() - self._scroll.height())
        screen = self.screen()
        cap = int(screen.availableGeometry().height() * 0.86) if screen else 720

        need = chrome + inner_h + 4
        if inner_h <= 0:                     # 还没完成布局，退回原来的做法
            need = self.sizeHint().height()
        self.resize(self.width(), max(360, min(need, cap)))
        # 装得下就不要滚动条：一个只在边缘露出来的滚动条最容易让人以为还有东西没看完
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if need > cap
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )


# ---------------------------------------------------------------- 工具


def _same_count(actions) -> int:
    """既不会被写、也不是分叉、也不进回传包的那些（一致 / 无正文 / 无法判定）。"""
    return len(
        [a for a in actions if not a.will_write and not a.div_keep and not a.back_candidate]
    )


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"font-size:11px;color:{theme.TEXT_TERTIARY};")
    return lab


def _btn_style(primary: bool = False, big: bool = False) -> str:
    size = "padding:7px 16px;font-size:12.5px;" if big else "padding:3px 10px;font-size:11.5px;"
    if primary:
        return (
            f"QPushButton{{background:{theme.PRIMARY};color:#fff;border:1px solid {theme.PRIMARY};"
            f"border-radius:7px;font-weight:600;{size}}}"
            f"QPushButton:hover{{background:{theme.PRIMARY_HOVER};}}"
            f"QPushButton:disabled{{background:#9AA3AE;border-color:#9AA3AE;}}"
        )
    return (
        f"QPushButton{{background:#fff;color:{theme.TEXT_PRIMARY};"
        f"border:1px solid {theme.BORDER_STRONG};border-radius:7px;{size}}}"
        f"QPushButton:hover{{background:{theme.BG_BAND};}}"
    )


def _fmt_time(value) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(value)).strftime("%m-%d %H:%M")
    except (OSError, ValueError, OverflowError, TypeError):
        return "（未知）"
