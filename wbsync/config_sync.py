"""配置同步 —— 基线 + 三方比对 + 配置包。

## 为什么配置同步不能照搬会话的「加法同步」

会话正文是**只追加**的日志，落后就补行，天然安全。
配置是**整体状态**：文件被改了就是被改了，没有"追加"这回事。
所以需要第三条信息 —— **基线**（上次同步后两端达成一致的状态），
用它区分"我改的"和"他改的"。

三方比对（local / remote / base）是 git 解决同一问题的标准做法：

    local==base, remote!=base  -> 对端改了        -> pull
    local!=base, remote==base  -> 本地改了        -> keep
    local!=base, remote!=base  -> 双方都改了      -> conflict（拒绝，交给人）
    没有 base（首次同步）且两边都不同 -> conflict

## 三条刻意的保守决定

1. **对端没有的项，绝不删除本地**。删除配置是不可逆的破坏性动作，
   而"包里漏了一项"更可能是导出时没勾选，不是对端真的删了。宁可不删。
2. **本地主动删除的项，不会被重新拉回来**（base 有、local 无 ⇒ 视为用户删除）。
   否则用户每次同步都得重新删一遍同一个技能。
3. **首次同步两边都有且不同 ⇒ 报冲突，不猜谁新**。
   文件 mtime 跨机器不可比（时钟、时区、拷贝都会改 mtime），用它判断"谁更新"是错的。

## 敏感项

mcp.json 之类可能含 API key。这类项标记 sensitive，默认不勾选导出，
需要用户明确选择 —— 密钥跨设备传输必须是有意识的决定。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config_items, guard, paths
from .config_items import KIND_FILE, KIND_KV, KIND_TREE, Item

CONFIG_FORMAT = "wbsync-config"
CONFIG_VERSION = 1
MANIFEST_NAME = "manifest.json"
PAYLOAD_PREFIX = "payloads"

BASELINE_NAME = "wbsync-config-baseline.json"

# 动作
ADOPT = "adopt"        # 本地没有 -> 采用对端
PULL = "pull"          # 对端更新 -> 拉过来
KEEP = "keep"          # 本地更新 / 对端没有 -> 保持
SKIP = "skip"          # 两端一致
SKIP_DELETED = "skip-deleted"  # 本地主动删除 -> 不恢复
SKIP_NO_PERSONA = "skip-no-persona"  # 本地还没有人格目录（全新安装）
CONFLICT = "conflict"  # 双方都改 -> 交给人

_LABEL = {
    ADOPT: "采用对端（本地没有）",
    PULL: "拉取（对端更新）",
    KEEP: "保持本地",
    SKIP: "一致·跳过",
    SKIP_DELETED: "本地已删除·不恢复",
    SKIP_NO_PERSONA: "需先启动客户端",
    CONFLICT: "冲突·需选择",
}

_WRITING = {ADOPT, PULL}

# 冲突项可由用户覆盖的选择
CHOICE_KEEP_LOCAL = "keep-local"
CHOICE_TAKE_REMOTE = "take-remote"


# --------------------------------------------------------------- 基线


@dataclass
class Baseline:
    """上次同步后达成一致的状态。只存 hash，不存内容。"""

    path: Path
    items: dict[str, str] = field(default_factory=dict)
    updated_at: int = 0

    @classmethod
    def load(cls, base: str | os.PathLike[str] | None = None) -> "Baseline":
        p = paths.session_data_dir(base) / BASELINE_NAME
        items: dict[str, str] = {}
        updated = 0
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    raw = data.get("items")
                    if isinstance(raw, dict):
                        items = {str(k): str(v) for k, v in raw.items()}
                    updated = int(data.get("updated_at") or 0)
            except (OSError, json.JSONDecodeError, ValueError):
                items = {}
        return cls(path=p, items=items, updated_at=updated)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = int(time.time())
        payload = {"version": CONFIG_VERSION, "updated_at": self.updated_at, "items": self.items}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str) -> str | None:
        return self.items.get(key)

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self.items.pop(key, None)
        else:
            self.items[key] = value

    @property
    def total(self) -> int:
        return len(self.items)


# --------------------------------------------------------------- 计划


@dataclass
class ConfigAction:
    item: Item            # 对端包里的项
    action: str
    reason: str = ""
    local_hash: str | None = None
    base_hash: str | None = None
    member: str = ""       # 包内负载前缀
    choice: str = ""       # 冲突项的用户选择

    @property
    def label(self) -> str:
        return _LABEL.get(self.action, self.action)

    @property
    def blocking(self) -> bool:
        return self.action == CONFLICT and not self.choice

    @property
    def will_write(self) -> bool:
        if self.action in _WRITING:
            return True
        if self.action == CONFLICT and self.choice == CHOICE_TAKE_REMOTE:
            return True
        return False


def decide(
    remote_hash: str | None,
    local_hash: str | None,
    base_hash: str | None,
) -> tuple[str, str]:
    """三方比对，返回 (动作, 说明)。

    ★ 判定顺序有讲究：先把「两边一致」和「单边缺失」处理掉，
      剩下的才需要基线参与推理。顺序错了会把"本地删了"误判成"对端新增"。
    """
    if local_hash == remote_hash:
        return SKIP, "两端一致"

    if remote_hash is None:
        return KEEP, "对端包内没有此项，不删除本地"

    if local_hash is None:
        if base_hash is None:
            return ADOPT, "本地没有，采用对端"
        return SKIP_DELETED, "本地已主动删除，不恢复（若需恢复请手动导入）"

    if base_hash is None:
        return CONFLICT, "首次同步且两端内容不同，无法判断谁更新"

    if local_hash == base_hash:
        return PULL, "本地未改动、对端已更新，拉取"

    if remote_hash == base_hash:
        return KEEP, "对端未改动、本地已修改，保持"

    return CONFLICT, "两端在上次同步后各自修改过"


def plan(
    remote_items: list[Item],
    local_items: list[Item],
    baseline: Baseline,
    members: dict[str, str] | None = None,
    base: str | os.PathLike[str] | None = None,
) -> list[ConfigAction]:
    """产出逐项决策。纯计算（除了一次目录存在性检查），不写任何东西。"""
    local_by_key = config_items.items_by_key(local_items)
    members = members or {}
    actions: list[ConfigAction] = []

    # 全新安装时本地还没有 user-*-personal 目录。此时把人格文件写进去是**白费**：
    # 客户端首启会按自己的 uid 另建目录，我们写的那个永远不会被读。
    has_persona_dir = config_items.persona_dir(base) is not None

    for item in remote_items:
        local = local_by_key.get(item.key)
        local_hash = local.payload_hash if local else None
        base_hash = baseline.get(item.key)
        action, reason = decide(item.payload_hash, local_hash, base_hash)

        if (
            action in _WRITING
            and item.kind == KIND_FILE
            and item.rel_path.startswith("user-")
            and not has_persona_dir
        ):
            action = SKIP_NO_PERSONA
            reason = "本地还没有 user-*-personal 目录，请先启动一次 WorkBuddy"

        actions.append(
            ConfigAction(
                item=item,
                action=action,
                reason=reason,
                local_hash=local_hash,
                base_hash=base_hash,
                member=members.get(item.key, ""),
            )
        )
    return actions


# --------------------------------------------------------------- 备份


@dataclass
class ConfigBackup:
    """配置备份。支持目录，因为技能是整棵目录。"""

    root: Path
    files: list[tuple[Path, Path]] = field(default_factory=list)   # (备份件, 原件)
    dirs: list[tuple[Path, Path]] = field(default_factory=list)    # (备份目录, 原目录)

    @property
    def empty(self) -> bool:
        return not self.files and not self.dirs

    def restore(self) -> int:
        ok = 0
        for src, dst in self.files:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                ok += 1
            except OSError:
                continue
        for src, dst in self.dirs:
            try:
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
                ok += 1
            except OSError:
                continue
        return ok

    def size_bytes(self) -> int:
        total = 0
        for src, _ in self.files:
            try:
                total += src.stat().st_size
            except OSError:
                pass
        for src, _ in self.dirs:
            for dirpath, _dn, fns in os.walk(src):
                for fn in fns:
                    try:
                        total += (Path(dirpath) / fn).stat().st_size
                    except OSError:
                        pass
        return total


def backup_items(
    items: list[Item],
    base: str | os.PathLike[str] | None = None,
    label: str = "config",
) -> ConfigBackup:
    """备份将被覆盖的项，整批一次。

    kv 项共用同一个 app-config.json，只拷一次（去重靠路径）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = guard.backup_dir(base) / f"{stamp}-{label}"
    root.mkdir(parents=True, exist_ok=True)
    bak = ConfigBackup(root=root)
    seen: set[Path] = set()

    for item in items:
        if item.kind == KIND_KV:
            src = config_items.container_path(item, base)
            if src in seen or not src.is_file():
                continue
            seen.add(src)
            dst = root / src.name
            try:
                shutil.copy2(src, dst)
                bak.files.append((dst, src))
            except OSError:
                continue
        elif item.kind == KIND_FILE:
            src = paths.session_data_dir(base) / item.rel_path
            if src in seen or not src.is_file():
                continue
            seen.add(src)
            (root / "files").mkdir(parents=True, exist_ok=True)
            dst = root / "files" / src.name
            try:
                shutil.copy2(src, dst)
                bak.files.append((dst, src))
            except OSError:
                continue
        elif item.kind == KIND_TREE:
            src = paths.session_data_dir(base) / item.rel_path
            if src in seen or not src.is_dir():
                continue
            seen.add(src)
            (root / "trees").mkdir(parents=True, exist_ok=True)
            dst = root / "trees" / src.name
            try:
                shutil.copytree(src, dst)
                bak.dirs.append((dst, src))
            except OSError:
                continue
    return bak


