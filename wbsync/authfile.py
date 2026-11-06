"""官方认证文件 `workbuddy-desktop.info` 的定位与读写 —— 账号切换的唯一落点。

## 登录态到底存在哪

    Windows  %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
    macOS    ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/...

它**不在** `~/.workbuddy` 里。这是本模块存在的理由：早期只在数据目录里翻，
于是得出「客户端登录态不可改、切换账号必须重新登录」的错误结论 —— 而文件
一直摆在那里，明文 JSON、四个顶层键：

    {"account": {...}, "auth": {...}, "accounts": [...], "allAccounts": [...]}

`account.uid` 就是当前登录账号；`auth.accessToken/refreshToken` 就是它的凭据。
把另一份 `{account, auth}` 写进去，客户端下次启动就是那个账号 —— 切换不需要
联网、不需要重新扫码。前提是**你事先存下了那个账号的这一对区块**（见 vault.py）。

## 加密信封：只搬运，不解密

WorkBuddy 5.6 起，`nickname` / `phoneNumber` / `accessToken` / `refreshToken`
被存成字段级信封：

    {"$wbEncrypted": 1, "envelope": "<base64(suite|keyId|nonce|tag|ciphertext)>"}

**我们不解密**，原因有三：

1. 没有依赖也能活。解密要 AES-256-GCM（本项目零第三方加密库），并且要先用
   `ELECTRON_RUN_AS_NODE=1` 拉起客户端原生绑定取 `atRestSecretKey`；
2. 没必要。信封是**本机同一把钥匙**封的，原样写回客户端自己解得开；
3. 更安全。解密意味着凭据明文落盘，而原样搬运只是把客户端自己写的东西挪个位置。

客户端的字段解码器是 `isEncryptedFieldWrapper(v) ? 解密 : 当作明文`，
所以**明文也照收**（下次写回时它自己重新封装）。这让「从其它工具导入的账号」
（往往只有明文 token）同样可用 —— 见 `vault.import_from_buddy_switch`。

## 写入的三道保险

1. **写前备份**：整份文件复制到 `wbsync-backup/<stamp>-auth/`；
2. **原子写**：临时文件 + `os.replace`，中途失败原文件不动；
3. **写后按值校验**：重新解析，逐项比对 `account.uid` 与 `auth.accessToken`。

   第 3 条特别强调**按值**：token 可能是字符串、也可能是信封对象。若用
   `str(v or "")` 取，两种形态都被折成 `""`，两侧都空 ⇒ 校验恒真 ⇒ 等于给
   「token 已写坏」盖章放行。假阳性比没有校验更坏。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

AUTH_FILENAME = "workbuddy-desktop.info"

# 相对于「home / AppData 根」的候选目录（按优先级）
_REL_CANDIDATES = (
    ("AppData", "Local", "CodeBuddyExtension", "Data", "Public", "auth"),
    ("AppData", "Roaming", "CodeBuddyExtension", "Data", "Public", "auth"),
)

_ENVELOPE_KEY = "$wbEncrypted"
_ENVELOPE_BODY = "envelope"


# ---------------------------------------------------------------- 路径


def is_envelope(value: Any) -> bool:
    """是否是字段级加密信封（`{"$wbEncrypted":1,"envelope":"..."}`）。

    严格按客户端的 `isStandardEncryptedFieldWrapper` 判定：恰好两个键、
    `$wbEncrypted === 1`、`envelope` 是字符串。多一个键就不认 —— 认错了会把
    一个普通对象当密文写到别处。
    """
    if not isinstance(value, dict):
        return False
    if set(value.keys()) != {_ENVELOPE_KEY, _ENVELOPE_BODY}:
        return False
    return value.get(_ENVELOPE_KEY) == 1 and isinstance(value.get(_ENVELOPE_BODY), str)


def secret_text(value: Any) -> str:
    """从字段里取可读文本：明文原样返回，信封返回空串（不解密，也不猜）。"""
    if isinstance(value, str):
        return value
    return ""


def has_secret(value: Any) -> bool:
    """该字段是否含有可用于登录的东西（明文或信封都算）。"""
    if isinstance(value, str):
        return bool(value.strip())
    return is_envelope(value) and bool(value.get(_ENVELOPE_BODY))


def candidates(root: str | os.PathLike[str] | None = None) -> list[Path]:
    """认证文件的全部候选路径，按优先级排序。

    `root` 用于单测注入假 home（此时**不读**环境变量，否则单测会打到真实目录）。
    """
    if root is not None:
        base = Path(root)
        return [base.joinpath(*rel) / AUTH_FILENAME for rel in _REL_CANDIDATES]

    home = Path(os.path.expanduser("~"))
    out: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    if local:
        out.append(Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / AUTH_FILENAME)
    if roaming:
        out.append(Path(roaming) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / AUTH_FILENAME)
    for rel in _REL_CANDIDATES:
        p = home.joinpath(*rel) / AUTH_FILENAME
        if p not in out:
            out.append(p)
    return out


def auth_path(root: str | os.PathLike[str] | None = None) -> Path:
    """当前认证文件路径：优先已存在的，都不存在时给第一候选（供报错文案用）。"""
    cands = candidates(root)
    for p in cands:
        if p.is_file():
            return p
    return cands[0]


# ---------------------------------------------------------------- 读取


def read_root(
    root: str | os.PathLike[str] | None = None,
    path: Path | None = None,
) -> dict | None:
    """读认证文件。不存在 / 不是合法 JSON 对象 → None（fail-open，不抛）。

    逐个候选探测：只有「文件不存在」才回退到下一个；文件存在但不可解析时
    **视为该位置权威**并返回 None —— 否则一份损坏的新文件会被旧残留静默盖过。
    """
    for p in ([Path(path)] if path is not None else candidates(root)):
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def current_record(
    root: str | os.PathLike[str] | None = None,
    path: Path | None = None,
) -> dict | None:
    """从认证文件抽出「一个账号」：`{uid, nickname, domain, account, auth}`。

    uid 取自 `account.uid`（其次 `account.id`、顶层 `uid`）。uid 取不到的
    文件对我们没有意义 —— 无法按账号归档，也就无法写回，直接返回 None。
    """
    doc = read_root(root, path)
    if doc is None:
        return None
    account = doc.get("account") if isinstance(doc.get("account"), dict) else {}
    auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
    uid = ""
    for source in (account, doc):
        for key in ("uid", "id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                uid = value.strip()
                break
        if uid:
            break
    if not uid:
        return None

    nickname = secret_text(account.get("nickname"))
    if not nickname:
        # 信封态昵称读不出来是正常的（要解密），退回手机号/空串
        nickname = secret_text(account.get("phoneNumber"))
    domain = secret_text(auth.get("domain")) or secret_text(doc.get("domain"))
    return {
        "uid": uid,
        "nickname": nickname,
        "domain": domain,
        "account": account,
        "auth": auth,
    }


def current_uid(root: str | os.PathLike[str] | None = None) -> str:
    rec = current_record(root)
    return rec["uid"] if rec else ""


# ---------------------------------------------------------------- 备份


def backup(
    base: str | os.PathLike[str] | None = None,
    path: Path | None = None,
    label: str = "auth",
) -> Path | None:
    """把当前认证文件复制到 `wbsync-backup/<stamp>-<label>/`，返回备份文件路径。

    失败返回 None（调用方据此决定是否继续）—— 但**切换流程里备份失败就是中止**，
    因为这是唯一能把登录态退回原状的凭据。

    ★ `path` 缺省时必须用 `auth_path(base)` 而不是 `auth_path()`：后者会解析到
      **真实** AppData，于是单测跑在假 home 上却备份了真文件、又把它回滚进假 home，
      结果是「测试全绿、行为全错」。这个 bug 真发生过。
    """
    from . import guard

    src = Path(path) if path is not None else auth_path(base)
    if not src.is_file():
        return None
    bak = guard.create_backup([src], base=base, label=label)
    for copy_path, _orig in bak.files:
        return copy_path
    return None


# ---------------------------------------------------------------- 写入


class AuthWriteError(RuntimeError):
    """认证文件写入/校验失败。"""


def build_document(record: dict, existing: dict | None = None) -> dict:
    """组装要写入的认证文档。

    `existing` 是当前磁盘上的文件：它的 `accounts` / `allAccounts` 会被保留，
    再把目标账号按 uid 去重后并入 —— 客户端靠这两个数组渲染自己的账号列表，
    整份覆盖会让用户的其它账号从列表里消失。
    """
    account = record.get("account")
    auth = record.get("auth")
    if not isinstance(account, dict) or not isinstance(auth, dict):
        raise AuthWriteError("账号记录缺少 account / auth 区块，无法写入")

    merged: list[dict] = []
    seen: set[str] = set()
    if isinstance(existing, dict):
        for key in ("allAccounts", "accounts"):
            raw = existing.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        merged.append(item)
                break  # 只取其中一个来源，避免两份内容重复叠加
    target_uid = str(record.get("uid") or account.get("uid") or "")
    out_list: list[dict] = []
    for item in merged:
        item_uid = item.get("uid") or item.get("id") or ""
        if item_uid and item_uid == target_uid:
            continue
        if item_uid in seen:
            continue
        if item_uid:
            seen.add(item_uid)
        out_list.append(item)
    out_list.append(account)

    return {
        "account": account,
        "auth": auth,
        "accounts": out_list,
        "allAccounts": out_list,
    }


def write_document(
    doc: dict,
    path: Path | None = None,
    expect_uid: str = "",
) -> None:
    """原子写入认证文件并做写后校验。

    ★ 这里**不备份**：备份由调用方在流程开始时做一次（`backup()`），
      避免「关进程前备一份、写之前再备一份」把凭据留在两个地方。
    """
    target = Path(path) if path is not None else auth_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(doc, ensure_ascii=False, indent=2)
    tmp = target.with_name(target.name + ".wbsync-tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise AuthWriteError(f"写入认证文件失败：{exc.strerror or exc}") from exc

    written = read_root(path=target)
    if written is None:
        raise AuthWriteError("认证文件写后不可解析，已中止")
    got_uid = ""
    account = written.get("account")
    if isinstance(account, dict):
        got_uid = str(account.get("uid") or "")
    if expect_uid and got_uid != expect_uid:
        raise AuthWriteError(
            f"认证文件写后校验失败：account.uid 期望 {expect_uid[:8]}，实际 {got_uid[:8] or '空'}"
        )

    # ★ 按值比对：字符串与信封对象必须用 `==`，不能各自 `.get()` 后当字符串比
    want = doc.get("auth", {}).get("accessToken") if isinstance(doc.get("auth"), dict) else None
    got = written.get("auth", {}).get("accessToken") if isinstance(written.get("auth"), dict) else None
    if want != got:
        raise AuthWriteError("认证文件写后校验失败：accessToken 与待写值不一致")


# ---------------------------------------------------------------- 自检


def selftest_line(base: str | os.PathLike[str] | None = None) -> str:
    """供 `--selftest` 用：只读探测认证文件，不打印任何凭据内容。"""
    p = auth_path(base)
    if not p.is_file():
        return f"auth: 未找到认证文件（{p.parent}）"
    rec = current_record(base)
    if rec is None:
        return f"auth: 认证文件存在但无法解析账号（{p.name}, {p.stat().st_size} 字节）"
    token = rec["auth"].get("accessToken")
    form = "信封" if is_envelope(token) else ("明文" if isinstance(token, str) else "缺失")
    return f"auth: 认证文件就绪, 账号 {rec['uid'][:8]}, 凭据形态 {form}"


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
