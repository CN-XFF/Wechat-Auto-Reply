# 微信自动回复助手安装与迁移指南

本指南用于在 Windows 电脑上安装通用版程序。项目包不应包含任何人的实际联系人列表、微信聊天记录、密码或本机运行数据。

## 准备

1. 安装并登录 Windows 微信，确认使用正确的账号。
2. 安装 64 位 Python 3.12。
3. 如需 AI 回复，安装并登录 Codex，并确认所选模型可用。登录凭据不会包含在项目中。
4. 将项目解压到固定、可写的目录；不要放在微信数据库目录中。

## 创建本机配置

在项目根目录复制 `config.example.json` 并命名为 `config.json`，然后只在本机填写：

- `db_dir`：当前微信账号的数据目录。
- `account`：当前微信账号标识。
- `targets`：经本人核对后需要监听的联系人。每项需要准确的 `name` 和 `username`，并逐项确认监听、自动回复和等待时间。
- 如确有需要，再配置指令联系人和新的本机密码。不要把密码写进说明、测试样例或公开仓库。
- 按当前 Codex 账号的可用权限设置模型与推理强度。

示例文件默认 `enabled=false`、`dry_run=true`、联系人列表为空、远程指令关闭。首次启动前保持这些安全状态并核对设置。不要把真实 `config.json`、聊天数据库、导出、日志或备份加入发行包。

## 安装依赖

在项目根目录打开 PowerShell，逐行执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".\third_party\wechatauto-replica[guia]"
```

## 试运行、启动与停止

先安装依赖并检查 `config.json`。`start_auto_reply.ps1` 会打开全局运行开关，因此只有在联系人、数据路径和试运行状态都核对后才运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_auto_reply.ps1
```

示例配置启用了试运行，程序不会真实发送。只有在本人确认风险、明确决定启用真实发送后，才可在本机配置中将 `dry_run` 改为 `false`，并确保自动回复联系人范围正确。

停止程序：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_auto_reply.ps1
```

测试通过不代表已验证当前微信版本、窗口定位、账号状态或真实发送。不要在真实联系人上盲测发送。

## 隐私与发行

- 公开发行时仅提供 `config.example.json`，不提供 `config.json`、`.venv`、微信数据库、聊天记录、日志、导出、运行状态、缓存和备份。
- 不要公开联系人标识、聊天内容、指令密码、API 凭据或用户目录路径。
- 本程序依赖桌面自动化，不是微信官方功能；界面更新、窗口尺寸、遮挡和系统缩放都可能影响运行，并存在账号风控风险。
- `third_party/wechatauto-replica` 是第三方依赖；发布前核对其许可证、版权声明和分发条件。
