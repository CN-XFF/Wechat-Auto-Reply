# 微信自动回复助手

Windows 桌面微信文字消息监听与自动回复工具。

本目录包含 Windows 安装包构建脚本和安装说明。运行 `build_release.ps1` 会在 `output` 与 `release` 目录生成发行候选；它只使用 `config.example.json`，不会读取或打包项目根目录的真实 `config.json`、备份、聊天数据库、日志、缓存或 `.git`。

维护者构建环境和发布流程见 `BUILDING.md`；GitHub 发布前检查表见仓库根目录的 `GITHUB_PUBLISHING_CHECKLIST.md`。

安装包默认按当前 Windows 用户安装。首次启动会要求用户填写本机微信数据库位置和账号目录名；自动回复、试运行、远程指令和近期联系人读取均采用安全默认值。安装器不会自动启动微信或发送消息。发布前请阅读 `INSTALLATION_GUIDE.md` 并完成其中的版权、许可、隐私和独立机器运行检查。

安装包还包含 `CODEX_AFTER_INSTALL_HANDOFF.md`，开始菜单提供打开该说明的入口。

本发行候选的第三方组件 `wechatauto-replica` 使用 Apache License 2.0；许可证文本会随发行文件一同收集。安装器编译器 Inno Setup 自身不随本软件安装包分发。
