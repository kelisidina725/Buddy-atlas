"""界面主题 —— 一套 QSS 收口所有颜色与间距。

## 设计稿（默认窗口 1114×660；以下尺寸为固定项，不随窗口缩放）

尺寸是**固定项**，任何一项都不随窗口变化：

    侧栏 232 · 页头 104 · 表头 44 · 行高 44 · 内容内边距 24 · 按钮高 34 · 卡片圆角 10

窗口变窄时被压缩的是「名称」列这类弹性单元格，不是这些定值。字号也不缩放：
标题 18 / 正文 13 / 元信息 11，四档固定。

## 配色

主色是**草绿** `#16A34A`：填充用它，压在白底上的文字用深一档 `#15803D`，
否则浅底绿字（`#E9F6EE` 上的 `#16A34A`）对比度不够，小字看着发虚。

背景三级：页面 `#F4F5F7` → 卡片/列表 `#FFFFFF` → 表带/分组行 `#FAFBFC`，
靠 1px `#E3E7EC` 描边分层，不用阴影。状态色只留三种（绿 / 琥珀 / 红），
超过三种人就记不住了。

## 三栏控制台的分层

`sessions` 页是三栏：左侧导航（白）、中间列表（白）、右侧详情（白）。
三块都是白底时只靠竖线分隔，所以**竖线必须落在栏与栏之间**，而不是每栏各画一条
——否则相邻两栏会出现双线。约定：导航画右线，详情画左线，列表不画。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 色板

PRIMARY = "#0E9F52"          # 主色（参考图实测 #0C9A4E ~ #12AD52 一带的翡翠绿）
PRIMARY_HOVER = "#0B8A46"
PRIMARY_SOFT = "#E7F6ED"     # 主色浅底（状态横幅 / 选中行）
PRIMARY_TINT = "#F2FBF6"     # 更淡一档
PRIMARY_TEXT = "#0B7D41"     # 浅底上的绿色文字

TEXT_PRIMARY = "#161A20"
TEXT_SECONDARY = "#5B6573"
TEXT_TERTIARY = "#9AA3AE"
TEXT_MUTED = "#8A9099"       # 参考图里的次级说明文字（统计列名 / 页脚）

BG_PAGE = "#F9FAFC"          # 参考图内容区实测 #F9FAFC
BG_CARD = "#FFFFFF"
BG_BAND = "#FAFBFC"          # 表头 / 分组行
BG_HOVER = "#F7F8FA"
BG_FIELD = "#F4F5F7"
BG_INSET = "#F5F7FA"          # 卡片内嵌块（数据带 / 统计条）：比页面冷半档

BORDER = "#E3E8EF"
BORDER_STRONG = "#D8DDE4"
BORDER_HAIR = "#F1F3F6"
# 表格行分隔线。★ 不能用 BORDER_HAIR：#F1F3F6 压在白底上几乎不可见，
# 整张表就成了一堵白墙，行与行分不开。比 BORDER 浅、比 HAIR 深一档。
BORDER_ROW = "#EAEEF3"
BG_ROW_ALT = "#F8FAFC"           # 表格隔行底色（与白行拉出可见但不刺眼的差）
CARD_BORDER = "#E3E8EF"      # 外层面板 / 账号卡描边

# ---- C 端整壳参考图新增（2026-11-04）----
SIDE_BG = "#2C4041"          # 左侧深色栏实测主色
SIDE_TEXT = "#DCE8E3"        # 未选中菜单文字
SIDE_TEXT_HOVER = "#FFFFFF"
SIDE_ACTIVE_BG = "#FFFFFF"   # 选中项是白色胶囊
SIDE_ACTIVE_TEXT = "#0EA653"
SIDE_DOT = "#22C461"         # 选中项与底部状态的绿点
GREEN_BRIGHT = "#18B25C"     # 头像 / 当前账号徽标

# 绿色渐变页头（参考图：左深右浅，斜向一道柔光）
HEADER_GREEN_FROM = "#0C9249"
HEADER_GREEN_MID = "#15A858"
HEADER_GREEN_TO = "#2FBD72"
ACTIVE_CARD_BG = "#F6FFFA"   # 当前账号卡的极浅薄荷底
ACTIVE_CARD_BORDER = "#0EA653"
TOP_BAR_RULE = "#EDF0F3"

DANGER = "#C2352B"
DANGER_SOFT = "#FDECEA"
WARN = "#B45309"
WARN_SOFT = "#FDF3E4"
INK = "#161A20"              # 深色页签底

TABULAR = "Consolas, 'Cascadia Mono', monospace"

# ---------------------------------------------------------------- 尺寸（固定项）

SIDEBAR_WIDTH = 232
HEADER_HEIGHT = 104
TABLE_HEAD_HEIGHT = 44
ROW_HEIGHT = 44
CONTENT_PADDING = 24
BUTTON_HEIGHT = 34
RADIUS = 10
RADIUS_SM = 8

# 卡片尺寸（列宽计算依赖这些常量）
CARD_MIN_WIDTH = 286
CARD_GAP = 12


def app_qss() -> str:
    """全局样式表。"""
    return f"""
