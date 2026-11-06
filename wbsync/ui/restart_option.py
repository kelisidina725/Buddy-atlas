"""「导入完成后重启 WorkBuddy，并进入该账号」—— 勾选项的统一文案与可用性。

## 为什么文案必须点名账号

客户端**不支持由外部切换登录账号**：登录态是 at-rest 加密信封，
切号走的是服务端接口（asar 里 `switchAccount` → `/v2/login/enterprise`），
外部进程没有入口。所以重启只会回到「当前登录账号」，跳不到别的账号。

这决定了文案的唯一正确写法：把「重启后会进入哪个账号」直接写出来。
写成「重启并切换账号」是假的 —— 用户会以为重启能带他去任意账号。

## 为什么识别不到账号时要禁用

没有本机登录账号 ⇒ 导入的会话保留源账号归属 ⇒ 本机客户端看不到它们。
这种状态下「重启」点下去什么也不会发生，禁用比让人点完再困惑要好。
宁可少一个选项，也不要给一个空承诺。
"""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox

from .. import accounts

NO_ACCOUNT_TEXT = "导入完成后自动重启 WorkBuddy"
NO_ACCOUNT_TIP = (
    "未能识别本机登录账号：导入的会话会保留源账号归属，本机客户端看不到它们，"
    "重启也没有意义。请先登录一次 WorkBuddy 再导入。"
)

TARGET_TIP = (
    "会话已统一归属到该账号，重启后客户端会重建侧边栏快照，导入内容直接可见。\n"
    "注意：客户端不支持由外部切换登录账号，重启只会回到当前登录的这个账号。\n"
    "若你中途在客户端里切到了别的账号，请先切回来再导入。"
)


def target_text(labels: "accounts.AccountLabels", uid: str) -> str:
    return f"导入完成后重启 WorkBuddy，并进入「{labels.display(uid)}」"


def option_for(labels: "accounts.AccountLabels", uid: str) -> tuple[str, str, bool, bool]:
    """纯决策：(文案, tooltip, 可用, 默认勾选)。与 Qt 无关，便于直接单测。"""
    if uid:
        return target_text(labels, uid), TARGET_TIP, True, True
    return NO_ACCOUNT_TEXT, NO_ACCOUNT_TIP, False, False


def apply(chk: QCheckBox, labels: "accounts.AccountLabels", uid: str) -> bool:
    """把文案、勾选态、可用性一次性写到勾选项上；返回是否可用。"""
    text, tip, enabled, checked = option_for(labels, uid)
    chk.setText(text)
    chk.setToolTip(tip)
    chk.setEnabled(enabled)
    chk.setChecked(checked)
    return enabled