# --------------------------------------------------------------- 配置包


@dataclass
class ConfigBundle:
    path: Path
    manifest: dict[str, object] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    members: dict[str, str] = field(default_factory=dict)   # key -> zip 内前缀
    # 从同步包 v2 里读取时，本节成员在 `<prefix>` 下；独立配置包为空串
    prefix: str = ""

    @property
    def source_host(self) -> str:
        src = self.manifest.get("source")
        return str(src.get("host", "")) if isinstance(src, dict) else ""

    @property
    def total(self) -> int:
        return len(self.items)

    def read_payload(self, item: Item) -> object:
        """按项类型解出负载。"""
        if item.kind == KIND_KV:
            for raw in self.manifest.get("items") or []:
                if raw.get("key") == item.key:
                    return raw.get("value")
            return None

        prefix = self.members.get(item.key, "")
        if not prefix:
            return None

        with zipfile.ZipFile(self.path) as z:
            names = {n[len(self.prefix):] for n in z.namelist() if n.startswith(self.prefix)}
            if item.kind == KIND_FILE:
                target = prefix
                if target not in names:
                    return None
                return z.read(self.prefix + target)
            if item.kind == KIND_TREE:
                base = prefix.rstrip("/") + "/"
                out: dict[str, bytes] = {}
                for n in names:
                    if n.startswith(base) and not n.endswith("/"):
                        out[n[len(base):]] = z.read(self.prefix + n)
                return out
        return None


