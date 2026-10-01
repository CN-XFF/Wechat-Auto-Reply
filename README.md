# 微信自动回复助手

面向 Windows 桌面微信的文字消息监听与自动回复工具。程序可按本机配置监听指定会话，并通过本地 Codex CLI 生成回复；敏感或高风险内容可要求人工确认。

本项目是社区工具，不是微信官方功能。桌面自动化会受微信版本、窗口状态和系统显示设置影响，使用者应先在试运行模式下检查行为，再自行决定是否启用真实发送。

## 功能与默认安全设置

- 支持按本机配置选择监听对象、回复规则和确认流程。
- 默认全局自动回复关闭、试运行开启、联系人列表为空，远程指令关闭。
- AI 回复通过本机 Codex CLI 运行；安装包不包含账号凭据或 API 密钥。
- 配置、日志和运行状态保存在本机。请勿把 `config.json`、微信数据库、日志或令牌提交到公开仓库。

## 安装

1. 从 GitHub Releases 下载 Windows x64 安装程序并运行。
2. 安装并登录本机微信；程序不会替你登录或迁移微信数据。
3. 如需 AI 回复，另行安装并登录 Codex CLI。
4. 首次配置时选择本机微信文件夹和当前账号，点击“保存并读取近期联系人”。名单先显示在界面中，监听和自动回复均关闭，由使用者逐个选择。
5. 需要更换账号或重新配置时，关闭程序，再从开始菜单打开“重新配置微信与联系人”。默认参数说明见 [`CONFIG_TUNING.md`](CONFIG_TUNING.md)。
6. 找不到 Codex 程序时，在主界面点击“选择 Codex 程序…”指定本机实际 codex.exe；保存前备份配置，也可恢复自动查找。
7. 主界面会后台查询 CLI 版本和模型目录，可点击“刷新模型列表”。下拉框采用 CLI 返回的模型与推理档位，目录可能来自缓存，权限以实际调用为准；旧 CLI 缺少必要参数时提示升级。

## 从源码构建

需要 Windows x64、Python 3.12 和 Inno Setup 7。准备构建环境后，在项目根目录运行：

```powershell
py -3.12 -m venv .\distribution\build-env
.\distribution\build-env\Scripts\python.exe -m pip install -r .\distribution\requirements-build.lock.txt
.\distribution\build-env\Scripts\python.exe -m pip install --no-deps .\vendor\wechatauto-replica
powershell -NoProfile -ExecutionPolicy Bypass -File .\distribution\build_release.ps1
```

更多说明见 [`distribution/BUILDING.md`](distribution/BUILDING.md)、[`distribution/INSTALLATION_GUIDE.md`](distribution/INSTALLATION_GUIDE.md) 和 [`CHANGELOG.md`](CHANGELOG.md)。

## 第三方组件与许可

`vendor/wechatauto-replica` 保留其上游许可文件。主程序自身的再分发许可尚未单独声明；第三方组件的许可不等同于主程序许可。