* {{
    font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
    font-size: 13px;
    color: {TEXT_PRIMARY};
}}

QMainWindow, QDialog {{
    background: {BG_PAGE};
}}

QWidget#Sidebar {{
    background: {SIDE_BG};
    border-right: none;
}}

/* ---- 页头：绿色渐变横幅，标题与副标题直接压在绿色上 ----
   渐变与斜向柔光由 PageHeader.paintEvent 绘制（QSS 只有两三个硬停点，
   做不出参考图那种过渡），这里只管文字颜色与右侧胶囊。 */
QWidget#PageHeader {{
    background: {HEADER_GREEN_MID};
}}
QWidget#Content {{
    background: {BG_PAGE};
}}

QLabel#BrandName {{
    /* 17px 不是随手调的：侧栏 232 减去左右各 16 的内边距、再减去图标 36 与
       间距 10，留给文字只有 154px；19px 粗体量出来是 158px，末尾会被切掉。 */
    font-size: 17px;
    font-weight: 700;
    color: #FFFFFF;
    background: transparent;
}}
QLabel#BrandSub {{
    font-size: 11px;
    color: {SIDE_TEXT};
    background: transparent;
}}
QLabel#CardMeta {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}
QLabel#PageSub {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}
QLabel#StatusLine {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}
QLabel#PageTitle {{
    font-size: 18px;
    font-weight: 600;
}}

/* ---- 页头文字：白字压在绿色渐变上，副标题要能读但不抢标题 ---- */
QLabel#IntroTitle {{
    font-size: 26px;
    font-weight: 700;
    color: #FFFFFF;
    background: transparent;
}}
QLabel#IntroSub {{
    font-size: 13px;
    color: rgba(255, 255, 255, 0.82);
    background: transparent;
}}
QLabel#CardTitle {{
    font-size: 13px;
    font-weight: 600;
}}
/* 概览页问候行：内容区第一句人话，比卡片标题大一档，但不抢页头标题 */
QLabel#GreetTitle {{
    font-size: 22px;
    font-weight: 700;
    color: {TEXT_PRIMARY};
    background: transparent;
}}

/* ---- 导航：深底白字，选中项是白色胶囊 + 绿点 + 绿字 ---- */
QPushButton#NavButton {{
    text-align: left;
    padding: 0 16px;
    min-height: 52px;
    border: none;
    border-radius: 23px;
    background: transparent;
    color: {SIDE_TEXT};
    font-size: 16px;
}}
QPushButton#NavButton:hover {{
    background: rgba(255, 255, 255, 0.09);
    color: {SIDE_TEXT_HOVER};
}}
QPushButton#NavButton:checked {{
    background: {SIDE_ACTIVE_BG};
    color: {SIDE_ACTIVE_TEXT};
    font-weight: 600;
}}
QPushButton#NavButton:checked:hover {{
    background: {SIDE_ACTIVE_BG};
    color: {SIDE_ACTIVE_TEXT};
}}
QPushButton#NavCount {{
    border: none;
    background: transparent;
    color: {TEXT_TERTIARY};
    font-size: 11px;
    padding: 0 6px;
}}

/* ---- 侧栏底部：客户端运行状态（深底上的绿点 + 浅字，无胶囊）---- */
QLabel#SideStatus {{
    background: transparent;
    color: {SIDE_TEXT};
    padding: 6px 4px;
    font-size: 12px;
}}
QLabel#SideStatusOff {{
    background: transparent;
    color: #93A8A1;
    padding: 6px 4px;
    font-size: 12px;
}}

/* ---- 页头右侧：白底绿字状态胶囊 + 白字账号胶囊 ----
   两态都是「能压在绿色上」的浅色：运行中用白底绿字（最醒目），
   离线用半透明白底白字（弱一档但不消失）。 */
