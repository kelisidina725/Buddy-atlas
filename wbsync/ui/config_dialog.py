"""配置同步界面 —— 本机配置项清单 + 导出/导入。

与会话同步的关键差别：这里每一项都是**整体替换**，所以界面上必须让人看清楚
「这一项现在是什么、包里的又是什么」，而不是给一个笼统的"同步"按钮。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config_items, config_sync, guard
from ..config_items import KIND_FILE, KIND_KV, KIND_TREE, Item
from ..config_sync import (
    CHOICE_KEEP_LOCAL,
    CHOICE_TAKE_REMOTE,
    CONFLICT,
    ConfigAction,
)
from .split_button import SplitButton
from .page_base import HeadedPage
from .theme import BG_FIELD, TEXT_SECONDARY, TEXT_TERTIARY

_KIND_LABEL = {KIND_KV: "设置项", KIND_FILE: "文件", KIND_TREE: "技能目录"}


def _fmt_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


class FuncWorker(QThread):
    """跑一个函数并回传结果。导出/导入都可能耗时，不能占主线程。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ConfigActionTable(QTableWidget):
    """配置动作表：逐项判定 + 冲突项下拉选择。

    抽成独立控件是为了让「同步包导入」的合并确认页与独立的配置导入页
    共用同一套呈现 —— 冲突项的选项、配色、统计口径只有一份实现。
    """

    changed = Signal()

    _COLUMNS = ["配置项", "处理方式", "说明", "选择"]

    def __init__(
        self, actions: list[ConfigAction], parent=None, checkable: bool = False
    ) -> None:
        """checkable=True 时最左多一列勾选框（同步包导入用；配置页不需要）。"""
        self._checkable = bool(checkable)
        off = 1 if self._checkable else 0
        self._checks: list[QCheckBox] = []
        super().__init__(0, len(self._COLUMNS) + off, parent)
        self._actions = list(actions)
        # 列号按是否有勾选列整体右移。用实例属性而不是模块常量，
        # 两种形态各自算各自的，调错列不会串到另一处。
        self.C_CHECK = 0 if self._checkable else -1
        self.C_ITEM = 0 + off
        self.C_ACTION = 1 + off
        self.C_REASON = 2 + off
        self.C_CHOICE = 3 + off

        headers = ([""] if self._checkable else []) + list(self._COLUMNS)
        self.setHorizontalHeaderLabels(headers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        hh = self.horizontalHeader()
        if self._checkable:
            hh.setSectionResizeMode(self.C_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.C_ITEM, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.C_ACTION, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.C_REASON, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.C_CHOICE, QHeaderView.ResizeMode.ResizeToContents)

        self._fill()

    def _fill(self) -> None:
        self.setRowCount(len(self._actions))
        self._checks.clear()
        for i, a in enumerate(self._actions):
            if self._checkable:
                box = QCheckBox()
                # 默认只勾「真的会写」的项：冲突项要在右边选处理方式，没选之前
                # 勾上等于假装会写，统计也会跟着虚高。
                box.setChecked(bool(a.will_write))
                box.setToolTip("取消勾选则本次不导入这一项")
                box.toggled.connect(lambda _on: self.changed.emit())
                self._checks.append(box)
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(5, 0, 0, 0)
                hl.addWidget(box)
                hl.addStretch(1)
                self.setCellWidget(i, self.C_CHECK, holder)
            self.setItem(i, self.C_ITEM, QTableWidgetItem(a.item.label))

            act_item = QTableWidgetItem(a.label)
            if a.action == CONFLICT:
                act_item.setForeground(Qt.GlobalColor.darkRed)
            elif a.will_write:
                act_item.setForeground(Qt.GlobalColor.darkGreen)
            else:
                act_item.setForeground(Qt.GlobalColor.gray)
            self.setItem(i, self.C_ACTION, act_item)
            self.setItem(i, self.C_REASON, QTableWidgetItem(a.reason))

            if a.action == CONFLICT:
                combo = QComboBox()
                combo.addItem("跳过（保持现状）", "")
                combo.addItem("保留本地版本", CHOICE_KEEP_LOCAL)
                combo.addItem("采用对端版本", CHOICE_TAKE_REMOTE)
                combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
                self.setCellWidget(i, self.C_CHOICE, combo)
            else:
                self.setItem(i, self.C_CHOICE, QTableWidgetItem("—"))

    def choices(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for i, a in enumerate(self._actions):
            if a.action != CONFLICT:
                continue
            w = self.cellWidget(i, self.C_CHOICE)
            if isinstance(w, QComboBox):
                data = w.currentData()
                if data:
                    out[a.item.key] = str(data)
        return out

    def stats(self) -> tuple[int, int]:
        """(将写入项数, 冲突项数)。冲突项选了「采用对端」才计入写入。

        checkable 时只统计勾上的行 —— 界面上写着「将写入 N 项」，N 就必须是
        真会写的那几条，不能把没勾的算进去。
        """
        choices = self.choices()
        writes = 0
        conflicts = 0
        for i, a in enumerate(self._actions):
            if not self._is_checked(i):
                continue
            if a.action == CONFLICT:
                conflicts += 1
                if choices.get(a.item.key) == CHOICE_TAKE_REMOTE:
                    writes += 1
            elif a.will_write:
                writes += 1
        return writes, conflicts

    # ---------------------------------------------------------- 勾选
    def _is_checked(self, i: int) -> bool:
        if not self._checkable or i >= len(self._checks):
            return True
        return self._checks[i].isChecked()

    def selected_actions(self) -> list[ConfigAction]:
        return [a for i, a in enumerate(self._actions) if self._is_checked(i)]

    def selected_count(self) -> int:
        return len(self.selected_actions())

    def set_all_checked(self, on: bool) -> None:
        for box in self._checks:
            box.setChecked(bool(on))


class ConfigPlanDialog(QDialog):
    """导入计划 —— 冲突项在这里逐个拍板。"""

    def __init__(self, bundle, actions: list[ConfigAction], parent=None) -> None:
        super().__init__(parent)
        self._actions = list(actions)
        self._dry_run = False
        self.setWindowTitle("配置导入计划")
        self.resize(960, 560)
        self._build(bundle)
        self._refresh_summary()

    def _build(self, bundle) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        src = bundle.manifest.get("source") or {}
        head = QLabel(
            f"来自：{src.get('host', '（未知机器）')}    共 {bundle.total} 项配置\n"
            "冲突项（两端在上次同步后都改过）需要你选择保留哪一边。"
        )
        head.setStyleSheet("font-size: 12px; color: #444;")
        layout.addWidget(head)

        self.table = ConfigActionTable(self._actions)
        self.table.changed.connect(self._refresh_summary)
        layout.addWidget(self.table, 1)

        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("font-size: 12px; color: #333;")
        layout.addWidget(self.lbl_summary)

        btns = QDialogButtonBox()
        self.btn_dry = btns.addButton("仅演练（不写入）", QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_ok = btns.addButton("执行同步", QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_dry.clicked.connect(self._on_dry)
        self.btn_ok.clicked.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _refresh_summary(self) -> None:
        writes, conflicts = self.table.stats()
        self.lbl_summary.setText(
            f"将写入 {writes} 项；冲突 {conflicts} 项（未选择的一律跳过）"
        )
        self.btn_ok.setEnabled(writes > 0)

    def choices(self) -> dict[str, str]:
        return self.table.choices()

    def _on_dry(self) -> None:
        self._dry_run = True
        self.accept()

    @property
    def dry_run(self) -> bool:
        return self._dry_run


class ConfigPage(HeadedPage):
    """本机配置项清单，勾选后导出；导入入口也在这里。

    做成 QWidget 而不是 QDialog，是为了能嵌进主界面的导航页里 ——
    配置同步与会话同步是同一个工具的两半，不该一个在页面里、一个在弹窗里。

    ## 页头的两个分裂按钮

    主动作都是**同步包**（会话 + 配置一起走），下拉才是「仅配置」的老格式包 ——
    后者只在「只想要配置、不想带会话」这种场面用，属于低频变体。
    把它放主按钮上会让绝大多数人默认导出一个不完整的包。
    """

    # 主动作交给主窗口：同步包的导出/导入牵涉账号、范围、关客户端，页面管不了
    sync_import_requested = Signal()
    sync_export_requested = Signal()

    # 设计稿的四列。指纹不进表：它对用户没有意义，降级进「大小」的 tooltip
    _COLUMNS = ["名称", "类型", "大小", "说明"]
    C_NAME, C_KIND, C_SIZE, C_NOTE = range(4)

    def __init__(self, parent=None, show_transfer: bool = True) -> None:
        super().__init__(parent)
        self._items: list[Item] = []
        self._worker: FuncWorker | None = None
        self._show_transfer = show_transfer
        self._build()
        self.reload()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # ---- 工具栏：左「已勾选 N / 全部 · 体积」+ 全选/全不选，右动作
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(8)
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("StatusLine")
        tools.addWidget(self.lbl_status)

        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("LinkButton")
        self.btn_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        tools.addWidget(self.btn_all)
        self.btn_none = QPushButton("全不选")
        self.btn_none.setObjectName("LinkButton")
        self.btn_none.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        tools.addWidget(self.btn_none)
        tools.addStretch(1)

        self.btn_refresh = QPushButton("重新扫描")
        self.btn_refresh.clicked.connect(self.reload)
        tools.addWidget(self.btn_refresh)

        self.btn_import = SplitButton("导入同步包")
        self.btn_import.setToolTip("读入同步包，一次完成会话与配置")
        self.btn_import.set_arrow_tooltip("其他导入方式")
        self.btn_import.clicked.connect(self.sync_import_requested.emit)
        self.btn_import.add_action("导入配置包（仅配置）…", self._import)
        tools.addWidget(self.btn_import)

        # 导出统一由主窗口那一个入口承担（当前登录账号的整包），
        # 这里不再提供第二个导出按钮，也不再有「仅配置」的残包。
        self.btn_export = SplitButton("导出同步包", primary=True)
        self.btn_export.clicked.connect(self.sync_export_requested.emit)
        self.btn_export.setVisible(False)
        layout.addLayout(tools)

        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setFixedHeight(44)
        # 「名称」吃掉剩余宽度：否则它被拉得很宽、右边留一大片空白
        hh.setSectionResizeMode(self.C_NAME, QHeaderView.ResizeMode.Stretch)
        for c in (self.C_KIND, self.C_SIZE):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.C_KIND, 100)
        self.table.setColumnWidth(self.C_SIZE, 172)
        # 「说明」给固定宽度：它是注释性文字，不该和工作列抢空间
        hh.setSectionResizeMode(self.C_NOTE, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.C_NOTE, 320)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

    # ---------------------------------------------------------- 数据
    def reload(self) -> None:
        self._items = config_items.discover()
        self._rows: dict[int, Item] = {}
        groups = self._grouped(self._items)

        # 行数 = 项数 + 分组带数。分组带用 setSpan 横跨整行，不参与勾选。
        # ★ 凡是遍历行的地方（全选 / 取选中项）都必须走 self._rows，
        #   按 rowCount 直接取会把分组带当成配置项。
        self.table.blockSignals(True)
        self.table.clearSpans()
        self.table.setRowCount(len(self._items) + sum(1 for g in groups if g[2]))
        r = 0
        for title, desc, items in groups:
            if not items:
                continue
            band = QTableWidgetItem(f"{title} · {len(items)} 项 · {desc}")
            band.setFlags(Qt.ItemFlag.NoItemFlags)
            band.setBackground(QColor(BG_FIELD))
            band.setForeground(QColor(TEXT_TERTIARY))
            self.table.setSpan(r, 0, 1, len(self._COLUMNS))
            self.table.setItem(r, 0, band)
            self.table.setRowHeight(r, 30)
            r += 1
            for item in items:
                self._fill_row(r, item)
                self._rows[r] = item
                r += 1
        self.table.setRowCount(r)
        self.table.blockSignals(False)

        bl = config_sync.Baseline.load()
        when = "从未同步"
        if bl.updated_at:
            from datetime import datetime

            try:
                when = datetime.fromtimestamp(bl.updated_at).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError, OverflowError):
                when = "未知"
        self._set_sub(f"{len(self._items)} 项可同步 · 上次同步：{when}")
        self._refresh_summary()
        self.lbl_status.setToolTip(f"基线记录 {bl.total} 项")

    @staticmethod
    def _grouped(items: list[Item]) -> list[tuple[str, str, list[Item]]]:
        """把配置项归成设计稿的四组。

        分组键是「这条描述的是我的偏好，还是这台机器的环境」：
          人格配置（默认同步的文件）-> 决定 AI 怎么说话
          设置项（JSON 字段）      -> 开关与参数的当前值
          本机设置（默认不同步）    -> 换了机器就该重新长
          技能目录                -> 整目录搬运
        """
        persona, kv, local, skills = [], [], [], []
        for item in items:
            if item.kind == KIND_TREE:
                skills.append(item)
            elif item.kind == KIND_KV:
                kv.append(item)
            elif item.default_checked:
                persona.append(item)
            else:
                local.append(item)
        return [
            ("人格配置", "决定 AI 记住你什么", persona),
            ("设置项", "开关与字段的当前值", kv),
            ("本机设置", "只在当前电脑生效，不会同步", local),
            ("技能目录", "带走的技能会在另一台电脑可用", skills),
        ]

    def _fill_row(self, row: int, item: Item) -> None:
        name_item = QTableWidgetItem(item.label)
        name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        tips = [item.rel_path or item.sub_key or item.key]
        if item.note:
            tips.append(item.note)
        if item.sensitive:
            tips.append("可能含凭据")
        name_item.setToolTip("\n".join(t for t in tips if t))
        # 敏感项默认不勾选 —— 把密钥带出去必须是有意识的决定。
        # default_checked=False 的项同理：它描述的是这台机器，不是用户偏好。
        name_item.setCheckState(
            Qt.CheckState.Checked
            if (item.default_checked and not item.sensitive)
            else Qt.CheckState.Unchecked
        )
        self.table.setItem(row, self.C_NAME, name_item)

        kind = QTableWidgetItem(_KIND_LABEL.get(item.kind, item.kind))
        kind.setForeground(QColor(TEXT_SECONDARY))
        self.table.setItem(row, self.C_KIND, kind)

        size = _fmt_size(item.size)
        if item.kind == KIND_TREE:
            size += f"（{item.file_count} 文件）"
        size_item = QTableWidgetItem(size)
        size_item.setForeground(QColor(TEXT_SECONDARY))
        size_item.setToolTip(f"指纹 {item.short_hash}")
        self.table.setItem(row, self.C_SIZE, size_item)

        # 说明列要分清两件事：
        #   sensitive        可能含凭据 —— 搬出去前必须自己看一眼
        #   不默认勾选        描述的是本机环境（代理 / 自启 / 公众号），
        #                    搬过去往往不适用，但不涉及凭据
        # 合并成一句「谨慎同步」会让用户以为所有项都有风险，等于没提示。
        # 两类标记合成一句短的：表格里放不下两句长话，长说明留给 tooltip
        notes: list[str] = []
        if item.sensitive and not item.default_checked:
            notes = ["含凭据 · 只在当前电脑生效"]
        elif item.sensitive:
            notes = ["含凭据，谨慎同步"]
        elif not item.default_checked:
            notes = ["只在当前电脑生效"]
        # 项自身的说明（「为什么不同步授权凭据」这类）不铺进表格：
        # 它动辄三四十字，放进 320px 就会折行、把行高顶成一高一低。悬停可看全文。
        note_item = QTableWidgetItem("；".join(notes))
        note_item.setToolTip(item.note or "；".join(notes))
        if item.sensitive:
            note_item.setForeground(QColor("#B4232A"))
        elif not item.default_checked:
            note_item.setForeground(QColor("#B45309"))
        else:
            note_item.setForeground(QColor(TEXT_TERTIARY))
        note_item.setToolTip(item.note or "；".join(notes))
        self.table.setItem(row, self.C_NOTE, note_item)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item is not None and item.column() == self.C_NAME:
            self._refresh_summary()

    def _refresh_summary(self) -> None:
        picked = self.selected_items()
        size = sum(i.size for i in picked)
        self.lbl_status.setText(
            f"已勾选 {len(picked)} / {len(self._items)} 项 · {_fmt_size(size)}"
        )

    def _set_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for row in getattr(self, "_rows", {}):
            cell = self.table.item(row, self.C_NAME)
            if cell is not None:
                cell.setCheckState(state)
        self.table.blockSignals(False)
        self._refresh_summary()

    def selected_items(self) -> list[Item]:
        out: list[Item] = []
        for row, item in getattr(self, "_rows", {}).items():
            cell = self.table.item(row, self.C_NAME)
            if cell is not None and cell.checkState() == Qt.CheckState.Checked:
                out.append(item)
        return out

    # 供主窗口的同步条与「更多」菜单读取/调用
    def items(self) -> list[Item]:
        return list(self._items)

    def item_count(self) -> int:
        return len(self._items)

    def export_config(self) -> None:
        self._export()

    def import_config(self) -> None:
        self._import()

    # ---------------------------------------------------------- 导出
    def _export(self) -> None:
        items = self.selected_items()
        if not items:
            QMessageBox.information(self, "导出配置包", "没有勾选任何项。")
            return
        if any(i.sensitive for i in items) and not self._confirm_sensitive(items):
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出配置包",
            "workbuddy-config.wbsync-config.zip",
            "配置包 (*.wbsync-config.zip *.zip)",
        )
        if not path:
            return
        if not path.endswith(".zip"):
            path += ".wbsync-config.zip"

        self._run_bg(
            lambda: config_sync.export_config_bundle(path, items),
            lambda p: self._on_exported(p, len(items)),
        )

    def _confirm_sensitive(self, items: list[Item]) -> bool:
        names = "、".join(i.label for i in items if i.sensitive)
        ans = QMessageBox.warning(
            self,
            "包含敏感项",
            f"以下项可能含凭据：\n{names}\n\n"
            "配置包是普通 zip，不含加密。请确认传输渠道安全。继续导出？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return ans == QMessageBox.StandardButton.Yes

    def _on_exported(self, path, count: int) -> None:
        size = Path(str(path)).stat().st_size
        QMessageBox.information(
            self,
            "导出完成",
            f"已导出 {count} 项到：\n{path}\n\n大小 {_fmt_size(size)}",
        )

    # ---------------------------------------------------------- 导入
    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择配置包", "", "配置包 (*.wbsync-config.zip *.zip);;所有文件 (*.*)"
        )
        if not path:
            return
        self._run_bg(
            lambda: (
                config_sync.read_config_bundle(path),
                config_items.discover(),
                config_sync.Baseline.load(),
            ),
            self._on_loaded,
        )

    def _on_loaded(self, payload) -> None:
        try:
            bundle, local, baseline = payload
        except (TypeError, ValueError):
            return
        # 配置是纯文本，写入不会像 SQLite 那样损坏数据；但客户端退出时
        # 可能用内存里的配置回写，导致「同步了却没生效」。提示但不强制拦截。
        if guard.is_client_running():
            ans = QMessageBox.warning(
                self,
                "建议先退出客户端",
                "WorkBuddy 正在运行。\n\n"
                "配置文件是纯文本，写入不会损坏数据，但客户端可能用内存中的配置"
                "覆盖本次修改，表现为「同步成功却不生效」。\n\n"
                "建议退出客户端后再同步。仍要继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        actions = config_sync.plan(
            bundle.items, local, baseline, bundle.members, base=None
        )
        dlg = ConfigPlanDialog(bundle, actions, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        choices = dlg.choices()
        dry = dlg.dry_run

        self._run_bg(
            lambda: config_sync.execute_config_sync(
                actions, bundle, dry_run=dry, choices=choices
            ),
            self._on_synced,
        )

    def _on_synced(self, rep) -> None:
        head = "演练完成（未写入）" if rep.dry_run else "配置同步完成"
        lines = [rep.summary(), ""]
        if rep.backup is not None and not rep.backup.empty:
            lines.append(f"备份：{rep.backup.root}")
            lines.append("")
        if rep.failed:
            lines.append(f"失败 {len(rep.failed)} 项：")
            for key, why in rep.failed[:8]:
                lines.append(f"  · {key} — {why}")
            lines.append("")
        if not rep.dry_run and rep.applied:
            lines.append("重启 WorkBuddy 后生效。")
        QMessageBox.information(self, head, "\n".join(lines))
        self.reload()

    # ---------------------------------------------------------- 后台
    def _run_bg(self, fn, on_done) -> None:
        self._set_busy(True)
        self._worker = FuncWorker(fn)
        self._worker.done.connect(on_done)
        self._worker.done.connect(lambda _r: self._set_busy(False))
        self._worker.failed.connect(self._on_bg_failed)
        self._worker.start()

    def _set_busy(self, busy: bool) -> None:
        for w in (self.btn_export, self.btn_import, self.btn_refresh, self.btn_all, self.btn_none):
            w.setEnabled(not busy)
        if busy:
            self.lbl_status.setText("处理中…")

    def _on_bg_failed(self, msg: str) -> None:
        self._set_busy(False)
        QMessageBox.critical(self, "操作失败", msg)


class ConfigSyncDialog(QDialog):
    """配置同步的独立窗口形态（保留给需要单开窗口的场景）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("配置同步")
        self.resize(880, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.page = ConfigPage(self)
        layout.addWidget(self.page, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.accept)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)

    @property
    def table(self):
        """转发给页面，保持既有调用与冒烟测试可用。"""
        return self.page.table

    def selected_items(self):
        return self.page.selected_items()

    def reload(self):
        return self.page.reload()
