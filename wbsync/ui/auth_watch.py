"""认证文件变更监听 —— 外部软件切换登录后，本工具当场就知道。

## 为什么盯目录，而不是只盯那个文件

`workbuddy-desktop.info` 的写法是「临时文件 + 原子替换」（客户端、本工具、
以及第三方的切换工具都这么干）。Windows 上 `os.replace` 等价于删旧建新，
`QFileSystemWatcher` 记的是那一份文件，替换之后监听就断了 —— 表现为
「第一次能收到事件，之后再也不响」。

所以这里**目录与文件都挂**：目录本身不会被替换，是长期有效的那一条；
文件那一条用来保证「改名/替换」这类目录级通知在某些平台上不触发时仍有信号，
每次判断完再按需补挂（`_arm` 幂等）。

## 去抖：一次切换会打好几次门

写临时文件、替换、客户端退出时的回写，各算一次事件。每次都读文件既浪费
也会让界面抖，所以合并成一个单发定时器（600ms）。依据：认证文件只有几 KB，
写入窗口远小于它；再长就会让人觉得「反应慢半拍」。

## 读到空 uid 不算「切换」

文件正在被截断重写时解析失败，uid 为空。这是中间态，必须忽略 —— 真正写完
之后还会再来一次事件，那时才判断。登出（uid 变空且不再回来）本工具也没有
可做的事，同样不报。

## 自己切换要提前打招呼

本工具的「切换登录」也写这个文件，写完一样会触发监听。`expect()` 用来登记
「接下来这个 uid 是我自己写进去的」，送到这一位时不发信号 —— 切换流程结束
本来就会重盘并弹结果框，再补一条「检测到登录已变更」纯属噪音。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from .. import authfile

DEBOUNCE_MS = 600
# 目录晚建时的兜底轮询间隔。只在这台机器「从没登录过、目录还不存在」时才会
# 生效，挂上监听就停 —— 不是一个常驻定时器。
PROBE_MS = 10_000


class AuthWatcher(QObject):
    """盯住认证文件，登录身份（`account.uid`）变化时发 `switched(old, new)`。

    只在「读到新身份且与已知身份不同」时报；读不到（写入中间态 / 已登出）
    一律跳过，且**不用空值覆盖已知身份** —— 否则一次瞬时删文件就会把
    「上次是谁」擦掉。首次登录（工具启动时还没登录）按 `("", uid)` 上报。
    """

    switched = Signal(str, str)

    def __init__(
        self,
        parent: QObject | None = None,
        path: Path | None = None,
        debounce_ms: int = DEBOUNCE_MS,
        probe_ms: int = PROBE_MS,
    ) -> None:
        super().__init__(parent)
        self._file = Path(path) if path is not None else authfile.auth_path()
        self._last_uid = self._read_uid()
        self._expected_uid = ""

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_changed)
        self._watcher.fileChanged.connect(self._on_changed)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._check)

        # 认证目录还不存在时没有可挂的路径（它的上级可能也不存在，一路新建）。
        # 这时用一个低频轮询兜底：挂上就停，挂上后立刻补一次判断 —— 登录那一刻
        # 写入的文件早于监听建立，不补判会一直停在「未登录」。
        self._probe = QTimer(self)
        self._probe.setInterval(probe_ms)
        self._probe.timeout.connect(self._probe_arm)

        if not self._arm():
            self._probe.start()

    # ------------------------------------------------------------ 监听
    def _arm(self) -> bool:
        """挂上目录与文件，返回认证目录是否已在监听。

        已经挂着的跳过；文件那条在原子替换后必然失效，所以每次判断都补挂。
        """
        d = self._file.parent
        ok = False
        if d.is_dir():
            s = str(d)
            if s not in self._watcher.directories():
                self._watcher.addPath(s)
            ok = True
            if self._probe.isActive():
                self._probe.stop()
        f = str(self._file)
        if self._file.is_file() and f not in self._watcher.files():
            self._watcher.addPath(f)
        return ok

    def _probe_arm(self) -> None:
        """兜底轮询：目录建出来就挂监听，并立刻按现状判断一次。"""
        if self._arm():
            self._probe.stop()
            self._check()

    def _on_changed(self, _path: str) -> None:
        # 目录里任何文件动一下都会通知；真正关心的是 uid，判断推迟到去抖之后
        self._timer.start()

    def _read_uid(self) -> str:
        """从**被监听的那份文件**取 uid。

        ★ 不能用 `authfile.current_uid()`：那个会按候选列表解析到本机真实
          认证文件，传了 `path`（单测、自检）时会读到别人家的身份 ——
          首轮比对直接两边不是同一份文件，测试全绿、行为全错。
        """
        rec = authfile.current_record(path=self._file)
        return rec["uid"] if rec else ""

    def _check(self) -> None:
        self._arm()  # 目录被删过又建回来时，重新挂上
        uid = self._read_uid()
        if not uid:
            return  # 写入中间态，等下一次事件
        if uid == self._last_uid:
            self._expected_uid = ""  # 内容没变 / 自己切换的回写
            return
        if uid == self._expected_uid:
            self._last_uid = uid
            self._expected_uid = ""
            return
        old, self._last_uid = self._last_uid, uid
        self.switched.emit(old, uid)

    # ------------------------------------------------------------ 外部接口
    def expect(self, uid: str) -> None:
        """登记「这个 uid 由本工具写入」；送到时不当作外部切换。传空串撤销。"""
        self._expected_uid = uid or ""
