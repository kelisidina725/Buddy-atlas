"""主窗尺寸的屏幕自适应 —— 让窗口在任何分辨率与缩放下都完整可见。

## 为什么必须算，不能写死

`resize(1114, 660)` 的单位是**逻辑像素**，不是物理像素。125% 缩放的 1920×1080
屏幕，Qt 眼里的可用区只有 1536×816：只要客户区逼近 780，加上标题栏就贴死屏幕。
早期版本写死 `resize(1440, 932)` + `setMinimumSize(1160, 792)`：932 比 816 还高，
792 加标题栏约 823 同样超过 816。于是窗口既缩不下去、也显示不全。1366×768 的
笔记本更糟，可用高度 728。

四条规则：

1. **设计稿只作上限**：装得下就用 1114×660，装不下就按可用区收，绝不溢出；
2. **最小尺寸跟着可用区走**：可用区比 620 矮时，最小值必须降到可用区以内 ——
   否则最小值本身就成了「显示不完整」的成因；
3. **记住上次尺寸**：用户已经手动调小过一次，下次启动不该再撑回设计稿；
4. **默认尺寸改了，旧记录失效**：几何旁边记一份「它是按哪个默认尺寸存的」，
   默认值变了就按新值重开一次。否则把 1440 改成 1114，用户本机存着的
   1440×778 会一直把窗口撑回 1440，改了等于没改。

`fit()` 是纯函数（只吃数字、只吐数字），因此不装 Qt 也能测；Qt 相关调用一律
延迟 import，避免非 GUI 环境（自检、单元测试）被拖下水。
"""
from __future__ import annotations

# 设计稿尺寸：1114×660（用户 2026-11-04 按红框标注指定，客户区口径）
# 上一版 1252×778 仍被判定「铺满上下框」：1920×1080@125% 的可用区是 1536×816，
# 778 客户区加标题栏 30 只剩 8px 余量，观感上就是贴死屏幕。1114×660 四周各留
# 六七十像素画布，同时仍装得下账号页两张卡与会话页整条工具栏。
DESIGN_SIZE = (1114, 660)
# 设计稿的最小可用窗口：再窄就让表格横向滚动，不缩字号也不缩圆角。
# 必须 ≤ DESIGN_SIZE —— 否则 fit() 会把窗口撑回最小值，默认尺寸等于没改。
DESIGN_MIN = (1040, 620)
# 硬性下限：低于这个尺寸三栏布局已经没有意义，但也不能比屏幕还大
HARD_MIN = (880, 520)

# 与可用区边界留出的余量。可用区已扣掉任务栏，这里只挡窗口边框与阴影贴边。
GAP_W = 16
GAP_H = 8

# 判定「保存的位置还在屏幕里」的比例：窗口至少要有这么多可见，否则就居中重放
_VISIBLE_RATIO = 0.6


def fit(
    desired: tuple[int, int] = DESIGN_SIZE,
    minimum: tuple[int, int] = DESIGN_MIN,
    available: tuple[int, int] = DESIGN_SIZE,
    hard_min: tuple[int, int] = HARD_MIN,
) -> tuple[int, int, int, int]:
    """把「想要的尺寸」夹进「屏幕装得下的尺寸」。

    返回 (宽, 高, 最小宽, 最小高)。

    最小尺寸同样要夹：屏幕比 `minimum` 还小时，最小尺寸必须退到可用区以内，
    否则 `setMinimumSize` 会把窗口强行撑出屏幕 —— 这正是「显示不完整」的来源。
    下限 `hard_min` 只在屏幕大到装得下时才生效，它保护的是布局不塌，
    不是跟屏幕较劲。
    """
    aw = max(int(available[0] or 0), 320)
    ah = max(int(available[1] or 0), 240)
    max_w = max(aw - GAP_W, 320)
    max_h = max(ah - GAP_H, 240)

    want_min_w = max(int(minimum[0]), int(hard_min[0]))
    want_min_h = max(int(minimum[1]), int(hard_min[1]))
    # 屏幕装不下设计稿的最小值时，以屏幕为准（少留一点余量，别把自己逼到没位置）
    min_w = min(want_min_w, max_w)
    min_h = min(want_min_h, max_h)

    w = min(max(int(desired[0]), min_w), max_w)
    h = min(max(int(desired[1]), min_h), max_h)
    return w, h, min_w, min_h


