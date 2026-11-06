"""导航图标 —— 四条 16px 线性图标，用 QPainter 现画。

## 为什么不用字符或字体图标

字体图标（如 Segoe MDL2）在打包环境里可能缺字形，缺了就变成豆腐块；
用 SVG 资源要额外进 spec 的 datas。这四条图形简单到不值得引入依赖，
现画的好处是：颜色随时可换（选中/未选中两态）、缩放清晰、零外部文件。

## 为什么颜色要两套

QSS 管不到 QPushButton 的 icon 颜色 —— 图标是位图，不会跟着 color 变。
所以选中态由 NavButton 自己换一张：浅绿底上必须是深绿描边，
否则白底灰图标在浅绿底上会糊成一团。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)

ICON_SIZE = 20
_CANVAS = 64          # 4x 画布，缩到 16px 显示，边缘不糊
_GAP_RIGHT = 16       # 画布右侧留白（4x），和文字拉开 4 逻辑像素
_GAP_LOGICAL = 4      # _GAP_RIGHT / 4 —— 补 QSS 管不到的 icon-text 间距
# 描边 1.2 逻辑像素。再粗一档（1.6）在 16px 下会把四宫格的 1.6px 间隙填满，
# 四个图形糊成一团墨块 —— 这是这组图标唯一难调的地方。
_STROKE = 1.2


def _pixmap(kind: str, color: str) -> QPixmap:
    pm = QPixmap(_CANVAS + _GAP_RIGHT, _CANVAS)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    pen = QPen(QColor(color))
    # ⚠️ 这里的宽度用「设计单位」，不能用画布像素：
    # 画笔宽度会被后面的 p.scale(k, k) 一起放大。写成 _CANVAS/16*_STROKE（=4.8）
    # 再乘 k=4 就成了 19px 描边，四条图形会被糊成一块实心墨。
    pen.setWidthF(_STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    k = _CANVAS / 16.0                # 以 16 为设计单位，便于按图纸读数
    p.scale(k, k)
    painter = _SHAPES.get(kind)
    if painter is not None:
        painter(p, pen)
    p.end()
    return pm


# ---------------------------------------------------------------- 图形
# 全部按 16×16 的设计单位取数，四条图形共用同一套边距（1.8 ~ 14.2），
# 这样四个图标在侧栏里左右对齐，不会一个胖一个瘦。
def _overview(p: QPainter, pen: QPen) -> None:
    """四宫格：概览＝把几类数字并排看。"""
    for x, y in ((1.8, 1.8), (8.9, 1.8), (1.8, 8.9), (8.9, 8.9)):
        p.drawRoundedRect(x, y, 5.3, 5.3, 1.2, 1.2)


def _sessions(p: QPainter, pen: QPen) -> None:
    """三行不齐的横线：一列会话（标题长的长、短的短）。"""
    for y, x2 in ((3.6, 14.2), (8.0, 14.2), (12.4, 11.0)):
        p.drawLine(1.8, y, x2, y)


def _config(p: QPainter, pen: QPen) -> None:
    """滑杆 + 实心滑块：配置＝可调的旋钮。

    滑块用实心小方块而不是空心圆：16px 下空心圆的内径只剩零点几个像素，
    渲染出来是一个灰点，看不出是「圆圈」还是「实心点」。
    """
    for y, cx in ((3.6, 10.4), (8.0, 5.2), (12.4, 8.8)):
        p.drawLine(1.8, y, 14.2, y)
        p.fillRect(QRectF(cx - 1.2, y - 1.2, 2.4, 2.4), QBrush(pen.color()))


def _backup(p: QPainter, pen: QPen) -> None:
    """带盖的盒子：备份＝把东西收进一个还能再取出的容器。"""
    p.drawRoundedRect(1.9, 4.6, 12.2, 9.4, 1.6, 1.6)
    p.drawLine(1.9, 8.1, 14.1, 8.1)
    p.drawLine(6.4, 10.6, 9.6, 10.6)


def _workspace(p: QPainter, pen: QPen) -> None:
    """文件夹：工作空间就是把若干会话收在同一处的容器。

    与「备份」的盒子刻意区分：盒子是平顶加一条横线，文件夹左边有标签凸起。
    """
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    path.moveTo(2.0, 13.4)
    path.lineTo(2.0, 3.7)
    path.lineTo(6.1, 3.7)
    path.lineTo(7.6, 5.6)
    path.lineTo(14.0, 5.6)
    path.lineTo(14.0, 13.4)
    path.closeSubpath()
    p.drawPath(path)


# ---------------------------------------------------------------- C 端整壳导航图标
# 参考图把六项导航换成「房子 / 气泡 / 滑块 / 立方体 / 证件 / 公文包」这组线性图形。
# 旧图形保留（表格列头等功能位置仍在用），导航改指向这里的新键。
def _home(p: QPainter, pen: QPen) -> None:
    from PySide6.QtGui import QPainterPath

    roof = QPainterPath()
    roof.moveTo(1.5, 7.6)
    roof.lineTo(8.0, 2.2)
    roof.lineTo(14.5, 7.6)
    p.drawPath(roof)
    p.drawRoundedRect(3.2, 7.2, 9.6, 6.8, 1.0, 1.0)
    p.drawLine(6.7, 14.0, 6.7, 10.9)
    p.drawLine(6.7, 10.9, 9.3, 10.9)
    p.drawLine(9.3, 10.9, 9.3, 14.0)


def _bubble(p: QPainter, pen: QPen) -> None:
    p.drawRoundedRect(1.8, 2.1, 12.4, 9.4, 3.2, 3.2)
    p.drawLine(4.8, 11.4, 3.4, 14.0)
    p.drawLine(3.4, 14.0, 7.2, 11.6)
    p.setBrush(QBrush(pen.color()))
    for cx in (5.4, 8.0, 10.6):
        p.drawEllipse(QRectF(cx - 0.75, 6.2, 1.5, 1.5))


def _sliders_square(p: QPainter, pen: QPen) -> None:
    p.drawRoundedRect(1.9, 1.9, 12.2, 12.2, 2.8, 2.8)
    p.drawLine(4.4, 6.0, 11.6, 6.0)
    p.fillRect(QRectF(7.6, 4.8, 2.0, 2.4), QBrush(pen.color()))
    p.drawLine(4.4, 10.0, 11.6, 10.0)
    p.fillRect(QRectF(5.4, 8.8, 2.0, 2.4), QBrush(pen.color()))


def _cube(p: QPainter, pen: QPen) -> None:
    p.drawLine(8.0, 1.6, 14.0, 5.0)
    p.drawLine(14.0, 5.0, 14.0, 11.0)
    p.drawLine(14.0, 11.0, 8.0, 14.4)
    p.drawLine(8.0, 14.4, 2.0, 11.0)
    p.drawLine(2.0, 11.0, 2.0, 5.0)
    p.drawLine(2.0, 5.0, 8.0, 1.6)
    p.drawLine(2.0, 5.0, 8.0, 8.2)
    p.drawLine(8.0, 8.2, 14.0, 5.0)
    p.drawLine(8.0, 8.2, 8.0, 14.4)


def _idcard(p: QPainter, pen: QPen) -> None:
    p.drawRoundedRect(1.9, 2.8, 12.2, 10.4, 2.2, 2.2)
    p.drawEllipse(QRectF(4.4, 5.2, 2.9, 2.9))
    p.drawLine(8.4, 6.1, 11.7, 6.1)
    p.drawLine(8.4, 9.0, 11.7, 9.0)
    p.drawLine(4.2, 10.7, 11.8, 10.7)


def _briefcase(p: QPainter, pen: QPen) -> None:
    p.drawRoundedRect(1.8, 4.8, 12.4, 9.2, 1.8, 1.8)
    p.drawLine(5.7, 4.8, 5.7, 3.0)
    p.drawLine(5.7, 3.0, 10.3, 3.0)
    p.drawLine(10.3, 3.0, 10.3, 4.8)
    p.drawLine(1.8, 8.4, 14.2, 8.4)
    p.setBrush(QBrush(pen.color()))
    p.drawEllipse(QRectF(6.3, 10.2, 1.4, 1.4))
    p.drawEllipse(QRectF(8.9, 10.2, 1.4, 1.4))


def _dot(p: QPainter, pen: QPen) -> None:
    """选中导航项用绿点取代线性图标 —— 参考图里就是这样。"""
    p.setBrush(QBrush(pen.color()))
    p.drawEllipse(QRectF(5.7, 5.7, 4.6, 4.6))


def _info(p: QPainter, pen: QPen) -> None:
    p.drawEllipse(QRectF(1.9, 1.9, 12.2, 12.2))
    p.setBrush(QBrush(pen.color()))
    p.drawEllipse(QRectF(7.2, 4.3, 1.6, 1.6))
    p.drawLine(8.0, 7.3, 8.0, 11.6)


def _minus_circle(p: QPainter, pen: QPen) -> None:
    p.drawEllipse(QRectF(1.9, 1.9, 12.2, 12.2))
    p.drawLine(5.2, 8.0, 10.8, 8.0)


# ---------------------------------------------------------------- 功能小图标
# 按钮内的小图形（刷新 / 人物 / 勾 / 加号 / 箭头）。同样现画，理由与导航图标一致。
def _refresh(p: QPainter, pen: QPen) -> None:
    """缺口圆环 + 箭头：重新扫描。"""
    p.drawArc(QRectF(2.0, 2.0, 12.0, 12.0), 40 * 16, 280 * 16)
    p.drawLine(10.6, 1.4, 13.6, 3.6)
    p.drawLine(10.6, 1.4, 8.9, 4.3)


def _person(p: QPainter, pen: QPen) -> None:
    """头 + 肩：账号胶囊里的人像。实心绘制，24px 下才看得见。"""
    p.setBrush(QBrush(pen.color()))
    p.drawEllipse(QRectF(5.9, 2.6, 4.2, 4.2))
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    path.moveTo(3.0, 13.4)
    path.cubicTo(3.0, 10.4, 5.4, 8.9, 8.0, 8.9)
    path.cubicTo(10.6, 8.9, 13.0, 10.4, 13.0, 13.4)
    path.closeSubpath()
    p.drawPath(path)


def _chevron(p: QPainter, pen: QPen) -> None:
    p.drawLine(6.2, 3.8, 10.2, 8.0)
    p.drawLine(10.2, 8.0, 6.2, 12.2)


def _chevron_down(p: QPainter, pen: QPen) -> None:
    p.drawLine(3.8, 6.2, 8.0, 10.2)
    p.drawLine(8.0, 10.2, 12.2, 6.2)


def _check(p: QPainter, pen: QPen) -> None:
    pen.setWidthF(2.0)
    p.drawLine(3.4, 8.6, 6.6, 11.6)
    p.drawLine(6.6, 11.6, 12.6, 4.6)


def _plus(p: QPainter, pen: QPen) -> None:
    p.drawLine(8.0, 3.4, 8.0, 12.6)
    p.drawLine(3.4, 8.0, 12.6, 8.0)


def _inbox_down(p: QPainter, pen: QPen) -> None:
    """向下的箭头 + 下方的托盘：导入。"""
    p.drawLine(8.0, 2.4, 8.0, 9.0)
    p.drawLine(5.2, 6.4, 8.0, 9.2)
    p.drawLine(10.8, 6.4, 8.0, 9.2)
    p.drawLine(2.6, 12.6, 13.4, 12.6)


def _outbox_down(p: QPainter, pen: QPen) -> None:
    """向下的箭头 + 上方缺失：导出到文件。"""
    p.drawLine(8.0, 2.4, 8.0, 9.6)
    p.drawLine(5.2, 6.6, 8.0, 9.4)
    p.drawLine(10.8, 6.6, 8.0, 9.4)
    p.drawLine(2.6, 12.8, 13.4, 12.8)


def _more(p: QPainter, pen: QPen) -> None:
    """三个点：低频入口。用画的而不是字符「⋯」—— 缺字形时会变成豆腐块。"""
    p.setBrush(QBrush(pen.color()))
    for cx in (3.4, 8.0, 12.6):
        p.drawEllipse(QRectF(cx - 0.9, 7.1, 1.8, 1.8))


def _search(p: QPainter, pen: QPen) -> None:
    p.drawEllipse(QRectF(2.6, 2.6, 8.2, 8.2))
    p.drawLine(9.9, 9.9, 13.4, 13.4)


_SHAPES = {
    "overview": _overview,
    "sessions": _sessions,
    "config": _config,
    "backup": _backup,
    "workspace": _workspace,
    "refresh": _refresh,
    "person": _person,
    "chevron": _chevron,
    "chevron_down": _chevron_down,
    "check": _check,
    "plus": _plus,
    "search": _search,
    "more": _more,
    "inbox_down": _inbox_down,
    "outbox_down": _outbox_down,
    # C 端整壳
    "home": _home,
    "bubble": _bubble,
    "sliders_square": _sliders_square,
    "cube": _cube,
    "idcard": _idcard,
    "briefcase": _briefcase,
    "dot": _dot,
    "info": _info,
    "minus_circle": _minus_circle,
}


def _glyph_pixmap(kind: str, color: str, px: int) -> QPixmap:
    """按目标像素直接画（不分 Gap），高分屏用 2x 位图。"""
    ratio = 2
    pm = QPixmap(px * ratio, px * ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.0, _STROKE * (px / 16.0)))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.scale(px * ratio / 16.0, px * ratio / 16.0)
    shape = _SHAPES.get(kind)
    if shape is not None:
        shape(p, pen)
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def glyph(kind: str, color: str, px: int = 16) -> QPixmap:
    """返回可直接贴到 QLabel / 按钮上的位图。"""
    return _glyph_pixmap(kind, color, px)


def glyph_icon(kind: str, color: str, px: int = 16) -> QIcon:
    return QIcon(_glyph_pixmap(kind, color, px))


def nav_icon(kind: str, color: str) -> QIcon:
    """返回单色导航图标。QIcon 内部按 16px 显示，2x 位图保证高分屏不糊。"""
    icon = QIcon()
    icon.addPixmap(_pixmap(kind, color))
    return icon


def icon_size() -> QSize:
    return QSize(ICON_SIZE + _GAP_LOGICAL, ICON_SIZE)


# --------------------------------------------------------------- 应用图标
# 应用图标是位图资源（wbsync/assets/appicon.*），由 tools/make_app_icon.py 生成。
# 查找顺序覆盖三种部署形态：
#   1. PyInstaller onefile —— 资源解在 sys._MEIPASS 下
#   2. 源码运行 —— 就在包目录里的 assets/
#   3. 绿色版（exe 与 assets 同级）
# 三处都找不到时用 QPainter 现画一个同款兜底，绝不返回空图标：
# 空图标在任务栏和标题栏上表现为一块白板，比画得糙更难排查。
def _asset_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "wbsync" / "assets")
        dirs.append(Path(meipass))
    dirs.append(here.parent / "assets")          # wbsync/assets
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent / "assets")
        dirs.append(Path(sys.executable).parent)
    return dirs


def _asset_file(name: str) -> Path | None:
    for d in _asset_dirs():
        p = d / name
        if p.is_file():
            return p
    return None


def _fallback_app_pixmap(px: int = 256) -> QPixmap:
    """资源缺失时的兜底：绿底圆角方 + 白环两段。"""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    grad = QLinearGradient(0, 0, px, px)
    grad.setColorAt(0.0, QColor("#3ACB80"))
    grad.setColorAt(1.0, QColor("#0A8241"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    r = px * 0.235
    p.drawRoundedRect(QRectF(0, 0, px, px), r, r)

    pen = QPen(QColor("#FFFFFF"))
    pen.setWidthF(px * 0.086)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    ring = px * 0.252
    c = px / 2.0
    box = QRectF(c - ring, c - ring, ring * 2, ring * 2)
    p.drawArc(box, 17 * 16, 146 * 16)
    p.drawArc(box, 197 * 16, 146 * 16)
    p.end()
    return pm


def app_icon() -> QIcon:
    """窗口 / 任务栏 / 关于框共用的应用图标。"""
    ico = _asset_file("appicon.ico")
    if ico is not None:
        icon = QIcon(str(ico))
        if not icon.isNull():
            return icon
    png = _asset_file("appicon.png")
    if png is not None:
        icon = QIcon(str(png))
        if not icon.isNull():
            return icon
    return QIcon(_fallback_app_pixmap())


def brand_pixmap(px: int = 36) -> QPixmap:
    """侧栏品牌位用的位图（按目标逻辑尺寸取，高分屏自动带 2x）。"""
    png = _asset_file("appicon.png")
    if png is not None:
        src = QPixmap(str(png))
        if not src.isNull():
            ratio = 2 if px <= 128 else 1
            scaled = src.scaled(
                px * ratio, px * ratio,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(ratio)
            return scaled
    return _fallback_app_pixmap(px)

