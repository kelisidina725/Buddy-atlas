"""配置项发现与读写 —— 第三版的地基。

## 三种同步单元

| kind | 形态 | 例子 | 为什么这样切 |
|------|------|------|--------------|
| kv   | JSON 里的一个子树 | `personalization.customPrompt` | app-config.json 里混着窗口状态等本机信息，**只能动子树**，整体覆盖会把对端的界面设置也带过来 |
| file | 单个文本文件 | `SOUL.md` | 人格文件独立成篇，整文件替换语义最清晰 |
| tree | 一个目录 | `skills/<name>/` | 技能是自包含单元，半同步（只覆盖部分文件）会留下坏掉的技能 |

## 必须排除的东西

- `workspace-state.json` —— 存的是本机模板播种时间，同步过去只会让对端误判迁移状态
- `skills/_backup_*` —— 用户自己的历史备份，同步过去是噪音
- `skills/*.zip` —— 1.2 MB 的安装包，不是配置
- `skills/.*migration*.json` —— 客户端内部迁移标记，双端覆盖会触发重迁移
- `device-id` / `keyblob` —— 设备身份与凭据，跨设备复制会造成身份冲突

## 为什么 personalization 拆成多个 kv 而不是一项

用户可能想同步「自定义指令」但不想把自己的语气风格带过去。
拆开才能勾选。粒度太粗的同步 = 用户不敢用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# 单元类型
KIND_KV = "kv"
KIND_FILE = "file"
KIND_TREE = "tree"

APP_CONFIG_REL = "app/app-config.json"
SETTINGS_REL = "settings.json"

# kv 项属于哪个 JSON 容器。
# ★ 不能靠 key 前缀区分：`sub_key` 必须是**文件内的点号路径**，写成
#   `settings.skillOverrides` 会真的在 app-config.json 里造一层 settings 子树。
CONTAINER_APP = "app-config"
CONTAINER_SETTINGS = "settings"

# app-config.json 中允许同步的子树（其余一律不动）
SYNC_KV_KEYS: tuple[str, ...] = (
    "personalization.customPrompt",
    "personalization.toneStyle",
    "locale",
)

_KV_LABELS = {
    "personalization.customPrompt": "自定义指令",
    "personalization.toneStyle": "语气风格",
    "locale": "界面语言",
}

# settings.json 里「值本身可能带凭据」的项（与默认勾选策略无关）
_SETTINGS_SENSITIVE = frozenset({"http", "claw.channels"})

# settings.json 中允许同步的子树。
# 每项：(点号路径, 标签, 默认勾选, 说明)
#
# 默认勾选只有前三条 —— 它们是「纯偏好、搬过去只会有好处」的开关：
# 用户在这一头逐条关掉的技能、启用的连接器，到了那一头不该全部复活。
#
# 后三条描述的是**这台机器**怎么联网、怎么开机、绑了哪个公众号：
#   代理     可能带内网地址甚至 user:pass@host
#   自启     跟着机器走，不该被另一台的决定覆盖
#   公众号   绑的是主体与回调地址，跨机搬过去等于把渠道指到别人的服务器
# 所以默认不勾，让用户自己判断。
SYNC_SETTINGS_KEYS: tuple[tuple[str, str, bool, str], ...] = (
    (
        "skillOverrides",
        "技能启停状态",
        True,
        "哪些技能被关掉、哪些只能手动调用。不迁移的话，目标机上得逐条重新关一遍"
        "（本机这类条目通常有 30 条以上）",
    ),
    (
        "enabledPlugins",
        "连接器开关",
        True,
        "每个连接器的启用/停用状态。只同步开关，不同步授权凭据 —— 凭据是按账号加密的，搬过去解不开",
    ),
    (
        "pluginConfigs",
        "连接器参数",
        True,
        "连接器的非敏感参数（如默认表格、模板名）。没有配置时不产生这一项",
    ),
    (
        "claw.channels",
        "公众号渠道",
        False,
        "Claw 的公众号接入配置（渠道类型、回调模式）。绑定在这台机器的公众号主体上，"
        "跨机照搬通常会让对端的渠道指向错误的服务",
    ),
    (
        "autoLaunchDesired",
        "开机自启",
        False,
        "描述的是这台机器要不要随系统启动。另一台电脑的习惯不必相同",
    ),
    (
        "http",
        "网络代理",
        False,
        "代理地址与支持模式（http.proxy / http.proxySupport）。"
        "可能含内网地址或账号密码，且换了网络环境往往就不适用",
    ),
)

# settings.json 里有些键名**本身就是点号字面量**（`"http.proxy": ""`），
# 不是嵌套结构。只按点号切分取不到它们，所以「网络代理」这一项要显式列出两个键。
SETTINGS_MULTI_KEY: dict[str, tuple[str, ...]] = {
    "http": ("http.proxy", "http.proxySupport"),
}


# 人格/身份文件（相对 user-*-personal 目录）
PERSONA_FILES: tuple[tuple[str, str], ...] = (
    ("SOUL.md", "人格 SOUL"),
    ("IDENTITY.md", "身份 IDENTITY"),
    ("USER.md", "用户画像 USER"),
    ("BOOTSTRAP.md", "启动引导 BOOTSTRAP"),
)

# 人格目录里明确不同步的文件
PERSONA_EXCLUDE = {"workspace-state.json"}

# 技能目录里不同步的项（按名字前缀/后缀匹配）
SKILL_EXCLUDE_PREFIX = ("_", ".")      # _backup_* / .migration
SKILL_EXCLUDE_SUFFIX = (".zip",)

# 已知可能含凭据、需要用户自行确认的项。
#
# 判定必须按「词」而不是子串：技能 `qa-testcase-authoring` 的 authoring 里
# 含子串 auth，子串匹配会把它标成含凭据 —— 而对该目录全量扫描的结果是零凭据。
# 误报的代价是用户在导出时反复被拦、进而怀疑整个勾选列表，所以宁可漏标不可错标：
# 真正的凭据文件几乎都叫 mcp.json / auth.json / api-key 这类**独立成词**的名字。
_SENSITIVE_WORDS = frozenset(
    {
        "key", "keys", "token", "tokens", "secret", "secrets",
        "password", "passwd", "pwd", "auth", "oauth", "login",
        "credential", "credentials", "cred", "mcp",
    }
)

# 这几个是拼接型写法（mcpserver、mycredentialstore），切词后不再是独立词，
# 额外留一道子串兜底。
_SENSITIVE_SUBSTRINGS = ("mcp", "credential")


@dataclass(frozen=True)
class Item:
    """一个可同步单元。"""

    key: str
    kind: str
    label: str
    payload_hash: str
    size: int
    rel_path: str = ""      # file/tree：相对 .workbuddy 的路径
    sub_key: str = ""       # kv：点号路径
    # kv：当一项覆盖多个键时列出它们（此时 payload 是 {键: 值}）。
    # 例：网络代理要同时搬 `http.proxy` 与 `http.proxySupport`。
    sub_keys: tuple[str, ...] = ()
    file_count: int = 0     # tree：文件数
    sensitive: bool = False
    # kv：所属 JSON 容器（app-config / settings）。file/tree 恒为空。
    container: str = CONTAINER_APP
    # 界面上是否默认勾选。默认 True，只有「描述本机环境」的项才设 False。
    default_checked: bool = True
    note: str = ""

    @property
    def short_hash(self) -> str:
        return self.payload_hash[:8]

    @property
    def exists(self) -> bool:
        return bool(self.payload_hash)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_json(value: object) -> str:
    return _sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _tokenize(text: str) -> set[str]:
    """把路径切成词：`api-key.json` -> {api, key, json}。"""
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def is_sensitive(key: str, rel_path: str = "") -> bool:
    """是否可能含凭据（需要用户自行确认后才同步）。

    按词匹配而非子串：authoring / keyboard / tokenizer 这类词不能被
    auth / key / token 命中。
    """
    text = f"{key} {rel_path}"
    if _tokenize(text) & _SENSITIVE_WORDS:
        return True
    low = text.lower()
    return any(s in low for s in _SENSITIVE_SUBSTRINGS)


# --------------------------------------------------------------- 路径定位


def app_config_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / APP_CONFIG_REL


def settings_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / SETTINGS_REL


def container_path(item: "Item", base: str | os.PathLike[str] | None = None) -> Path:
    """kv 项所在的 JSON 文件。file/tree 用不用它都行，但调用方只该对 kv 调。"""
    if item.container == CONTAINER_SETTINGS:
        return settings_path(base)
    return app_config_path(base)


def persona_dir(base: str | os.PathLike[str] | None = None) -> Path | None:
    """定位 `user-<uid>-personal` 目录。

    ⚠️ 必须用 glob 而不是拼固定 uid：两台电脑的 uid 可能不同，
       写死 uid 会让对端的配置项全部「找不到」。
    """
    root = paths.session_data_dir(base)
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name.startswith("user-") and entry.name.endswith("-personal"):
            return entry
    return None


def skills_dir(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / "skills"


# --------------------------------------------------------------- kv 读写


def _dig(obj: dict, dotted: str) -> object:
    # ① 先按「整体键名」找：settings.json 里存在 `"http.proxy"` 这种字面量键，
    #    直接切分会永远取不到（表现为「明明是代理项却扫不出来」）。
    if dotted in obj:
        return obj[dotted]
    cur: object = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def dig_keys(obj: dict, keys: tuple[str, ...]) -> dict | None:
    """按一组键取出一份子集；全部缺失时返回 None。"""
    vals: dict[str, object] = {}
    for k in keys:
        v = _dig(obj, k)
        if v is not None:
            vals[k] = v
    return vals or None


def _set_dig(obj: dict, dotted: str, value: object) -> None:
    # 字面量键名优先：`http.proxy` 在 settings.json 里就是一个带点的键，
    # 按点号切分会改成造一层 http 子树，客户端根本不读。
    if dotted in obj:
        obj[dotted] = value
        return
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def read_app_config(base: str | os.PathLike[str] | None = None) -> dict:
    return _read_json(app_config_path(base))


def read_settings(base: str | os.PathLike[str] | None = None) -> dict:
    return _read_json(settings_path(base))


def _read_json(p: Path) -> dict:
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_app_config(data: dict, base: str | os.PathLike[str] | None = None) -> None:
    _write_json(app_config_path(base), data)


def write_settings(data: dict, base: str | os.PathLike[str] | None = None) -> None:
    _write_json(settings_path(base), data)


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".wbsync-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read_container(item: "Item", base: str | os.PathLike[str] | None = None) -> dict:
    """按项的 container 读整份 JSON（写入时也走这个，保证只改子树）。"""
    if item.container == CONTAINER_SETTINGS:
        return read_settings(base)
    return read_app_config(base)


def write_container(
    data: dict, item: "Item", base: str | os.PathLike[str] | None = None
) -> None:
    if item.container == CONTAINER_SETTINGS:
        write_settings(data, base)
    else:
        write_app_config(data, base)


# --------------------------------------------------------------- 内容读取


def read_item_payload(item: Item, base: str | os.PathLike[str] | None = None) -> object:
    """读取一个单元的内容。

    kv  -> 原生 JSON 值
    file -> bytes
    tree -> dict[相对路径 -> bytes]
    """
    if item.kind == KIND_KV:
        data = read_container(item, base)
        if item.sub_keys:
            return dig_keys(data, item.sub_keys)
        return _dig(data, item.sub_key)
    if item.kind == KIND_FILE:
        p = paths.session_data_dir(base) / item.rel_path
        try:
            return p.read_bytes()
        except OSError:
            return None
    if item.kind == KIND_TREE:
        root = paths.session_data_dir(base) / item.rel_path
        out: dict[str, bytes] = {}
        if root.is_dir():
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    fp = Path(dirpath) / fn
                    rel = fp.relative_to(root).as_posix()
                    try:
                        out[rel] = fp.read_bytes()
                    except OSError:
                        continue
        return out
    return None


# --------------------------------------------------------------- 发现


def _discover_kv(base) -> list[Item]:
    data = read_app_config(base)
    out: list[Item] = []
    for dotted in SYNC_KV_KEYS:
        value = _dig(data, dotted)
        if value is None:
            continue
        out.append(
            Item(
                key=f"kv:{dotted}",
                kind=KIND_KV,
                label=_KV_LABELS.get(dotted, dotted),
                payload_hash=_hash_json(value),
                size=len(json.dumps(value, ensure_ascii=False).encode("utf-8")),
                sub_key=dotted,
                container=CONTAINER_APP,
                sensitive=is_sensitive(dotted),
            )
        )
    return out


def _discover_settings_kv(base) -> list[Item]:
    """settings.json 里的行为开关。

    ★ 这一组是「导入后仍要手动调」的根因：技能关掉了哪些、连接器启用了哪些，
      全在这个文件里，而它此前整份都不在同步清单中。

    ★ 不含 sandbox / 安装标记（`autoLaunchDefaultApplied` 等）。前者是 21 KB 的
      写权限规则、涉及路径重写，留到下一批；后者是「本机已修复过某问题」的戳，
      同步过去会让对端误以为已经处理，属于**绝不能搬**的一类。
    """
    data = read_settings(base)
    out: list[Item] = []
    for dotted, label, default_checked, note in SYNC_SETTINGS_KEYS:
        multi = SETTINGS_MULTI_KEY.get(dotted)
        value = dig_keys(data, multi) if multi else _dig(data, dotted)
        if value is None:
            continue
        # 空 dict / 空串照样能同步（「把这头的空状态搬过去」也是有效意图），
        # 但空容器在界面上没有信息量，只保留有内容的
        if isinstance(value, (dict, list, str)) and not value:
            continue
        out.append(
            Item(
                key=f"kv:{dotted}",
                kind=KIND_KV,
                label=label,
                payload_hash=_hash_json(value),
                size=len(json.dumps(value, ensure_ascii=False).encode("utf-8")),
                sub_key=dotted,
                sub_keys=multi or (),
                container=CONTAINER_SETTINGS,
                default_checked=default_checked,
                note=note,
                # 只有「值里可能带凭据」的项才算 sensitive：
                #   代理 → 可能写 user:pass@host；公众号渠道 → 可能含 appid/secret
                # 开机自启是个 bool，标成「含凭据」只会让警示失去意义 ——
                # 满屏都是红色的提示等于没有提示。
                sensitive=dotted in _SETTINGS_SENSITIVE,
            )
        )
    return out


def _discover_persona(base) -> list[Item]:
    d = persona_dir(base)
    if d is None:
        return []
    out: list[Item] = []
    for rel_name, label in PERSONA_FILES:
        p = d / rel_name
        if not p.is_file() or rel_name in PERSONA_EXCLUDE:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        out.append(
            Item(
                key=f"file:{rel_name}",
                kind=KIND_FILE,
                label=label,
                payload_hash=_sha256(data),
                size=len(data),
                rel_path=f"{d.name}/{rel_name}",
                sensitive=is_sensitive(rel_name),
            )
        )
    return out


def _tree_hash(root: Path) -> tuple[str, int, int]:
    """目录指纹 = 排序后的「相对路径 + 文件哈希」清单再哈希。

    ⚠️ 必须排序：os.walk 的遍历顺序不保证稳定，不排序会让同样的目录算出不同指纹。
    """
    entries: list[tuple[str, str]] = []
    total = 0
    if root.is_dir():
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                fp = Path(dirpath) / fn
                rel = fp.relative_to(root).as_posix()
                try:
                    data = fp.read_bytes()
                except OSError:
                    continue
                total += len(data)
                entries.append((rel, _sha256(data)))
    entries.sort()
    blob = json.dumps(entries, ensure_ascii=False).encode("utf-8")
    return _sha256(blob), len(entries), total


def _discover_skills(base) -> list[Item]:
    root = skills_dir(base)
    if not root.is_dir():
        return []
    out: list[Item] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if any(name.startswith(p) for p in SKILL_EXCLUDE_PREFIX):
            continue
        if any(name.endswith(s) for s in SKILL_EXCLUDE_SUFFIX):
            continue
        h, count, total = _tree_hash(entry)
        if count == 0:
            continue
        out.append(
            Item(
                key=f"tree:skills/{name}",
                kind=KIND_TREE,
                label=f"技能 {name}",
                payload_hash=h,
                size=total,
                rel_path=f"skills/{name}",
                file_count=count,
                sensitive=is_sensitive(name),
            )
        )
    return out


def discover(base: str | os.PathLike[str] | None = None) -> list[Item]:
    """扫描本机全部可同步配置项。"""
    items = (
        _discover_kv(base)
        + _discover_settings_kv(base)
        + _discover_persona(base)
        + _discover_skills(base)
    )
    # 两个容器共用 `kv:<点号路径>` 命名空间。一旦重名，写入会静默改错文件
    # （后写的那份赢），所以这里直接拒绝，而不是让用户在导入时才发现。
    seen: set[str] = set()
    for it in items:
        if it.key in seen:
            raise ValueError(f"配置项 key 重复：{it.key}（container={it.container}）")
        seen.add(it.key)
    items.sort(key=lambda i: (i.kind, i.key))
    return items


def items_by_key(items: list[Item]) -> dict[str, Item]:
    return {i.key: i for i in items}


# --------------------------------------------------------------- 写入


def write_item_payload(
    item: Item,
    payload: object,
    base: str | os.PathLike[str] | None = None,
) -> None:
    """把一个单元的内容写到本机。

    ⚠️ kv 只改子树：先读整份 app-config.json，改完再整体写回，
       这样对端不知道的字段（窗口位置、最近项目等）原样保留。
    """
    if item.kind == KIND_KV:
        data = read_container(item, base)
        if item.sub_keys:
            assert isinstance(payload, dict), "多键项的值必须是 {键: 值}"
            for k, v in payload.items():
                if k in item.sub_keys:
                    # 字面量写：这些键名本身就带点（http.proxy），
                    # 走 _set_dig 的切分逻辑会造出嵌套结构
                    data[k] = v
        else:
            _set_dig(data, item.sub_key, payload)
        write_container(data, item, base)
        return

    if item.kind == KIND_FILE:
        rel = item.rel_path
        # ★ 人格目录名内嵌 uid（user-<uid>-personal）。两端 uid 不同时，
        #   照搬包里的路径会新建一个孤立的 user-<对端uid>-personal 目录，
        #   客户端根本不读它 —— 表现得像"同步成功但没生效"。必须重定向到本地目录。
        if "/" in rel:
            head, tail = rel.split("/", 1)
            if head.startswith("user-") and head.endswith("-personal"):
                local = persona_dir(base)
                if local is not None:
                    rel = f"{local.name}/{tail}"
        p = paths.session_data_dir(base) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        assert isinstance(payload, (bytes, bytearray))
        tmp = p.with_name(p.name + ".wbsync-tmp")
        tmp.write_bytes(bytes(payload))
        os.replace(tmp, p)
        return

    if item.kind == KIND_TREE:
        root = paths.session_data_dir(base) / item.rel_path
        assert isinstance(payload, dict)
        root.mkdir(parents=True, exist_ok=True)
        wanted = set(payload.keys())
        # 先清掉本地多余文件 —— 否则旧版本残留文件会让技能行为诡异
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                fp = Path(dirpath) / fn
                rel = fp.relative_to(root).as_posix()
                if rel not in wanted:
                    try:
                        fp.unlink()
                    except OSError:
                        pass
        for rel, data in payload.items():
            fp = root / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(data)
        # 清理写入后变空的目录
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if not dirnames and not filenames and Path(dirpath) != root:
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass
        return


__all__ = [
    "CONTAINER_APP",
    "CONTAINER_SETTINGS",
    "KIND_FILE",
    "KIND_KV",
    "KIND_TREE",
    "PERSONA_FILES",
    "Item",
    "app_config_path",
    "container_path",
    "discover",
    "items_by_key",
    "persona_dir",
    "read_app_config",
    "read_container",
    "read_item_payload",
    "read_settings",
    "settings_path",
    "skills_dir",
    "write_app_config",
    "write_container",
    "write_item_payload",
    "write_settings",
]
