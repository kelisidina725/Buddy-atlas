"""账号可用积分 —— 只读复用 Buddy Switch 已落盘的观察快照。

## 数据来源

部分账号切换工具会把「每个账号最近一次观察到的积分」写到本地
`~/.buddy-switch/credit_usage_snapshots.json`（示例结构）：

    [{"accountId": "...", "accountName": "...", "remaining": 1000.0,
      "total": 1200.0, "ts": 1700000000000}, ...]

同一目录的 `accounts.json` 每行同时带 `id`（Buddy Switch 内部 id，对应快照的
`accountId`）与 `uid`（WorkBuddy 真实 uid，也就是本工具的账号 uid）。两张表拼起来
就能把积分落到本工具的账号卡上，无需重新实现计费接口。

## 边界（改这里之前先读完）

1. **不联网、不解密、不读凭据**：只读 `accounts.json` 的 `id/uid` 与快照的
   `accountId/remaining/total/ts` 共六个白名单字段。那份文件里还有
   `access_token` / `refresh_token` / `auth_raw` / `profile_raw`，一律不碰。
   本工具的产品定位是离线兜底，实时查余额需要解密客户端加密信封并外发请求，
   那属于另一个需要用户单独授权的能力，不能顺手做进来。
2. **显示的是最近一次观察值，不是实时余额**：界面必须带上观察时间，否则
   「可用积分」会被当成此刻精确的账户余额。
3. **缺失即降级**：文件不存在、损坏、类型不对、负数 / NaN，一律返回 `None`
   让界面显示「—」。绝不用 0 冒充「没积分」——那是最容易误导人的假数据。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

STORE_DIRNAME = ".buddy-switch"
ACCOUNTS_FILE = "accounts.json"
SNAPSHOT_FILE = "credit_usage_snapshots.json"


@dataclass(frozen=True)
class Credit:
    """一个账号最近一次被观察到的积分。"""

    remaining: float
    total: float
    observed_at: int  # 毫秒时间戳

    @property
    def value_text(self) -> str:
        return f"{self.remaining:,.2f}"

    @property
    def time_text(self) -> str:
        if not self.observed_at:
            return "时间未知"
        try:
            dt = datetime.fromtimestamp(self.observed_at / 1000)
        except (OSError, OverflowError, ValueError):
            return "时间未知"
        if dt.date() == datetime.now().date():
            return f"今天 {dt:%H:%M}"
        return f"{dt:%m-%d %H:%M}"


def store_dir(base: str | os.PathLike[str] | None = None) -> Path:
    """积分数据目录。传 base 时按「已经是 .buddy-switch 目录」解释，供单测隔离。"""
    return Path(base) if base is not None else Path.home() / STORE_DIRNAME


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        # 文件不在 / 半个写入 / 编码坏了都算「没有数据」，不抛到界面上
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _id_to_uid(base: str | os.PathLike[str] | None) -> dict[str, str]:
    rows = _load_json(store_dir(base) / ACCOUNTS_FILE)
    if not isinstance(rows, list):
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 白名单取值：整行对象不往外传，避免把凭据字段带进调用方
        buddy_id, uid = row.get("id"), row.get("uid")
        if isinstance(buddy_id, str) and buddy_id and isinstance(uid, str) and uid:
            out[buddy_id] = uid
    return out


def read_credits(
    base: str | os.PathLike[str] | None = None,
) -> dict[str, Credit]:
    """按本工具 uid 返回每个账号最新的一条积分快照；没有数据返回空字典。"""
    mapping = _id_to_uid(base)
    if not mapping:
        return {}
    rows = _load_json(store_dir(base) / SNAPSHOT_FILE)
    if not isinstance(rows, list):
        return {}
    latest: dict[str, Credit] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        account_id = row.get("accountId")
        uid = mapping.get(account_id) if isinstance(account_id, str) else None
        if not uid:
            continue
        ts = row.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        remaining = _number(row.get("remaining"))
        if remaining is None:
            continue
        total = _number(row.get("total"))
        if total is None:
            total = remaining
        prev = latest.get(uid)
        if prev is not None and prev.observed_at >= int(ts):
            continue
        latest[uid] = Credit(
            remaining=remaining, total=total, observed_at=int(ts)
        )
    return latest


def credit_for(
    uid: str, base: str | os.PathLike[str] | None = None
) -> Credit | None:
    """单个账号的积分；拿不到就返回 None，由界面显示降级文案。"""
    if not uid:
        return None
    return read_credits(base).get(uid)
