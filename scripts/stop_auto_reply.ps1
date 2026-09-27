$ErrorActionPreference = "Stop"

$appDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$configPath = Join-Path $appDir "config.json"
$pythonPath = Join-Path $appDir ".venv\Scripts\python.exe"
$pythonwPath = Join-Path $appDir ".venv\Scripts\pythonw.exe"

. (Join-Path $PSScriptRoot "auto_reply_processes.ps1")

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$config = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$config.enabled = $false
[System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 20), $utf8NoBom)

$running = @(Get-WeChatAutoReplyProcesses -PythonwPath $pythonwPath -PythonPath $pythonPath)
if ($running.Count -eq 0) {
    Write-Host "没有发现正在运行的自动回复进程；已确认 enabled=false"
    return
}

$runningIds = @($running | ForEach-Object { [int]$_.ProcessId })
foreach ($windowProcess in @(Get-Process -Name pythonw, python -ErrorAction SilentlyContinue)) {
    if ($runningIds -contains [int]$windowProcess.Id -and
        $windowProcess.MainWindowTitle -eq "微信自动回复运行中") {
        [void]$windowProcess.CloseMainWindow()
    }
}

$deadline = (Get-Date).AddSeconds(6)
do {
    Start-Sleep -Milliseconds 250
    $running = @(Get-WeChatAutoReplyProcesses -PythonwPath $pythonwPath -PythonPath $pythonPath)
} while ($running.Count -gt 0 -and (Get-Date) -lt $deadline)

if ($running.Count -gt 0) {
    $roots = @($running | Where-Object {
        $_.ExecutablePath -ieq $pythonwPath -or $_.ExecutablePath -ieq $pythonPath
    })
    if ($roots.Count -gt 0) {
        foreach ($process in $roots) {
            & taskkill.exe /PID ([int]$process.ProcessId) /T /F | Out-Null
        }
    } else {
        foreach ($process in $running) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
}

$deadline = (Get-Date).AddSeconds(5)
do {
    Start-Sleep -Milliseconds 250
    $running = @(Get-WeChatAutoReplyProcesses -PythonwPath $pythonwPath -PythonPath $pythonPath)
} while ($running.Count -gt 0 -and (Get-Date) -lt $deadline)

if ($running.Count -gt 0) {
    throw "自动回复进程仍未退出：$((($running | ForEach-Object ProcessId) -join ', '))"
}
Write-Host "自动回复已停止；进程树已退出，enabled=false"