QLabel#StatusPill {{
    background: #FFFFFF;
    color: {PRIMARY_TEXT};
    border-radius: 15px;
    padding: 0 14px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#StatusPillIdle {{
    background: rgba(255, 255, 255, 0.20);
    color: #FFFFFF;
    border-radius: 15px;
    padding: 0 14px;
    font-size: 12px;
}}
QFrame#AccountChip {{
    background: transparent;
    border: none;
    border-radius: 18px;
}}
QFrame#AccountChip:hover {{
    background: rgba(255, 255, 255, 0.16);
}}
QLabel#ChipAvatar {{
    background: #FFFFFF;
    color: {PRIMARY_TEXT};
    border-radius: 15px;
    font-size: 13px;
    font-weight: 700;
}}
QLabel#ChipUid {{
    font-size: 13px;
    font-weight: 500;
    color: #FFFFFF;
    background: transparent;
}}
QLabel#ChipMeta {{
    font-size: 13px;
    font-weight: 600;
    color: #FFFFFF;
    background: transparent;
}}
QLabel#ChipChev {{
    font-size: 11px;
    color: rgba(255, 255, 255, 0.90);
    background: transparent;
}}

/* ---- 账号抽屉（v1.6 取代「⋯」菜单） ----
   两套状态视觉必须能区分：
     浅绿底 + 「当前」       = 正在查看谁（纯界面筛选，可多处）
     「已登录」描边徽标      = 客户端登录身份（唯一，需重启才变）
   混成同一个高亮，用户会以为点一下就把客户端账号换了。 */
QWidget#DrawerScrim {{
    background: rgba(22, 26, 32, 0.18);
}}
QFrame#AccountDrawer {{
    background: {BG_CARD};
    border-right: 1px solid {BORDER};
}}
QFrame#DrawerHead {{
    border: none;
}}
QLabel#DrawerTitle {{
    font-size: 17px;
    font-weight: 600;
}}
QLabel#DrawerCount {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
    background: transparent;
}}
QPushButton#DrawerClose {{
    border: none;
    background: transparent;
    color: {TEXT_TERTIARY};
    font-size: 15px;
    border-radius: 6px;
}}
QPushButton#DrawerClose:hover {{
    background: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}

/* 账号卡片：当前 = 浅绿无描边；其余 = 白底描边 */
QFrame#AccountRow, QFrame#AccountRowActive {{
    border-radius: {RADIUS}px;
    background: {BG_CARD};
    border: 1px solid {BORDER};
}}
QFrame#AccountRow:hover {{
    border-color: {BORDER_STRONG};
    background: {BG_HOVER};
}}
QFrame#AccountRowActive {{
    background: {PRIMARY_TINT};
    border: 1px solid {PRIMARY_TINT};
}}
QLabel#DrawerName {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
    background: transparent;
}}
QLabel#DrawerState {{
    font-size: 12px;
    color: {PRIMARY_TEXT};
    background: transparent;
}}
QLabel#DrawerBadge {{
    font-size: 10px;
    color: {PRIMARY_TEXT};
    background: {BG_CARD};
    border: 1px solid {PRIMARY};
    border-radius: 4px;
    padding: 0 4px;
}}
QLabel#DrawerShort {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
    background: transparent;
    font-family: {TABULAR};
}}
QLabel#DrawerMeta {{
    font-size: 11px;
    color: {TEXT_SECONDARY};
    background: transparent;
}}
QLabel#DrawerHint {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
    background: transparent;
}}
QLabel#DrawerAdd {{
    font-size: 13px;
    color: {PRIMARY_TEXT};
    background: transparent;
    padding: 8px 0;
}}
QLabel#DrawerAdd:hover {{
    color: {PRIMARY_HOVER};
}}
QFrame#DrawerRule {{
    background: {BORDER};
    border: none;
    max-height: 1px;
}}
/* 抽屉里的动作行：灰底圆角条 + 右侧箭头 */
QFrame#DrawerAction {{
    background: {BG_HOVER};
    border: 1px solid {BG_HOVER};
    border-radius: {RADIUS}px;
}}
QFrame#DrawerAction:hover {{
    background: {BG_FIELD};
    border-color: {BORDER};
}}
QLabel#DrawerActionText {{
    font-size: 13px;
    color: {TEXT_PRIMARY};
    background: transparent;
}}
QLabel#DrawerActionChev {{
    font-size: 13px;
    color: {TEXT_TERTIARY};
    background: transparent;
}}
QFrame#DrawerFoot {{
    border-top: 1px solid {BORDER};
}}
QPushButton#DrawerMinor {{
    font-size: 11px;
    padding: 6px 8px;
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    background: {BG_CARD};
    color: {TEXT_PRIMARY};
}}
QPushButton#DrawerMinor:hover {{
    background: {BG_HOVER};
}}
/* 登录身份切换不是主色填充按钮：它要关客户端，视觉重量必须低于「确认」 */
QPushButton#DrawerPrimary {{
    font-size: 12px;
    font-weight: 600;
    padding: 8px 12px;
    border: 1px solid {PRIMARY};
    border-radius: {RADIUS_SM}px;
    background: {BG_CARD};
    color: {PRIMARY_TEXT};
}}
QPushButton#DrawerPrimary:hover {{
    background: {PRIMARY_SOFT};
}}

