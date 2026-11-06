# Buddy-atlas

Windows 桌面工具，用来盘点、清理、对齐多个账号下的本地会话与工作空间。数据全部留在本机。

> 非官方项目。与腾讯、WorkBuddy / CodeBuddy 及其开发方没有隶属、授权或背书关系。文中提到的 `WorkBuddy`、`CodeBuddy`、`Buddy Switch` 只用于说明数据来自哪里，商标归各自权利人所有。本工具不修改、不上传第三方客户端的数据，也不调用其在线接口。

## 能做什么

按账号列出本地会话、工作空间和云端映射状态，用互斥分档区分「已删除 / 缺正文 / 本地可见 / 未在侧边栏」。

批量隐藏已删除会话。云端会话不在本地物理删除：那一行是客户端判断「这条我删了」的唯一信号，删掉之后重启会被重新下发，所以改成隐藏，随时可以恢复。

在多账号之间做会话并集对齐，按正文内容指纹判重，落点分级决定自动采用还是留作候选。跨设备补齐靠导出的离线同步包（会话正文、配置、可选的工作空间记忆）；导入时按正文末行时间戳判断分叉，覆盖前单独备份。

账号卡只读展示昵称和积分快照里最近一次观察到的数值，没有记录就显示「—」，不用 0 冒充余额。

## 隐私边界

不联网，不上传，不读取或解密凭据文件。只读取本机这几处数据：

| 用途 | 位置 |
|---|---|
| 会话正文与元数据 | `~/.workbuddy/projects/**/*.jsonl`、`~/.workbuddy/workbuddy.db` |
| 云端映射 | `~/.workbuddy/edge-sync-mapping-v*.db` |
| 账号信息 | 客户端 auth 信息文件、`~/.buddy-switch/accounts.json`、`credit_usage_snapshots.json` |

积分列是外部工具留下的观察值，不是实时余额，界面会标出观察时间。会话正文是你的私有数据，导出的同步包自己保管好。

这些目录属于第三方客户端的内部实现，客户端升级可能改格式，本工具不保证兼容未来版本。

## 运行

Windows 10 / 11，Python 3.13+（3.13 和 3.14 验过），PySide6 6.9+。

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## 打包成单文件 exe

```bash
pip install pyinstaller
pyinstaller wbsync.spec
```

`wbsync.spec` 从 `wbsync/__init__.py` 的 `__version__` 取版本号，写进 exe 的文件属性。想用 UPX 压缩就自己准备官方未修改版，通过 `--upx-dir` 指定；仓库里不带任何第三方二进制文件。

## 版本

首个公开版是 v1.14.0，沿用内部开发序列，所以不是从 1.0.0 起。版本号只在 `wbsync/__init__.py` 定义一次，窗口标题、侧栏和 exe 文件属性都从这里读。

## 许可与贡献

源码用 [MIT](LICENSE)。第三方组件许可和分发二进制时要做的事写在 [NOTICE](NOTICE)。

有问题开 issue 或发 PR 都行。别在 issue、截图、测试夹具里贴真实账号、会话正文、uid 或本机绝对路径。安全问题走 GitHub 的私有安全报告通道，不要开公开 issue。

## 已知限制

只支持 Windows，进程控制部分直接调 Win32 API。仓库只放运行和构建必需的源码，不含单元测试、设计稿和内部发布脚本。依赖第三方客户端的本地数据格式，客户端升级后可能需要适配。