def minimum_for(
    desired: tuple[int, int],
    minimum: tuple[int, int] = DESIGN_MIN,
) -> tuple[int, int]:
    """用户上次给过比设计稿更小的尺寸时，最小值跟着降到那个尺寸。

    否则「记住尺寸」是假的：把 700 高的窗口还原回来，`setMinimumSize(…, 792)`
    立刻又把它撑回 792 —— 用户调小过一次的动作等于白做。
    """
    return (min(int(minimum[0]), int(desired[0])), min(int(minimum[1]), int(desired[1])))


def available_size(screen=None) -> tuple[tuple[int, int], tuple[int, int]]:
    """当前屏幕的可用区（已扣掉任务栏），返回 ((宽, 高), (x, y))。

    拿不到屏幕信息时退回 1280×720 —— 这个值不会把窗口撑爆，
    而猜一个大屏反而会重演「显示不完整」。
    """
    try:
        from PySide6.QtGui import QGuiApplication

        scr = screen
        if scr is None:
            app = QGuiApplication.instance()
            scr = app.primaryScreen() if app is not None else None
        geo = scr.availableGeometry() if scr is not None else None
        if geo is not None and geo.width() > 0 and geo.height() > 0:
            return (int(geo.width()), int(geo.height())), (int(geo.x()), int(geo.y()))
    except Exception:  # noqa: BLE001 - 尺寸探测失败不能拦住程序启动
        pass
    return (1280, 720), (0, 0)


def _visible_enough(pos: tuple[int, int], size: tuple[int, int], area: tuple[int, int, int, int]) -> bool:
    """窗口落在屏幕里的部分够不够多 —— 拔掉外接显示器后位置会失效。"""
    ax, ay, aw, ah = area
    left = max(pos[0], ax)
    top = max(pos[1], ay)
    right = min(pos[0] + size[0], ax + aw)
    bottom = min(pos[1] + size[1], ay + ah)
    if right <= left or bottom <= top:
        return False
    shown = (right - left) * (bottom - top)
    total = max(size[0] * size[1], 1)
    return shown / total >= _VISIBLE_RATIO


