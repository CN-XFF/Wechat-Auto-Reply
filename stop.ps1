$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'scripts\stop_auto_reply.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw "未找到停止脚本：$scriptPath" }
& $scriptPath