/* ---- 按钮：高 34、圆角 8 ---- */
QPushButton {{
    background: {BG_CARD};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 6px 14px;
    font-size: 13px;
    min-height: 20px;
}}
QPushButton:hover {{
    background: {BG_HOVER};
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed {{
    background: {BG_FIELD};
}}
QPushButton:disabled {{
    color: {TEXT_TERTIARY};
    border-color: {BORDER};
    background: {BG_CARD};
}}
QPushButton#Primary, QPushButton#PrimaryButton {{
    background: {PRIMARY};
    border: 1px solid {PRIMARY};
    color: #FFFFFF;
    font-weight: 500;
}}
QPushButton#Primary:hover, QPushButton#PrimaryButton:hover {{
    background: {PRIMARY_HOVER};
    border-color: {PRIMARY_HOVER};
}}
QPushButton#Primary:disabled, QPushButton#PrimaryButton:disabled {{
    background: #A9D7B9;
    border-color: #A9D7B9;
    color: #F2FAF5;
}}
QPushButton#Danger {{
    background: {DANGER};
    border: 1px solid {DANGER};
    color: #FFFFFF;
}}
QPushButton#Danger:hover {{
    background: #A62A22;
}}
QPushButton#Danger:disabled {{
    background: #E5B9B5;
    border-color: #E5B9B5;
    color: #FBF0F0;
}}

/* 会话页「批量删除」入口：红描边、白底，与绿色主按钮区分，又不像实心红那样刺眼 */
QPushButton#BatchBtn {{
    background: {BG_CARD};
    border: 1px solid {DANGER};
    border-radius: {RADIUS_SM}px;
    color: {DANGER};
    font-size: 13px;
    padding: 6px 14px;
}}
QPushButton#BatchBtn:hover {{
    background: {DANGER_SOFT};
}}
QPushButton#BatchBtn:checked {{
    background: {DANGER_SOFT};
}}

/* ---- 分裂按钮（主导航动作 + 低频变体下拉） ---- */
QPushButton#SplitMain {{
    background: {BG_CARD};
    border: 1px solid {BORDER_STRONG};
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
    border-top-left-radius: {RADIUS_SM}px;
    border-bottom-left-radius: {RADIUS_SM}px;
    padding: 6px 12px;
}}
QPushButton#SplitArrow {{
    background: {BG_CARD};
    border: 1px solid {BORDER_STRONG};
    border-left: none;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    border-top-right-radius: {RADIUS_SM}px;
    border-bottom-right-radius: {RADIUS_SM}px;
    padding: 0;
    font-size: 10px;
    color: {TEXT_SECONDARY};
}}
QPushButton#SplitMain:hover, QPushButton#SplitArrow:hover {{
    background: {BG_HOVER};
}}
QPushButton#SplitMain[kind="primary"] {{
    background: {PRIMARY};
    border: 1px solid {PRIMARY};
    color: #FFFFFF;
}}
QPushButton#SplitArrow[kind="primary"] {{
    background: {PRIMARY};
    border: 1px solid {PRIMARY};
    border-left: 1px solid #4CB873;
    color: #FFFFFF;
}}
QPushButton#SplitMain[kind="primary"]:hover,
QPushButton#SplitArrow[kind="primary"]:hover {{
    background: {PRIMARY_HOVER};
}}
QPushButton#SplitMain:disabled, QPushButton#SplitArrow:disabled {{
    color: {TEXT_TERTIARY};
    border-color: {BORDER};
    background: {BG_CARD};
}}
QPushButton#SplitMain[kind="primary"]:disabled,
QPushButton#SplitArrow[kind="primary"]:disabled {{
    background: #A9D7B9;
    border-color: #A9D7B9;
    color: #F2FAF5;
}}

