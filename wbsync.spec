# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 单文件免安装 exe。

## 体积是怎么压下来的

PySide6 完整安装 656 MB，但本工具只用到 QtCore / QtGui / QtWidgets。
大头全是白带的：
  - WebEngine（Chromium）约 300 MB —— 本工具不显示网页
  - Addons（QtMultimedia / QtCharts / Qt3D …）约 200 MB —— 一个都用不到
  - Qt Qml / Quick 运行时 —— 纯 Widgets 程序不需要

所以这里做了三层裁剪：
  1. excludes 排除模块（PyInstaller 不分析、不收集）
  2. 过滤 Qt 插件与翻译文件（translations/*.qm 有数 MB，界面全中文自绘）
  3. onefile + UPX + optimize=2（去 docstring）

## 必须保留的东西

`platforms/qwindows.dll` 是 Qt 的窗口平台插件，删掉它程序会直接启动失败
（报 "could not find or load the Qt platform plugin"）。
`styles/qmodernwindowsstyle.dll` 决定原生外观，也保留。
"""
import os
import re
import sys

block_cipher = None

# ---------------------------------------------------------------- 版本资源
# 版本号唯一来源是 wbsync/__init__.py 的 __version__。
# 这里用正则读文件而不是 import，避免 spec 执行阶段因包路径/依赖问题失败。
def _read_version() -> str:
    init = os.path.join(SPECPATH, "wbsync", "__init__.py")
    try:
        with open(init, encoding="utf-8") as f:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    print("!! 警告：未能读到 wbsync/__init__.py 的 __version__，将标记为 0.0.0",
          file=sys.stderr)
    return "0.0.0"


APP_VERSION = _read_version()

# FixedFileInfo 要 4 段整数元组，容忍 1.0 / 1.0.0 这类写法
_nums = re.findall(r"\d+", APP_VERSION)
_ver4 = tuple(int(x) for x in (_nums + ["0"] * 4)[:4])

# PyInstaller 会解析该文件并写入 PE 的 VS_VERSIONINFO 资源，
# 效果是 exe 右键属性里能看到版本号 —— 发 Release 时用来核对下载到的是哪一版。
_version_file = os.path.join(SPECPATH, "build", "wbsync_version_info.txt")
os.makedirs(os.path.dirname(_version_file), exist_ok=True)
with open(_version_file, "w", encoding="utf-8") as _vf:
    _vf.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_ver4},
    prodvers={_ver4},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0',
        [StringStruct('CompanyName', 'Buddy-atlas'),
         StringStruct('FileDescription', 'Buddy-atlas 会话与配置同步'),
         StringStruct('FileVersion', '{APP_VERSION}'),
         StringStruct('InternalName', 'Buddy-atlas'),
         StringStruct('OriginalFilename', 'Buddy-atlas.exe'),
         StringStruct('ProductName', 'Buddy-atlas'),
         StringStruct('ProductVersion', '{APP_VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

print(f"-- 版本资源：{APP_VERSION} -> {_version_file}", file=sys.stderr)

# exe 文件的图标（资源管理器里看到的那个）。和运行时窗口图标是两件事：
# 这里不设，exe 显示的是 PyInstaller 默认图标；datas 里那份则负责窗口与任务栏。
_icon_file = os.path.join(SPECPATH, "wbsync", "assets", "appicon.ico")
if not os.path.isfile(_icon_file):
    print(f"!! 警告：找不到 {_icon_file}，exe 将使用默认图标", file=sys.stderr)
    _icon_file = None


# ---------------------------------------------------------------- 排除清单

# 标准库里用不到的部分
# http / urllib.request / email / ssl 这一组不能排除：检查更新要用标准库
# 发一次 HTTPS 请求。排掉之后源码运行正常，打包出的 exe 却会在点开
# 「检查更新」那一刻 ImportError，而且平时完全看不出来。
STDLIB_EXCLUDES = [
    "tkinter", "_tkinter", "turtle", "pydoc", "doctest", "unittest",
    "lib2to3", "distutils", "setuptools", "pip", "wheel", "pkg_resources",
    "xmlrpc", "pdb", "cProfile", "pstats", "profile", "timeit",
    "asyncio", "multiprocessing", "socketserver",
    "html", "imaplib", "smtplib",
    "ftplib", "telnetlib", "xml.dom", "xml.sax",
]
# ★ `concurrent` **不能**排除：`wbsync/copysession.py` 用
#   `concurrent.futures.ThreadPoolExecutor` 并行复制会话正文。
#   排除后打包出来的 exe 只有「点下复制会话」那一刻才会 ImportError ——
#   平时启动、盘点、界面全都正常，自检不跑到那条路径也发现不了。

# 第三方库里用不到的部分（PyInstaller 会按 import 分析，这里再兜一层）
THIRD_PARTY_EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "PyQt5", "PyQt6",
    "IPython", "pytest", "sqlalchemy", "requests",
]

# PySide6 里用不到的子模块
PYSIDE_EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtQmlModels",
    "PySide6.QtQmlWorkerScript", "PySide6.QtQuickTest",
    "PySide6.QtNetwork", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtXml", "PySide6.QtXmlPatterns", "PySide6.QtDBus",
    "PySide6.QtConcurrent", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtUiTools", "PySide6.QtPrintSupport", "PySide6.QtSvg",
    "PySide6.QtSvgWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtGraphs",
    "PySide6.QtHttpServer", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
]

a = Analysis(
    ["app.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    # 应用图标与品牌位图。不带上这两类文件，运行时 icons.py 会退回
    # QPainter 现画的兜底图 —— 能看，但不是设计稿那一版。
    datas=[
        (os.path.join("wbsync", "assets", "appicon.ico"), os.path.join("wbsync", "assets")),
        (os.path.join("wbsync", "assets", "appicon.png"), os.path.join("wbsync", "assets")),
        (os.path.join("wbsync", "assets", "appicon_32.png"), os.path.join("wbsync", "assets")),
    ],
    hiddenimports=[
        # 检查更新的入口是 main_window._open_update_dialog 里的延迟导入，
        # 静态分析未必追得到；把这条链路和标准库 HTTPS 依赖显式钉住。
        "wbsync.updater", "wbsync.ui.update_dialog",
        "urllib.request", "http.client", "ssl", "email", "email.parser",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=STDLIB_EXCLUDES + THIRD_PARTY_EXCLUDES + PYSIDE_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=2,
)

# ---------------------------------------------------------------- 过滤产物


# 按文件名精确剔除的 DLL。每一条都有实测体积依据（见 spec 顶部说明）。
DROP_FILENAMES = {
    "opengl32sw.dll",       # 5.3 MB，软件 OpenGL 兜底渲染器。纯 Widgets 程序不碰 OpenGL
    "qt6network.dll",       # 1.7 MB，检查更新走 Python 标准库，不用 QtNetwork
    # libcrypto-3-x64.dll / libssl-3-x64.dll 必须留在包里，https 证书校验靠它们。
    # 64 位 Windows 上 PyInstaller 用的是带 -x64 的名字，这里不再按文件名剔除。
    "qt6svg.dll",           # 0.6 MB，不用 SVG 图标
    "qt6qml.dll",
    "qt6quick.dll",
    "d3dcompiler_47.dll",   # 着色器编译器，Widgets 用不到
    "qdirect2d.dll",        # 1.0 MB，可选平台插件；qwindows 才是我们要的
}


def _keep_binary(dest: str) -> bool:
    """按路径决定是否保留一个二进制/数据文件。"""
    low = dest.replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]

    # Qt 翻译文件：界面文字全是我们自己写的中文，用不到 Qt 自带翻译
    if "/translations/" in low and low.endswith(".qm"):
        return False

    if name in DROP_FILENAMES:
        return False

    # imageformats 里只有 qico 是运行时真要用的：窗口 / 任务栏图标走
    # QIcon("appicon.ico")，没有这个插件就读不出来（会退回 PNG 兜底）。
    # 其余（jpeg / tiff / webp / svg / gif …）界面一张图都不放，照旧裁掉；
    # PNG 本身编译在 QtGui 里，不受这里影响。
    if "/plugins/imageformats/" in low:
        return name == "qico.dll"

    # 用不到的功能插件
    drop_dirs = (
        "/plugins/sqldrivers/",       # 不用 SQL 驱动（sqlite3 是 Python 内置）
        "/plugins/tls/",              # 不联网
        "/plugins/networkinformation/",
        "/plugins/multimedia/",
        "/plugins/position/",
        "/plugins/sensors/",
        "/plugins/canbus/",
        "/plugins/designer/",
        "/plugins/qmltooling/",
        "/plugins/webview/",
        "/plugins/imageformats/",     # 界面无图片，图标插件纯占地方（约 2 MB）
        "/plugins/iconengines/",      # 同上
    )
    if any(d in low for d in drop_dirs):
        return False

    return True


a.binaries = [b for b in a.binaries if _keep_binary(b[0])]
a.datas = [d for d in a.datas if _keep_binary(d[0])]

# 兜底校验：平台插件丢了程序就起不来，宁可在打包时报错也不要在用户机器上崩
_platform_ok = any("qwindows" in b[0].lower() for b in a.binaries)
if not _platform_ok:
    print("!! 警告：未收集到 platforms/qwindows.dll，生成的 exe 可能无法启动", file=sys.stderr)

# PyInstaller 6.x 已移除字节码加密（cipher），这里只用纯 PYZ
pyz = PYZ(a.pure)

# 设 WBSYNC_ONEDIR=1 可打成目录版：启动更快（不用每次解压），
# 也便于排查「到底装了什么」。默认单文件。
ONEDIR = os.environ.get("WBSYNC_ONEDIR") == "1"

if ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Buddy-atlas",
        debug=False,
        strip=False,
        upx=True,
        console=False,
        version=_version_file,
        icon=_icon_file,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=["vcruntime140.dll", "python314.dll"],
        name="Buddy-atlas",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Buddy-atlas",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=["vcruntime140.dll", "python314.dll"],  # 这两个 UPX 压缩后易出问题
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=_icon_file,
        version=_version_file,
    )
