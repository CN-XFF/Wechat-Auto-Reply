$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'scripts\start_auto_reply.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw "未找到启动脚本：$scriptPath" }
& $scriptPath
