"""主窗口 —— 三栏控制台：左侧导航、中间列表、右侧详情。

## 为什么是「列表 + 详情」而不是「列表 + 弹窗」

要处理一条会话时，人往往要连着看好几条：看完详情还要点下一条。
弹窗会盖住列表，来回开关两三次就没人愿意再看详情了 —— 于是只能凭标题猜，
而标题恰恰是最不可靠的信息（同名会话在实测数据里遍地都是）。

## 为什么四个页面挂在左侧导航上

会话、配置、备份是三条不同的「范围」：动作的对象不一样，能做的事也不一样。
页签暗示它们共享同一组操作，导航暗示各管各的 —— 后者才符合实际。
「概览」放在最前面，是因为「这台机器上到底有多少东西」是每次打开都想先确认的事。

## 破坏性动作跟着「选中对象」走

物理清除只活在右侧详情面板里：没选中会话，面板就是空的，按钮根本不存在。
旧版把它放在页头的通用「更多」菜单里，没选中时也能点，点下去才提示
「请先选中一条」—— 一个点了才知道是空壳的入口，迟早会被当成「清除全部」。

## 范围型动作跟着「页面」走

导出会话包在会话页（导出同步包的箭头下拉），导出配置包在配置页（导出按钮的下拉）。
同一个动作放在两个页面里，用户就得先想「我现在在哪一页」，再想「这按钮导的是哪个范围」。

## 保留表格视图

列表胜在扫视快，但比对「行数 / 体积 / 云端映射 / 工作空间」这类细节时，
表格的列对齐才是对的工具。两个视图共用同一份筛选结果，随时切换。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from .. import accounts as accounts_mod
from .. import bundle as bundle_mod
from .. import (
    closeflow,
    config_items,
    config_sync,
    guard,
    importer,
    inventory,
    memory_pack,
    rehome,
    winproc,
)
from .. import pack as pack_mod
from .. import purge as purge_mod
from .. import restore as restore_mod
from .. import switch as switch_mod
from .. import cwd_map, uistate
from .accounts_page import AccountsPage
from .backup_page import BackupPage
from .config_dialog import ConfigPage
from .detail_panel import DetailPanel
from .export_dialog import ExportDialog
from .header_bar import PageHeader
from .loading import LoadingOverlay
from .overview_page import OverviewPage
from .page_base import HeadedPage
from .merge_dialog import MergeDialog
from .purge_dialog import PurgeDialog
from .switch_dialog import SwitchAccountDialog
from . import icons
from .session_cards import CardGrid, ElidedLabel, fmt_size, fmt_when_exact, space_label
from .workspace_page import WorkspacePage
from .flow_layout import FlowBox, ResponsiveBar
from .sync_plan_dialog import SyncPlanDialog
from . import winsize
from .theme import (
    BUTTON_HEIGHT,
    CONTENT_PADDING,
    PRIMARY,
    PRIMARY_TEXT,
    SIDE_DOT,
    SIDE_TEXT,
    SIDEBAR_WIDTH,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
)

# 产品名。本工具是 WorkBuddy 客户端的**配套同步工具**，与客户端同名会让人
# 分不清窗口里是哪一个，所以标题、侧栏品牌位统一走这里。
APP_NAME = "Buddy-atlas"

COLUMNS = ["标题", "账号", "行数", "大小", "云端映射", "状态", "工作空间", "更新时间"]
COL_TITLE, COL_ACCOUNT, COL_LINES, COL_SIZE, COL_CLOUD, COL_STATUS, COL_CWD, COL_TIME = range(8)

ALL_ACCOUNTS = "__all__"

SCOPE_VISIBLE = "visible"
SCOPE_ALL = "all"
SCOPE_NO_BODY = "no-body"
SCOPE_UNMAPPED = "unmapped"
SCOPE_DELETED = "deleted"
SCOPE_HIDDEN = "hidden"

VIEW_CARD = 0
VIEW_TABLE = 1

# 侧栏宽度来自 theme：设计稿的固定项，抽屉/顶栏/内容内边距全都按它对齐。
# 保留这个名字是因为外部（冒烟测试、旧的引用）按 main_window.SIDEBAR_WIDTH 取。

# 页面栈索引（**不要改这几个常量的数值**：_go / pages.setCurrentIndex /
# 冒烟测试都按它取页）。_NAV_ITEMS 只决定侧栏里六个按钮的**显示顺序**。
NAV_OVERVIEW, NAV_SESSIONS, NAV_CONFIG, NAV_WORKSPACE, NAV_BACKUP, NAV_ACCOUNTS = range(6)

# 2026-11-04 参考图要求：账号紧随概览，其余五项保持原相对顺序。
# 只调显示顺序、不动索引，页面栈与所有按 NAV_* 常量跳转的入口都不用改。
_NAV_ITEMS = (
    (NAV_OVERVIEW, "概览"),
    (NAV_ACCOUNTS, "账号"),
    (NAV_SESSIONS, "会话"),
    (NAV_CONFIG, "配置"),
    (NAV_WORKSPACE, "工作空间"),
    (NAV_BACKUP, "备份与包"),
)

# 导航项 → icons.nav_icon 的图形键（C 端整壳那组线性图标）。
_NAV_ICONS = {
    NAV_OVERVIEW: "home",
    NAV_ACCOUNTS: "idcard",
    NAV_SESSIONS: "bubble",
    NAV_CONFIG: "sliders_square",
    NAV_WORKSPACE: "cube",
    NAV_BACKUP: "briefcase",
}

def build_brand() -> tuple[QHBoxLayout, QLabel]:
    """侧栏品牌区：图标 + 产品名 + 版本号。

    返回 `(布局, 版本号 label)`。拆成函数是为了让「侧栏显示版本号」这件事
    能在不构造主窗口（会真盘本机数据库）的前提下被测试钉住。
    版本号取自 `wbsync.__version__`，与窗口标题、exe 文件属性同一个源。
    """
    brand = QHBoxLayout()
    brand.setSpacing(10)
    logo = QLabel()
    logo.setFixedSize(36, 36)
    logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
    logo.setPixmap(icons.brand_pixmap(36))
    logo.setToolTip(f"{APP_NAME} · 会话与配置同步")
    brand.addWidget(logo)
    # 产品名与版本号竖排：版本号单独一行、字号小一档，深底上不抢标题
    title = QVBoxLayout()
    title.setContentsMargins(0, 0, 0, 0)
    title.setSpacing(1)
    name = QLabel(APP_NAME)
    name.setObjectName("BrandName")
    title.addWidget(name)
    version = QLabel(f"v{__version__}")
    version.setObjectName("BrandSub")
    version.setToolTip(f"当前版本 v{__version__}")
    title.addWidget(version)
    brand.addLayout(title)
    brand.addStretch(1)
    return brand, version


_SCOPE_LABELS = {
    SCOPE_VISIBLE: "本地可见",
    SCOPE_ALL: "全部",
    SCOPE_NO_BODY: "缺正文",
    SCOPE_UNMAPPED: "未注册云端",
    SCOPE_DELETED: "已删除",
    SCOPE_HIDDEN: "未在侧边栏",
}

# 设计稿的顺序：全部 → 本地可见 → 已删除 → 缺正文 → 未在侧边栏。
# 旧版把「本地可见」放第一位、把「全部会话」排在其后，读起来像两个同义项，
# 而「全部」只有排在首位才是它字面的意思（含已删除的总数）。
#
# 走查补一档「未在侧边栏」：有正文、已注册云端，但客户端侧边栏里看不到的那批。
# 少了它，页签数字永远比「全部」少一截 —— 用户会当成工具漏数。
#
# ★「未注册云端」不再占一档（v1.12.1）。它原来的位置在「可见」之前，于是
#   「可见但没注册云端」的会话被抢走，页签写「本地可见 3」而副标题写「可见 13」，
#   同一句话两个数字。现在它降级成叠加在档位上的诊断过滤（见「⋯」菜单），
#   页签只剩「看得到 / 死了 / 缺件」三种互斥状态，相加恒等于「全部」。
_CHIP_ORDER = (
    SCOPE_ALL,
    SCOPE_VISIBLE,
    SCOPE_DELETED,
    SCOPE_NO_BODY,
    SCOPE_HIDDEN,
)

# 永远显示的「主路径」档位：数量为 0 也是有效信息。
# 其余是诊断档，0 条时不显示 —— 一个点了什么都没有的页签，只会挤掉搜索框。
_ALWAYS_SHOWN_SCOPES = (SCOPE_ALL, SCOPE_VISIBLE, SCOPE_DELETED)

_SCOPE_TIPS = {
    SCOPE_ALL: "库里的全部会话，含客户端已删除的那些",
    SCOPE_VISIBLE: "有正文、客户端侧边栏里看得到的（含未注册云端的）",
    SCOPE_DELETED: "在客户端里被删除的（可恢复，也可彻底清除）",
    SCOPE_NO_BODY: "有元数据但找不到正文文件（三件套缺件）",
    SCOPE_UNMAPPED: "有正文但云端映射缺失，另一台机器看不到",
    SCOPE_HIDDEN: "有正文、已注册云端，但客户端侧边栏里不显示",
}


def _scope_bucket(record) -> str:
    """一条会话属于哪一档 —— **互斥，且并集正好是「全部」**。

    页签上的数字必须能对上总数：用户会拿「21 + 54」去减「94」，差 20 就会怀疑
    工具漏数了。原先每档各按自己的判据筛，中间那批「有正文、已注册云端、
    但客户端侧边栏里没有」谁也不管，于是永远差一截。

    判定顺序即优先级：已删除最先（死的就是死的），其次缺正文（有壳没内容），
    再看客户端里是否看得到。**云端映射不参与分档**：它是「能不能跨机看到」的
    诊断属性，拿它切分档位会让「本地可见」只剩「完全正常」的那一小撮，
    与副标题的「可见 N」对不上（v1.12.1 修）。
    """
    if record.is_deleted:
        return SCOPE_DELETED
    if not record.has_body:
        return SCOPE_NO_BODY
    if record.visible_by_default:
        return SCOPE_VISIBLE
    return SCOPE_HIDDEN


def count_visible_for_uid(uid: str, *, base=None) -> int | None:
    """按 uid 单独只读盘一次，数出这个账号真实的「本地可见」条数。

    「查看」要盘的是被查看的那个账号，不是手里已有的 `MainWindow._inv`：
    后者被 `_scan_uid` 限死在登录账号（`inventory.build(uid=…)` 会把 `records`
    过滤掉其它账号），拿它数别的账号必然得到 0 —— 那是「没盘过」，
    不是「没有会话」，写成「0 条」比留白更误导。

    这里临时取一份只读快照，**不赋给 `self._inv`、不改 `_scan_uid`、不改导出
    口径**，所以「查看」仍然是无损操作。`with_fingerprint=False` 跳过正文 hash
    （分档只需要 `has_body` 与侧边栏状态），`use_cache=False` 不写指纹缓存；
    实测单账号约 0.1s，放 UI 线程不会卡。

    盘点失败返回 None，让卡片继续显示「待盘点」；绝不用 0 冒充。
    """
    if not uid or uid == ALL_ACCOUNTS:
        return None
    try:
        inv = inventory.build(
            base=base,
            with_fingerprint=False,
            use_cache=False,
            uid=uid,
        )
    except Exception:  # noqa: BLE001 - 只读盘点失败不该打断「查看」
        return None
    # build(uid=…) 已按 `user_id == uid 或 uid in sidebar_uids` 过滤；
    # 这里再按统一分档口径数一遍（与副标题 / 下拉的「可见 N」同源）。
    return sum(1 for r in inv.records if _scope_bucket(r) == SCOPE_VISIBLE)


def _needs_cloud(record) -> bool:
    """活着、有正文、却没有云端映射 —— 换台机器就看不到它。

    已删除与缺正文的会话谈不上跨机可见，把它们算进来只会让这个提示永远
    大于 0，提示也就没人看了。
    """
    return not record.is_deleted and record.has_body and not record.has_cloud_mapping


def _unmapped_count(rows) -> int:
    return sum(1 for r in rows if _needs_cloud(r))


def _remember_memory_map(acts) -> None:
    """把记忆节用过的「源工作空间 → 本机项目目录」写进路径映射记忆。

    记忆页每行都能改落点（`memory_path_map`），但那份映射以前只活在这一轮
    导入里：写完就没了，工作空间页也因此永远是空的 —— 用户看着「还没有
    路径映射」，会以为自己刚设的东西丢了。记进 `cwd_map` 之后，这些落点
    既能在工作空间页反显，也能在下次导入时直接带出。
    """
    seen: set[str] = set()
    for a in acts or []:
        src = rehome.normalize(getattr(a.workspace, "path", "") or "")
        dst = rehome.normalize(getattr(a, "root", "") or "")
        # 落点没变（源路径在本机本来就存在）的不记：那不是映射，是原样
        if not src or not dst or src == dst or src in seen:
            continue
        seen.add(src)
        cwd_map.remember(src, dst)


class InventoryWorker(QThread):
    """后台盘点。96MB 正文算指纹要 7 秒，放主线程会卡死界面。"""

    progressed = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        with_fingerprint: bool,
        uid: str = "",
        use_cache: bool = True,
        clear_cache: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._with_fingerprint = with_fingerprint
        # 空串 = 全部账号；非空 = 只盘这个账号（口径由 inventory.build 决定）
        self._uid = uid
        self._use_cache = use_cache
        self._clear_cache = clear_cache

    def run(self) -> None:
        try:
            if self._clear_cache:
                from .. import fpcache

                fpcache.open_cache().clear()
            inv = inventory.build(
                with_fingerprint=self._with_fingerprint,
                uid=self._uid,
                use_cache=self._use_cache,
                progress=lambda done, total: self.progressed.emit(done, total),
            )
            self.finished.emit(inv)
        except Exception as exc:  # noqa: BLE001 - 兜底，线程内异常不能静默
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SyncExportWorker(QThread):
    """导出一个或两节内容的同步包。"""

    progressed = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        out_path: str,
        records,
        config_items_list,
        source_label: str,
        source_uid: str,
        parent=None,
        memory_workspaces=None,
    ) -> None:
        super().__init__(parent)
        self._out = out_path
        self._records = records
        self._items = config_items_list
        self._label = source_label
        self._uid = source_uid
        self._memory = list(memory_workspaces or [])

    def run(self) -> None:
        try:
            pack_mod.export_package(
                self._out,
                self._records,
                self._items,
                source_label=self._label,
                source_uid=self._uid,
                progress=lambda d, t: self.progressed.emit(d, t),
                memory_workspaces=self._memory,
            )
            self.finished.emit(self._out)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SyncPlanWorker(QThread):
    """读同步包并同时产出三节的动作表。"""

    finished = Signal(object, object, object, object)  # pkg, sessions, config, memory
    failed = Signal(str)

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        try:
            pkg = pack_mod.read_package(self._path)
            s_acts: list = []
            if pkg.has_sessions:
                s_acts = importer.plan_import(pkg.sessions)

            c_acts: list = []
            if pkg.has_config:
                local = config_items.discover()
                baseline = config_sync.Baseline.load()
                c_acts = config_sync.plan(
                    pkg.config.items, local, baseline, pkg.config.members
                )

            m_acts: list = []
            if pkg.has_memory:
                # 复用 cwd_map 的记忆：源路径 -> 本机目标目录
                path_map = {
                    src: str(entry.get("target") or "")
                    for src, entry in cwd_map.load_map().items()
                    if entry.get("target")
                }
                m_acts = memory_pack.plan_import(pkg.memory, path_map)
            self.finished.emit(pkg, s_acts, c_acts, m_acts)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ConfigImportWorker(QThread):
    """写配置节。会话写完后再跑，失败不影响已完成的会话导入。"""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, actions, bundle, dry_run: bool, choices, parent=None) -> None:
        super().__init__(parent)
        self._actions = actions
        self._bundle = bundle
        self._dry_run = dry_run
        self._choices = choices

    def run(self) -> None:
        try:
            rep = config_sync.execute_config_sync(
                self._actions,
                self._bundle,
                dry_run=self._dry_run,
                choices=self._choices,
            )
            self.finished.emit(rep)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class PurgeWorker(QThread):
    """后台执行物理删除。会先断言客户端已退出。"""

    progressed = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, plan, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan

    def run(self) -> None:
        try:
            rep = purge_mod.execute_purge(
                self._plan, progress=lambda d, t: self.progressed.emit(d, t)
            )
            self.finished.emit(rep)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class RestoreWorker(QThread):
    """后台执行恢复。会先断言客户端已退出。"""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, plan, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan

    def run(self) -> None:
        try:
            self.finished.emit(restore_mod.execute_restore(self._plan))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SwitchWorker(QThread):
    """后台执行登录账号切换。

    这一步自己会关客户端、复制 GB 级正文、写认证文件、再拉起来 —— 全部同步跑
    会让界面在几十秒里完全无响应，用户只会以为程序死了。
    """

    staged = Signal(str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, plan, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan

    def run(self) -> None:
        try:
            rep = switch_mod.execute_switch(
                self._plan, progress=lambda text: self.staged.emit(text)
            )
            self.finished.emit(rep)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _chip(text: str) -> QPushButton:
    """筛选页签。选中态是深色胶囊（设计稿），不是绿胶囊 ——
    绿色在这套界面里表示「正常/通过」，用它表示「当前筛选」会误导。"""
    b = QPushButton(text)
    b.setObjectName("Tab")
    b.setCheckable(True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setMinimumHeight(BUTTON_HEIGHT)
    return b


# ---------------------------------------------------------------- 备份时间口径
# 概览的「最近备份」要写「今天 21:43」「昨天 21:38」—— 与客户端左栏同一套说法。
# 三个小函数放在模块级，是为了让它们能被单测直接覆盖（不依赖窗口）。


def _day_stamp(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, ValueError, OverflowError):
        return ""


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _backup_clock(ts: float) -> str:
    """最近一次备份的时刻：只有时分（问候行已经说了「今天」）。"""
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


def _backup_day(ts: float) -> str:
    """列表里的「哪一天 + 几点」：今天 / 昨天 / 10-28。"""
    try:
        dt = datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return "时间未知"
    today = datetime.now().date()
    days = (today - dt.date()).days
    if days == 0:
        return f"今天 {dt.strftime('%H:%M')}"
    if days == 1:
        return f"昨天 {dt.strftime('%H:%M')}"
    return dt.strftime("%m-%d %H:%M")


def _backup_size(n: int) -> str:
    n = int(n or 0)
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{max(n // 1024, 1)} KB"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · 会话与配置同步  v{__version__}")
        # 图标不只在标题栏：QMessageBox / 文件对话框这些自建窗口会继承父窗口或
        # 应用级图标，这里两处都设，避免它们弹出来时标题栏是一块白板。
        self.setWindowIcon(icons.app_icon())
        # 尺寸不能写死：设计稿的 1114×660 在 125% 缩放的笔记本上比屏幕可用区
        # （1020/1.25 ≈ 816）还高，底部必然被切。winsize 先把设计稿夹进当前屏幕，
        # 再还原用户上次调好的尺寸 —— 两个动作都不允许越出可用区。
        winsize.restore(self)

        self._inv: inventory.Inventory | None = None
        self._rows: list[inventory.SessionRecord] = []
        self._selected_record = None
        self._labels = accounts_mod.AccountLabels.load()
        # 顺手把当前登录账号的凭据收进凭据库：切走之后就再也读不到它了
        # （认证文件只存当前这一个账号），所以每次启动都收一次。
        # 失败静默 —— 采集不到只影响「以后能不能切回来」，不该拦住程序启动。
        try:
            from .. import vault as vault_mod

            vault_mod.Vault.load().capture_current()
        except (OSError, ValueError):
            pass
        self._accounts: list[accounts_mod.AccountInfo] = []
        self._bundle = None
        self._pkg = None
        self._worker: QThread | None = None
        self._current_uid = self._labels.primary_uid or ALL_ACCOUNTS
        # 默认**只盘当前账号**：全量 96 条里有 52 条属于别的账号，
        # 每次打开都为它们算一遍指纹纯属浪费（4.7s vs 0.2s）。
        # 要看全部账号时从「重新盘点 ▾」里选，或直接在下拉里切过去（会自动重盘）。
        self._scan_uid = "" if self._current_uid == ALL_ACCOUNTS else self._current_uid
        self._scope = SCOPE_VISIBLE
        # 「只看未注册云端」是叠加在当前档位上的诊断过滤器，不是第六个档位。
        # 它曾经是一个并列页签，结果「本地可见 3 / 未注册云端 10」两档互斥地把
        # 13 条可见会话切成两半 —— 用户点「本地可见」只看到 3 条，剩下的 10 条
        # 明明就在客户端里，却要换个页签才找得到。
        self._only_unmapped = False
        # 切换向导单实例闸门：exec() 是嵌套事件循环，期间到达的第二次触发
        # 若再开一个向导，屏幕上就会叠出两套底栏（2026-11-05 用户截图）
        self._switch_dialog_open = False
        # 导入编排状态：待执行的导入参数、客户端 exe 路径、是否自动重启
        self._pending_import: tuple = ([], True, False)
        self._pending_sync: tuple = ()
        self._sync_session_report = None
        self._sync_config_report = None
        self._client_exe = ""
        self._auto_restart = False
        # 会话页副标题（顶栏显示）：由 _update_summary 维护，切页时复用
        self._sess_sub = ""
        # 「让所有账号共用同一份数据」：只记意愿，不自动写库
        self._share_data = uistate.load_share_data()
        # 本地隐藏的会话（云端残影）：盘点时排除，不碰磁盘与客户端
        self._hidden = uistate.load_hidden()
        self._pending_purge = None
        # 物理清除时被分出来、等着「隐藏」的云端会话（删不掉的那拨）
        self._pending_hide: list = []
        self._pending_restore = None
        self._snapshot_text = "未读取"
        # 批量删除多选模式状态
        self._selmode = False
        self._pending_batch = False
        self._batch_local_n = 0
        self._batch_backup_made = False

        self._build_ui()
        self._refresh()

    # ============================================================ 布局
    def _build_ui(self) -> None:
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        row.addWidget(self._build_sidebar())

        # 主区 = 绿色渐变页头（104 固定）+ 页面栈。
        # 参考图把「我在哪一页」和「机器什么状态」压进同一块绿色横幅：
        # 原来白色 64px 产品条已经去掉 —— 产品名在侧栏顶部，重复一次只是
        # 白吃高度，而默认窗口客户区只有 660。
        right = QWidget()
        right.setObjectName("Content")
        right.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        self.header = PageHeader()
        self.header.account_clicked.connect(lambda: self._go(NAV_ACCOUNTS))
        rv.addWidget(self.header)
        # 旧名保留成同一个对象：改动前有代码/测试按 .intro 找页头，
        # 现在标题和状态都在一根横幅里，别让它们指向两个控件。
        self.intro = self.header

        # 账号下拉不再占顶栏的位置，但它是全部账号逻辑的唯一数据源
        # （清单、切号、待重盘判定都在它身上），所以照样创建，只是不放进布局。
        self.cmb_account = QComboBox(self)
        self.cmb_account.setVisible(False)
        self.cmb_account.currentIndexChanged.connect(self._on_account_changed)

        self.config_page = ConfigPage()
        self.config_page.sync_export_requested.connect(self._export_package)
        self.config_page.sync_import_requested.connect(self._import_package)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_overview_page())       # NAV_OVERVIEW
        self.pages.addWidget(self._build_sessions_page())       # NAV_SESSIONS
        self.pages.addWidget(self._wrap(self.config_page))      # NAV_CONFIG
        self.pages.addWidget(self._build_workspace_page())      # NAV_WORKSPACE
        self.pages.addWidget(self._build_backup_page())         # NAV_BACKUP
        self.pages.addWidget(self._build_accounts_page())       # NAV_ACCOUNTS
        # 默认落在「概览」：先给一眼"这台机器上有什么、刚才发生了什么"，
        # 再从这里进会话 / 配置 / 备份 —— 与客户端把首页放在最前是一致的。
        # 这里直接设索引而不走 _go()：_go 会顺带 reload 各页，而此时盘点还没开始。
        self.pages.setCurrentIndex(NAV_OVERVIEW)
        first = self.nav_buttons.get(NAV_OVERVIEW)
        if first is not None:
            first.setChecked(True)
        self._paint_nav_icons()

        rv.addWidget(self.pages, 1)
        row.addWidget(right, 1)
        self.setCentralWidget(root)

        # 加载遮罩覆盖**整个内容区**（顶栏 + 全部页面），而不是只挂在会话页上。
        # ★ 2026-11-03 实测：导入入口有三个（概览页快捷操作、会话页工具条、配置页），
        #   遮罩原先的父控件是会话页 —— 在概览页点「导入同步包」时它照样 start()、
        #   也照样在跑，但整个控件在**隐藏的页面**里，用户看到的是一动不动、没有任何
        #   提示的窗口，于是反馈「导入时没有 loading」。
        self.overlay = LoadingOverlay(right)

        # 页面的副标题变化要立刻反映到顶栏（盘点、筛选、导入完成都会改它）
        for page in (self.overview_page, self.config_page, self.workspace_page,
                     self.backup_page, self.accounts_page):
            if isinstance(page, HeadedPage):
                page.sub_changed.connect(self._sync_header)
        self._sync_header()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_header()

    def _sync_header(self, *_args) -> None:
        """把当前页的标题与副标题贴到绿色页头。

        副标题的**唯一**来源是各页自己（口径在页面手里：盘了多少、筛了哪一档）。
        主窗口不重复计算，否则「页头写着 37、页脚写着 32」这类矛盾迟早出现。
        """
        key = self.pages.currentIndex()
        title = dict(_NAV_ITEMS).get(key, "")
        page = self.pages.currentWidget()
        sub = page.subtitle() if isinstance(page, HeadedPage) else ""
        if key == NAV_SESSIONS:
            sub = self._sess_sub
        elif key == NAV_CONFIG:
            sub = self.config_page.subtitle()
        self.header.set_page(title, sub)

    def _build_overview_page(self) -> QWidget:
        self.overview_page = OverviewPage()
        self.overview_page.goto_sessions.connect(self._goto_scope)
        self.overview_page.goto_config.connect(lambda: self._go(NAV_CONFIG))
        self.overview_page.goto_backup.connect(lambda: self._go(NAV_BACKUP))
        self.overview_page.goto_workspace.connect(lambda: self._go(NAV_WORKSPACE))
        self.overview_page.import_package.connect(self._import_package)
        self.overview_page.export_package.connect(self._export_package)
        return self.overview_page

    def _build_accounts_page(self) -> QWidget:
        self.accounts_page = AccountsPage()
        self.accounts_page.switch_requested.connect(self._open_switch_dialog)
        self.accounts_page.view_requested.connect(self._view_account)
        self.accounts_page.rename_requested.connect(self._rename_account)
        self.accounts_page.open_dir_requested.connect(self._open_data_dir)
        self.accounts_page.add_requested.connect(self._add_account)
        self.accounts_page.align_requested.connect(self._open_align_dialog)
        self.accounts_page.share_toggled.connect(self._set_share_data)
        return self.accounts_page

    def _build_backup_page(self) -> QWidget:
        self.backup_page = BackupPage()
        return self.backup_page

    def _build_workspace_page(self) -> QWidget:
        self.workspace_page = WorkspacePage()
        self.workspace_page.fixed.connect(self._refresh)
        return self.workspace_page

    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Sidebar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 232 是设计稿的固定项：变窄时被压缩的是列表里的「名称」列，不是导航
        bar.setFixedWidth(SIDEBAR_WIDTH)
        v = QVBoxLayout(bar)
        v.setContentsMargins(16, 22, 16, 20)
        v.setSpacing(22)

        # 品牌：应用图标 + 产品名 + 当前版本号。图标自带绿底圆角方，不再套一层
        # 圆形绿底；原先那个「B」取的是客户端 WorkBuddy 的首字母，改名后已没意义。
        brand, self.lbl_version = build_brand()
        v.addLayout(brand)
        v.addSpacing(14)

        # 显示顺序见 _NAV_ITEMS（账号紧随概览）；索引值没变，_go 照旧。
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self.nav_buttons: dict[int, QPushButton] = {}
        for key, label in _NAV_ITEMS:
            b = QPushButton(label)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setIconSize(icons.icon_size())
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(52)
            b.clicked.connect(lambda _checked=False, k=key: self._go(k))
            self._nav_group.addButton(b, key)
            self.nav_buttons[key] = b
            v.addWidget(b)
        self.nav_buttons[NAV_SESSIONS].setChecked(True)
        self._paint_nav_icons()

        v.addStretch(1)

        # 侧栏底部只说一件事：客户端在不在跑（能不能写库）。
        self.lbl_client = QLabel()
        self.lbl_client.setObjectName("SideStatus")
        self.lbl_client.setContentsMargins(8, 0, 8, 0)
        self.lbl_client.setText(
            f'<span style="color:{SIDE_DOT};">●</span>&nbsp;&nbsp;客户端运行中'
        )
        self.lbl_client.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.lbl_client)
        return bar

    def _go(self, key: int) -> None:
        # 离开会话页时退出多选模式，避免勾选框与状态残留在别的页面背后
        if key != NAV_SESSIONS:
            self._exit_select_mode()
        btn = self.nav_buttons.get(key)
        if btn is not None:
            btn.setChecked(True)
        self._paint_nav_icons()
        self.pages.setCurrentIndex(key)
        # 每次进入都重取：配置项与备份都是「外面会变」的东西，
        # 缓存一份进页面必然和磁盘对不上
        if key == NAV_CONFIG:
            self.config_page.reload()
        elif key == NAV_BACKUP:
            self.backup_page.reload()
        elif key == NAV_WORKSPACE:
            self.workspace_page.reload()
        elif key == NAV_OVERVIEW:
            self._refresh_overview()
        elif key == NAV_ACCOUNTS:
            self._refresh_accounts_page()
        self._sync_header()

    def _paint_nav_icons(self) -> None:
        """按选中态重画导航图标。

        QSS 的 color 传不到 QPushButton 的图标上 —— 图标是位图。深底上未选中
        用浅色线条，选中项按参考图换成一颗绿点（白色胶囊里不再放线性图标）。
        """
        for key, btn in self.nav_buttons.items():
            if btn.isChecked():
                btn.setIcon(icons.nav_icon("dot", SIDE_DOT))
            else:
                btn.setIcon(icons.nav_icon(_NAV_ICONS.get(key, "home"), SIDE_TEXT))

    def keyPressEvent(self, event) -> None:
        # 多选模式下 Esc 退出，避免还要去点「退出」按钮
        if self._selmode and event.key() == Qt.Key.Key_Escape:
            self._exit_select_mode()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_nav(self, index: int) -> None:
        """旧入口保留：外部仍可能按索引切页。"""
        self._go(index)

    def _goto_scope(self, scope: str) -> None:
        """概览页的快捷入口：切到会话页并选好筛选档。"""
        self._go(NAV_SESSIONS)
        btn = self._chips.get(scope)
        if btn is not None:
            btn.setChecked(True)
        self._on_scope(scope)

    def _build_sessions_page(self) -> QWidget:
        """会话页 = 工具栏 + 列表卡片 + 详情面板。

        设计稿里没有选中项时详情面板不出现 —— 列表吃满整宽，点一行才让出右栏。
        弹窗会遮住列表，而「对照着看」正是这里最常见的动作：看完详情还要接着点
        下一条，所以详情是三栏的第三栏，只是默认收着。
        """
        page = QWidget()
        page.setObjectName("Content")
        h = QHBoxLayout(page)
        h.setContentsMargins(CONTENT_PADDING, 20, CONTENT_PADDING, 14)
        h.setSpacing(14)

        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ---- 工具栏：筛选页签在左，搜索与动作在右
        #
        # 一行放不下时**整组换行**，不硬挤：1080 宽的窗口叠加右侧详情面板后，
        # 列表区只剩约 530px，而这一行在理想宽度下要 1000px 出头 ——
        # QHBoxLayout 会按比例把每个控件压到 minimumSizeHint 以下，
        # 文字被裁成「地可见」「已删除(F 侧栏)」这种半截。
        # 页签走 FlowLayout（放不下自动换行），动作组整体落到第二行。
        chips_box = FlowBox(spacing=8)
        self._chips_box = chips_box
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self._chips: dict[str, QPushButton] = {}
        for key in _CHIP_ORDER:
            b = _chip(_SCOPE_LABELS[key])
            b.clicked.connect(lambda _checked=False, k=key: self._on_scope(k))
            self.chip_group.addButton(b)
            self._chips[key] = b
            chips_box.flow.addWidget(b)
        self._chips[SCOPE_VISIBLE].setChecked(True)

        # 「清除已删除…」不放页签行：那里五个页签已经是「筛选」的语义，
        # 混进一个红色动作，看着像第六种筛选。它属于「对当前这批做整体处理」，
        # 所以落在页脚 —— 先看到 24 条，再决定要不要一次清空。
        self.btn_purge_deleted = QPushButton("清除已删除…")
        self.btn_purge_deleted.setObjectName("LinkDanger")
        self.btn_purge_deleted.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_purge_deleted.setToolTip("把当前账号下已删除的会话从库里彻底移除")
        self.btn_purge_deleted.setVisible(False)
        self.btn_purge_deleted.clicked.connect(self._purge_deleted_all)

        # 动作组单独一个容器：整组要么在右、要么整组下移。
        # 逐个控件各自被压缩，正是「搜索框被压成一条缝」的成因。
        # 容器自身也走流式：窗口拉到最小且详情面板展开时宁可换行，
        # 也不能把「导出同步包」压成只剩一个省略号。
        actions = FlowBox(spacing=8)
        acts = actions.flow

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("搜索会话标题…")
        self.txt_search.setFixedHeight(BUTTON_HEIGHT)
        self.txt_search.setFixedWidth(212)
        self.txt_search.addAction(
            icons.glyph_icon("search", TEXT_TERTIARY, 14),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.txt_search.textChanged.connect(self._apply_filter)
        acts.addWidget(self.txt_search)

        # 「重新盘点」+ 范围下拉合成一个方形图标按钮：范围不是主按钮，
        # 主按钮是「再盘一次」，而设计稿里这颗按钮只占 34px。
        self.btn_refresh = QToolButton()
        self.btn_refresh.setObjectName("IconButton")
        self.btn_refresh.setIcon(icons.glyph_icon("refresh", TEXT_SECONDARY, 15))
        self.btn_refresh.setFixedSize(BUTTON_HEIGHT, BUTTON_HEIGHT)
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setToolTip("重新盘点当前登录账号 · 右键查看更多")
        self.btn_refresh.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        scan_menu = QMenu(self)
        # 只盘当前登录账号：全量盘点会把别的账号也算一遍指纹，
        # 而导出永远只导当前登录账号，那份数据在这里没有用处。
        scan_menu.addAction("盘点当前登录账号").triggered.connect(self._rescan_current)
        scan_menu.addSeparator()
        scan_menu.addAction("清空指纹缓存并重新盘点").triggered.connect(self._rescan_fresh)
        self.btn_refresh.setMenu(scan_menu)
        # 旧名字保留：冒烟测试按 btn_scan_menu.menu() 检查三个范围动作
        self.btn_scan_menu = self.btn_refresh
        acts.addWidget(self.btn_refresh)

        self.btn_import_pkg = QPushButton("  导入同步包")
        self.btn_import_pkg.setIcon(icons.glyph_icon("inbox_down", TEXT_SECONDARY, 14))
        self.btn_import_pkg.setFixedHeight(BUTTON_HEIGHT)
        self.btn_import_pkg.setToolTip("读入同步包，一次完成会话与配置")
        self.btn_import_pkg.clicked.connect(self._import_package)
        acts.addWidget(self.btn_import_pkg)

        # 导出只有一个入口、一种产物：当前登录账号的整包。
        # 「仅会话」的老格式导出已收掉 —— 它会产出只有一半内容的包。
        self.btn_export_pkg = QPushButton("导出全部同步包")
        self.btn_export_pkg.setToolTip(
            "把当前登录账号的会话、不含凭据的配置、全部工作空间记忆打成一个包"
        )
        self.btn_export_pkg.setMinimumHeight(BUTTON_HEIGHT)
        self.btn_export_pkg.clicked.connect(self._export_package)
        acts.addWidget(self.btn_export_pkg)

        # 低频入口留一个无边框的「⋯」：旧格式包导入、物理清除都不该挤进工具栏
        self.btn_more = QToolButton()
        self.btn_more.setObjectName("IconButtonGhost")
        self.btn_more.setIcon(icons.glyph_icon("more", TEXT_SECONDARY, 16))
        self.btn_more.setFixedSize(30, BUTTON_HEIGHT)
        self.btn_more.setToolTip("低频入口")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        self.act_view = menu.addAction("表格视图")
        self.act_view.setCheckable(True)
        self.act_view.toggled.connect(self._set_table_view)
        menu.addSeparator()
        menu.addAction("导入旧格式包…").triggered.connect(self._import_bundle)
        # 隐藏必须可逆：否则「清除」就成了一次不可撤销的操作
        self.act_unhide = menu.addAction("显示已隐藏的会话")
        self.act_unhide.triggered.connect(self._unhide_all)
        self.act_unhide.setVisible(False)
        menu.addSeparator()
        # 「未注册云端」是诊断过滤，不是档位：勾上后在当前档位里只看
        # 「活着、有正文、却没有云端映射」的那些 —— 它们换台机器就看不到了
        self.act_unmapped = menu.addAction("只看未注册云端")
        self.act_unmapped.setCheckable(True)
        self.act_unmapped.setToolTip(_SCOPE_TIPS[SCOPE_UNMAPPED])
        self.act_unmapped.toggled.connect(self._on_unmapped_only)
        self.act_unmapped.setVisible(False)
        self.btn_more.setMenu(menu)
        acts.addWidget(self.btn_more)

        # 批量删除入口：常态就是一个红描边按钮，点一下进入多选模式；
        # 多选模式里它变成「退出多选」，避免在工具栏再塞一个切换开关
        self.btn_batch = QPushButton("批量删除")
        self.btn_batch.setObjectName("BatchBtn")
        self.btn_batch.setFixedHeight(BUTTON_HEIGHT)
        self.btn_batch.setToolTip("进入多选模式，勾选会话后一次性删除")
        self.btn_batch.clicked.connect(self._toggle_select_mode)
        acts.addWidget(self.btn_batch)

        self.tools_bar = ResponsiveBar(chips_box, actions)
        self.tools_bar.narrow_changed.connect(self._on_tools_narrow)
        v.addWidget(self.tools_bar)

        self.card_view = CardGrid()
        self.card_view.selection_changed.connect(self._on_card_selected)
        self.card_view.card_activated.connect(self._open_detail)
        self.card_view.check_count_changed.connect(self._on_batch_count)
        # 折叠状态是本地偏好：注入进去之后由列表自己维护显隐，
        # 上层只在变化时负责落盘
        self._collapsed_groups = uistate.load_collapsed()
        self._expanded_groups = uistate.load_expanded()
        self.card_view.set_state(self._collapsed_groups, self._expanded_groups)
        self.card_view.group_toggled.connect(self._on_group_toggled)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # 只有标题列拉伸：让它吃掉剩余宽度。工作空间用固定宽度，
        # 否则两列平分会把标题挤到只能显示半句。
        hh.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_CWD, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(COL_CWD, 168)
        for c in (COL_ACCOUNT, COL_LINES, COL_SIZE, COL_CLOUD, COL_STATUS, COL_TIME):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self._on_table_activated)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.card_view)
        self.stack.addWidget(self.table)

        # 列表外面套一张圆角卡片：设计稿里列表与页面底色之间有一层白卡
        card = QFrame()
        card.setObjectName("ListCard")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        # 批量删除成功后的内联横幅：替代第二个模态框，删除完直接回列表看结果
        self.batch_banner = QFrame()
        self.batch_banner.setObjectName("Banner")
        self.batch_banner.setStyleSheet(
            "background:#E9F6EE;border:1px solid #CDEBD7;border-radius:8px;"
        )
        bl = QHBoxLayout(self.batch_banner)
        bl.setContentsMargins(12, 8, 10, 8)
        bl.setSpacing(8)
        self.lbl_batch_banner = QLabel()
        self.lbl_batch_banner.setStyleSheet("font-size:12px;color:#15803D;")
        bl.addWidget(self.lbl_batch_banner, 1)
        self.btn_banner_close = QPushButton("✕")
        self.btn_banner_close.setObjectName("IconButtonGhost")
        self.btn_banner_close.setFixedSize(20, 20)
        self.btn_banner_close.clicked.connect(lambda: self.batch_banner.setVisible(False))
        bl.addWidget(self.btn_banner_close)
        self.batch_banner.setVisible(False)
        cv.addWidget(self.batch_banner)

        cv.addWidget(self.stack)
        v.addWidget(card, 1)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        # 页脚这行数字很多，必须可省略：QLabel 的最小宽度等于整行文字宽度，
        # 一句长摘要就能把整个窗口的下限顶到 1350px 以外
        self.lbl_foot = ElidedLabel()
        self.lbl_foot.setObjectName("StatusLine")
        self.lbl_foot.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        foot.addWidget(self.lbl_foot, 1)
        foot.addWidget(self.btn_purge_deleted)
        self.lbl_foot_right = QLabel()
        self.lbl_foot_right.setObjectName("StatusLine")
        foot.addWidget(self.lbl_foot_right, 0)

        # 批量删除的底部动作条：进入多选模式才出现，浮在列表与页脚之间
        self.batch_bar = QWidget()
        self.batch_bar.setObjectName("SyncBar")
        bb = QHBoxLayout(self.batch_bar)
        bb.setContentsMargins(16, 8, 16, 8)
        bb.setSpacing(10)
        self.lbl_batch_count = QLabel("已选 0 项")
        self.lbl_batch_count.setObjectName("StatusLine")
        bb.addWidget(self.lbl_batch_count)
        self.btn_sel_all = QPushButton("全选当前列表")
        self.btn_sel_all.setObjectName("LinkMuted")
        self.btn_sel_all.clicked.connect(lambda: self.card_view.select_all_visible(True))
        bb.addWidget(self.btn_sel_all)
        self.btn_clear_sel = QPushButton("清空选择")
        self.btn_clear_sel.setObjectName("LinkMuted")
        self.btn_clear_sel.clicked.connect(self.card_view.clear_checks)
        bb.addWidget(self.btn_clear_sel)
        bb.addStretch(1)
        self.lbl_batch_cloud = QLabel()
        self.lbl_batch_cloud.setObjectName("PillBad")
        self.lbl_batch_cloud.setVisible(False)
        bb.addWidget(self.lbl_batch_cloud)
        self.btn_batch_del = QPushButton("批量删除")
        self.btn_batch_del.setObjectName("Danger")
        self.btn_batch_del.setEnabled(False)
        self.btn_batch_del.clicked.connect(self._do_batch_delete)
        bb.addWidget(self.btn_batch_del)
        self.btn_batch_exit = QPushButton("退出")
        self.btn_batch_exit.clicked.connect(self._exit_select_mode)
        bb.addWidget(self.btn_batch_exit)
        self.batch_bar.setVisible(False)

        v.addLayout(foot)
        v.insertWidget(v.count() - 1, self.batch_bar)

        # 加载遮罩**不在这里建**：它必须覆盖整个内容区，否则从别的页面发起的操作
        # （概览页的「导入同步包」）遮罩会落在隐藏的页面上，界面上什么都看不到。
        # 唯一实例在 _build_ui 里创建，父控件是内容区容器。

        h.addWidget(left, 1)

        self.detail = DetailPanel()
        self.detail.restore_requested.connect(self._restore_one)
        self.detail.purge_requested.connect(self._purge_one)
        self.detail.closed.connect(self._on_detail_closed)
        # 设计稿：没有选中项时不占位。选中行才让出右栏。
        self.detail.setVisible(False)
        h.addWidget(self.detail)
        return page

    def _on_tools_narrow(self, narrow: bool) -> None:
        """窄行里搜索框改成可伸缩，把动作行剩下的宽度吃满。

        宽屏必须回到 212 定宽：列表区一变宽搜索框就跟着长，右侧动作组会
        左右横跳，看起来像布局在抖。
        """
        if narrow:
            # 顺序不能反：先放开上限，下限才降得下来
            self.txt_search.setMaximumWidth(16777215)
            self.txt_search.setMinimumWidth(120)
        else:
            self.txt_search.setFixedWidth(212)

    # ============================================================ 导航与账号
    def _account_login_uid(self) -> str:
        """客户端此刻登录的账号 —— 认证文件的 `account.uid` 是唯一权威。

        退路用骨架快照/目录 mtime 的判断，但那条只在刚装完、认证文件还没写时
        才用得上；切换过账号之后认证文件一定存在，且立刻反映新身份。
        """
        try:
            from .. import authfile

            uid = authfile.current_uid()
        except Exception:  # noqa: BLE001 - 读不到就当没登录
            uid = ""
        return uid or accounts_mod.detect_primary_uid(snapshot=self._labels.snapshot)

    def _account_view_uid(self) -> str:
        """抽屉里该高亮谁：正在看的那个账号。"""
        uid = self._current_uid
        if uid == ALL_ACCOUNTS or not uid:
            return self._account_login_uid()
        return uid

    def _toggle_account_drawer(self) -> None:
        """旧入口保留：v1.6.11 起账号是独立一页，点顶栏胶囊/侧栏「账号」都走这里。

        名字没改是给旧调用方（冒烟测试、快捷键）留的桥；内部只做一件事 —— 跳页。
        """
        self._go(NAV_ACCOUNTS)

    def _view_account(self, uid: str) -> None:
        """查看某账号的会话（纯视图筛选，不动客户端登录身份）。

        「查看」现在会**按这个 uid 单独只读盘一次**（`count_visible_for_uid`），
        把真实的可见条数填进卡片；不换盘点范围、不碰 `_scan_uid` 与导出口径。
        以前只数手里那份「登录账号范围」的盘点结果，其它账号一律数出 0 条 ——
        那是没盘过，不是没有会话。
        """
        idx = self.cmb_account.findData(uid)
        if idx < 0:
            QMessageBox.information(self, "查看账号", "这个账号在本机数据库里没有记录。")
            return
        self.cmb_account.setCurrentIndex(idx)
        # setCurrentIndex 会走 _on_account_changed → 刷新账号页，所以计数必须在
        # 它之后回写，否则刚写上的数字会被重建的卡片冲掉。
        count = self.accounts_page.session_count(uid)
        if count is None:                     # 本次盘点结果里还没数过这个账号
            count = count_visible_for_uid(uid)
        if count is not None:                 # 盘点失败就保留「待盘点」，不写假 0
            self.accounts_page.apply_session_count(uid, count)

    def _open_align_dialog(self) -> None:
        """本机账号对齐（跨账号的范围型操作）—— 四步向导，写盘在第 4 步内完成。

        ★ 向导自己负责「关客户端 → 备份 → 写入 → 重启」，所以这里不再走
          `_do_align`：再执行一次会对着已经写过的库再来一遍。
          结束时只按结果刷新（数据变了：会话数、账号列表都要重盘）。
        """
        from .align_dialog import AlignWizard

        if self._inv is None:
            QMessageBox.information(self, "本机账号对齐", "还没有盘点结果，请先重新盘点。")
            return
        dlg = AlignWizard(self._accounts, self._labels, self._account_login_uid(), parent=self)
        dlg.exec()
        rep = dlg.report
        if rep is not None and rep.ok:
            self._set_busy(True)
            self.overlay.start("正在重新盘点…", "对齐已完成，更新列表")
            self._refresh(uid=self._scan_uid, clear_cache=True)
        elif rep is not None and rep.error:
            QMessageBox.warning(self, "对齐未完成", rep.error)

    # 对齐的执行已并入向导第 4 步（`AlignWizard._start_run`）：
    # 它自己关客户端、备份、写入、重启，主窗口不再持有一条平行的执行路径。

    # ---------------------------------------------------- 切换登录身份
    def _open_switch_dialog(self, target_uid: str = "") -> None:
        """真正的账号切换：改认证文件 + 重启客户端。

        与 `_view_account`（只切列表视图）是两件事，不能混成一个入口 ——
        一个是「我现在看谁的数据」，一个是「客户端现在是谁登录着」。
        """
        # QAction.triggered 会把 checked 传进来；bool 不是 uid，必须挡掉，
        # 否则 False 会被当成"目标账号是 False"，在列表里找不到就静默不预选。
        if not isinstance(target_uid, str):
            target_uid = ""
        if not self._accounts:
            QMessageBox.information(self, "切换登录账号", "还没有盘点出任何账号，请先重新盘点。")
            return
        # 向导是模态的，但 exec() 跑的是嵌套事件循环：这期间到达的第二次触发
        # 仍会进到这里。不挡住就会在旧向导底下再开一个，屏幕上叠出两套底栏。
        if self._switch_dialog_open:
            return
        self._switch_dialog_open = True
        current = self._current_uid
        if current == ALL_ACCOUNTS or not current:
            current = self._account_login_uid()

        # 抽屉进来时已经指定了目标；其余入口留空，让用户自己选
        if target_uid and target_uid == current:
            target_uid = ""

        dlg = SwitchAccountDialog(
            self._accounts,
            self._labels,
            current,
            parent=self,
            preselect_uid=target_uid,
        )
        try:
            accepted = dlg.exec() == SwitchAccountDialog.DialogCode.Accepted
        finally:
            # 只有对话框真正关掉才释放闸门；它还在屏幕上时重复触发一律丢弃
            self._switch_dialog_open = False
        if not accepted:
            return
        target = dlg.target_uid
        if not target:
            return

        # 重算一遍计划（勾选可能改过）。这一步要算指纹、比内容，可能要一两秒；
        # 不先出遮罩，用户看到的就是「对话框一关，界面僵住」。
        self.overlay.start("正在准备切换…", "统计要迁移的会话与长期记忆")
        QApplication.processEvents()
        try:
            plan = switch_mod.plan_switch(
                target,
                copy_cids=dlg.copy_cids(),
                do_memory=dlg.do_memory,
            )
        finally:
            self.overlay.stop()

        if not plan.ready:
            QMessageBox.warning(self, "无法切换", plan.block_reason)
            return
        # 向导第 3 步本身就是确认页（目标、步骤、关客户端的代价都写在上面），
        # 这里不再弹第二个「确定吗」：关掉对话框又冒出一个框，用户会以为
        # 点完「开始切换」什么都没发生 —— 那正是这次反馈里最刺眼的一处。
        # 只有计划带了需要人拍板的警告时，才补一次确认。
        if plan.warnings and not self._confirm_switch(plan):
            return
        self._do_switch(plan)

    def _confirm_switch(self, plan) -> bool:
        """把「客户端会被关掉」摆到明面上再让人点头 —— 和导入同一套说法。"""
        lines = ["即将执行：", ""]
        lines += [f"  {i}. {step}" for i, step in enumerate(plan.steps(), 1)]
        if plan.warnings:
            lines += ["", "注意："] + [f"  · {w}" for w in plan.warnings]

        box = QMessageBox(self)
        box.setWindowTitle("确认切换登录账号")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"要把 WorkBuddy 的登录身份切换到「{plan.target_name}」吗？")
        box.setInformativeText(
            "关闭客户端会导致：\n"
            "· 未发送的消息输入丢失\n"
            "· 正在进行的对话中断（包括当前这个对话）\n"
            "· 后台任务被中断\n\n"
            + "\n".join(lines)
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _do_switch(self, plan) -> None:
        self._set_busy(True)
        self.overlay.start("正在切换账号…", "关闭客户端 → 搬数据 → 写入登录身份 → 重启")
        self._switcher = SwitchWorker(plan, self)
        self._switcher.staged.connect(self._on_switch_stage)
        self._switcher.finished.connect(self._on_switch_done)
        self._switcher.failed.connect(self._on_switch_failed)
        self._switcher.start()

    def _on_switch_stage(self, text: str) -> None:
        # 只换标题，不重启遮罩：start() 会把计时清零，用户看到「已用 0.0 秒」
        # 一跳一跳，反而更像卡死。阶段文案自带「3/7」的步号，进度因此可读。
        self.overlay.set_title(text)

    def _on_switch_failed(self, message: str) -> None:
        self._set_busy(False)
        self.overlay.stop()
        self._refresh_client_state()
        QMessageBox.critical(self, "切换失败", message)

    def _on_switch_done(self, rep) -> None:
        self._set_busy(False)
        self.overlay.stop()
        self._refresh_client_state()

        # 身份已经变了：重读昵称缓存与账号归属，再重盘一次列表
        self._labels = accounts_mod.AccountLabels.load()

        lines = [rep.summary(), ""]
        if rep.auth_backup is not None:
            lines.append(f"认证文件备份：{rep.auth_backup}")
        if rep.copy_report is not None and rep.copy_report.copied:
            cr = rep.copy_report
            lines.append(cr.summary())
            if cr.backup is not None and not cr.backup.empty:
                lines.append(f"数据库备份：{cr.backup.root}")
        if rep.memory_report is not None and rep.memory_report.changed:
            lines.append(rep.memory_report.summary())
        if rep.warnings:
            lines.append("")
            lines += [f"· {w}" for w in rep.warnings]
        if not rep.ok:
            lines.append("")
            lines.append(rep.error or "切换未完成")

        box = QMessageBox(self)
        box.setWindowTitle("切换到「%s」" % (rep.target_name or "目标账号"))
        box.setIcon(
            QMessageBox.Icon.Information if rep.ok else QMessageBox.Icon.Warning
        )
        box.setText("切换完成" if rep.ok else "切换未完成")
        box.setInformativeText("\n".join(lines))
        box.exec()

        # 盘点范围跟着走到新账号：切完还看着旧账号的列表，会让人以为数据没了
        if rep.ok and rep.target_uid:
            self._refresh(uid=rep.target_uid)
        else:
            self._refresh(uid=self._scan_uid)

    def _update_account_card(self) -> None:
        """顶栏账号胶囊与「账号」页的数据一起刷。

        两处显示的是同一件事（这台机器上有谁、现在看的/登录的是谁），
        分开算必然有一天对不上 —— 所以只有一个出口。
        """
        uid = self._current_uid
        if uid == ALL_ACCOUNTS:
            main, meta = "全部账号", f"{len(self._accounts)} 个账号"
        else:
            name = (self._labels.name_of(uid) if uid else "") or "未识别账号"
            nick = self._labels.nickname_of(uid) if uid else ""
            edition = self._labels.edition_of(uid) if uid else ""
            # 主行给人名（别名/昵称），副行补「真名 · 套餐」—— 一串 uuid 对用户没有信息量
            bits = [b for b in (nick if nick and nick != name else "", edition) if b]
            main, meta = name, " · ".join(bits)
        self.header.set_account(main, meta)
        self._refresh_accounts_page()

    def _refresh_accounts_page(self) -> None:
        """把账号清单灌进「账号」页。没盘过就传空表 —— 页面自己显示空态。"""
        self.accounts_page.set_accounts(
            self._accounts,
            login_uid=self._account_login_uid(),
            view_uid=self._account_view_uid(),
            share=self._share_data,
            unscanned={a.uid for a in self._accounts if self._needs_rescan(a.uid)},
        )

    def _set_share_data(self, on: bool) -> None:
        """「让所有账号共用同一份数据」= 跑一次本机账号对齐。

        ★ 关掉只是不再记录这个意愿，**不删任何数据**。所以文案必须说清
          「关＝不提醒」，而不是让人以为关掉会把多出来的会话清掉。
        """
        self._share_data = bool(on)
        uistate.save_share_data(self._share_data)
        self._refresh_accounts_page()
        if not on:
            return
        box = QMessageBox(self)
        box.setWindowTitle("共用同一份数据")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要让所有账号拥有同一份数据吗？")
        box.setInformativeText(
            "做法是把每个账号名下的会话与人格文件并集到其余账号（各自一份副本）。\n"
            "需要先关闭客户端；写库前会自动备份一次。会话越多耗时越长。\n\n"
            "这个开关本身不删任何东西 —— 关掉只是不再提醒。"
        )
        ok = box.addButton("现在对齐…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is ok:
            self._open_align_dialog()

    def _add_account(self) -> None:
        """添加账号：客户端登录过一次就能被收进来；也支持从别的工具导入凭据。"""
        added = updated = 0
        skipped: list[str] = []
        try:
            from .. import vault as vault_mod

            added, updated, skipped = vault_mod.Vault.load().import_from_buddy_switch()
        except Exception as exc:  # noqa: BLE001 - 导入失败不该拦住用户看说明
            skipped = [f"{type(exc).__name__}: {exc}"]

        lines = [
            "账号由客户端自己的登录状态决定，工具不去碰登录流程：",
            "· 在 WorkBuddy 里登录过一次的账号，会被自动收进这个列表；",
            "· 换到另一台电脑时，用同步包或对照工具搬完会话，账号也会跟着出现。",
            "",
        ]
        if added or updated:
            lines.append(f"已从其它工具的账号库导入：新增 {added} 个，更新 {updated} 个。")
        else:
            lines.append("没有从其它工具的账号库读到新账号（没装过或没有该文件都属正常）。")
        if skipped[:3]:
            lines.append("跳过：" + "、".join(skipped[:3]))
        box = QMessageBox(self)
        box.setWindowTitle("添加账号")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("怎么把另一个账号加进来")
        box.setInformativeText("\n".join(lines))
        box.exec()
        self._refresh(clear_cache=False)

    # ============================================================ 概览
    def _refresh_overview(self) -> None:
        inv = self._inv
        if inv is None:
            return
        scoped = self._account_scoped()
        # 与页签「本地可见」同一个判据（见 _scope_bucket）：概览卡点进去
        # 落在会话页，两处数字必须相等
        visible = sum(1 for r in scoped if _scope_bucket(r) == SCOPE_VISIBLE)
        dead = sum(1 for r in scoped if r.is_deleted)
        bodyless = sum(1 for r in scoped if not r.has_body and not r.is_deleted)

        try:
            entries = guard.scan_backups()
            backup_bytes = guard.backup_total_bytes(entries)
        except OSError:
            entries, backup_bytes = [], 0

        recent = [
            (_backup_day(e.created_at), e.kind_text, _backup_size(e.size_bytes))
            for e in entries[:5]
        ]
        today = _today_stamp()
        today_count = sum(1 for e in entries if _day_stamp(e.created_at) == today)
        last_text = _backup_clock(entries[0].created_at) if entries else ""
        sessions_bytes = sum(int(r.jsonl_size or 0) for r in scoped)

        bl = config_sync.Baseline.load()
        if bl.updated_at:
            try:
                baseline = datetime.fromtimestamp(bl.updated_at).strftime("%m-%d %H:%M")
            except (OSError, ValueError, OverflowError):
                baseline = "时间未知"
        else:
            baseline = "从未同步"

        uid = self._current_uid
        account_text = (
            "全部账号" if uid == ALL_ACCOUNTS else self._labels.name_of(uid)
        )
        self.overview_page.refresh(
            greeting_name=self._labels.name_of(uid) if uid != ALL_ACCOUNTS else "",
            today_backup_count=today_count,
            last_backup_text=last_text,
            visible_count=visible,
            total_count=inv.total,
            deleted_count=dead,
            bodyless_count=bodyless,
            config_count=self.config_page.item_count(),
            backup_count=len(entries),
            backup_bytes=backup_bytes,
            client_running=bool(guard.running_client_processes()),
            account_text=account_text,
            scope_text="当前账号" if self._scan_uid else "全部账号",
            snapshot_text=self._snapshot_text,
            baseline_text=baseline,
            sessions_bytes=sessions_bytes,
            recent_backups=recent,
        )

    # ============================================================ 列表渲染
    def _render_rows(self) -> None:
        """渲染列表与表格。

        列表改成按工作空间分组后不再分页：分页和分组语义冲突 ——
        第二页可能从某一组的中间接上，组标题会重复出现、条数也对不上。
        当前规模（单账号几十条、全账号百来条）全量渲染没有可感的延迟，
        真涨到几百条再引入虚拟滚动，而不是先加一个会出错的折中。
        """
        self.card_view.set_records(self._rows)
        self._fill_table(self._rows)

    def _wrap(self, inner: QWidget) -> QWidget:
        """给外面包一层带内边距的页面容器（配置页自己是纯控件，不带边距）。"""
        holder = QWidget()
        holder.setObjectName("Content")
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        v = QVBoxLayout(holder)
        v.setContentsMargins(CONTENT_PADDING, 20, CONTENT_PADDING, 14)
        v.setSpacing(0)
        v.addWidget(inner, 1)
        return holder

    # ============================================================ 盘点
    def _refresh(self, uid: str | None = None, clear_cache: bool = False) -> None:
        """重新盘点。

        uid 为 None 表示沿用当前范围；传空串表示切到全部账号。
        盘点范围变了要同步记进 self._scan_uid —— 界面上所有统计都跟着它走，
        记错了就会出现「盘了 20 条却报库内 95 条」。
        """
        if self._worker is not None and self._worker.isRunning():
            return
        if uid is not None:
            self._scan_uid = uid

        self._set_busy(True)
        self._sess_sub = "正在盘点本机会话…"
        if self.pages.currentIndex() == NAV_SESSIONS:
            self._sync_header()
        # 加载时给下拉一个占位，否则是个空白框，看着像没加载出来
        if self.cmb_account.count() == 0:
            self.cmb_account.blockSignals(True)
            self.cmb_account.addItem("正在读取…", ALL_ACCOUNTS)
            self.cmb_account.blockSignals(False)

        scope = "全部账号" if not self._scan_uid else "当前账号"
        self.overlay.start(
            f"正在盘点{scope}…",
            "首次约需数秒，之后走指纹缓存",
        )

        self._worker = InventoryWorker(
            True, uid=self._scan_uid, clear_cache=clear_cache
        )
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _rescan_current(self) -> None:
        """盘点**当前登录账号**（不是下拉里选中的那个）。

        侧栏可以切到别的账号只读浏览，但盘点范围与导出口径都跟着登录状态走，
        否则同一台机器上「选谁」会悄悄改变导出的内容。
        """
        uid = accounts_mod.detect_primary_uid(snapshot=self._labels.snapshot)
        if not uid:
            uid = self._labels.primary_uid or self._current_uid
        if not uid or uid == ALL_ACCOUNTS:
            self._refresh()
            return
        # 下拉跟着跳回登录账号：盘完却停在别的账号上，会让人以为盘点没生效
        self._current_uid = uid
        self._refresh(uid=uid)

    def _rescan_fresh(self) -> None:
        """清空指纹缓存后重盘 —— 怀疑缓存不对时使用。"""
        self._refresh(uid=self._scan_uid, clear_cache=True)

    def _on_progress(self, done: int, total: int) -> None:
        self.overlay.set_counter(done, total)

    def _on_done(self, inv: inventory.Inventory) -> None:
        self._inv = inv
        self._set_busy(False)
        self.overlay.stop()
        # 盘点结果整个换了一份：按旧 inv 数出来的会话条数不能再留在卡片上
        self.accounts_page.clear_session_counts()
        self._rebuild_accounts()
        self._apply_filter()
        self._refresh_overview()

    def _rebuild_accounts(self) -> None:
        inv = self._inv
        if inv is None:
            return
        # 名单取库里出现过的全部账号（inv.all_uids 不受盘点范围影响），
        # 否则只盘当前账号时别的账号会从下拉里消失，切不过去。
        uids = list(inv.all_uids) or sorted({r.user_id for r in inv.records if r.user_id})
        by_uid = inv.by_uid()
        counts = {u: len(by_uid.get(u, ())) for u in uids}
        # 下拉框的「X 条可见」必须和 chip / 副标题 / 底部用同一口径：
        # 侧边栏快照 ∪ 导入待确认。此前这里用的是纯快照条数，
        # 于是导完 16 条后下拉显示 9、页面上写着 25，两个数字互相打脸。
        side = {
            u: sum(
                1
                for r in inv.records
                if (r.user_id == u or u in r.sidebar_uids)
                and _scope_bucket(r) == SCOPE_VISIBLE
            )
            for u in uids
        }
        # 抽屉第二行的两个数：占用与最近使用。都从盘点结果现算，不额外扫盘
        # （jsonl_size 已在盘点时 stat 过，这里只是相加）。
        sizes: dict[str, int] = {}
        used: dict[str, int] = {}
        for uid, rows in by_uid.items():
            sizes[uid] = sum(int(r.jsonl_size or 0) for r in rows)
            used[uid] = max(
                (max(int(r.last_activity_at or 0), int(r.updated_at or 0)) for r in rows),
                default=0,
            )
        self._accounts = accounts_mod.build_accounts(
            sorted(counts.keys()), self._labels, counts, side, sizes, used
        )

        self.cmb_account.blockSignals(True)
        self.cmb_account.clear()
        # 没盘过的范围不给条数：那数字得靠猜，猜出来的只会让人以为盘点错了
        pending = "（未盘点，仅浏览）"
        self.cmb_account.addItem(
            f"全部账号（{len(inv.visible)} 条可见）"
            if not self._scan_uid
            else f"全部账号{pending}",
            ALL_ACCOUNTS,
        )
        for a in self._accounts:
            tag = a.label
            if self._needs_rescan(a.uid):
                tag += pending
            elif a.sidebar_count:
                tag += f"（{a.sidebar_count} 条可见）"
            self.cmb_account.addItem(tag, a.uid)

        idx = self.cmb_account.findData(self._current_uid)
        if idx < 0:
            fallback = self._labels.primary_uid or (
                self._accounts[0].uid if self._accounts else ALL_ACCOUNTS
            )
            self._current_uid = fallback
            idx = max(self.cmb_account.findData(fallback), 0)
        self.cmb_account.setCurrentIndex(idx)
        self.cmb_account.blockSignals(False)

        # 侧边栏快照的时间戳只用于展示口径说明，放进概览页脚注
        if inv.sidebar_saved_at:
            when = datetime.fromtimestamp(inv.sidebar_saved_at / 1000).strftime("%m-%d %H:%M")
            self._snapshot_text = when
        else:
            self._snapshot_text = "无快照（按全部会话展示）"
        self._update_account_card()

    def _on_account_changed(self, _idx: int) -> None:
        data = self.cmb_account.currentData()
        self._current_uid = str(data) if data else ALL_ACCOUNTS
        # ★ 下拉切账号只换视图，不触发盘点：盘点范围固定为当前登录账号。
        #   账号卡「查看」另走 _view_account —— 那里会按该 uid 单独只读盘一次
        #   填出真实条数，但同样不换 _scan_uid、不改导出口径。
        self._apply_filter()

    def _needs_rescan(self, uid: str) -> bool:
        """该账号是否有本次盘点到的数据（没有就只能只读浏览）。

        盘点范围固定为当前登录账号，所以切换账号不再触发重盘 ——
        这个方法现在只用来给下拉框加「未盘点」标注。
        """
        return bool(self._scan_uid) and uid != self._scan_uid

    def _viewing_unscanned_account(self) -> bool:
        """下拉里选的是不是「本次没盘点过的账号」。"""
        return self._needs_rescan(self._current_uid)

    def _rename_account(self) -> None:
        uid = self._current_uid
        if not uid or uid == ALL_ACCOUNTS:
            QMessageBox.information(
                self, "改账号名", "请先在账号卡片里切到一个具体账号，再改名。"
            )
            return
        current = self._labels.name_of(uid)
        nick = self._labels.nickname_of(uid)
        tip = f"给账号 {accounts_mod.short_uid(uid)} 起个名字"
        if nick and nick != current:
            # 客户端真名一直盖在别名下面。不自动替换（那是用户的显式设置），
            # 但必须摆出来，否则用户永远不知道自己手填的名字和真名不一致
            tip += f"\n客户端昵称：{nick}（清空则用昵称）"
        else:
            tip += "\n（留空恢复默认）"
        text, ok = QInputDialog.getText(self, "改账号名", tip + "：", text=current)
        if not ok:
            return
        self._labels.set_alias(uid, text)
        try:
            self._labels.save()
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._rebuild_accounts()
        self.detail.set_labels(self._labels)
        self._apply_filter()
        self._refresh_overview()

    # ============================================================ 筛选
    def _on_scope(self, key: str) -> None:
        self._scope = key
        # chip 的选中态必须跟着走：概览页的快捷入口是直接调 _on_scope 的，
        # 只改 _scope 会让「已删除」档亮着「本地可见」的绿胶囊
        btn = self._chips.get(key)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        self._apply_filter()

    def _on_unmapped_only(self, on: bool) -> None:
        """「⋯ → 只看未注册云端」：叠加在当前档位上的过滤，不改变档位本身。"""
        self._only_unmapped = bool(on)
        self._apply_filter()

    def _account_scoped(self, uid: str | None = None) -> list[inventory.SessionRecord]:
        """按账号过滤；uid 缺省取当前选中的账号。

        ⚠️ 两个判据要取**并集**，缺一不可：
          - `user_id` 是数据库里的归属，覆盖「该账号有、但已不在侧边栏」的历史记录
            （本机账号 22 条里只有 8 条还在侧边栏，只看快照会凭空少 14 条）
          - `sidebar_uids` 是「这个账号的界面上能看到」，覆盖跨账号可见的副本
        只用一个都会漏数据，用户切到「全部会话」时会发现条数对不上。
        """
        inv = self._inv
        if inv is None:
            return []
        rows = inv.records
        uid = self._current_uid if uid is None else uid
        if uid and uid != ALL_ACCOUNTS:
            rows = [r for r in rows if r.user_id == uid or uid in r.sidebar_uids]
        # 隐藏名单最后过滤：它是「用户已经决定不再看」的最终口径，
        # 任何档位的数字都不该再把它算进去（否则「已删除 26」清了又回来）。
        if self._hidden:
            rows = [r for r in rows if r.cid not in self._hidden]
        return rows

    def _filtered(self) -> list[inventory.SessionRecord]:
        rows = self._account_scoped()

        if self._scope != SCOPE_ALL:
            # 每一档都是同一个函数切出来的切片 ⇒ 各档数字相加必然等于「全部」。
            # 旧版每档各写一遍判据，中间那批会话没人认领，数字自然对不上。
            rows = [r for r in rows if _scope_bucket(r) == self._scope]
        # 「全部」就是全部 —— 含客户端已删除的那些。
        # 旧版在这里排除已删除，结果「全部 22」和「本地可见 22」一模一样，
        # 用户看着两个数字相同的档位，会以为筛选坏了。
        # 想看干净的用「本地可见」，想看死掉的用「已删除」，都各有其档。

        # 「未注册云端」是叠加过滤：在选定的档位里再筛出跨不了机的那批。
        # 它不参与分档，所以「全部」的条数不受影响。
        if self._only_unmapped:
            rows = [r for r in rows if _needs_cloud(r)]

        kw = self.txt_search.text().strip().lower()
        if kw:
            rows = [
                r
                for r in rows
                if kw in (r.title or "").lower()
                or kw in (r.cwd or "").lower()
                or kw in r.cid.lower()
            ]
        return rows

    def _apply_filter(self) -> None:
        rows = self._filtered()
        self._rows = rows
        self._selected_record = None
        # 筛选一变，右栏那条会话未必还在列表里 —— 详情必须跟着清，
        # 否则会对着一条已经不在当前视图里的会话做删除/恢复
        self.detail.set_record(None)
        # 列表的重建不会主动丢掉 _selected_cid，若它恰好还在新结果里，
        # 重建后会被重新点亮 —— 于是出现「行是绿的、右栏是空的」。先清掉。
        self.card_view.clear_selection()
        self.btn_purge_deleted.setVisible(self._scope == SCOPE_DELETED)

        if self._inv is not None:
            self._update_chip_labels()

        self._render_rows()
        self._update_summary(rows)

    def _update_chip_labels(self) -> None:
        base = self._account_scoped()
        counts = {key: 0 for key in _SCOPE_LABELS}
        counts[SCOPE_ALL] = len(base)
        for r in base:
            counts[_scope_bucket(r)] += 1
        for key, btn in self._chips.items():
            n = counts.get(key, 0)
            btn.setText(f"{_SCOPE_LABELS[key]} {n}")
            btn.setToolTip(_SCOPE_TIPS.get(key, ""))
            # 诊断档 0 条时收起：一个点了什么都没有的页签只会挤掉搜索框。
            # 当前选中的那档无论如何都留着，否则选完就"消失"了。
            btn.setVisible(key in _ALWAYS_SHOWN_SCOPES or n > 0 or key == self._scope)
        # 页签的显隐与数字都会改变「一行需要多宽」，不重算就还按旧判断换行
        bar = getattr(self, "tools_bar", None)
        if bar is not None:
            bar.sync()
        # 「已隐藏」的恢复入口：没有隐藏项时整项藏起来，不占菜单位
        act = getattr(self, "act_unhide", None)
        if act is not None:
            act.setText(f"显示已隐藏的会话（{len(self._hidden)}）")
            act.setVisible(bool(self._hidden))
        # 「只看未注册云端」：数量为 0 且没勾上时收起 —— 一个点进去空白的菜单项
        # 只会让人以为筛选坏了
        act_u = getattr(self, "act_unmapped", None)
        if act_u is not None:
            n_unmapped = _unmapped_count(base)
            act_u.setText(
                f"只看未注册云端（{n_unmapped}）" if n_unmapped else "只看未注册云端"
            )
            act_u.setVisible(n_unmapped > 0 or self._only_unmapped)
            if act_u.isChecked() != self._only_unmapped:
                act_u.blockSignals(True)
                act_u.setChecked(self._only_unmapped)
                act_u.blockSignals(False)

    def _update_summary(self, rows: list[inventory.SessionRecord]) -> None:
        inv = self._inv
        if inv is None:
            return

        # 副标题固定报「范围概览」，不随筛选变。
        # 旧版每档各给一句解释，结果「已删除」档一句话被省略号截断在半截，
        # 而用户真正想知道的是「这盘子有多大、我处在哪一部分」。
        scoped = self._account_scoped()
        # ★ 副标题的「可见」必须和页签的「本地可见」同源：都取同一套互斥分档。
        #   原先这里数 `visible_by_default`，而页签数的是「有正文 + 已注册云端 +
        #   可见」，于是同一屏出现「可见 13」与「本地可见 3」两个数字（v1.12.1 修）。
        buckets = {key: 0 for key in _SCOPE_LABELS}
        for r in scoped:
            buckets[_scope_bucket(r)] += 1
        visible_now = buckets[SCOPE_VISIBLE]
        in_bar_now = sum(1 for r in scoped if r.in_sidebar)
        pending_now = sum(1 for r in scoped if r.visible_by_default and not r.in_sidebar)
        dead_now = buckets[SCOPE_DELETED]
        unmapped_now = _unmapped_count(scoped)
        all_size = sum(r.jsonl_size or 0 for r in scoped)
        sub_bits = [f"盘点 {len(scoped)} 条", f"可见 {visible_now}"]
        if dead_now:
            sub_bits.append(f"已删除 {dead_now}")
        sub_bits.append(f"合计 {fmt_size(all_size)}")
        self._sess_sub = " · ".join(sub_bits)
        if self.pages.currentIndex() == NAV_SESSIONS:
            self._sync_header()

        # 页脚只放「这一屏 + 这一档」的数字，长尾诊断信息进 tooltip
        total_size = sum(r.jsonl_size or 0 for r in rows)
        with_body = sum(1 for r in rows if r.has_body)
        shown = len(rows)
        tail_note = "按对话 / 工作区分组，点组名可折叠"
        # 侧栏可以切到别的账号看数据，但盘点只做当前登录账号。
        # 那个账号没有数据时要说明原因，否则「共 0 条」看起来像盘点坏了。
        if self._viewing_unscanned_account() and not scoped:
            tail_note = "该账号本次未盘点：只读浏览已盘点数据，导出固定用当前登录账号"
        if self.stack.currentIndex() == VIEW_TABLE:
            tail_note = "表格视图，双击一行看详情"
        if self._scope == SCOPE_DELETED and rows and all(r.has_body for r in rows):
            tail_note += " · 全部可恢复"
        if self._hidden:
            # 隐藏条数必须排在最前：页脚是 ElidedLabel，尾巴上的字会被省略号吃掉；
            # 当前档清空时直接改成解释，否则「共 0 条」看起来像筛选坏了
            tail_note = (
                f"已隐藏 {len(self._hidden)} 条 · {tail_note}"
                if rows
                else f"已隐藏 {len(self._hidden)} 条云端会话（「⋯」里可恢复显示）"
            )
        if self._only_unmapped:
            # 叠加过滤没有页签可看，页脚必须自己说明 —— 否则「已删除 10 条」
            # 点进去是空的，看起来就像筛选坏了
            tail_note = (
                f"只看未注册云端 · {tail_note}"
                if rows
                else "当前档没有未注册云端的会话（「⋯」里可取消这个筛选）"
            )
        self.lbl_foot.setText(f"共 {shown} 条 · {tail_note}")
        # 「未注册云端」不再独占页签，但它是这套工具最要紧的一条警告
        # （换台机器就看不到），所以留在页脚右侧常驻
        tail_right = f"合计 {fmt_size(total_size)}　有正文 {with_body}"
        if unmapped_now:
            tail_right += f"　未注册云端 {unmapped_now}"
        self.lbl_foot_right.setText(tail_right)

        # 「库内」这个词只在真的盘了全库时才成立；只盘了当前账号却报库内条数，
        # 等于告诉用户一个本次没去数过的数字。
        if self._scan_uid:
            tail = f"已盘点当前账号 {inv.total} 条 · 可见 {visible_now} 条"
        else:
            tail = f"库内 {inv.total} 条 · 当前账号 {visible_now} 条可见"
        if pending_now:
            tail += f"（{in_bar_now} 已在侧边栏 / {pending_now} 待确认）"
        if dead_now:
            tail += f" · 已删除 {dead_now}"
        if unmapped_now:
            tail += f" · 未注册云端 {unmapped_now}"
        self.lbl_foot.setToolTip(f"{tail}\n{inv.cache_stats or '未使用指纹缓存'}")

    def _fill_table(self, rows: list[inventory.SessionRecord]) -> None:
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            fp = r.fingerprint
            lines = str(fp.line_count) if fp else ("—" if not r.has_body else "未计算")
            if r.is_deleted:
                status = "已删除"
            elif not r.has_body:
                status = "缺正文"
            elif not r.has_cloud_mapping:
                status = "未注册云端"
            else:
                status = "正常"
            account = "、".join(self._labels.name_of(u) for u in r.sidebar_uids) or self._labels.name_of(
                r.user_id
            )
            # 云端通道的完整值（convmsg:uuid）有 45 个字符，铺在列里会把标题挤没。
            # 对用户来说有意义的只是「注册了没有」，完整值放 tooltip。
            cloud_text = "已注册" if r.has_cloud_mapping else "未注册"
            values = [
                r.title,
                account,
                lines,
                fmt_size(r.jsonl_size),
                cloud_text,
                status,
                space_label(r.sidebar_space or r.cwd),
                fmt_when_exact(r.updated_at),
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == COL_STATUS:
                    if status == "缺正文":
                        item.setForeground(Qt.GlobalColor.darkRed)
                    elif status == "已删除":
                        item.setForeground(Qt.GlobalColor.gray)
                    elif status == "未注册云端":
                        item.setForeground(Qt.GlobalColor.darkYellow)
                if c == COL_CLOUD:
                    if not r.has_cloud_mapping:
                        item.setForeground(Qt.GlobalColor.gray)
                    item.setToolTip(r.cloud_channel or "未注册云端映射")
                if c == COL_TITLE:
                    item.setToolTip(r.title)
                if c == COL_CWD:
                    item.setToolTip(r.sidebar_space or r.cwd)
                self.table.setItem(i, c, item)

    def _toggle_view(self) -> None:
        """旧入口保留：外部按名字调用它切视图。"""
        self._set_table_view(self.stack.currentIndex() != VIEW_TABLE)

    def _set_table_view(self, on: bool) -> None:
        target = VIEW_TABLE if on else VIEW_CARD
        self.stack.setCurrentIndex(target)
        if self.act_view.isChecked() != bool(on):
            self.act_view.blockSignals(True)
            self.act_view.setChecked(bool(on))
            self.act_view.blockSignals(False)
        self._render_rows()
        self._update_summary(self._rows)

    def _on_card_selected(self, record) -> None:
        self._selected_record = record
        # 单击即送进右栏。三栏控制台里详情面板就是为「点一条看一条」存在的，
        # 要求双击才显示详情，等于让用户多点一次才拿到本来就该并排出现的信息
        self.detail.set_record(record)
        self.detail.setVisible(record is not None)

    def _on_group_toggled(self, node: str, collapsed: bool) -> None:
        """分组折叠变化：只落盘偏好，显隐由列表自己处理完了。

        `node` 是三级树的节点键（`对话` / `对话|2026-11-02-14-14-08` /
        `工作区|h:/project/demo`）。一级默认展开、二级默认收起，
        所以两级各存一份名单：收起名单管"一级被收起来了"，展开名单管
        "二级被点开过"。
        """
        if collapsed:
            self._collapsed_groups.add(node)
            self._expanded_groups.discard(node)
        else:
            self._collapsed_groups.discard(node)
            self._expanded_groups.add(node)
        uistate.save_collapsed(self._collapsed_groups)
        uistate.save_expanded(self._expanded_groups)
        # 收起时列表已经把选中项清空了，这里同步清掉详情面板指向的那条
        if collapsed and self.card_view.selected_record is None:
            self._selected_record = None
            self.detail.set_record(None)

    def _on_detail_closed(self) -> None:
        self._selected_record = None
        self.card_view.clear_selection()
        self.table.clearSelection()
        self.detail.setVisible(False)

    def _on_table_activated(self, index) -> None:
        row = index.row()
        if 0 <= row < len(self._rows):
            self._open_detail(self._rows[row])

    def _open_detail(self, record) -> None:
        """双击 = 把这条送进右栏，而不是弹一个挡住列表的详情框。"""
        if record is None:
            return
        self._selected_record = record
        self.detail.set_record(record)
        self.detail.setVisible(True)
        # 表格里双击时同步点亮列表里的同一行，两个视图的选中态不能各说各话
        self.card_view.select_cid(record.cid)

    # ============================================================ 物理删除
    def _purge_targets(self) -> list[inventory.SessionRecord]:
        """取**显式选中**的记录。

        ⚠️ 不能复用 _selected_records()：它在「没选中」时会回退成全部行，
           拿来删数据就是灾难。这里没选中就明确提示，绝不兜底。
        """
        if self.stack.currentIndex() == VIEW_TABLE:
            picked = {i.row() for i in self.table.selectionModel().selectedRows()}
            if not picked:
                QMessageBox.information(
                    self, "物理删除", "请先在表格中选中要删除的会话（可按住 Ctrl 多选）。"
                )
                return []
            return [self._rows[i] for i in sorted(picked) if i < len(self._rows)]
        if self._selected_record is not None:
            return [self._selected_record]
        QMessageBox.information(self, "物理删除", "请先选中一条会话。")
        return []

    def _purge_one(self, record) -> None:
        """详情面板里的单条物理清除。入口只在有选中对象时存在。"""
        if record is not None:
            self._start_purge([record])

    def _purge_selected(self) -> None:
        rows = self._purge_targets()
        if rows:
            self._start_purge(rows)

    def _purge_deleted_all(self) -> None:
        rows = [r for r in self._account_scoped() if r.is_deleted]
        if not rows:
            QMessageBox.information(self, "清除已删除", "当前范围内没有已删除的会话。")
            return
        self._start_purge(rows)

    def _start_purge(self, rows: list[inventory.SessionRecord], cloud: list | None = None) -> None:
        """物理删除的统一入口 —— 单条 / 勾选 / 全部已删除三个调用点共用。

        已删除会话必须分两拨：本地有正文的可以物理删；`transport=cloud` 的删了会被
        云端重新下发（实测 26 条回流：rowid 连续追加在表尾、删除标记原样带回），
        只能在本工具里隐藏。分流放在这里而不是调用点 —— 漏掉任何一个入口，
        用户看到的就是「删了又出来了」。
        """
        local, extra_cloud = purge_mod.split_cloud(rows)
        cloud = list(cloud or []) + extra_cloud
        if not local:
            self._hide_sessions(cloud)
            return

        try:
            plan = purge_mod.plan_purge(local)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法准备删除", f"{type(exc).__name__}: {exc}")
            return
        if plan.empty:
            if cloud:
                self._hide_sessions(cloud)
            else:
                QMessageBox.information(self, "物理删除", "这些会话在数据库里已经不存在了。")
            return

        dlg = PurgeDialog(plan, self, cloud_only=len(cloud))
        if dlg.exec() != PurgeDialog.DialogCode.Accepted:
            return
        plan.backup = dlg.backup
        # 删完接着处理云端那拨：留在 _on_purge_done 里一并交代，避免两个连续弹框
        self._pending_hide = list(cloud)

        if winproc.is_running():
            if not self._confirm_close(True, action="删除"):
                return
            self._pending_purge = plan
            self._set_busy(True)
            self.overlay.start("正在关闭 WorkBuddy…")
            self._closer = CloseClientWorker()
            self._closer.tick.connect(self._on_close_tick)
            self._closer.finished.connect(self._on_closed_purge)
            self._closer.failed.connect(self._on_failed)
            self._closer.start()
            return

        # 客户端本来就没开：别沿用上次导入留下的重启参数
        self._auto_restart = False
        self._client_exe = ""
        self._do_purge(plan)

    def _on_closed_purge(self, outcome) -> None:
        if not outcome.ok:
            self._set_busy(False)
            self.overlay.stop()
            QMessageBox.critical(
                self,
                "未能关闭 WorkBuddy",
                f"{outcome.message}\n\n已中止删除，数据库未做任何修改。",
            )
            return
        plan = self._pending_purge
        self._pending_purge = None
        if plan is None:
            return
        self._client_exe = getattr(outcome, "exe_path", "") or ""
        self._auto_restart = bool(self._client_exe)
        self._do_purge(plan)

    def _do_purge(self, plan) -> None:
        self._set_busy(True)
        self.overlay.start("正在删除会话…", "请勿关闭本窗口")
        self._purger = PurgeWorker(plan, self)
        self._purger.progressed.connect(self._on_progress)
        self._purger.finished.connect(self._on_purge_done)
        self._purger.failed.connect(self._on_failed)
        self._purger.start()

    def _on_purge_done(self, rep) -> None:
        self._set_busy(False)
        self.overlay.stop()
        restarted = self._restart_client()
        self._refresh_client_state()

        # 批量删除走内联横幅而不是第二个模态框：删完直接回列表看结果
        if self._pending_batch:
            self._pending_batch = False
            hidden_n = self._apply_hidden(self._pending_hide)
            self._pending_hide = []
            self._batch_backup_made = bool(rep.backup)
            self._show_batch_banner(self._batch_local_n, hidden_n)
            self._exit_select_mode()
            self._refresh()
            return

        lines = [rep.summary(), ""]
        if rep.backup is not None and not rep.backup.empty:
            lines.append(f"备份：{rep.backup.root}")
            lines.append(
                f"{len(rep.backup.files)} 个文件，"
                f"{rep.backup.size_bytes() / 1048576:.2f} MB"
            )
            lines.append("可在左侧「备份与包」查看或删除；删除备份后无法再恢复。")
            lines.append("")
        if rep.body_failed:
            lines.append("以下正文未能删除（可重试）：")
            for p, why in rep.body_failed[:8]:
                lines.append(f"  · {p.name} — {why}")
            lines.append("")
        if rep.failed:
            lines.append("失败项：")
            for key, why in rep.failed[:8]:
                lines.append(f"  · {key} — {why}")
            lines.append("")
        for warn in rep.warnings:
            lines.append(f"提示：{warn}")
            lines.append("")
        # 云端那拨在同一个结果框里交代：本地删不掉，只能隐藏
        hidden_n = self._apply_hidden(self._pending_hide)
        self._pending_hide = []
        if hidden_n:
            lines.append(
                f"另有 {hidden_n} 条云端会话已从本工具隐藏（本地删不掉，"
                "客户端重启会把它们重新拉回）。"
            )
            lines.append("「⋯ → 显示已隐藏的会话」可恢复；要真正删除请到客户端里删并等云端同步。")
            lines.append("")
        if restarted:
            lines.append("WorkBuddy 已重新启动。")
        elif not rep.failed:
            lines.append("请启动 WorkBuddy 后确认侧边栏状态。")

        QMessageBox.information(self, "物理删除完成", "\n".join(lines))
        self._refresh()

    # ============================================================ 隐藏云端残影
    def _apply_hidden(self, rows) -> int:
        """把 cid 记进隐藏名单并落盘，返回本次新增条数。

        名单只增不清：盘点范围可能只覆盖当前账号，此时别的账号的 cid 不在
        `records` 里，若按「库里还有没有」去修剪，会把它们误删、下次切账号又冒出来。
        """
        cids = {str(getattr(r, "cid", "") or "") for r in (rows or [])} - {""}
        new = cids - self._hidden
        if not new:
            return 0
        self._hidden |= new
        uistate.save_hidden(self._hidden)
        return len(new)

    def _hide_sessions(self, rows) -> None:
        """云端会话的「清除」：只在本工具里隐藏，不写库、不删正文。"""
        rows = [r for r in (rows or []) if getattr(r, "cid", "")]
        if not rows:
            return
        n = len(rows)
        box = QMessageBox(self)
        box.setWindowTitle("隐藏云端会话")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"这 {n} 条会话来自云端，物理删除会被客户端重新拉回。")
        box.setInformativeText(
            "将只在本工具里隐藏它们：不写数据库、不动正文，客户端侧边栏不受影响。\n"
            "随时可用「⋯ → 显示已隐藏的会话」恢复显示。\n\n"
            "要真正删除，请在客户端里删除这些会话并等云端同步完成 —— "
            "本地删行反而会抹掉客户端的删除标记，导致它们被重新下发。"
        )
        hide_btn = box.addButton(f"隐藏这 {n} 条", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not hide_btn:
            return
        self._apply_hidden(rows)
        self._apply_filter()

    def _unhide_all(self) -> None:
        n = len(self._hidden)
        if not n:
            return
        if (
            QMessageBox.question(
                self,
                "显示已隐藏的会话",
                f"把之前隐藏的 {n} 条会话重新显示出来？\n只影响本工具的列表，不动任何数据。",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._hidden.clear()
        uistate.save_hidden(set())
        self._apply_filter()

    # ============================================================ 批量删除
    def _toggle_select_mode(self) -> None:
        """工具栏「批量删除」按钮：进入/退出多选模式来回切。"""
        if self._selmode:
            self._exit_select_mode()
        else:
            self._enter_select_mode()

    def _enter_select_mode(self) -> None:
        # 批量删除基于卡片视图的勾选；表格视图的 Ctrl 多选是另一套，先进卡片视图
        self._set_table_view(False)
        self._selmode = True
        self.card_view.set_select_mode(True)
        self.btn_batch.setText("退出多选")
        self.batch_bar.setVisible(True)
        # 关闭详情面板：多选模式下点行只勾选，不会开详情，留着旧内容反而误导
        self.detail.set_record(None)
        self.detail.setVisible(False)

    def _exit_select_mode(self) -> None:
        if not self._selmode:
            return
        self._selmode = False
        self.card_view.set_select_mode(False)
        self.btn_batch.setText("批量删除")
        self.batch_bar.setVisible(False)

    def _on_batch_count(self, n: int) -> None:
        self.lbl_batch_count.setText(f"已选 {n} 项")
        self.btn_batch_del.setEnabled(n > 0)
        recs = self.card_view.checked_records()
        cloud = sum(1 for r in recs if getattr(r, "transport", "") == "cloud")
        if cloud:
            self.lbl_batch_cloud.setVisible(True)
            self.lbl_batch_cloud.setText(f"云端 {cloud} · 将隐藏")
        else:
            self.lbl_batch_cloud.setVisible(False)

    def _do_batch_delete(self) -> None:
        recs = self.card_view.checked_records()
        if not recs:
            return
        self._batch_delete(recs)

    def _batch_delete(self, records: list) -> None:
        """批量删除的统一入口：复用 _start_purge 的分流与 PurgeDialog。

        - 本地会话 → 物理删除（带关闭客户端 / 备份 / 分级确认）
        - 云端会话 → 只隐藏，不写库
        两者都在 _on_purge_done 里一次性交代，避免两个连续弹框。
        """
        local, cloud = purge_mod.split_cloud(records)
        self._batch_local_n = len(local)
        if not local:
            # 全是云端：直接隐藏 + 内联横幅，不弹第二个模态
            self._apply_hidden(cloud)
            self._show_batch_banner(0, len(cloud))
            self._exit_select_mode()
            self._apply_filter()
            return
        self._pending_batch = True
        self._start_purge(local, cloud=cloud)

    def _show_batch_banner(self, local_n: int, cloud_n: int) -> None:
        bits = []
        if local_n:
            bits.append(f"已彻底删除 {local_n} 条本地会话")
        if cloud_n:
            bits.append(f"隐藏 {cloud_n} 条云端会话")
        text = ("，".join(bits) if bits else "没有可删除的会话") + "。"
        if self._batch_backup_made:
            text += "（已备份到备份区）"
        self.lbl_batch_banner.setText(text)
        self.batch_banner.setVisible(True)

    # ============================================================ 恢复
    def _restore_one(self, record) -> None:
        """把一条软删除的会话恢复成「活着」。

        只有详情面板会调用它，而详情面板只在选中对象存在时才有内容 ——
        所以这里不需要「请先选中一条会话」这种空转提示。
        """
        if record is None:
            return
        try:
            plan = restore_mod.plan_restore([record])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法准备恢复", f"{type(exc).__name__}: {exc}")
            return
        if plan.empty:
            QMessageBox.information(
                self,
                "恢复会话",
                "这条会话在当前库里已经不是删除状态（可能已被客户端恢复）。\n"
                "重新盘点一次即可看到最新状态。",
            )
            return

        running = winproc.is_running()
        box = QMessageBox(self)
        box.setWindowTitle("恢复会话")
        box.setText(f"恢复「{record.title or '(未命名会话)'}」？")
        body = [
            "清掉删除标记，正文一个字都不动。",
            "写入前会先把会话库备份到 wbsync-backup，需要时能整体回滚。",
        ]
        if running:
            body.append("WorkBuddy 正在运行，恢复前必须先关掉它（写库会损坏全部会话）。")
        box.setInformativeText("\n".join(f"· {b}" for b in body))
        b_yes = box.addButton(
            "关闭并恢复" if running else "恢复", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_yes)
        box.exec()
        if box.clickedButton() is not b_yes:
            return

        if not running:
            self._auto_restart = False
            self._client_exe = ""
            self._do_restore(plan)
            return

        self._pending_restore = plan
        self._set_busy(True)
        self.overlay.start("正在关闭 WorkBuddy…")
        self._closer = CloseClientWorker()
        self._closer.tick.connect(self._on_close_tick)
        self._closer.finished.connect(self._on_closed_restore)
        self._closer.failed.connect(self._on_failed)
        self._closer.start()

    def _on_closed_restore(self, outcome) -> None:
        if not outcome.ok:
            self._set_busy(False)
            self.overlay.stop()
            QMessageBox.critical(
                self,
                "未能关闭 WorkBuddy",
                f"{outcome.message}\n\n已中止恢复，数据库未做任何修改。",
            )
            return
        plan = self._pending_restore
        self._pending_restore = None
        if plan is None:
            return
        self._client_exe = getattr(outcome, "exe_path", "") or ""
        self._auto_restart = bool(self._client_exe)
        self._do_restore(plan)

    def _do_restore(self, plan) -> None:
        self._set_busy(True)
        self.overlay.start("正在恢复会话…", "先备份会话库，再清删除标记")
        self._restorer = RestoreWorker(plan, self)
        self._restorer.finished.connect(self._on_restore_done)
        self._restorer.failed.connect(self._on_failed)
        self._restorer.start()

    def _on_restore_done(self, rep) -> None:
        self._set_busy(False)
        self.overlay.stop()
        restarted = self._restart_client()
        self._refresh_client_state()

        lines = [rep.summary(), ""]
        if rep.backup is not None and not rep.backup.empty:
            lines.append(f"备份：{rep.backup.root}")
        if rep.failed:
            lines.append("")
            lines.append("失败项：")
            for key, why in rep.failed[:8]:
                lines.append(f"  · {key} — {why}")
        for warn in rep.warnings:
            lines.append("")
            lines.append(f"提示：{warn}")
        if restarted:
            lines.append("")
            lines.append("WorkBuddy 已重新启动。")

        QMessageBox.information(self, "恢复完成", "\n".join(lines))
        self._refresh()

    # ============================================================ 客户端状态
    def _refresh_client_state(self) -> None:
        procs = guard.running_client_processes()
        running = bool(procs)
        if running:
            self.lbl_client.setText(
                f'<span style="color:{SIDE_DOT};">●</span>&nbsp;&nbsp;客户端运行中'
            )
            self.lbl_client.setObjectName("SideStatus")
            self.lbl_client.setToolTip("导入写库前必须完全退出 WorkBuddy")
        else:
            self.lbl_client.setText(
                f'<span style="color:#93A8A1;">●</span>&nbsp;&nbsp;客户端未运行'
            )
            self.lbl_client.setObjectName("SideStatusOff")
            self.lbl_client.setToolTip("可以安全写入")
        self.lbl_client.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_client.style().unpolish(self.lbl_client)
        self.lbl_client.style().polish(self.lbl_client)
        self.header.set_running(running, "运行中" if running else "离线")

    # ============================================================ 导出 / 导入
    def _selected_records(self) -> list[inventory.SessionRecord]:
        if self.stack.currentIndex() == VIEW_TABLE:
            picked = {i.row() for i in self.table.selectionModel().selectedRows()}
            if picked:
                return [self._rows[i] for i in sorted(picked) if i < len(self._rows)]
        elif self._selected_record is not None:
            return [self._selected_record]
        return list(self._rows)

    def _set_busy(self, busy: bool) -> None:
        # 账号入口也在忙时禁用：盘点/写库期间切账号会让界面状态和手里的数据打架
        for btn in (
            self.btn_refresh,
            self.btn_export_pkg,
            self.btn_import_pkg,
            self.btn_more,
            self.btn_purge_deleted,
            self.btn_scan_menu,
        ):
            btn.setEnabled(not busy)
        self.act_view.setEnabled(not busy)

    # ---------------------------------------------------------- 同步包
    def _current_alias(self) -> tuple[str, str]:
        """(别名, uid)。导出恒取当前登录账号，与侧栏选中谁无关。"""
        uid = accounts_mod.detect_primary_uid(snapshot=self._labels.snapshot)
        if not uid:
            uid = self._labels.primary_uid or ""
        name = self._labels.name_of(uid) if uid else "workbuddy"
        return name, uid

    def _export_package(self) -> None:
        if self._inv is None:
            QMessageBox.information(self, "导出全部同步包", "还没盘点完，请稍候。")
            return

        # 配置页可能自上次打开后没刷新过，导出前确保拿到最新项
        self.config_page.reload()

        # ★ 恒取当前登录账号：侧栏可以切到别的账号只读浏览，
        #   但导出的口径不受它影响（否则「我选的是 B，怎么导出的是 A」）。
        alias, uid = self._current_alias()
        scoped = self._account_scoped(uid or None)
        records = [r for r in scoped if not r.is_deleted and r.has_body]

        # 含凭据的项固定排除：整包没有勾选界面，也就没有那道显式确认
        all_items = list(self.config_page.items())
        items = [i for i in all_items if not i.sensitive]
        excluded = len(all_items) - len(items)

        # 候选工作空间记忆：从要导出的会话 cwd 里找带 .workbuddy/memory 的目录。
        # 只读目录列文件，不读正文，开销很小。
        try:
            mem_candidates = memory_pack.discover_from_records(records)
        except OSError:
            mem_candidates = []

        dlg = ExportDialog(
            records,
            items,
            mem_candidates,
            alias,
            target_uid=uid,
            excluded_sensitive=excluded,
            parent=self,
        )
        if dlg.exec() != ExportDialog.DialogCode.Accepted:
            return
        opt = dlg.options
        if opt is None or opt.empty:
            QMessageBox.information(
                self, "导出全部同步包", "这个账号下没有可导出的内容。"
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出全部同步包", dlg.suggested_name, "同步包 (*.wbsync.zip)"
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += pack_mod.SUFFIX

        self._set_busy(True)
        mem_n = len(opt.memory_workspaces)
        self.overlay.start(
            "正在打包…",
            f"会话 {len(opt.records)} 条 · 配置 {len(opt.config_items)} 项"
            + (f" · 记忆 {mem_n} 个工作空间" if mem_n else ""),
        )
        self._sync_exporter = SyncExportWorker(
            path, opt.records, opt.config_items, alias, uid or "", self,
            memory_workspaces=opt.memory_workspaces,
        )
        self._sync_exporter.progressed.connect(self._on_progress)
        self._sync_exporter.finished.connect(self._on_package_exported)
        self._sync_exporter.failed.connect(self._on_failed)
        self._sync_exporter.start()

    def _on_package_exported(self, path: str) -> None:
        self._set_busy(False)
        self.overlay.stop()
        p = Path(path)
        size = p.stat().st_size / 1048576 if p.is_file() else 0
        QMessageBox.information(
            self,
            "导出完成",
            f"已导出到：\n{path}\n\n大小 {size:.2f} MB\n"
            "把它拷到另一台电脑，用「导入同步包」读入即可一次完成会话、配置与记忆"
            "（记忆按更新时间保留最新）。",
        )

    def _import_package(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择同步包",
            "",
            "同步包 (*.wbsync.zip *.zip);;所有文件 (*.*)",
        )
        if not path:
            return
        self._set_busy(True)
        self.overlay.start("正在读取同步包…", "并与本机逐条比对")
        self._sync_planner = SyncPlanWorker(path, self)
        self._sync_planner.finished.connect(self._on_sync_plan_done)
        self._sync_planner.failed.connect(self._on_failed)
        self._sync_planner.start()

    def _on_sync_plan_done(self, pkg, s_acts, c_acts, m_acts=None) -> None:
        self._set_busy(False)
        self.overlay.stop()
        self._pkg = pkg
        m_acts = list(m_acts or [])

        if not s_acts and not c_acts and not m_acts:
            QMessageBox.information(self, "导入同步包", "这个包里没有可比对的内容。")
            return

        remaps = (
            importer.plan_cwd_remaps(pkg.sessions, actions=s_acts)
            if pkg.has_sessions
            else []
        )
        dlg = SyncPlanDialog(
            pkg, s_acts, c_acts, cwd_remaps=remaps, parent=self, memory_actions=m_acts
        )
        if dlg.exec() != SyncPlanDialog.DialogCode.Accepted:
            return
        # ★ 只拿「勾中的」动作：分节开关与逐项勾选都在对话框里，
        #   这里再按 actions 全量执行就等于把它们当摆设了。
        s_sel = dlg.session_actions()
        c_sel = dlg.config_actions()
        m_sel = dlg.memory_actions()
        if not s_sel and not c_sel and not m_sel:
            QMessageBox.information(
                self, "导入同步包", "没有勾选任何内容，未做任何修改。"
            )
            return
        self._run_sync_import(
            s_sel, c_sel, dlg.config_choices(), False, dlg.auto_restart,
            dlg.cwd_choices(), dlg.include_duplicates, dlg.memory_path_map(), m_sel,
        )

    def _run_sync_import(
        self,
        s_acts,
        c_acts,
        choices,
        dry_run: bool,
        auto_restart: bool,
        cwd_choices=None,
        include_duplicates: bool = False,
        memory_map=None,
        memory_acts=None,
    ) -> None:
        if self._pkg is None:
            return
        self._pending_sync = (s_acts, c_acts, choices, dry_run, auto_restart, cwd_choices or {})
        # 与 _run_import 同一个原因：_pending_sync 是按固定元数解包的，
        # 往里加元素会在关窗回调里炸；用实例属性带过去。
        self._import_include_dups = bool(include_duplicates)
        # 同步包入口不做「拿包内覆盖本机」：对话框没有取舍界面，
        # 与 importer.downgrade_div_overwrite 的口径保持一致。
        self._import_allow_div_overwrite = False
        self._sync_memory_map = dict(memory_map or {})
        # None 表示「整节执行」；列表表示只写这些文件
        self._sync_m_acts = None if memory_acts is None else list(memory_acts)

        # 只有含会话才必须关客户端；纯配置包直接写，避免多关一次
        needs_close = bool(s_acts) and not dry_run

        if not needs_close:
            self._do_sync_import(s_acts, c_acts, choices, dry_run, False, "", cwd_choices)
            return

        if not winproc.is_running():
            # 同 _run_import：路径要能落到「关着也能找到」的那条链上，不能只看进程
            exe = winproc.resolve_client_exe(remembered=lambda: uistate.load_client_exe())
            self._do_sync_import(
                s_acts, c_acts, choices, False, auto_restart, exe, cwd_choices
            )
            return

        if not self._confirm_close(auto_restart):
            return

        self._set_busy(True)
        self.overlay.start("正在关闭 WorkBuddy…")
        self._closer = CloseClientWorker(self)
        self._closer.tick.connect(self._on_close_tick)
        self._closer.finished.connect(self._on_closed_sync)
        self._closer.failed.connect(self._on_failed)
        self._closer.start()

    def _on_closed_sync(self, outcome) -> None:
        if not outcome.ok:
            self._set_busy(False)
            self.overlay.stop()
            QMessageBox.critical(
                self,
                "未能关闭 WorkBuddy",
                f"{outcome.message}\n\n"
                "已中止导入，数据库未做任何修改。\n"
                "请手动退出 WorkBuddy 后重试。",
            )
            return
        s_acts, c_acts, choices, dry_run, auto, cwd_choices = self._pending_sync
        self._do_sync_import(s_acts, c_acts, choices, dry_run, auto, outcome.exe_path, cwd_choices)

    def _do_sync_import(self, s_acts, c_acts, choices, dry_run, auto_restart, exe_path, cwd_choices=None) -> None:
        self._client_exe = exe_path
        self._auto_restart = auto_restart
        if exe_path:
            uistate.save_client_exe(exe_path)
        self._sync_c_acts = list(c_acts)
        self._sync_choices = dict(choices)
        self._sync_dry = dry_run
        self._sync_cwd_remaps = cwd_choices or {}
        self._sync_session_report = None
        self._sync_config_report = None
        self._sync_memory_report = None

        self._set_busy(True)
        if not s_acts:
            # 纯配置包：没有会话可写，直接进配置阶段
            self._on_sync_sessions_done(None)
            return

        self.overlay.start("正在写入会话…", "请勿关闭本窗口")
        self._importer = ImportWorker(
            s_acts,
            self._pkg.sessions,
            dry_run,
            self,
            cwd_remaps=self._sync_cwd_remaps,
            include_duplicates=getattr(self, "_import_include_dups", False),
        )
        self._importer.progressed.connect(self._on_progress)
        self._importer.finished.connect(self._on_sync_sessions_done)
        self._importer.failed.connect(self._on_import_failed)
        self._importer.start()

    def _on_sync_sessions_done(self, rep) -> None:
        """会话阶段结束。无论成败都继续配置阶段 —— 两节互不牵连。"""
        self._sync_session_report = rep

        # 导入成功的映射记进本地记忆：下次再同步同一源机直接带出
        if rep is not None and not self._sync_dry and self._sync_cwd_remaps:
            for src, new in self._sync_cwd_remaps.items():
                cwd_map.remember(src, new)

        if not self._sync_c_acts:
            self._on_sync_config_done(None)
            return

        self.overlay.start("正在写入配置…", "会话已处理完，继续写配置")
        self._cfg_importer = ConfigImportWorker(
            self._sync_c_acts,
            self._pkg.config,
            self._sync_dry,
            self._sync_choices,
            self,
        )
        self._cfg_importer.finished.connect(self._on_sync_config_done)
        self._cfg_importer.failed.connect(self._on_sync_config_failed)
        self._cfg_importer.start()

    def _on_sync_config_failed(self, msg: str) -> None:
        """配置阶段异常：会话可能已经写成功，必须分开说明，不能一句「失败」带过。"""
        self._set_busy(False)
        self.overlay.stop()
        restarted = self._restart_client() if not self._sync_dry else False
        self._refresh_client_state()

        sr = self._sync_session_report
        head = "配置写入失败"
        lines = []
        if sr is not None and sr.applied:
            lines.append(f"会话部分已写入 {len(sr.applied)} 条，未受影响。")
        lines.append(f"配置未写入：{msg}")
        if restarted:
            lines.append("WorkBuddy 已重新启动。")
        QMessageBox.warning(self, head, "\n".join(lines))
        self._refresh()

    def _run_memory_stage(self) -> None:
        """写工作空间记忆。纯文件 IO、量小，同步执行；失败不拖垮会话/配置。"""
        self._sync_memory_report = None
        pkg = self._pkg
        if pkg is None or not pkg.has_memory:
            return
        try:
            path_map = getattr(self, "_sync_memory_map", {}) or {}
            acts = memory_pack.plan_import(pkg.memory, path_map)
            selected = getattr(self, "_sync_m_acts", None)
            if selected is not None:
                # 只写勾中的文件。用 (工作空间, 文件名) 做键：重新 plan 之后
                # 目标路径可能已被 path_map 改过，但这两个字段是稳定的。
                keep = {(a.workspace.wid, a.file.name) for a in selected}
                acts = [a for a in acts if (a.workspace.wid, a.file.name) in keep]
            if self._sync_dry:
                self._sync_memory_report = memory_pack.MemReport(actions=acts, dry_run=True)
            else:
                self.overlay.start("正在写入工作空间记忆…", "按更新时间保留最新")
                self._sync_memory_report = memory_pack.execute(acts, pkg.memory)
                self._remember_memory_map(acts)
        except Exception as exc:  # noqa: BLE001
            self._sync_memory_report = memory_pack.MemReport(
                failed=[("memory", f"{type(exc).__name__}: {exc}")]
            )

    def _remember_memory_map(self, acts) -> None:
        """见模块级 `_remember_memory_map`（放在外面是为了能脱离窗口单测）。"""
        _remember_memory_map(acts)

    def _on_sync_config_done(self, rep) -> None:
        self._sync_config_report = rep
        self._run_memory_stage()
        self._set_busy(False)
        self.overlay.stop()

        dry = self._sync_dry
        sr = self._sync_session_report
        cr = self._sync_config_report
        mr = self._sync_memory_report
        wrote = (sr is not None and sr.applied) or (cr is not None and cr.applied)

        # 即便有失败项也要拉起客户端：不能让人停在「客户端没了」的状态
        restarted = self._restart_client() if (wrote and not dry) else False
        self._refresh_client_state()

        head = "演练完成（未写入任何数据）" if dry else "同步完成"
        lines: list[str] = []

        if sr is not None:
            lines.append(f"【会话】{sr.summary()}")
            if sr.failed:
                lines.append(f"  失败 {len(sr.failed)} 项")
        if cr is not None:
            lines.append(f"【配置】{cr.summary()}")
            if cr.failed:
                lines.append(f"  失败 {len(cr.failed)} 项")
        if mr is not None:
            lines.append(f"【记忆】{mr.summary()}")
            if mr.failed:
                lines.append(f"  失败 {len(mr.failed)} 项")
        lines.append("")

        backups: list[str] = []
        if sr is not None and sr.backup is not None and not sr.backup.empty:
            backups.append(
                f"· 会话备份：{sr.backup.root}"
                f"（{len(sr.backup.files)} 个文件，{sr.backup.size_bytes() / 1048576:.2f} MB）"
            )
        if cr is not None and cr.backup is not None and not cr.backup.empty:
            backups.append(f"· 配置备份：{cr.backup.root}")
        if mr is not None and getattr(mr.backup, "files", None):
            backups.append(f"· 记忆备份：{mr.backup.root}")
        if backups:
            lines.append("已备份：")
            lines.extend(backups)
            lines.append("备份可在左侧「备份与包」查看与清理。")
            lines.append("")

        if sr is not None and sr.failed:
            lines.append("会话失败项：")
            for cid, why in sr.failed[:8]:
                # 用标题而不是 uuid：用户要判断「要不要重导这一条」，
                # 一串 id 帮不上任何忙（实测反馈原话是「名称也看不懂」）
                lines.append(f"  · 「{sr.label_of(cid)}」 — {why}")
            lines.append("")
        if cr is not None and cr.failed:
            lines.append("配置失败项：")
            for key, why in cr.failed[:8]:
                lines.append(f"  · {key} — {why}")
            lines.append("")

        warns = getattr(sr, "warnings", ()) if sr is not None else ()
        for warn in warns or ():
            lines.append(f"提示：{warn}")
            lines.append("")

        if wrote and not dry:
            if restarted:
                lines.append(
                    f"WorkBuddy 已重新启动，并回到「{self._account_after_restart()}」，"
                    "稍候会话就会出现在侧边栏，配置同时生效。"
                )
            elif not self._auto_restart:
                lines.append(
                    "本次未勾选「导入完成后自动重启」—— 请手动启动 WorkBuddy，"
                    "导入的会话与配置才会生效。"
                )
            elif self._client_exe:
                lines.append(f"自动启动失败，请手动打开：\n{self._client_exe}")
            elif cr is not None and cr.applied:
                lines.append(
                    "未能定位 WorkBuddy.exe，无法自动启动 —— 配置已写入，"
                    "手动启动客户端后生效。"
                )
            else:
                lines.append(
                    "未能定位 WorkBuddy.exe，无法自动启动 —— 请手动打开客户端，"
                    "导入的会话已经写进库里，不会丢。"
                )

        QMessageBox.information(self, head, "\n".join(lines))
        if wrote and not dry:
            self._refresh()

    # ---------------------------------------------------------- 其他入口
    def _open_backup_manager(self) -> None:
        self._go(NAV_BACKUP)

    def _open_data_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .. import paths

        d = paths.session_data_dir()
        if d.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _import_bundle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择会话包", "", "会话包 (*.wbsync.zip *.zip);;所有文件 (*.*)"
        )
        if not path:
            return
        self._set_busy(True)
        self.overlay.start("正在读取会话包…", "并与本机逐条比对")
        self._planner = PlanWorker(path)
        self._planner.finished.connect(self._on_plan_done)
        self._planner.failed.connect(self._on_failed)
        self._planner.start()

    def _on_plan_done(self, b, acts) -> None:
        self._set_busy(False)
        self.overlay.stop()
        self._bundle = b
        remaps = importer.plan_cwd_remaps(b, actions=acts)
        dlg = MergeDialog(b, acts, cwd_remaps=remaps, parent=self)
        if dlg.exec() != MergeDialog.DialogCode.Accepted:
            return
        # 分叉取舍已经在对话框里就地改到 acts 上（importer.apply_div_choice），
        # 这里不需要再传一遍，避免两处各算一次。
        self._run_import(
            acts,
            dlg.dry_run,
            dlg.auto_restart,
            dlg.cwd_choices(),
            dlg.include_duplicates,
            # 用户在合并面板里逐条看过取舍，覆盖才算被授权
            allow_div_overwrite=True,
        )

    # ---------------------------------------------------------- 导入编排
    def _account_after_restart(self) -> str:
        """重启后会进入的账号 —— 与 importer.detect_local_uid 同一判据（当前登录账号）。

        客户端不支持由外部切换登录账号（登录态是 at-rest 信封，切号走服务端接口），
        所以文案只说「回到」，不承诺「切到任意账号」。
        """
        uid = self._labels.primary_uid
        return self._labels.display(uid) if uid else "当前登录账号"

    def _confirm_close(self, auto_restart: bool, action: str = "导入") -> bool:
        """写库前必须关客户端，代价要摊开讲清楚再让用户点头。"""
        box = QMessageBox(self)
        box.setWindowTitle("需要先关闭 WorkBuddy")
        box.setText(f"{action}会话数据前必须关闭 WorkBuddy。")
        box.setInformativeText(
            "关闭会导致：\n"
            "· 未发送的消息输入丢失\n"
            "· 正在进行的对话中断（包括当前这个对话）\n"
            "· 后台任务被中断\n\n"
            + (
                f"{action}完成后会自动重启 WorkBuddy，并回到「{self._account_after_restart()}」。"
                if auto_restart
                else f"{action}完成后需要你手动启动 WorkBuddy。"
            )
        )
        b_yes = box.addButton(f"关闭并{action}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_yes)
        box.exec()
        return box.clickedButton() is b_yes

    def _run_import(
        self,
        acts,
        dry_run: bool,
        auto_restart: bool = True,
        cwd_choices=None,
        include_duplicates: bool = False,
        allow_div_overwrite: bool = False,
    ) -> None:
        if self._bundle is None:
            return
        self._pending_import = (acts, dry_run, auto_restart, cwd_choices or {})
        # 用实例属性而不是塞进 _pending_import 元组：那个元组在 _on_closed 里
        # 按 4 元解包，加一个元素会在关窗回调里炸掉（导入已经开始了）。
        self._import_include_dups = bool(include_duplicates)
        self._import_allow_div_overwrite = bool(allow_div_overwrite)

        if dry_run:
            self._do_import(acts, dry_run, "", False, cwd_choices)
            return

        if not winproc.is_running():
            # 客户端没开着就不用关，直接写。但**路径仍要解析**：
            # 这里原先用 primary_exe()，而它只认正在运行的进程 —— 客户端一关就返回空串，
            # 于是勾了「导入完成后自动重启」也静默不启动，对话框里还一句解释都没有。
            # resolve_client_exe 会接着查「上次记住的路径 → 注册表 → 默认安装目录」。
            exe = winproc.resolve_client_exe(remembered=lambda: uistate.load_client_exe())
            self._do_import(acts, dry_run, exe, auto_restart, cwd_choices)
            return

        if not self._confirm_close(auto_restart):
            return

        self._set_busy(True)
        self.overlay.start("正在关闭 WorkBuddy…")
        self._closer = CloseClientWorker()
        self._closer.tick.connect(self._on_close_tick)
        self._closer.finished.connect(self._on_closed)
        self._closer.failed.connect(self._on_failed)
        self._closer.start()

    def _on_close_tick(self, note: str) -> None:
        self.overlay.set_note(note)

    def _on_closed(self, outcome) -> None:
        if not outcome.ok:
            self._set_busy(False)
            self.overlay.stop()
            QMessageBox.critical(
                self,
                "未能关闭 WorkBuddy",
                f"{outcome.message}\n\n"
                "已中止导入，数据库未做任何修改。\n"
                "请手动退出 WorkBuddy 后重试。",
            )
            return
        acts, dry_run, auto, cwd_choices = self._pending_import
        self._do_import(acts, dry_run, outcome.exe_path, auto, cwd_choices)

    def _do_import(self, acts, dry_run: bool, exe_path: str, auto_restart: bool, cwd_choices=None) -> None:
        self._client_exe = exe_path
        self._auto_restart = auto_restart
        # 记住路径：下次客户端没开时也能把它拉起来（这是「自动重启」唯一的可靠来源）
        if exe_path:
            uistate.save_client_exe(exe_path)
        self._import_cwd_remaps = cwd_choices or {}
        self._set_busy(True)
        self.overlay.start("正在写入会话…", "请勿关闭本窗口")
        self._importer = ImportWorker(
            acts,
            self._bundle,
            dry_run,
            cwd_remaps=self._import_cwd_remaps,
            include_duplicates=getattr(self, "_import_include_dups", False),
            allow_div_overwrite=getattr(self, "_import_allow_div_overwrite", False),
        )
        self._importer.progressed.connect(self._on_progress)
        self._importer.finished.connect(self._on_import_done)
        self._importer.failed.connect(self._on_import_failed)
        self._importer.start()

    def _on_import_failed(self, msg: str) -> None:
        """写入阶段异常：仍要尝试拉起客户端，不能让人卡在「数据没写成、客户端也没了」。"""
        self._restart_client()
        self._on_failed(msg)

    def _restart_client(self) -> bool:
        if not self._auto_restart:
            return False
        if not self._client_exe:
            # 关客户端之前没取到路径（或用户本来就是关着的）：再找一次，别直接放弃
            self._client_exe = winproc.resolve_client_exe(
                remembered=lambda: uistate.load_client_exe()
            )
        if not self._client_exe:
            return False
        ok = closeflow.restart(self._client_exe)
        if ok:
            # 客户端加载要十几秒，稍后再刷新状态徽章
            QTimer.singleShot(2500, self._refresh_client_state)
        return ok

    def _on_import_done(self, rep) -> None:
        self._set_busy(False)
        self.overlay.stop()

        # 导入成功的映射记进本地记忆：下次再同步同一源机直接带出
        if not rep.dry_run and getattr(self, "_import_cwd_remaps", None):
            for src, new in self._import_cwd_remaps.items():
                cwd_map.remember(src, new)

        # 先拉起客户端：即便有失败项，也不能让人停在「客户端没了」的状态
        restarted = self._restart_client() if not rep.dry_run else False
        self._refresh_client_state()

        head = "演练完成（未写入任何数据）" if rep.dry_run else "导入完成"
        lines = [rep.summary(), ""]
        if rep.backup is not None and not rep.backup.empty:
            lines.append(f"备份位置：{rep.backup.root}")
            lines.append(f"备份 {len(rep.backup.files)} 个文件，共 {rep.backup.size_bytes() / 1048576:.2f} MB")
            lines.append("")
        if getattr(rep, "merge_backup", None) is not None and not rep.merge_backup.empty:
            # 分叉取舍覆盖掉的正文单独一份：只回滚这几份，不会连累同批新增的会话
            lines.append(f"分叉取舍备份：{rep.merge_backup.root}")
            lines.append(
                "被覆盖的那几份正文都在这里。要还原：先关闭 WorkBuddy，"
                "再把里面的 .jsonl 拷回 .workbuddy\\projects\\<原目录>\\"
                "（文件名就是会话 id，原目录名见备份前的 projects 目录）。"
            )
            lines.append("")
        if rep.failed:
            lines.append(f"失败 {len(rep.failed)} 项：")
            for cid, why in rep.failed[:10]:
                lines.append(f"  · {cid[:20]} — {why}")
            lines.append("")
        for warn in getattr(rep, "warnings", ()) or ():
            lines.append(f"提示：{warn}")
            lines.append("")
        if not rep.dry_run and rep.applied:
            # 四种结局都要说清楚 —— 尤其是「没重启」，不说就等于让用户以为工具骗了他
            if restarted:
                lines.append(
                    f"WorkBuddy 已重新启动，并回到「{self._account_after_restart()}」，"
                    "稍候会话就会出现在侧边栏。"
                )
            elif not self._auto_restart:
                lines.append(
                    "本次未勾选「导入完成后自动重启」—— 请手动启动 WorkBuddy，"
                    "导入的会话才会出现在侧边栏。"
                )
            elif self._client_exe:
                lines.append(f"自动启动失败，请手动打开：\n{self._client_exe}")
            else:
                lines.append(
                    "未能定位 WorkBuddy.exe，无法自动启动 —— 请手动打开客户端，"
                    "导入的会话已经写进库里，不会丢。"
                )
        # 分叉取舍覆盖了正文时，给一个直达备份目录的入口。
        # 「一键回滚」不提供：还原是写路径，得走关客户端那套铁律，混在这个
        # 提示框里做等于绕开守卫。
        merge_bak = getattr(rep, "merge_backup", None)
        if merge_bak is not None and not merge_bak.empty:
            box = QMessageBox(self)
            box.setWindowTitle(head)
            box.setText("\n".join(lines))
            btn_dir = box.addButton(
                "打开取舍备份目录", QMessageBox.ButtonRole.ActionRole
            )
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is btn_dir:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices

                QDesktopServices.openUrl(QUrl.fromLocalFile(str(merge_bak.root)))
        else:
            QMessageBox.information(self, head, "\n".join(lines))
        if not rep.dry_run and rep.applied:
            self._refresh()

    def _on_failed(self, msg: str) -> None:
        self._set_busy(False)
        self.overlay.stop()
        QMessageBox.critical(self, "操作失败", msg)

    # ============================================================ 生命周期
    def showEvent(self, event) -> None:
        super().showEvent(event)
        # resize 只设客户区，标题栏另算 —— 必须等窗口真正显示后按真实边框复核一次，
        # 否则「808 客户区 + 31 标题栏」在 816 高的可用区里仍会切掉底部。
        winsize.enforce(self)
        # 只有真正显示过的窗口才值得记住几何：自检里也会 new MainWindow()，
        # 没显示就 close 会把「算出来的尺寸」当用户偏好写进去。
        self._geometry_seen = True
        self._refresh_client_state()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """退出前记下窗口几何 —— 用户调小过一次，下次不该再撑回设计稿。"""
        if getattr(self, "_geometry_seen", False):
            winsize.save(self)
        super().closeEvent(event)