/* ---- 浅绿主按钮 / 文字按钮 / 危险文字按钮 ---- */
QPushButton#SoftPrimary {{
    background: {PRIMARY_SOFT};
    border: 1px solid {PRIMARY_SOFT};
    border-radius: {RADIUS_SM}px;
    color: {PRIMARY_TEXT};
    font-weight: 500;
    padding: 6px 12px;
}}
QPushButton#SoftPrimary:hover {{
    background: #DDF1E5;
}}
QPushButton#LinkButton {{
    border: none;
    background: transparent;
    color: {PRIMARY_TEXT};
    font-size: 12px;
    padding: 4px 6px;
}}
QPushButton#LinkButton:hover {{
    color: {PRIMARY_HOVER};
    background: {PRIMARY_SOFT};
    border-radius: 6px;
}}
QPushButton#LinkDanger {{
    border: none;
    background: transparent;
    color: {DANGER};
    font-size: 12px;
    padding: 5px 6px;
}}
QPushButton#LinkDanger:hover {{
    background: {DANGER_SOFT};
    border-radius: 6px;
}}
QPushButton#LinkMuted {{
    border: none;
    background: transparent;
    color: {TEXT_SECONDARY};
    font-size: 12px;
    padding: 4px 6px;
}}
QPushButton#LinkMuted:hover {{
    color: {TEXT_PRIMARY};
    background: {BG_HOVER};
    border-radius: 6px;
}}

/* ---- 页签：会话页筛选（选中＝深色胶囊） ---- */
QPushButton#Tab {{
    border: none;
    background: transparent;
    color: {TEXT_SECONDARY};
    font-size: 12px;
    padding: 6px 12px;
    border-radius: {RADIUS_SM}px;
}}
QPushButton#Tab:hover {{
    background: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}
QPushButton#Tab:checked {{
    background: {INK};
    color: #FFFFFF;
    font-weight: 500;
}}
QPushButton#Chip {{
    border: 1px solid {BORDER_STRONG};
    border-radius: 15px;
    padding: 6px 14px;
    font-size: 12px;
    color: {TEXT_SECONDARY};
    background: {BG_CARD};
}}
QPushButton#Chip:checked {{
    background: {INK};
    border-color: {INK};
    color: #FFFFFF;
}}

/* ---- 图标按钮（页头/工具栏里的方形小按钮） ---- */
QPushButton#IconButton, QToolButton#IconButton {{
    border: 1px solid {BORDER_STRONG};
    background: {BG_CARD};
    color: {TEXT_SECONDARY};
    font-size: 14px;
    padding: 0;
    border-radius: {RADIUS_SM}px;
}}
QPushButton#IconButton:hover, QToolButton#IconButton:hover {{
    background: {BG_HOVER};
    border-color: {BORDER_STRONG};
    color: {TEXT_PRIMARY};
}}
QToolButton#IconButton::menu-indicator {{
    image: none;
    width: 0;
}}
/* 无边框变体：塞在卡片角落里的「⋯」 */
QPushButton#IconButtonGhost, QToolButton#IconButtonGhost {{
    border: none;
    background: transparent;
    color: {TEXT_TERTIARY};
    font-size: 15px;
    padding: 0 6px;
    border-radius: 6px;
}}
QPushButton#IconButtonGhost:hover, QToolButton#IconButtonGhost:hover {{
    background: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}
QToolButton#IconButtonGhost::menu-indicator {{
    image: none;
    width: 0;
}}

/* ---- 三栏：列表栏 / 详情面板 ---- */
QWidget#ListColumn {{
    background: {BG_CARD};
}}
QFrame#ListCard {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QWidget#DetailPanel {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#PanelTitle {{
    font-size: 12px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
}}
QLabel#DetailTitle {{
    font-size: 15px;
    font-weight: 600;
}}
QLabel#SectionTitle {{
    font-size: 12px;
    font-weight: 600;
    color: {TEXT_SECONDARY};
}}
QLabel#CalloutStrong {{
    font-size: 12px;
    font-weight: 500;
    color: {PRIMARY_TEXT};
}}
QLabel#KeyLabel {{
    font-size: 12px;
    color: {TEXT_TERTIARY};
}}
QLabel#ValLabel {{
    font-size: 12px;
}}
QLabel#HintLabel {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}

