# 一鍵啟動空拍機雙影像 RTSP 串流模擬器。
$ErrorActionPreference = 'Stop'

function Test-Python310 {
    param(
        [string]$Executable,
        [string[]]$Prefix = @()
    )

    try {
        & $Executable @Prefix -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

$pythonExecutable = $null
$pythonPrefix = @()
$localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ((Test-Path -LiteralPath $localPython -PathType Leaf) -and (Test-Python310 -Executable $localPython)) {
    $pythonExecutable = $localPython
}

if (-not $pythonExecutable) {
    $pythonCommands = @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($commandInfo in $pythonCommands) {
        $candidate = $commandInfo.Path
        # WindowsApps 的 python.exe 可能只是導向 Microsoft Store 的別名。
        if ($candidate -and $candidate -notmatch '(?i)[\\/]WindowsApps[\\/]' -and
            (Test-Path -LiteralPath $candidate -PathType Leaf) -and
            (Test-Python310 -Executable $candidate)) {
            $pythonExecutable = $candidate
            break
        }
    }
}

if (-not $pythonExecutable) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if ((Test-Path -LiteralPath $bundledPython -PathType Leaf) -and (Test-Python310 -Executable $bundledPython)) {
        $pythonExecutable = $bundledPython
    }
}

if (-not $pythonExecutable) {
    $pyCommands = @(Get-Command py.exe -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($commandInfo in $pyCommands) {
        if (Test-Python310 -Executable $commandInfo.Path -Prefix @('-3')) {
            $pythonExecutable = $commandInfo.Path
            $pythonPrefix = @('-3')
            break
        }
    }
}

if (-not $pythonExecutable) {
    throw '找不到可用的 Python 3.10 以上版本。請提供 Python，或建立本資料夾的 .venv；啟動器不會自動安裝軟體。'
}

$simulatorPath = Join-Path $PSScriptRoot 'drone_stream_simulator.py'
if (-not (Test-Path -LiteralPath $simulatorPath -PathType Leaf)) {
    throw "找不到模擬器：$simulatorPath"
}

$exitCode = 0
$launchArguments = @($args)
if ($launchArguments -notcontains '--headless' -and $launchArguments -notcontains '--rtsp') {
    $launchArguments = @('--rtsp') + $launchArguments
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $pythonExecutable @pythonPrefix $simulatorPath @launchArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode
