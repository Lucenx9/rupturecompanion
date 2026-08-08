[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$GameCommand
)

$ErrorActionPreference = "Stop"
if (-not $GameCommand -or $GameCommand.Count -eq 0) {
    throw "Usage: run-with-companion.ps1 <game command> [arguments...]"
}

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Companion Python environment not found. Run: uv sync --group dev"
}
$BootstrapPython = $Python
$StateDir = Join-Path $env:LOCALAPPDATA "RuptureCompanion"
$DataDir = $StateDir
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

& $BootstrapPython (Join-Path $PSScriptRoot "updater.py") 2>> `
    (Join-Path $StateDir "updater.log")
$BackendDir = Join-Path $DataDir "backend"
$BackendPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath (Join-Path $BackendDir "daemon.py")) {
    & uv sync --project $BackendDir --locked --no-dev 2>> `
        (Join-Path $StateDir "updater.log")
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $BackendPython)) {
        $Python = $BackendPython
    } else {
        & $BootstrapPython (Join-Path $PSScriptRoot "updater.py") --rollback 2>> `
            (Join-Path $StateDir "updater.log")
        $BackendDir = $PSScriptRoot
    }
} else {
    $BackendDir = $PSScriptRoot
}
$DaemonReadyProtocol = 0
if (Select-String -LiteralPath (Join-Path $BackendDir "daemon.py") `
    -SimpleMatch "READY_PROTOCOL_VERSION = 1" -Quiet) {
    $DaemonReadyProtocol = 1
}
$env:RC_DAEMON_READY_PROTOCOL = [string]$DaemonReadyProtocol

if ($env:RC_BRIDGE_DIR) {
    $BridgeDir = $env:RC_BRIDGE_DIR
} else {
    $GameExecutable = [System.IO.Path]::GetFullPath($GameCommand[0])
    $BridgeDir = Join-Path ([System.IO.Path]::GetDirectoryName($GameExecutable)) `
        "RuptureCompanion"
}
$env:RC_BRIDGE_DIR = $BridgeDir
New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null

function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)
    if ($Process -and -not $Process.HasExited) {
        & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    }
}

function Start-CompanionDaemon {
    $daemonScript = Join-Path $BackendDir "daemon.py"
    $daemonArgument = '"' + $daemonScript.Replace('"', '\"') + '"'
    return Start-Process -FilePath $Python -ArgumentList $daemonArgument `
        -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $StateDir "daemon.log") `
        -RedirectStandardError (Join-Path $StateDir "daemon-error.log")
}

function Wait-CompanionDaemon {
    param([System.Diagnostics.Process]$Process)
    $lockFile = Join-Path $BridgeDir "daemon.lock"
    $readyFile = Join-Path $BridgeDir "daemon.ready"
    $startupAttempts = 0
    $daemonLive = $false
    while ($true) {
        $Process.Refresh()
        if ($Process.HasExited) { return $false }
        if (Test-Path -LiteralPath $lockFile) {
            $lockPid = ([System.IO.File]::ReadAllText($lockFile)).Trim()
            if ($lockPid -eq [string]$Process.Id) {
                $daemonLive = $true
                if ($DaemonReadyProtocol -ne 1) { return $true }
                if (Test-Path -LiteralPath $readyFile) {
                    $readyPid = ([System.IO.File]::ReadAllText($readyFile)).Trim()
                    if ($readyPid -eq [string]$Process.Id) { return $true }
                }
            }
        }
        if (-not $daemonLive) {
            $startupAttempts++
            if ($startupAttempts -ge 200) { return $false }
        }
        Start-Sleep -Milliseconds 50
    }
}

function ConvertTo-NativeArgument {
    param([string]$Argument)
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
        } elseif ($character -eq [char]34) {
            [void]$builder.Append((('\' * (2 * $backslashes + 1)) -join ''))
            [void]$builder.Append('"')
            $backslashes = 0
        } else {
            [void]$builder.Append((('\' * $backslashes) -join ''))
            [void]$builder.Append($character)
            $backslashes = 0
        }
    }
    [void]$builder.Append((('\' * (2 * $backslashes)) -join ''))
    [void]$builder.Append('"')
    return $builder.ToString()
}

$Daemon = Start-CompanionDaemon
$Game = $null
try {
    if (-not (Wait-CompanionDaemon $Daemon)) {
        & $BootstrapPython (Join-Path $PSScriptRoot "updater.py") --rollback 2>> `
            (Join-Path $StateDir "updater.log")
        throw "Companion daemon did not start. Check $StateDir."
    }
    & $BootstrapPython (Join-Path $PSScriptRoot "updater.py") --confirm 2>> `
        (Join-Path $StateDir "updater.log")

    $Executable = $GameCommand[0]
    $Arguments = @()
    if ($GameCommand.Count -gt 1) {
        $Arguments = $GameCommand[1..($GameCommand.Count - 1)]
    }
    $GameInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $GameInfo.FileName = $Executable
    $GameInfo.UseShellExecute = $false
    if ($GameInfo.PSObject.Properties.Name -contains "ArgumentList") {
        foreach ($Argument in $Arguments) { [void]$GameInfo.ArgumentList.Add($Argument) }
    } else {
        $GameInfo.Arguments = (($Arguments | ForEach-Object {
            ConvertTo-NativeArgument $_
        }) -join ' ')
    }
    $Game = [System.Diagnostics.Process]::Start($GameInfo)

    while (-not $Game.HasExited) {
        if ($Daemon) {
            $Daemon.Refresh()
            if ($Daemon.HasExited) {
                $Daemon.WaitForExit()
                $Daemon = Start-CompanionDaemon
                if (Wait-CompanionDaemon $Daemon) {
                    & $BootstrapPython (Join-Path $PSScriptRoot "updater.py") --confirm `
                        2>> (Join-Path $StateDir "updater.log")
                } else {
                    Write-Warning "Companion daemon could not restart; check $StateDir."
                    Stop-ProcessTree $Daemon
                    $Daemon = $null
                }
            }
        }
        Start-Sleep -Milliseconds 250
        $Game.Refresh()
    }
    $Game.WaitForExit()
    $GameStatus = $Game.ExitCode
} finally {
    Stop-ProcessTree $Daemon
    if ($Game -and -not $Game.HasExited) {
        Stop-ProcessTree $Game
    }
}
exit $GameStatus