/* ---- 会话列表行 ---- */
QFrame#SessionRow {{
    background: {BG_CARD};
    border: none;
    border-bottom: 1px solid {BORDER_HAIR};
}}
QFrame#SessionRow:hover {{
    background: {BG_BAND};
}}
QFrame#SessionRow[selected="true"] {{
    background: {PRIMARY_SOFT};
}}
QFrame#RowAccent {{
    background: {PRIMARY};
    border: none;
}}
QLabel#RowTitle {{
    font-size: 13px;
}}
QLabel#RowVol {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}
QLabel#RowTime {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}

/* ---- 分组标题 ---- */
QWidget#GroupHead {{
    background: {BG_BAND};
    border: none;
    border-bottom: 1px solid {BORDER_HAIR};
}}
QWidget#GroupHead:hover {{
    background: {BG_HOVER};
}}
QLabel#GroupTitle {{
    font-size: 12px;
    font-weight: 500;
    color: {TEXT_SECONDARY};
}}
/* 一级（对话 / 工作区）：白底 + 深色粗体，与二级（日期 / 空间）的浅灰条
   拉开一档 —— 三级缩进之后，还需要一眼能看出"这里是分区头" */
QWidget#GroupHead[level="root"] {{
    background: {BG_CARD};
    border-bottom: 1px solid {BORDER};
}}
QWidget#GroupHead[level="root"]:hover {{
    background: {BG_HOVER};
}}
QLabel#GroupTitle[level="root"] {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QLabel#GroupCount {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}

/* ---- 卡片 ---- */
QFrame#Card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QFrame#Card[selected="true"] {{
    border: 1px solid {PRIMARY};
}}
QFrame#Callout {{
    background: {BG_BAND};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
}}
QFrame#Banner {{
    background: {PRIMARY_SOFT};
    border: none;
    border-radius: {RADIUS}px;
}}
QLabel#BannerTitle {{
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}}
QLabel#BannerDesc {{
    font-size: 11px;
    color: {TEXT_SECONDARY};
    background: transparent;
}}
/* 概览「快捷操作」里的行 */
QFrame#ActionRow {{
    background: {BG_HOVER};
    border: 1px solid {BG_HOVER};
    border-radius: {RADIUS_SM}px;
}}
QFrame#ActionRow:hover {{
    background: {BG_FIELD};
    border-color: {BORDER};
}}
QLabel#ActionText {{
    font-size: 13px;
    background: transparent;
}}
QLabel#ActionChev {{
    font-size: 13px;
    color: {TEXT_TERTIARY};
    background: transparent;
}}

/* ---- 概览统计卡 ---- */
QFrame#StatCard {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QFrame#StatCard:hover {{
    border: 1px solid {PRIMARY};
    background: {PRIMARY_SOFT};
}}
QLabel#StatValue {{
    font-size: 22px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QLabel#StatLabel {{
    font-size: 11px;
    color: {TEXT_SECONDARY};
}}
QLabel#StatHint {{
    font-size: 11px;
    color: {TEXT_TERTIARY};
}}

/* ---- 提示框（恢复效果 / 说明） ---- */
QFrame#AlignNote {{
    background: {WARN_SOFT};
    border: 1px solid #F3DFC0;
    border-radius: {RADIUS_SM}px;
}}
QTreeWidget#AlignTree {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    background: {BG_CARD};
}}

/* ---- 页签容器 ---- */
QTabWidget::pane {{
    border: none;
    background: {BG_PAGE};
}}
QTabBar::tab {{
    background: transparent;
    padding: 7px 14px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    color: {TEXT_SECONDARY};
    font-size: 13px;
}}
QTabBar::tab:hover {{
    color: {TEXT_PRIMARY};
}}
QTabBar::tab:selected {{
    color: {PRIMARY_TEXT};
    border-bottom: 2px solid {PRIMARY};
    font-weight: 500;
}}

/* ---- 底部同步条 ---- */
QWidget#SyncBar {{
    background: {BG_CARD};
    border-top: 1px solid {BORDER};
}}

/* ---- 复选框 ---- */
QCheckBox {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    background: {BG_CARD};
}}
QCheckBox::indicator:hover {{
    border-color: {PRIMARY};
}}
QCheckBox::indicator:checked {{
    background: {PRIMARY};
    border-color: {PRIMARY};
}}
QCheckBox::indicator:disabled {{
    background: {BG_HOVER};
    border-color: {BORDER};
}}

