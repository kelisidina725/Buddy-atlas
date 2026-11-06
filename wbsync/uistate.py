"""界面偏好 —— 这是「这台机器怎么用这个工具」，不参与同步。

## 为什么单独一个文件

`wbsync-accounts.json` 存的是账号别名与客户端昵称缓存，语义是"这些账号在这台机器上
叫什么"；这里是"列表哪几组被我收起来了"。两者都是本地偏好，但改变频率差得远：
别名一年改两次，折叠状态一天点十次。合成一个文件的话，每次点折叠都要
重写一份别名表，写坏一次就把名字丢了。

## 为什么只存折叠的分组名，不存展开的

新增的工作空间默认就应该是展开的 —— 存"展开列表"会导致换个项目后
新组默认收起，用户找不到内容。存"收起名单"是 fail-open 的：
名单里没有的一律展开。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import paths

CONFIG_NAME = "wbsync-ui.json"
CONFIG_VERSION = 1


def _config_path(base: str | os.PathLike[str] | None = None) -> Path:
    return paths.session_data_dir(base) / CONFIG_NAME


def _read_all(base: str | os.PathLike[str] | None = None) -> dict:
    """整份偏好读成一个 dict。缺失/损坏/类型不对一律当空，绝不上抛。"""
    p = _config_path(base)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_all(data: dict, base: str | os.PathLike[str] | None = None) -> None:
    """整份写回。

    先写临时文件再替换：这个文件在用户每次点折叠、每次改窗口大小时都会被写，
    中途崩一次留下半个 JSON，下次启动就整份偏好读不出来了。
    """
    p = _config_path(base)
    payload = dict(data)
    payload["version"] = CONFIG_VERSION
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        # 偏好存不下来不该阻断任何主流程：下次再改还会尝试保存
        tmp.unlink(missing_ok=True)


def _update(changes: dict, base: str | os.PathLike[str] | None = None) -> None:
    """按 key 合并写回 —— 每个保存函数只碰自己那几个键。

    合并而不是整体覆盖：折叠状态与窗口尺寸由不同时机写入，
    谁后写谁就会把对方的键抹掉，那是「调好的窗口大小过两天又被重置」的成因。
    """
    data = _read_all(base)
    data.update(changes)
    _write_all(data, base)


def load_collapsed(base: str | os.PathLike[str] | None = None) -> set[str]:
    """读回收起的分组名。文件缺失、损坏、字段类型不对都当作"没有收起任何组"。"""
    names = _read_all(base).get("collapsed_groups")
    if not isinstance(names, list):
        return set()
    return {str(n) for n in names if str(n).strip()}


def save_collapsed(names: set[str], base: str | os.PathLike[str] | None = None) -> None:
    """写回收起的分组名（其余偏好原样保留）。"""
    _update({"collapsed_groups": sorted(names)}, base)


def load_expanded(base: str | os.PathLike[str] | None = None) -> set[str]:
    """读回**被主动展开过**的二级分组名。

    三级树里「一级展开、二级收起」是默认态，只记收起名单就不够用了：新出现的
    日期/空间与用户手动展开过的分组在文件里长得一模一样，重开一次又全部缩回去。
    所以二级另存一份「展开过的」名单，默认（不在名单里）就是收起。
    """
    names = _read_all(base).get("expanded_groups")
    if not isinstance(names, list):
        return set()
    return {str(n) for n in names if str(n).strip()}


def save_expanded(names: set[str], base: str | os.PathLike[str] | None = None) -> None:
    """写回展开过的二级分组名（其余偏好原样保留）。"""
    _update({"expanded_groups": sorted(names)}, base)


def load_share_data(base: str | os.PathLike[str] | None = None) -> bool:
    """「让所有账号共用同一份数据」这个开关。

    它记录的是**意愿**，不是数据状态：开着表示用户希望各账号数据一致，
    关掉只表示不再提醒，绝不会删掉任何一条会话。
    """
    return bool(_read_all(base).get("share_data"))


def save_share_data(on: bool, base: str | os.PathLike[str] | None = None) -> None:
    """只改 share_data 这一个键，其余偏好（折叠名单、窗口尺寸）原样保留。"""
    _update({"share_data": bool(on)}, base)


# ---------------------------------------------------------------- 隐藏的会话
# 「清除已删除」对 `transport=cloud` 的会话是无效的：删掉本地行等于抹掉客户端
# 告诉云端「这条我删了」的唯一信号，客户端重启后对账会把会话重新下发
# （实测 26 条一次性回流：rowid 连续追加在表尾、deleted_at 仍为 -1、云端映射为空）。
# 所以这类目标改为**只在本工具里隐藏**：记下 cid，盘点时排除。
# 磁盘与客户端状态一律不动，误操作可一键恢复显示。


def load_hidden(base: str | os.PathLike[str] | None = None) -> set[str]:
    """读回被隐藏的会话 cid。缺失/损坏/类型不对一律当作「没有隐藏任何会话」。"""
    cids = _read_all(base).get("hidden_sessions")
    if not isinstance(cids, list):
        return set()
    return {str(c) for c in cids if str(c).strip()}


def save_hidden(cids, base: str | os.PathLike[str] | None = None) -> None:
    """整体替换隐藏名单（其余偏好原样保留）。

    传空集合即「全部恢复显示」—— 不存在增量接口，避免名单在两处各改一半。
    """
    _update({"hidden_sessions": sorted({str(c).strip() for c in cids if str(c).strip()})}, base)


# ---------------------------------------------------------------- 窗口尺寸
# 存的是**逻辑像素**的 (x, y, 宽, 高)。设计稿 1114×660 在 125% 缩放的笔记本上
# 比屏幕还高，所以「用户调小过一次」这件事必须记住，否则每次启动都重演。
# 旁边另存 window_geometry_design = 记这份几何时的默认尺寸：默认值一改
# （例如 1440×932 → 1114×660），旧几何就不该再把窗口撑回旧宽度。


def load_window_geometry(base: str | os.PathLike[str] | None = None) -> tuple[int, int, int, int] | None:
    """读回窗口几何。字段缺失或数值不合理一律当没有记录。"""
    item = _read_all(base).get("window_geometry")
    if not isinstance(item, (list, tuple)) or len(item) != 4:
        return None
    try:
        x, y, w, h = (int(v) for v in item)
    except (TypeError, ValueError):
        return None
    # 太小说明写坏了；位置允许为负（副屏在主屏左侧是常态），只卡尺寸
    if w < 600 or h < 400 or w > 20000 or h > 20000:
        return None
    return x, y, w, h


def save_window_geometry(
    rect,
    base: str | os.PathLike[str] | None = None,
    design: tuple[int, int] | None = None,
) -> None:
    """记下几何；`design` 是此刻的默认窗口尺寸，供 restore 判断记录是否过期。

    `base` 保持第二位位置参数不变 —— 既有调用与单测都按 (rect, base) 传。
    两个键一次写回，避免「几何写进去了、版本没写」的半截状态。
    """
    x, y, w, h = (int(v) for v in rect)
    changes: dict = {"window_geometry": [x, y, w, h]}
    if design is not None:
        changes["window_geometry_design"] = [int(design[0]), int(design[1])]
    _update(changes, base)


def load_window_geometry_design(
    base: str | os.PathLike[str] | None = None,
) -> tuple[int, int] | None:
    """读回记几何时的默认尺寸；老文件没有这个键时返回 None（视为过期）。"""
    item = _read_all(base).get("window_geometry_design")
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        return None
    try:
        w, h = (int(v) for v in item)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return w, h


# ---------------------------------------------------------------- 客户端路径
# 「导入完成后自动重启」依赖客户端 exe 路径。进程在跑时可以从进程映像取，
# 一旦用户先手动退出（或上一次导入后没被拉起），进程枚举就什么也拿不到。
# 记住上一次成功定位到的路径，这种情况仍然能把客户端拉起来。


def load_client_exe(base: str | os.PathLike[str] | None = None) -> str:
    path = _read_all(base).get("client_exe")
    return str(path) if isinstance(path, str) and path.strip() else ""


def save_client_exe(path: str, base: str | os.PathLike[str] | None = None) -> None:
    text = (path or "").strip()
    if not text:
        return
    if load_client_exe(base) == text:
        return  # 没变化就不写盘：这个键在每次导入时都会被碰一次
    _update({"client_exe": text}, base)
