"""导出确认 —— 只读清单，一次确认。

## 为什么不再有勾选

导出被收敛成**唯一一种产物**：当前登录账号的会话 + 不含凭据的配置 + 全部
工作空间记忆。既然只有一种，就不该再让人在对话框里做三类勾选、两种账号档、
两种记忆模式 —— 那些组合里有一半会产出「只有一半内容的包」，搬过去之后
才发现少了东西，代价比多点一次按钮大得多。

## 为什么账号口径写在正文里

侧栏可以切到别的账号看数据（只读），导出却永远取**当前登录账号**。这两个
状态不一致，如果不写在对话框里，用户会以为「我选的是 B，导出的就是 B」。
所以这里用一行字把口径钉死。

## 为什么敏感项不给开关

含凭据的配置项必须由人显式勾选才能出门，这是配置页既有的红线。整包导出没有
勾选界面，也就没有那道确认 —— 因此固定排除，并在清单里写明排除了几项。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import pack


@dataclass
class ExportOptions:
    """固定为整包。字段保留，方便调用方与测试按名字取值。"""

    scope: str = pack.SCOPE_ACCOUNT
    content: str = pack.CONTENT_FULL
    records: list = field(default_factory=list)
    config_items: list = field(default_factory=list)
    memory_workspaces: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.records and not self.config_items and not self.memory_workspaces


def _fmt_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


class ExportDialog(QDialog):
    """展示这个包里有什么，确认即导出。没有可勾选的东西。"""

    def __init__(
        self,
        records,
        config_items,
        memory_workspaces,
        alias: str,
        target_uid: str = "",
        excluded_sensitive: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._records = list(records or [])
        self._config = list(config_items or [])
        self._memory = list(memory_workspaces or [])
        self._alias = (alias or "workbuddy").strip() or "workbuddy"
        self._target_uid = target_uid
        self._excluded = int(excluded_sensitive or 0)
        self._options = ExportOptions(
            records=self._records,
            config_items=self._config,
            memory_workspaces=self._memory,
        )

        self.setWindowTitle("导出全部同步包")
        self.setMinimumWidth(640)
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        where = QLabel()
        where.setWordWrap(True)
        where.setStyleSheet(
            "font-size:12px;color:#444;background:#F4F5F7;border-radius:6px;"
            "padding:8px 10px;"
        )
        if self._target_uid:
            where.setText(
                "导出范围固定为<b>当前登录账号</b>；侧栏当前选中哪个账号都不影响这里。"
            )
        else:
            where.setText("未识别到当前登录账号，将导出库里能读到的全部会话。")
        layout.addWidget(where)

        body = QVBoxLayout()
        body.setSpacing(7)

        sess_bytes = sum(int(getattr(r, "jsonl_size", 0) or 0) for r in self._records)
        body.addWidget(
            self._row(
                "会话",
                f"{len(self._records)} 条 · 已删除与无正文的不含",
                _fmt_size(sess_bytes),
            )
        )

        cfg_bytes = sum(int(getattr(i, "size", 0) or 0) for i in self._config)
        cfg_desc = f"{len(self._config)} 项 · 设置、人格与身份文件、技能包"
        if self._excluded:
            cfg_desc += f"；<b>{self._excluded} 项含凭据的项固定不含</b>"
        body.addWidget(self._row("配置", cfg_desc, _fmt_size(cfg_bytes)))

        mem_files = sum(w.file_count for w in self._memory)
        mem_bytes = sum(w.total_bytes for w in self._memory)
        body.addWidget(
            self._row(
                "工作空间记忆",
                f"{len(self._memory)} 个工作空间 · {mem_files} 个 md（全部纳入）",
                _fmt_size(mem_bytes),
            )
        )
        layout.addLayout(body)

        warn = QLabel("包不加密。工作空间记忆含个人笔记与本机路径，这次会一起带出门。")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "font-size:11.5px;color:#B45309;background:#FDF3E4;"
            "border:1px solid #F0DCB8;border-radius:6px;padding:8px 10px;"
        )
        layout.addWidget(warn)

        estimate = sess_bytes // 5 + cfg_bytes + mem_bytes // 3
        foot = QLabel(
            f"文件名　{self.suggested_name}<br>"
            f"保存在下一步的系统保存对话框里选；预计约 {_fmt_size(estimate)}（压缩后）。"
        )
        foot.setWordWrap(True)
        foot.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        foot.setStyleSheet("font-size:11.5px;color:#5B6573;")
        layout.addWidget(foot)

        layout.addStretch(1)

        btns = QDialogButtonBox()
        self.btn_ok = btns.addButton("导出全部", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_ok.setObjectName("Primary")
        btns.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_ok.clicked.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _row(self, title: str, desc: str, size: str) -> QWidget:
        row = QFrame()
        row.setStyleSheet(
            "QFrame{background:#FFFFFF;border:1px solid #E3E7EC;border-radius:8px;}"
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(11, 9, 11, 9)
        h.setSpacing(10)

        tick = QLabel("✓")
        tick.setFixedSize(15, 15)
        tick.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tick.setStyleSheet(
            "background:#0E9F52;color:#FFFFFF;border-radius:3px;font-size:10px;"
        )
        h.addWidget(tick, 0, Qt.AlignmentFlag.AlignTop)

        name = QLabel(title)
        name.setStyleSheet("font-size:13px;font-weight:600;border:none;")
        name.setFixedWidth(88)
        h.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)

        text = QLabel(desc)
        text.setWordWrap(True)
        text.setStyleSheet("font-size:11.5px;color:#5B6573;border:none;")
        h.addWidget(text, 1)

        sz = QLabel(size)
        sz.setStyleSheet("font-size:11.5px;color:#9AA3AE;border:none;")
        sz.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        h.addWidget(sz, 0, Qt.AlignmentFlag.AlignTop)
        return row

    # ------------------------------------------------------------ 结果
    @property
    def options(self) -> ExportOptions:
        return self._options

    @property
    def suggested_name(self) -> str:
        return pack.suggest_filename(
            self._alias, pack.SCOPE_ACCOUNT, pack.CONTENT_FULL
        )
