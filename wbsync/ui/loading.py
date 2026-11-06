"""加载遮罩 —— 转圈 + 文案 + 读秒。

## 为什么不用底部进度条

原先是内容区底部一条 3px 的横条。它有两个问题：
  1. 位置在视觉边缘，用户盯着中间的内容区根本注意不到
  2. 不确定进度（setRange(0,0)）时只是一段来回滚的色块，读起来像"卡住了"

改成居中的环形转圈后，"在干活"这件事一眼可见。

## 为什么一定要读秒

盘点 68 MB 正文约 5 秒、关闭客户端可能十几秒。没有数字时，超过 3 秒用户就会怀疑程序挂了；
有数字在跳，同样的等待感受完全不同。所以这里把「已用时间」做成常驻信息，
有总数时再叠加「12 / 74」的计数。

## 自绘而不是 GIF

GIF 要额外资源文件，打包后容易漏；`QMovie` 还会在缺失时静默不显示。
自绘 30 行代码，零依赖，颜色跟着主题走。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme

SPINNER_SIZE = 30
FRAME_MS = 60          # 每帧 60ms ≈ 16fps，足够顺滑又不费 CPU
DEGREE_PER_FRAME = 30
TEXT_UPDATE_MS = 100   # 读秒刷新间隔


class Spinner(QWidget):
    """环形转圈。自绘，无资源依赖。"""

    def __init__(self, parent=None, size: int = SPINNER_SIZE) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 90
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle - DEGREE_PER_FRAME) % 360
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5)

        track = QPen(QColor(theme.BORDER), 3)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawEllipse(rect)

        arc = QPen(QColor(theme.PRIMARY), 3)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc)
        # drawArc 的角度单位是 1/16 度
        painter.drawArc(rect, self._angle * 16, 100 * 16)
        painter.end()


class LoadingOverlay(QWidget):
    """覆盖在内容区上的加载遮罩。

    用法::

        self.overlay = LoadingOverlay(self.content_host)
        self.overlay.start("正在读取本机会话…", "68 MB 正文约需 5 秒")
        self.overlay.set_counter(12, 74)
        self.overlay.stop()
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._started_at = 0.0
        self._title = ""
        self._hint = ""
        self._done = 0
        self._total = 0
        self._note = ""

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._build()
        self._text_timer = QTimer(self)
        self._text_timer.setInterval(TEXT_UPDATE_MS)
        self._text_timer.timeout.connect(self._refresh_text)
        self.hide()

        # 跟随父控件尺寸
        parent.installEventFilter(self)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addStretch(1)

        self.spinner = Spinner(self)
        layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)

        self.lbl_title = QLabel()
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(self.lbl_title.font())
        f.setPointSizeF(10)
        self.lbl_title.setFont(f)
        self.lbl_title.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        layout.addWidget(self.lbl_title)

        self.lbl_sub = QLabel()
        self.lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f2 = QFont(self.lbl_sub.font())
        f2.setPointSizeF(8.5)
        self.lbl_sub.setFont(f2)
        self.lbl_sub.setStyleSheet(f"color: {theme.TEXT_TERTIARY};")
        layout.addWidget(self.lbl_sub)

        layout.addStretch(1)

    # ---------------------------------------------------------- 生命周期
    def start(self, title: str, hint: str = "") -> None:
        self._title = title
        self._hint = hint
        self._done = 0
        self._total = 0
        self._note = ""
        self._started_at = time.monotonic()
        self.lbl_title.setText(title)
        self._refresh_text()
        self.spinner.start()
        self._text_timer.start()
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()

    def stop(self) -> None:
        self._text_timer.stop()
        self.spinner.stop()
        self.hide()

    def set_counter(self, done: int, total: int) -> None:
        """有明确总数时显示「12 / 74」。"""
        self._done = done
        self._total = total
        self._refresh_text()

    def set_note(self, note: str) -> None:
        """替换副文案的前半段，例如「正在等待 WorkBuddy 退出」。"""
        self._note = note
        self._refresh_text()

    def set_title(self, title: str) -> None:
        self._title = title
        self.lbl_title.setText(title)

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self._started_at, 0.0) if self._started_at else 0.0

    # ---------------------------------------------------------- 内部
    def _refresh_text(self) -> None:
        parts: list[str] = []
        if self._note:
            # 等待退出这类场景只要读秒，不要计数
            parts.append(f"{self._note}… {self.elapsed:.0f} 秒")
        else:
            if self._total:
                parts.append(f"{self._done} / {self._total}")
            if self._started_at:
                parts.append(f"已用 {self.elapsed:.1f} 秒")
            if self._hint and not self._total:
                parts.append(self._hint)
        self.lbl_sub.setText(" · ".join(parts))

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
        return False

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # 半透明白底：盖住旧内容又不至于完全断开视觉联系
        painter.fillRect(self.rect(), QColor(255, 255, 255, 214))
        painter.end()
