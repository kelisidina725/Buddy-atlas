"""工作空间映射记忆 —— 源机 cwd → 本机 cwd 的持久化缓存。

跨机导入、或反复修复同一批失效路径时，让用户「设一次、次次带出」，
不必每次都重新手填本机落点。

## 存储

`~/.workbuddy/wbsync-cwd-map.json`

    {
      "version": 1,
      "entries": {
        "c:/users/alice/project/foo": {"target": "h:/project/foo", "updated_at": 1761900000},
        "d:/pycharmprojects/demo":  {"target": "h:/project/demo", "updated_at": 1761903600}
      }
    }

- key   = 源 cwd 归一化（`rehome.normalize`，盘符小写、反斜杠转正斜杠、去尾分隔符）
- value = 本机 cwd 的归一化 + 时间戳
- 同一 key 再次 `remember` 直接覆盖（先按覆盖更新实现，不提供清除入口）

## 为什么单独一个文件

记忆是「这台机器怎么用这个工具」，不参与同步、也不属于任何账号。
它和 `wbsync-ui.json`（折叠偏好）、`wbsync-accounts.json`（别名）是同一类本地偏好，
只是改得没那么频繁。

## 为什么不进同步包

记忆是单机的：同一份源 cwd 在 A 机该指到 `H:/project/x`，在 B 机可能指到
`D:/code/x`。把它打进同步包，等于把 A 机的落点强塞给 B 机，反而坏事。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths, rehome

MAP_NAME = "wbsync-cwd-map.json"
MAP_VERSION = 1


def _map_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / MAP_NAME


def _safe_int(value: object) -> int:
    """把任意值转成 int，转不动就按 0。

    记忆文件是纯本地文本，可能被手工编辑或被半截写入污染。`updated_at`
    只用于展示/排序，读不出来也不该让整份映射作废 —— 所以这里是容错的，
    与 load_map 的 fail-open 承诺一致（不仅对 JSON 语法损坏容错，也对
    字段类型不合法容错）。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def load_map(base: str | os.PathLike[str] | None = None) -> dict[str, dict]:
    """读全部映射。文件缺失 / 损坏 / 字段类型不对都当作空表（fail-open）。"""
    p = _map_path(base)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in entries.items():
        if isinstance(v, dict) and v.get("target"):
            out[str(k)] = {
                "target": str(v["target"]),
                "updated_at": _safe_int(v.get("updated_at")),
            }
    return out


def save_map(entries: dict[str, dict], base: str | os.PathLike[str] | None = None) -> None:
    """原子写：先写 .tmp 再 replace。

    中途崩只留下一个 .tmp，下次读回空表，不会污染已有记忆。
    """
    p = _map_path(base)
    payload = {"version": MAP_VERSION, "entries": entries}
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, p)
    except OSError:
        # 记忆存不下来不该阻断主流程：下次还能按经验推测
        tmp.unlink(missing_ok=True)


def remember(source_cwd: str, new_cwd: str, base=None) -> None:
    """记下一条映射（覆盖同 key 的旧值）。空值不记。"""
    src = rehome.normalize(source_cwd)
    new = rehome.normalize(new_cwd)
    if not src or not new:
        return
    entries = load_map(base)
    entries[src] = {"target": new, "updated_at": int(time.time())}
    save_map(entries, base)


def forget(source_cwd: str, base=None) -> bool:
    """删除一条记忆。返回是否真删到了。"""
    src = rehome.normalize(source_cwd)
    entries = load_map(base)
    if src in entries:
        del entries[src]
        save_map(entries, base)
        return True
    return False


def has(source_cwd: str, base=None) -> bool:
    """记忆里有没有这个源 cwd 的落点。"""
    return rehome.normalize(source_cwd) in load_map(base)


def suggest_new_cwd(source_cwd: str, base=None) -> str:
    """预填建议：命中记忆返回本机落点，否则回退 `rehome.default_new_cwd`。

    命中记忆时 UI 会打蓝标「记忆」；没命中就是按经验推测，需用户确认。
    """
    src = rehome.normalize(source_cwd)
    m = load_map(base)
    if src in m:
        return m[src]["target"]
    return rehome.default_new_cwd(source_cwd)
