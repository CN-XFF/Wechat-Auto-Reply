$ErrorActionPreference = "Stop"

$appDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$appPath = Join-Path $appDir "app.py"
$configPath = Join-Path $appDir "config.json"
$pythonPath = Join-Path $appDir ".venv\Scripts\python.exe"
$pythonwPath = Join-Path $appDir ".venv\Scripts\pythonw.exe"
$launcherPath = if (Test-Path -LiteralPath $pythonwPath) { $pythonwPath } else { $pythonPath }

. (Join-Path $PSScriptRoot "auto_reply_processes.ps1")

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$config = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$wasEnabled = [bool]$config.enabled
$running = @(Get-WeChatAutoReplyProcesses -PythonwPath $pythonwPath -PythonPath $pythonPath)
$runningIds = @($running | ForEach-Object { [int]$_.ProcessId })
$statusWindows = @(Get-Process -Name pythonw, python -ErrorAction SilentlyContinue | Where-Object {
    $runningIds -contains [int]$_.Id -and $_.MainWindowTitle -eq "微信自动回复运行中"
})

if ($running.Count -gt 0 -and $wasEnabled -and $statusWindows.Count -gt 0) {
    Write-Host "微信自动回复已在运行，状态窗口 PID: $(($statusWindows.Id) -join ', ')"
    return
}

if ($running.Count -gt 0) {
    if ($wasEnabled) {
        Write-Host "检测到自动回复进程存在但状态窗口未打开，正在清理后重新启动"
    } else {
        Write-Host "检测到自动回复进程仍在运行但配置为暂停，正在重启以应用开启状态"
    }
    foreach ($windowProcess in @(Get-Process -Name pythonw, python -ErrorAction SilentlyContinue)) {
        if ($runningIds -contains [int]$windowProcess.Id -and
            $windowProcess.MainWindowTitle -eq "微信自动回复运行中") {
            [void]$windowProcess.CloseMainWindow()
        }
    }

    $deadline = (Get-Date).AddSeconds(5)
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
        $deadline = (Get-Date).AddSeconds(5)
        do {
            Start-Sleep -Milliseconds 250
            $running = @(Get-WeChatAutoReplyProcesses -PythonwPath $pythonwPath -PythonPath $pythonPath)
        } while ($running.Count -gt 0 -and (Get-Date) -lt $deadline)
        if ($running.Count -gt 0) {
            throw "无法停止旧自动回复进程，已取消启动新实例"
        }
    }
}

$config.enabled = $true
[System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 20), $utf8NoBom)
if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "找不到 Python 启动器：$launcherPath"
}
$process = Start-Process -FilePath $launcherPath -ArgumentList ('"{0}"' -f $appPath) -WorkingDirectory $appDir -PassThru
$deadline = (Get-Date).AddSeconds(45)
$readyProcess = $null
do {
    Start-Sleep -Milliseconds 300
    $running = @(Get-WeChatAutoReplyProcesses -PythonwPath $pythonwPath -PythonPath $pythonPath)
    foreach ($candidate in $running) {
        $window = Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction SilentlyContinue
        if ($window -and $window.MainWindowTitle -eq "微信自动回复运行中") {
            $readyProcess = $window
            break
        }
    }
} while (-not $readyProcess -and (Get-Date) -lt $deadline)

if (-not $readyProcess) {
    $logPath = Join-Path $appDir "logs\wechat-auto-reply.log"
    $state = if ($running.Count -eq 0) { "程序进程已退出" } else { "进程存在但状态窗口没有启动" }
    $message = "自动回复启动未确认：$state。请检查日志：$logPath"
    if ($running.Count -gt 0) {
        # 不留下无窗口、也没有进入监听状态的隐藏进程，避免之后重复启动互相抢锁。
        $roots = @($running | Where-Object {
            $_.ExecutablePath -ieq $pythonwPath -or $_.ExecutablePath -ieq $pythonPath
        })
        if ($roots.Count -gt 0) {
            foreach ($stuck in $roots) {
                & taskkill.exe /PID ([int]$stuck.ProcessId) /T /F | Out-Null
            }
        } else {
            foreach ($stuck in $running) {
                Stop-Process -Id ([int]$stuck.ProcessId) -Force -ErrorAction SilentlyContinue
            }
        }
        $message += "；已清理未完成启动的后台进程"
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $message, "微信自动回复启动失败",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        Write-Error $message
    }
    throw $message
}

Write-Host "自动回复已启动并确认状态窗口，PID: $($readyProcess.Id)"