/* ---- 视图里的勾选框（树/列表的 item check）----
   不写这一段时，套了全局样式表的视图会把勾选状态画成一个"没有边框的对勾"，
   和旁边的展开箭头并排看就像两个箭头。这里跟 QCheckBox 用同一套方块语言。 */
QTreeView::indicator, QListView::indicator, QTableView::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
    background: {BG_CARD};
}}
QTreeView::indicator:checked, QListView::indicator:checked, QTableView::indicator:checked {{
    background: {PRIMARY};
    border-color: {PRIMARY};
}}
QTreeView::indicator:indeterminate, QListView::indicator:indeterminate,
QTableView::indicator:indeterminate {{
    background: {PRIMARY_SOFT};
    border-color: {PRIMARY};
}}
QTreeView::indicator:disabled, QListView::indicator:disabled,
QTableView::indicator:disabled {{
    background: {BG_HOVER};
    border-color: {BORDER};
}}

/* ---- 账号选择 ---- */
QComboBox {{
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 5px 9px;
    background: {BG_CARD};
    font-size: 12px;
    min-width: 168px;
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    border: 1px solid {BORDER};
    background: {BG_CARD};
    selection-background-color: {PRIMARY_SOFT};
    selection-color: {PRIMARY_TEXT};
    outline: none;
}}

QLineEdit {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    background: {BG_CARD};
    font-size: 12px;
}}
QLineEdit:focus {{
    border-color: {PRIMARY};
}}
QLineEdit#InlineEdit {{
    border: 1px solid transparent;
    background: transparent;
    padding: 4px 6px;
}}
QLineEdit#InlineEdit:hover {{
    border-color: {BORDER};
    background: {BG_CARD};
}}
QLineEdit#InlineEdit:focus {{
    border-color: {PRIMARY};
    background: {BG_CARD};
}}

/* ---- 表格：表头 44，行高 44，无网格 ---- */
QTableWidget {{
    background: {BG_CARD};
    alternate-background-color: {BG_ROW_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    gridline-color: transparent;
    selection-background-color: {PRIMARY_SOFT};
    selection-color: {TEXT_PRIMARY};
    outline: none;
}}
QTableWidget::item {{
    padding: 0 10px;
    border-bottom: 1px solid {BORDER_ROW};
}}
QTableWidget::item:selected {{
    background: {PRIMARY_SOFT};
    color: {TEXT_PRIMARY};
}}
QHeaderView::section {{
    background: {BG_INSET};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 0 10px;
    height: {TABLE_HEAD_HEIGHT}px;
    font-size: 11px;
    color: {TEXT_TERTIARY};
    font-weight: 400;
}}
QTableWidget QTableCornerButton::section {{
    background: {BG_BAND};
    border: none;
}}

/* ---- 滚动条 ---- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #CFD5DC;
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #B4BCC6;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: #CFD5DC;
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QProgressBar {{
    border: none;
    background: {BORDER};
    border-radius: 2px;
    height: 4px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {PRIMARY};
    border-radius: 2px;
}}

/* ---- 对齐向导（四步） ---- */
QWidget#WizardHead {{
    background: {BG_CARD};
    border-bottom: 1px solid {BORDER};
}}
QWidget#WizardFoot {{
    background: {BG_CARD};
    border-top: 1px solid {BORDER};
}}
QFrame#WizardStrip {{
    background: {BG_BAND};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    min-height: 40px;
    max-height: 40px;
}}
QFrame#WarnBanner {{
    background: {WARN_SOFT};
    border: none;
    border-radius: {RADIUS}px;
}}
QTreeWidget#PickTree {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    outline: none;
}}
QTreeWidget#PickTree::item {{
    height: 32px;
}}
QTreeWidget#PickTree::item:selected {{
    background: {PRIMARY_SOFT};
    color: {TEXT_PRIMARY};
}}
/* ---- 切换向导的迁移树：对话 / 工作区 → 空间 → 会话 ---- */
QTreeWidget#SwitchTree {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    outline: none;
    show-decoration-selected: 0;
}}
QTreeWidget#SwitchTree::item {{
    height: 30px;
}}
QTreeWidget#SwitchTree::item:selected {{
    background: transparent;
}}
QTreeWidget#SwitchTree QHeaderView::section {{
    background: {BG_INSET};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 0 8px;
    height: 30px;
    font-size: 11px;
    color: {TEXT_TERTIARY};
    font-weight: 400;
}}

