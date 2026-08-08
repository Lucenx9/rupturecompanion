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
$StateDir = Join-Path $env:LOCALAPPDATA "RuptureCompanion"
$DataDir = $StateDir
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

& $Python (Join-Path $PSScriptRoot "updater.py") 2>> `
    (Join-Path $StateDir "updater.log")
$BackendDir = Join-Path $DataDir "backend"
$BackendPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath (Join-Path $BackendDir "daemon.py")) {
    & uv sync --project $BackendDir --locked --no-dev 2>> `
        (Join-Path $StateDir "updater.log")
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $BackendPython)) {
        $Python = $BackendPython
    } else {
        $BackendDir = $PSScriptRoot
    }
} else {
    $BackendDir = $PSScriptRoot
}

if ($env:RC_BRIDGE_DIR) {
    $BridgeDir = $env:RC_BRIDGE_DIR
} else {
    $GameExecutable = [System.IO.Path]::GetFullPath($GameCommand[0])
    $BridgeDir = Join-Path ([System.IO.Path]::GetDirectoryName($GameExecutable)) `
        "RuptureCompanion"
}
$env:RC_BRIDGE_DIR = $BridgeDir
New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null

$DaemonScript = Join-Path $BackendDir "daemon.py"
$DaemonArgument = '"' + $DaemonScript.Replace('"', '\"') + '"'
$Daemon = Start-Process -FilePath $Python -ArgumentList $DaemonArgument `
    -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $StateDir "daemon.log") `
    -RedirectStandardError (Join-Path $StateDir "daemon-error.log")
try {
    $Ready = $false
    $LockFile = Join-Path $BridgeDir "daemon.lock"
    for ($Attempt = 0; $Attempt -lt 200; $Attempt++) {
        if ($Daemon.HasExited) { break }
        if (Test-Path -LiteralPath $LockFile) {
            $LockPid = ([System.IO.File]::ReadAllText($LockFile)).Trim()
            if ($LockPid -eq [string]$Daemon.Id) {
                $Ready = $true
                break
            }
        }
        Start-Sleep -Milliseconds 50
    }
    if (-not $Ready) {
        throw "Companion daemon did not start. Check $StateDir."
    }

    $Executable = $GameCommand[0]
    $Arguments = @()
    if ($GameCommand.Count -gt 1) {
        $Arguments = $GameCommand[1..($GameCommand.Count - 1)]
    }
    & $Executable @Arguments
    $GameStatus = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
} finally {
    if ($Daemon -and -not $Daemon.HasExited) {
        & taskkill.exe /PID $Daemon.Id /T /F 2>$null | Out-Null
    }
}
exit $GameStatus
