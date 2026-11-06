"""工作空间路径 → 空间名，以及「对话 / 工作区 → 日期·空间 → 会话」三级归组。

## 为什么归组逻辑要从界面里搬出来

会话列表（卡片/表格）、详情面板、切换向导、对齐向导都要回答同一个问题：「这条会话
属于哪个空间」。四处各写一遍的话，改一处就会让另外三个界面互相打脸 —— 详情面板写
`project-a`、列表写 `project/project-a`，用户并排一看就不知道是不是同一个。

所以路径规则只在这里定义一次，界面层只做展示。

## 三级归组的由来

第一版是「对话 / 工作区 → 空间 → 会话」两级树，2026-11-03 起在两级之间补了一层
「日期」：客户端左栏把临时会话按 `2026-11-02-14-14-08` 这样的目录分组，用户是
按「我那天什么时候聊的」来回忆的，摊平成一组 40 条日期名反而更难找。

- **对话** = 没有工作目录的会话，或工作目录是 WorkBuddy 自动建的临时时间戳目录；
  二级是那个时间戳目录名（与客户端左栏逐字一致），没有目录时退回最近活动日期。
- **工作区** = 有真实工作目录的会话，按空间聚合；二级是空间名。
- 三级都是会话本身。

这也是客户端左栏的口径，两边对照着看不会错位。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

KIND_CHAT = "chat"
KIND_SPACE = "workspace"

# 与客户端左栏同一套说法：临时目录里的会话归「对话」，按目录归组的归「工作区」。
# 用「任务 / 空间」自造词会让两边并排看时对不上号 —— 用户是按客户端的分区名
# 来认这些东西的。
CHAT_LABEL = "对话"
SPACE_ROOT_LABEL = "工作区"

# WorkBuddy 给临时会话自动建的目录：2026-10-31 或 2026-10-31-13-42-37
_STAMP_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-\d{2}-\d{2}-\d{2})?$")

UNKNOWN_DATE = "未记录日期"

# 毫秒戳的合理下限（2000-01-01）。夹具与旧数据里常见 `updated_at=20` 这种占位值，
# 直接换算会得到 1970-01-01 并在树上排成一组 —— 宁可说「未记录日期」。
_MIN_STAMP_MS = 946684800000


def _parts(raw: str) -> list[str]:
    """路径 → 段列表（统一分隔符，去掉空段）。"""
    text = (raw or "").strip()
    if not text:
        return []
    return [p for p in text.replace("\\", "/").rstrip("/").split("/") if p.strip()]


def _strip_stamp(parts: list[str]) -> list[str]:
    """剥掉末尾的自动时间戳目录 —— 它们在客户端里不是空间，是「一次性会话」。"""
    out = list(parts)
    while len(out) > 1 and _STAMP_DIR.match(out[-1]):
        out.pop()
    return out


def _tail(raw: str, n: int) -> str:
    """取路径末尾 n 段（跳过 WorkBuddy 自动建的时间戳目录）。"""
    parts = _strip_stamp(_parts(raw))
    if not parts:
        return ""
    return "/".join(parts[-n:]) if n > 1 else parts[-1]


def is_temp_dir(raw: str) -> bool:
    """cwd 是不是 WorkBuddy 给临时会话自动建的目录（`2026-11-02-14-14-08`）。

    客户端把这类会话放在左栏的「对话」区，而不是「工作区」。判断依据是**末段**
    是否形如时间戳：`C:/Users/demo/WorkBuddy` 本身是真实空间，
    只有它下面的 `2026-11-02-14-14-08` 才是临时会话目录。
    """
    parts = _parts(raw)
    return bool(parts) and bool(_STAMP_DIR.match(parts[-1]))


def space_label(raw: str) -> str:
    """工作目录 → 空间名。与客户端左栏的写法对齐。

    客户端显示的是 `project-a` / `project-b` / `1.0.0` 这种一级名字，
    不是 `project/project-a` 这种末两段 —— 后者在两边并排看时对不上号。

    ⚠️ 还有一个坑：WorkBuddy 会给每个临时会话建一个 `2026-10-31-13-42-37`
    目录。若照搬末段，每个会话都自成一个「空间」，22 条会话能裂出十几个组，
    分组就彻底失去意义了。所以遇到这种自动目录要往上退一级。
    """
    return _tail(raw, 1)


def short_space(name: str) -> str:
    """工作空间简名：长路径只留末尾两段，够分辨就行。"""
    if not name:
        return ""
    s = name.replace("\\", "/").rstrip("/")
    parts = [p for p in s.split("/") if p]
    if len(parts) <= 2:
        return s
    return parts[-2] + "/" + parts[-1]


def space_key(raw: str) -> str:
    """分组键：剥掉时间戳后的小写完整路径。

    键用完整路径而不是末段名 —— 本机确实有两个都叫 `WorkBuddy` 的目录，
    用末段当键会把它们合成一组，用户勾了"全部"却只搬走一半。

    统一小写是因为 **Windows 路径不区分大小写**：库里同时存着
    `C:/Users/alice/WorkBuddy` 与 `c:/Users/alice/WorkBuddy`，不归一化就会
    在树上裂成两个同名空间，用户会以为是两份不同的东西。
    """
    return "/".join(_strip_stamp(_parts(raw))).lower()


def _activity(item) -> int:
    """会话的最近活动毫秒戳（缺字段时依次回退，全无则 0）。

    卡片列表用 `last_activity_at`，复制计划里的 CopyItem 只有 `updated_at`，
    这里统一一次，免得每个调用点各写一套 getattr 链。
    """
    for name in ("last_activity_at", "updated_at", "created_at"):
        try:
            value = int(getattr(item, name, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value >= _MIN_STAMP_MS:
            return value
    return 0


def day_label(ms: int) -> str:
    """毫秒戳 → `2026-11-02`。取不到就给可读的兜底，不抛。"""
    if not ms:
        return UNKNOWN_DATE
    try:
        dt = datetime.fromtimestamp(ms / 1000)
    except (OSError, ValueError, OverflowError):
        return UNKNOWN_DATE
    return dt.strftime("%Y-%m-%d")


def _raw_of(item) -> str:
    """会话归属的分组依据：优先 `cwd`，没有目录时退回侧边栏快照里的空间名。

    快照的 `spaceName` 在本机 v1.8 的数据里全是空串（客户端不再写），所以正常
    情况下永远是 cwd 说了算；这条退路是给**导入进来的记录**与单测夹具用的 ——
    它们只有 sidebar_space，没有 cwd。
    """
    return (
        (getattr(item, "cwd", "") or "").strip()
        or (getattr(item, "sidebar_space", "") or "").strip()
    )


def is_chat(item) -> bool:
    """属于「对话」区：没有工作目录，或工作目录是临时时间戳目录。"""
    raw = _raw_of(item)
    if not raw:
        return True
    return is_temp_dir(raw)


def chat_group_of(item) -> tuple[str, str]:
    """对话会话的二级分组：(键, 显示名)。

    有临时目录就用**目录名**（`2026-11-02-14-14-08`）—— 客户端左栏显示的就是
    它，两边并排比对时逐行对得上；同一自然日开过两次对话自然是两个节点，
    与客户端一致。没有目录（纯聊天、历史导入的空 cwd）才退回活动日期。
    """
    raw = _raw_of(item)
    if raw:
        parts = _parts(raw)
        if parts and _STAMP_DIR.match(parts[-1]):
            return (parts[-1], parts[-1])
    day = day_label(_activity(item))
    # `d:` 前缀避免与时间戳目录名撞键（两者都可能是 2026-11-02 这种纯日期）
    return (f"d:{day}", day)


def group_key(record) -> tuple[str, str]:
    """会话 → (二级分组键, 显示名)。

    对话会话给的是「日期/时间戳」这一层的键；工作区会话给的是空间键。

    ⚠️ 工作区的键用的是「剥掉时间戳目录后的路径」，不是原始 cwd —— 界面上走的是
       `group_sessions`（按 `space_key` 分桶，那里统一小写），本函数保留原始大小写，
       供历史调用点与单测使用。

    ⚠️ 临时时间戳目录**不再**被剥掉合并：它们在客户端里是「对话」的一条，
       见 `chat_group_of`。把 40 条不同日期的对话并成一组「WorkBuddy」正是
       2026-11-03 反馈里最刺眼的那处。
    """
    if is_chat(record):
        return chat_group_of(record)
    raw = _raw_of(record)
    parts = _strip_stamp(_parts(raw))
    if not parts:
        return chat_group_of(record)
    return ("/".join(parts), parts[-1])


@dataclass(frozen=True)
class SessionGroup:
    """一个二级分组：「对话」下面的日期/时间戳，或「工作区」下面的空间。

    `items` 保持原对象的顺序，界面层直接拿去渲染。

    `path` 是剥掉时间戳后的**原始大小写**路径：分组键为比较方便统一小写，
    但拿它当悬停提示会显示成 `d:/pycharmprojects/workbuddysync`，比不给还难认。
    对话分组里存的是完整 cwd（时间戳目录），悬停时能看出到底是哪个目录。
    """

    key: str
    label: str
    items: tuple
    path: str = ""

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class GroupedSessions:
    """归组结果：对话（含日期分组）+ 各空间分组。"""

    chats: tuple
    spaces: tuple[SessionGroup, ...]
    chat_groups: tuple[SessionGroup, ...] = ()

    @property
    def total(self) -> int:
        return len(self.chats) + sum(g.count for g in self.spaces)

    @property
    def space_count(self) -> int:
        return len(self.spaces)


def unique_labels(paths: list[str]) -> list[str]:
    """给一批**互不相同的**路径算互不相同的显示名。

    先取末段；撞车就退到末两段；再撞继续往前加段，直到全都能区分或已到全路径。

    ⚠️ 不能只退一次：`h:/project/project-a` 与 `H:/project/project-a`
       在只差盘符大小写时，末两段都是 `project/project-a` —— 一次退让得到的
       两个标签仍然一模一样，用户看到的是两组同名分组，会以为是两份不同的东西。
       （表现：导入后同一工作空间裂成两组，见 2026-11-03 实测。）
    """
    parts_list = [_strip_stamp(_parts(p)) for p in paths]
    depth = [1] * len(parts_list)

    def label(i: int) -> str:
        parts = parts_list[i]
        if not parts:
            return ""
        return "/".join(parts[-depth[i]:])

    while True:
        labels = [label(i) for i in range(len(parts_list))]
        dup = {text for text in labels if labels.count(text) > 1}
        if not dup:
            return labels
        grew = False
        for i, text in enumerate(labels):
            if text in dup and depth[i] < max(len(parts_list[i]), 1):
                depth[i] += 1
                grew = True
        if not grew:
            # 已经退到全路径还撞车：说明两组的路径确实一样（同路径不同写法已在上游合并），
            # 此时再分也分不开，原样返回让上层自己决定怎么标
            return labels


def _disambiguate(groups: list[SessionGroup], origins: dict[str, str]) -> list[SessionGroup]:
    """同名空间降级成更长的显示名（末两段起，仍撞车继续加段）。

    两个不同路径都叫 `WorkBuddy` 时，只显示末段会让用户以为是一组；
    撞车的那几个改用 `WorkBuddy/xxx` 这种写法，键不变、显示变。
    降级用**原始路径**而不是分组键 —— 键是小写的，拿来显示会变成
    `admin/workbuddy`，比撞车还难认。
    """
    seen: dict[str, int] = {}
    for g in groups:
        seen[g.label] = seen.get(g.label, 0) + 1
    if not any(n > 1 for n in seen.values()):
        return groups

    labels = unique_labels([origins.get(g.key, g.path or g.key) for g in groups])
    out: list[SessionGroup] = []
    for g, text in zip(groups, labels):
        if seen.get(g.label, 0) > 1 and text:
            out.append(SessionGroup(g.key, text, g.items, g.path))
        else:
            out.append(g)
    return out


def group_sessions(items) -> GroupedSessions:
    """按「有没有工作目录」分成对话与工作区，再各分一级。

    - 对话：二级＝时间戳目录名（或活动日期），组间按最近活动倒序 ——
      刚聊完的那天在最上面，与客户端左栏一致；
    - 工作区：二级＝空间名，空间按条数从多到少、同条数按名字。条数多的项目
      通常就是用户这轮最想搬的，放前面少找一次。
    """
    chats: list = []
    chat_buckets: dict[str, list] = {}
    chat_labels: dict[str, str] = {}
    chat_paths: dict[str, str] = {}

    buckets: dict[str, list] = {}
    labels: dict[str, str] = {}
    origins: dict[str, str] = {}

    def _add_chat(item) -> None:
        chats.append(item)
        key, label = chat_group_of(item)
        chat_buckets.setdefault(key, []).append(item)
        chat_labels.setdefault(key, label)
        chat_paths.setdefault(key, _raw_of(item))

    for item in items:
        if is_chat(item):
            _add_chat(item)
            continue
        raw = _raw_of(item)
        key = space_key(raw)
        if not key:
            # 路径只有时间戳目录之类的异常值，当作对话
            _add_chat(item)
            continue
        buckets.setdefault(key, []).append(item)
        labels.setdefault(key, _tail(raw, 1) or key)
        origins.setdefault(key, "/".join(_strip_stamp(_parts(raw))))

    chat_groups = [
        SessionGroup(key=k, label=chat_labels.get(k, k), items=tuple(v), path=chat_paths.get(k, ""))
        for k, v in chat_buckets.items()
    ]
    # 按**目录名**（＝日期时间）倒序，最近的在最上面。
    # ⚠️ 不能按「组内最近活动」排：2026-11-03 实测，10-28 那个目录被重新打开过
    #    （last_activity 是 11-02），于是它排在 11-02 的目录上面 —— 一列日期
    #    看着是乱的，用户第一反应就是"没排序"。目录名本身带时间，按它排才对。
    # 「未记录日期」永远垫底：它不是一个真实的时间点。
    chat_groups.sort(key=lambda g: (g.label != UNKNOWN_DATE, g.label), reverse=True)

    spaces = [
        SessionGroup(key=k, label=labels.get(k, k), items=tuple(v), path=origins.get(k, ""))
        for k, v in buckets.items()
    ]
    spaces = _disambiguate(spaces, origins)
    spaces.sort(key=lambda g: (-g.count, g.label))
    return GroupedSessions(chats=tuple(chats), spaces=tuple(spaces), chat_groups=tuple(chat_groups))