def apply(
    win,
    desired: tuple[int, int] = DESIGN_SIZE,
    minimum: tuple[int, int] = DESIGN_MIN,
    center: bool = True,
) -> tuple[int, int]:
    """按当前屏幕设定窗口尺寸与最小值，返回最终 (宽, 高)。

    注意这只设**客户区**尺寸，标题栏与边框不在其中；真正贴边与否由
    `enforce()` 在 show 之后复核（那时 `frameGeometry()` 才有值）。
    """
    (aw, ah), (ax, ay) = available_size(win.screen() if win is not None else None)
    w, h, min_w, min_h = fit(desired, minimum, (aw, ah))
    win.setMinimumSize(min_w, min_h)
    win.resize(w, h)
    if center:
        win.move(ax + max((aw - w) // 2, 0), ay + max((ah - h) // 2, 0))
    return w, h


def enforce(win) -> tuple[int, int]:
    """show 之后按**真实窗口边框**再夹一次，返回最终客户区 (宽, 高)。

    为什么需要第二次：

    - `resize()` 设的是客户区，标题栏（约 31 逻辑像素）在 `frameGeometry()` 里；
    - 于是「客户区 808 + 标题栏 31」在 816 高的可用区里照样溢出，底部被切掉一条；
    - 边框尺寸在窗口创建前拿不到，只能等第一次 show 之后量。

    实测：1920×1080@125%，可用区 1536×816，客户区 808 时窗口实际 839 —— 差 23px。
    """
    (aw, ah), (ax, ay) = available_size(win.screen() if win is not None else None)
    frame = win.frameGeometry()
    geo = win.geometry()
    extra_w = max(frame.width() - geo.width(), 0)
    extra_h = max(frame.height() - geo.height(), 0)

    max_cw = max(aw - GAP_W - extra_w, 320)
    max_ch = max(ah - GAP_H - extra_h, 240)

    cw, ch = win.width(), win.height()
    new_cw, new_ch = min(cw, max_cw), min(ch, max_ch)
    if (new_cw, new_ch) != (cw, ch):
        # 先降最小尺寸，再缩：否则 setMinimumSize 会把 resize 顶回去
        win.setMinimumSize(
            min(win.minimumWidth(), new_cw), min(win.minimumHeight(), new_ch)
        )
        win.resize(new_cw, new_ch)

    frame = win.frameGeometry()
    if (
        frame.left() < ax
        or frame.top() < ay
        or frame.right() > ax + aw
        or frame.bottom() > ay + ah
    ):
        win.move(
            ax + max((aw - win.width() - extra_w) // 2, 0),
            ay + max((ah - win.height() - extra_h) // 2, 0),
        )
    return win.width(), win.height()


def geometry_matches_design(saved_design, desired: tuple[int, int] = DESIGN_SIZE) -> bool:
    """这份几何是不是按当前默认尺寸存的。

    老版本偏好文件没有 design 字段（返回 None），一律当作「过期」：这样改动
    `DESIGN_SIZE` 之后，第一次启动就会按新尺寸重开，而不是被旧记录钉死。
    纯函数，便于不装 Qt 也能测。
    """
    if saved_design is None:
        return False
    try:
        return tuple(int(v) for v in saved_design) == tuple(int(v) for v in desired)
    except (TypeError, ValueError):
        return False


def restore(win, desired: tuple[int, int] = DESIGN_SIZE, minimum: tuple[int, int] = DESIGN_MIN) -> tuple[int, int]:
    """启动时还原上次的尺寸/位置；没有记录或记录已失效则按设计稿居中。"""
    from .. import uistate

    saved = uistate.load_window_geometry()
    (aw, ah), (ax, ay) = available_size(win.screen() if win is not None else None)

    # 默认尺寸变过 → 旧几何（往往是更宽的 1440）不再代表用户主动调过，
    # 按新的设计稿重开一次；之后用户自己调整过的尺寸照常记住。
    if saved is None or not geometry_matches_design(
        uistate.load_window_geometry_design(), desired
    ):
        return apply(win, desired, minimum, center=True)

    x, y, sw, sh = saved
    w, h, min_w, min_h = fit((sw, sh), minimum_for((sw, sh), minimum), (aw, ah))
    win.setMinimumSize(min_w, min_h)
    win.resize(w, h)
    if _visible_enough((x, y), (w, h), (ax, ay, aw, ah)):
        win.move(x, y)
    else:
        win.move(ax + max((aw - w) // 2, 0), ay + max((ah - h) // 2, 0))
    return w, h


def save(win) -> None:
    """关窗时记下几何。失败静默 —— 偏好存不下来不该拦住退出。

    存的是「边框左上角 + 客户区宽高」：`move()` 收的是边框坐标、`resize()` 收的是
    客户区尺寸，两者混用会让窗口每次启动往下漂一个标题栏的高度。
    """
    from .. import uistate

    try:
        frame = win.frameGeometry()
        if frame.isValid() and frame.width() > 0:
            x, y = int(frame.x()), int(frame.y())
        else:  # 没显示过就没有边框，退回客户区坐标
            x, y = int(win.x()), int(win.y())
        rect = (x, y, int(win.width()), int(win.height()))
    except Exception:  # noqa: BLE001
        return
    uistate.save_window_geometry(rect, design=DESIGN_SIZE)
