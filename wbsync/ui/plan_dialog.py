"""导入计划确认对话框 —— 写入前的最后一道关口。

## 为什么必须先「看计划」再「执行」

导入是不可逆的（虽然自动备份了）。让人先看到「哪条会被新增、哪条会被追加、
哪条因分叉被拒绝」，才有机会发现「这个包不是我想要的那个」。

## 为什么客户端运行时禁止点执行

SQLite WAL 模式下客户端持有锁，并发写会损坏**全部**会话记录 —— 不是丢这一条，
是丢全部。所以这里不做「强行继续」的口子，只能关掉客户端再点重新检测。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import accounts, guard
from . import restart_option, theme
from .cwd_remap_widget import CwdRemapWidget

_COLUMNS = ["标题", "判定", "说明"]
COL_TITLE, COL_ACTION, COL_REASON = range(3)

# 需要人留意的判定
_WARNING_ACTIONS = {"diverged", "unknown", "no-cwd"}


class PlanDialog(QDialog):
    def __init__(self, bundle, actions, cwd_remaps=None, parent=None) -> None:
        super().__init__(parent)
        self._actions = list(actions)
        self._cwd_remaps = list(cwd_remaps or [])
        self._dry_run = False
        self.setWindowTitle("导入计划")
        self.resize(880, 520)
        self._build_ui(bundle)
        self._fill()  # 依赖 _build_ui 建好的 lbl_summary
        self._refresh_client_state()

    # --------------------------------------------------------------- 界面
    def _build_ui(self, bundle) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        src = bundle.manifest.get("source") or {}
        # 导入会把这些会话的归属统一改写成**本机登录账号**，而重启后客户端也只会
        # 回到这个账号 —— 两件事是同一个前提，所以只解析一次，两处共用。
        self._labels = accounts.AccountLabels.load()
        self._target_uid = accounts.detect_primary_uid(snapshot=self._labels.snapshot)
        uid, labels = self._target_uid, self._labels
        if uid:
            target = labels.display(uid)
        else:
            target = "（未识别到本机账号，将保留源账号归属）"

        head = QLabel(
            f"来自：{src.get('host', '（未知机器）')}    导出时间：{_fmt_ts(bundle.manifest.get('created_at'))}\n"
            f"包内 {bundle.total} 条会话　|　导入后统一归属到「{target}」，不保留源账号"
        )
        head.setStyleSheet("font-size: 12px; color: #444;")
        head.setWordWrap(True)
        layout.addWidget(head)
        layout.addWidget(self._build_rehome_hint())

        # 会话表放进一个页签，便于和「工作空间映射」页签并列
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sessions_tab(), f"会话 · {len(self._actions)}")
        if self._cwd_remaps:
            self.remap_widget = CwdRemapWidget(self._cwd_remaps)
            self.tabs.addTab(self.remap_widget, f"工作空间映射 · {len(self._cwd_remaps)}")
        layout.addWidget(self.tabs, 1)

        # 客户端状态行
        state = QHBoxLayout()
        self.lbl_state = QLabel()
        self.lbl_state.setStyleSheet("font-size: 12px;")
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.clicked.connect(self._refresh_client_state)
        state.addWidget(self.lbl_state, 1)
        state.addWidget(self.btn_recheck)
        layout.addLayout(state)

        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("font-size: 12px; color: #333;")
        layout.addWidget(self.lbl_summary)

        # 客户端运行中时，写库前必须关掉它。这里让用户预先决定要不要自动拉起来，
        # 免得导入完还得手动开一次才能看到会话进侧边栏。
        # 文案点名到账号：重启只会回到当前登录账号，不点名就等于暗示「能跳到任意账号」。
        self.chk_restart = QCheckBox()
        restart_option.apply(self.chk_restart, self._labels, self._target_uid)
        layout.addWidget(self.chk_restart)

        # 「内容重复」：本地已有一段一模一样的对话（只是 id 不同），默认不导入。
        # 与同步包对话框同一口径：展示、标注、可强制勾选，但不默默复制第二份。
        self._dups = [a for a in self._actions if a.overridable]
        self.chk_dups = QCheckBox(
            f"仍要导入这 {len(self._dups)} 条内容重复的会话（导入后侧边栏里会有两份）"
        )
        self.chk_dups.setVisible(bool(self._dups))
        self.chk_dups.setToolTip(
            "这些会话在本地已有内容完全相同的一条（id 不同，通常是换机搬运或参考工具留下的副本）。"
        )
        self.chk_dups.toggled.connect(self._refresh_client_state)
        layout.addWidget(self.chk_dups)

        self.buttons = QDialogButtonBox()
        self.btn_dry = self.buttons.addButton("仅演练（不写入）", QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_ok = self.buttons.addButton("执行导入", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_cancel = self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_dry.clicked.connect(self._on_dry)
        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_cancel.clicked.connect(self.reject)
        layout.addWidget(self.buttons)

    def _build_rehome_hint(self) -> QLabel:
        """跨账号导入会把会话「搬走」—— 归属改到当前账号，原账号里就没有了。

        与同步包对话框同一段文案：两处入口说两套话，用户会以为自己点错了东西。
        """
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"font-size:12px;color:{theme.WARN};background:{theme.WARN_SOFT};"
            "border-radius:4px;padding:7px 9px;"
        )
        moved = [a for a in self._actions if a.action == "rehome"]
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

    def _build_sessions_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(6)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_ACTION, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_REASON, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)
        return page

    def _fill(self) -> None:
        self.table.setRowCount(len(self._actions))
        for i, a in enumerate(self._actions):
            values = [a.session.title, a.label, a.reason]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == COL_ACTION and a.action in _WARNING_ACTIONS:
                    item.setForeground(Qt.GlobalColor.darkRed)
                elif c == COL_ACTION and a.overridable:
                    # 「不建议」而不是「做不到」，用琥珀色与拒绝区分
                    item.setForeground(Qt.GlobalColor.darkYellow)
                elif c == COL_ACTION and a.will_write:
                    item.setForeground(Qt.GlobalColor.darkGreen)
                self.table.setItem(i, c, item)

        counts: dict[str, int] = {}
        for a in self._actions:
            counts[a.label] = counts.get(a.label, 0) + 1
        parts = [f"{k} {v}" for k, v in sorted(counts.items())]
        if self._dups and not self.include_duplicates:
            parts.append(f"（{len(self._dups)} 条内容重复默认跳过）")
        self.lbl_summary.setText("；".join(parts) if parts else "无动作")

    # --------------------------------------------------------------- 逻辑
    def _refresh_client_state(self) -> None:
        procs = guard.running_client_processes()
        if procs:
            # 不禁用按钮：执行导入前会自动关闭客户端（见 main_window._run_import）
            self.lbl_state.setText(
                f"WorkBuddy 正在运行（{'、'.join(procs)}）。点「执行导入」时会先自动关闭它"
                "（一般 20 秒内），写入完成后再按你的选择启动。"
            )
            self.lbl_state.setStyleSheet("font-size: 12px; color: #8a6d00;")
            self.btn_ok.setToolTip(
                "执行前会先关闭 WorkBuddy：未发送的输入会丢失、正在进行的对话会中断"
            )
        else:
            self.lbl_state.setText("✓ 未检测到 WorkBuddy 进程，可以安全写入")
            self.lbl_state.setStyleSheet("font-size: 12px; color: #1b7f3b;")
            self.btn_ok.setToolTip("")
        self.btn_ok.setEnabled(self._writable_count() > 0)

    def _writable_count(self) -> int:
        """会写盘的条数 —— 勾了「仍要导入重复项」时把那批也算进去。"""
        n = sum(1 for a in self._actions if a.will_write)
        if self.include_duplicates:
            n += len(self._dups)
        return n

    @property
    def include_duplicates(self) -> bool:
        return bool(getattr(self, "chk_dups", None) and self.chk_dups.isChecked())

    def _on_dry(self) -> None:
        self._dry_run = True
        self.accept()

    def _on_ok(self) -> None:
        """执行前再确认一次 —— 写入不可逆，多问一句是值得的。"""
        count = self._writable_count()
        if count == 0:
            self.reject()
            return
        blocked = [a for a in self._actions if a.blocking]
        msg = f"即将写入 {count} 条会话。"
        if blocked:
            msg += f"\n另有 {len(blocked)} 条因分叉/无法判定被拒绝，不会被改动。"
        skipped = len(self._dups) if not self.include_duplicates else 0
        if skipped:
            msg += f"\n{skipped} 条因本地已有同内容副本被跳过。"
        moved = sum(1 for a in self._actions if a.action == "rehome")
        if moved:
            msg += f"\n其中 {moved} 条会从其它账号改归当前账号 —— 那是搬走，原账号里将看不到它们。"
        msg += "\n\n写入前会自动备份，但导入本身不可撤销。继续？"
        from PySide6.QtWidgets import QMessageBox

        ans = QMessageBox.question(self, "确认导入", msg)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._dry_run = False
        self.accept()

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def auto_restart(self) -> bool:
        return self.chk_restart.isChecked()

    def cwd_choices(self) -> dict[str, str]:
        """返回勾选且非空的「源 cwd → 本机路径」映射。"""
        if hasattr(self, "remap_widget"):
            return self.remap_widget.choices()
        return {}


def _fmt_ts(value: object) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError, TypeError):
        return "（未知）"