def collect_config(
    items: list[Item],
    base: str | os.PathLike[str] | None = None,
    progress=None,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """把一批配置项整理成清单条目 + 成员映射（成员路径相对**本节根**）。

    ★ 不写 zip —— 独立配置包与同步包 v2 共用这一步，差别只在写入前缀。
    """
    entries: list[dict[str, object]] = []
    members: dict[str, str] = {}

    for idx, item in enumerate(items):
        slot = f"{PAYLOAD_PREFIX}/{idx:03d}"
        payload = config_items.read_item_payload(item, base)

        entry: dict[str, object] = {
            "key": item.key,
            "kind": item.kind,
            "label": item.label,
            "hash": item.payload_hash,
            "size": item.size,
            "sensitive": item.sensitive,
            # ★ 必须带上完整相对路径：key 里只有文件名，落盘位置信息在 rel_path 里。
            #   丢了它，人格文件会被写到 .workbuddy/ 根目录下（而不是 user-*-personal/）。
            "rel_path": item.rel_path,
        }
        if item.kind == KIND_KV:
            entry["value"] = payload
            # ★ 必须带 container：两个 JSON 文件共用 `kv:` 命名空间，只凭 key
            #   无法判断该写回 app-config.json 还是 settings.json。旧包没有这个
            #   字段，读取时默认按 app-config 处理（旧版只同步过它）。
            entry["container"] = item.container
            if item.sub_keys:
                # 多键项（网络代理）必须带上键名清单，否则读回包时写成嵌套结构
                entry["sub_keys"] = list(item.sub_keys)
        elif item.kind == KIND_FILE:
            entry["member"] = f"{slot}/payload.bin"
            members[item.key] = f"{slot}/payload.bin"
        elif item.kind == KIND_TREE:
            entry["member"] = f"{slot}/"
            members[item.key] = f"{slot}/"
            entry["file_count"] = item.file_count
        entries.append(entry)
        if progress:
            progress(idx + 1, len(items))
    return entries, members


def config_manifest(
    entries: list[dict[str, object]],
    base: str | os.PathLike[str] | None = None,
    host_label: str = "",
) -> dict[str, object]:
    """配置节清单。host_label 用于在同步包里标注导出时的账号别名。"""
    source: dict[str, object] = {
        "host": socket.gethostname(),
        "os": platform.platform(),
        "data_dir": str(paths.session_data_dir(base)),
    }
    if host_label:
        source["label"] = host_label
    return {
        "format": CONFIG_FORMAT,
        "version": CONFIG_VERSION,
        "created_at": int(time.time()),
        "source": source,
        "items": entries,
    }


def write_config_payloads(
    z: zipfile.ZipFile,
    items: list[Item],
    members: dict[str, str],
    base: str | os.PathLike[str] | None = None,
    prefix: str = "",
) -> int:
    """把配置负载写进已打开的 zip，返回写入的文件数。"""
    written = 0
    for item in items:
        payload = config_items.read_item_payload(item, base)
        member = members.get(item.key, "")
        if not member:
            continue
        if item.kind == KIND_FILE and isinstance(payload, (bytes, bytearray)):
            z.writestr(prefix + member, bytes(payload))
            written += 1
        elif item.kind == KIND_TREE and isinstance(payload, dict):
            for rel, data in payload.items():
                z.writestr(f"{prefix}{member}{rel}", data)
                written += 1
    return written


def export_config_bundle(
    out_path: str | os.PathLike[str],
    items: list[Item],
    base: str | os.PathLike[str] | None = None,
    progress=None,
) -> Path:
    """导出**独立配置包**（仅配置）。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    entries, members = collect_config(items, base, progress)
    manifest = config_manifest(entries, base)

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))
            write_config_payloads(z, items, members, base)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    os.replace(tmp, out_path)
    return out_path


def read_config_bundle(path: str | os.PathLike[str], prefix: str = "") -> ConfigBundle:
    """打开并解析配置节。prefix 非空时从同步包 v2 的 `<prefix>` 子目录读。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"包不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是有效的配置包（非 zip）：{path}")

    with zipfile.ZipFile(path) as z:
        if (prefix + MANIFEST_NAME) not in z.namelist():
            raise ValueError(f"包内缺少 {prefix}{MANIFEST_NAME}")
        manifest = json.loads(z.read(prefix + MANIFEST_NAME).decode("utf-8"))

    if manifest.get("format") != CONFIG_FORMAT:
        raise ValueError(
            f"不是配置包：format={manifest.get('format')!r}（会话包请用「导入会话包」）"
        )
    if int(manifest.get("version") or 0) > CONFIG_VERSION:
        raise ValueError(f"包版本 {manifest.get('version')} 高于工具支持的 {CONFIG_VERSION}")

    items: list[Item] = []
    members: dict[str, str] = {}
    for raw in manifest.get("items") or []:
        key = str(raw.get("key") or "")
        kind = str(raw.get("kind") or "")
        if not key or kind not in (KIND_KV, KIND_FILE, KIND_TREE):
            continue
        # rel_path 缺失（旧包）时回退到从 key 推导，至少 file/tree 还能用
        rel_path = str(raw.get("rel_path") or "")
        if not rel_path and kind in (KIND_FILE, KIND_TREE):
            rel_path = key.split(":", 1)[1]
        items.append(
            Item(
                key=key,
                kind=kind,
                label=str(raw.get("label") or key),
                payload_hash=str(raw.get("hash") or ""),
                size=int(raw.get("size") or 0),
                sub_key=key.split(":", 1)[1] if kind == KIND_KV else "",
                sub_keys=tuple(str(x) for x in (raw.get("sub_keys") or ())),
                rel_path=rel_path,
                file_count=int(raw.get("file_count") or 0),
                sensitive=bool(raw.get("sensitive")),
                container=str(raw.get("container") or config_items.CONTAINER_APP),
                default_checked=bool(raw.get("default_checked", True)),
                note=str(raw.get("note") or ""),
            )
        )
        member = str(raw.get("member") or "")
        if member:
            members[key] = member

    return ConfigBundle(
        path=path, manifest=manifest, items=items, members=members, prefix=prefix
    )


