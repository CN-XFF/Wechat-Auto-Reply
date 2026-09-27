$ErrorActionPreference = "Stop"

function Get-WeChatAutoReplyProcesses {
    param(
        [Parameter(Mandatory = $true)][string]$PythonwPath,
        [Parameter(Mandatory = $true)][string]$PythonPath
    )

    # 兼容绝对路径、相对路径以及旧启动器附加参数的命令行。
    $scriptPattern = '(?i)(?:^|[\s\\/"])app\.py(?=["\s]|$)'
    $all = @(Get-CimInstance -ClassName Win32_Process)
    $found = [System.Collections.Generic.Dictionary[int, object]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()

    foreach ($process in $all) {
        $isLauncher = ($process.ExecutablePath -ieq $PythonwPath) -or
                      ($process.ExecutablePath -ieq $PythonPath)
        if ($isLauncher -and $process.CommandLine -match $scriptPattern) {
            $pidValue = [int]$process.ProcessId
            $found[$pidValue] = $process
            $queue.Enqueue($pidValue)
        }
    }

    # 虚拟环境启动器会再创建真正的 Python 解释器进程；一并纳入同一进程树。
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($process in $all) {
            $pidValue = [int]$process.ProcessId
            if ([int]$process.ParentProcessId -eq $parentId -and
                $process.Name -match '^python(w)?\.exe$' -and
                $process.CommandLine -match $scriptPattern -and
                -not $found.ContainsKey($pidValue)) {
                $found[$pidValue] = $process
                $queue.Enqueue($pidValue)
            }
        }
    }

    # 兜底识别启动器已退出但程序窗口仍存活的解释器进程。
    foreach ($windowProcess in @(Get-Process -Name pythonw, python -ErrorAction SilentlyContinue)) {
        if ($windowProcess.MainWindowTitle -ne "微信自动回复运行中") {
            continue
        }
        $pidValue = [int]$windowProcess.Id
        if ($found.ContainsKey($pidValue)) {
            continue
        }
        $process = $all | Where-Object { [int]$_.ProcessId -eq $pidValue } | Select-Object -First 1
        if ($process -and $process.CommandLine -match $scriptPattern) {
            $found[$pidValue] = $process
        }
    }

    return @($found.Values)
}
