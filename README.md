# 微信自动回复助手

适用于 Windows 桌面微信的文字消息监听与回复工具。可按本机配置监听指定会话，并通过本机 Codex CLI 生成回复。它不是微信官方功能。

> 本项目仍处于测试发行阶段。微信界面、系统缩放或窗口状态变化可能影响定位和发送；真实发送存在误发和账号风控风险。

## 使用安装包

从 GitHub Releases 下载 Windows 安装程序并按向导安装。首次启动会要求选择微信数据库根目录并填写账号文件夹名称；首次设置不会自动启用自动回复、远程指令或近期联系人读取。

运行 AI 回复还需要本机已安装并由使用者登录的 Codex CLI。软件运行不需要 Codex 插件或 OpenAI API Key。安装程序不会替使用者登录微信或 Codex。

## 从源码运行

要求 Windows 10/11、64 位 Python 3.12 和 Windows 微信客户端。AI 回复需要另行准备已登录的 Codex CLI。

在仓库根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".\third_party\wechatauto-replica[guia]"
```

运行 `python app.py` 后按首次设置窗口填写本机数据库根目录和账号文件夹名称。源码模式也可先将 `config.example.json` 复制为 `config.json`；示例配置中的路径和账号是占位值。

初次检查保持 `enabled=false`、`dry_run=true`、联系人列表为空。`scripts\start_auto_reply.ps1` 会开启全局运行开关；运行前先逐项核对路径、联系人和发送设置。只有你明确决定启用真实发送后，才更改试运行状态。

停止脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_auto_reply.ps1
```

## 构建 Windows 安装包

维护者可按 [安装包构建说明](distribution/BUILDING.md) 准备隔离的构建环境并生成安装程序。构建输出和安装程序被 `.gitignore` 排除；发布时应将安装程序作为 GitHub Release 附件，而不是提交到源码仓库。

## 隐私与安全

- 本机 `config.json` 可能含账号路径、联系人标识、风格内容或指令密码；不要提交、分享或放进安装包。
- 程序从本机微信数据目录读取会话数据。启用 AI 回复时，相关消息和配置的上下文会交给本机 Codex CLI 使用的模型处理；请先确认自己接受其数据处理范围。
- 不要将微信数据库、聊天记录、日志、导出内容、密钥或备份加入公开仓库。`.gitignore` 已排除常见本机数据和构建目录，但公开前仍需人工检查 Git 暂存内容。
- `third_party/wechatauto-replica` 是单独许可的第三方依赖，其 Apache-2.0 许可文本随源码保留。

## 许可证

本项目自身尚未声明开源许可证。公开前请由项目所有者选择并添加许可证；第三方依赖的许可证不自动适用于本项目。