# --------------------------------------------------------------- 执行


@dataclass
class ConfigReport:
    actions: list[ConfigAction] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup: ConfigBackup | None = None
    dry_run: bool = False

    @property
    def conflicts(self) -> list[ConfigAction]:
        return [a for a in self.actions if a.action == CONFLICT]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.label] = counts.get(a.label, 0) + 1
        parts = [f"{k} {v}" for k, v in sorted(counts.items())]
        head = "（演练，未写入）" if self.dry_run else ""
        return head + ("；".join(parts) if parts else "无动作")


def execute_config_sync(
    actions: list[ConfigAction],
    bundle: ConfigBundle,
    base: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    choices: dict[str, str] | None = None,
    progress=None,
) -> ConfigReport:
    """执行配置同步。

    choices: key -> CHOICE_KEEP_LOCAL / CHOICE_TAKE_REMOTE，用于解决冲突项。
    """
    report = ConfigReport(actions=[], dry_run=dry_run)
    choices = choices or {}

    # 把用户选择写回 action，再决定实际动作
    resolved: list[ConfigAction] = []
    for a in actions:
        if a.action == CONFLICT:
            a.choice = choices.get(a.item.key, a.choice)
        resolved.append(a)
    report.actions = resolved

    todo = [a for a in resolved if a.will_write]
    report.skipped = [a.item.key for a in resolved if not a.will_write]

    if dry_run:
        report.applied = [a.item.key for a in todo]
        return report

    baseline = Baseline.load(base)

    if not todo:
        # 没有写入项，但冲突项的「保留本地」选择仍要落基线 ——
        # 否则下次同步会把同一批冲突再报一遍，用户会以为工具失忆了。
        _record_baseline(resolved, baseline)
        try:
            baseline.save()
        except OSError as e:
            report.failed.append(("baseline", f"基线保存失败：{e}"))
        return report

    # 整批备份一次（只备份会被覆盖的）。
    # ⚠️ discover 只做一次：它在循环里会让复杂度退化成 O(n²)，
    #    技能目录动辄上百文件，实测量级差异很明显。
    local_by_key = config_items.items_by_key(config_items.discover(base))
    cover = [local_by_key[a.item.key] for a in todo if a.item.key in local_by_key]
    # kv 项共用 app-config.json，backup_items 内部按路径去重
    report.backup = backup_items(cover, base=base, label="config-sync") if cover else None

    for idx, a in enumerate(todo):
        try:
            payload = bundle.read_payload(a.item)
            if payload is None:
                report.failed.append((a.item.key, "包内负载缺失"))
            else:
                config_items.write_item_payload(a.item, payload, base)
                report.applied.append(a.item.key)
                baseline.set(a.item.key, a.item.payload_hash)
        except Exception as e:  # 单条失败不拖垮整批
            report.failed.append((a.item.key, f"{type(e).__name__}: {e}"))
        if progress:
            progress(idx + 1, len(todo))

    # 写后校验：重新扫描本机比对 hash。校验失败的**不落基线**，
    # 否则下次会误以为已同步，把问题永久掩盖掉。
    expect = {
        a.item.key: a.item.payload_hash for a in todo if a.item.key in set(report.applied)
    }
    if expect:
        for bad_key in verify_after_sync(expect, base):
            report.failed.append((bad_key, "写后校验失败：落盘内容与包内不一致"))
            baseline.set(bad_key, None)

    _record_baseline(resolved, baseline)

    try:
        baseline.save()
    except OSError as e:
        report.failed.append(("baseline", f"基线保存失败：{e}"))

    return report


def _record_baseline(actions: list[ConfigAction], baseline: Baseline) -> None:
    """把被用户确认过的状态落进基线。

    ⚠️ SKIP_DELETED **故意不动基线**：基线要保留"上次一致时的值"，
       这样下次仍判定为"本地已删除"。若清掉基线，下次会变成 ADOPT
       把用户删掉的项重新拉回来。
    """
    for a in actions:
        if a.action in (SKIP, KEEP):
            baseline.set(a.item.key, a.local_hash)
        elif a.action == CONFLICT and a.choice == CHOICE_KEEP_LOCAL:
            baseline.set(a.item.key, a.local_hash)


def verify_after_sync(
    expect: dict[str, str],
    base: str | os.PathLike[str] | None = None,
) -> list[str]:
    """写后校验：重新扫描本机，比对 hash。返回不一致的 key 列表。"""
    actual = config_items.items_by_key(config_items.discover(base))
    bad: list[str] = []
    for key, want in expect.items():
        got = actual.get(key)
        if got is None or got.payload_hash != want:
            bad.append(key)
    return bad