/* ---- 单选钮：账号只能选一个 ---- */
QRadioButton {{
    spacing: 6px;
    background: transparent;
}}
QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 8px;
    background: {BG_CARD};
}}
QRadioButton::indicator:hover {{
    border-color: {PRIMARY};
}}
QRadioButton::indicator:checked {{
    border: 4px solid {PRIMARY};
    background: {BG_CARD};
}}

/* ---- 状态胶囊 ---- */
QLabel#PillOk {{
    background: {PRIMARY_SOFT};
    color: {PRIMARY_TEXT};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}
QLabel#PillWarn {{
    background: {WARN_SOFT};
    color: {WARN};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}
QLabel#PillBad {{
    background: {DANGER_SOFT};
    color: {DANGER};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}
QLabel#PillMuted {{
    background: {BG_HOVER};
    color: {TEXT_TERTIARY};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}

/* ---- 账号页（C 端参考图）：外层面板 + 每账号一张卡 ---- */
QFrame#AccountPanel {{
    background: {BG_CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: 12px;
}}
QFrame#AccountCard, QFrame#AccountCardActive {{
    background: {BG_CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: 12px;
}}
QFrame#AccountCardActive {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {ACTIVE_CARD_BG}, stop:1 #FFFFFF);
    border: 1px solid {ACTIVE_CARD_BORDER};
}}
QFrame#AccountRule {{
    background: #EEF1F4;
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame#AccountRuleActive {{
    background: #DCEFE3;
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QLabel#AcctSummary {{
    font-size: 14px;
    color: {TEXT_SECONDARY};
    background: transparent;
}}
QLabel#AcctName {{
    font-size: 20px;
    font-weight: 700;
    color: {TEXT_PRIMARY};
    background: transparent;
}}
QLabel#AcctId {{
    font-size: 13px;
    color: #6B7280;
    font-family: {TABULAR};
    background: transparent;
}}
QLabel#AcctBadge {{
    background: {GREEN_BRIGHT};
    color: #FFFFFF;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#StatKey {{
    font-size: 13px;
    color: {TEXT_MUTED};
    background: transparent;
}}
QLabel#StatVal {{
    font-size: 15px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
    background: transparent;
}}
QLabel#StatValMuted {{
    font-size: 15px;
    color: {TEXT_MUTED};
    background: transparent;
}}
QLabel#AcctFoot {{
    font-size: 13px;
    color: {TEXT_MUTED};
    background: transparent;
}}
QLabel#AcctEmpty {{
    font-size: 13px;
    color: {TEXT_MUTED};
    background: transparent;
    padding: 24px 0;
}}
QPushButton#AcctAdd {{
    background: {PRIMARY};
    border: 1px solid {PRIMARY};
    color: #FFFFFF;
    font-size: 14px;
    font-weight: 500;
    border-radius: 8px;
    padding: 0 18px;
    min-height: 40px;
}}
QPushButton#AcctAdd:hover {{
    background: {PRIMARY_HOVER};
    border-color: {PRIMARY_HOVER};
}}
QPushButton#AcctSwitch {{
    background: {BG_CARD};
    border: 1px solid {PRIMARY};
    border-radius: 8px;
    color: {PRIMARY_TEXT};
    font-size: 14px;
    padding: 8px 18px;
    min-height: 38px;
}}
QPushButton#AcctSwitch:hover {{
    background: {PRIMARY_SOFT};
}}
QPushButton#AcctCurrent {{
    background: #F1F3F5;
    border: 1px solid #E7EAEE;
    border-radius: 8px;
    color: {TEXT_TERTIARY};
    font-size: 14px;
    padding: 8px 18px;
    min-height: 38px;
}}

/* ---- 菜单 ---- */
QMenu {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background: {BG_HOVER};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}
"""


# ---------------------------------------------------------------- 账号卡内联样式
# 只有叶子控件（头像）可以带自己的 QSS：给容器 setStyleSheet 会把它下面整棵
# 子树的全局 QSS 挡掉，徽标、按钮、统计文字会一起失色。卡片两态走 objectName
# + unpolish/polish，规则里那两条线同理只作用于自身。
def account_avatar_qss(active: bool, size: int = 38) -> str:
    bg = GREEN_BRIGHT if active else "#A6A9B0"
    return (
        f"QLabel{{background:{bg};color:#FFFFFF;border-radius:{size // 2}px;"
        "font-size:18px;font-weight:600;}"
    )

