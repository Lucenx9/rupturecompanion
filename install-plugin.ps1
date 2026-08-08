[CmdletBinding()]
param(
    [string]$GameRoot = $env:RC_GAME_DIR
)

$ErrorActionPreference = "Stop"
$ReleaseBase = "https://github.com/Lucenx9/rupturecompanion/releases/latest/download"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Find-StarRupture {
    param([string]$RequestedRoot)

    if ($RequestedRoot) {
        return [System.IO.Path]::GetFullPath($RequestedRoot)
    }

    $steamRoots = [System.Collections.Generic.List[string]]::new()
    try {
        $steamPath = (Get-ItemProperty "HKCU:\Software\Valve\Steam").SteamPath
        if ($steamPath) { $steamRoots.Add($steamPath) }
    } catch {
    }
    $defaultSteam = Join-Path ${env:ProgramFiles(x86)} "Steam"
    if (Test-Path -LiteralPath $defaultSteam) { $steamRoots.Add($defaultSteam) }

    foreach ($steamRoot in $steamRoots) {
        $libraries = [System.Collections.Generic.List[string]]::new()
        $libraries.Add($steamRoot)
        $vdf = Join-Path $steamRoot "steamapps\libraryfolders.vdf"
        if (Test-Path -LiteralPath $vdf) {
            $vdfText = [System.IO.File]::ReadAllText($vdf)
            foreach ($match in [regex]::Matches($vdfText, '"path"\s+"([^"]+)"')) {
                $libraries.Add($match.Groups[1].Value.Replace("\\", "\"))
            }
        }
        foreach ($library in $libraries) {
            $manifest = Join-Path $library "steamapps\appmanifest_1631270.acf"
            if (Test-Path -LiteralPath $manifest) {
                return Join-Path $library "steamapps\common\StarRupture"
            }
        }
    }
    throw "StarRupture was not found. Pass -GameRoot 'C:\path\to\StarRupture'."
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

$GameRoot = Find-StarRupture $GameRoot
$BinaryDir = Join-Path $GameRoot "StarRupture\Binaries\Win64"
$Exe = Join-Path $BinaryDir "StarRuptureGameSteam-Win64-Shipping.exe"
$PluginDir = Join-Path $BinaryDir "ModLoader\Plugins"
$LogDir = Join-Path $BinaryDir "ModLoader\Logs"
$ConfigFile = Join-Path $BinaryDir "ModLoader\modloader.ini"

if (-not (Test-Path -LiteralPath $Exe)) {
    throw "StarRupture was not found under: $GameRoot"
}
if (-not (Test-Path -LiteralPath (Join-Path $BinaryDir "dwmapi.dll")) -or
    -not (Test-Path -LiteralPath $PluginDir)) {
    throw "AlienX's StarRupture Mod Loader is not installed in $BinaryDir"
}

$InterfaceMin = $null
$InterfaceMax = $null
if ($env:RC_PLUGIN_INTERFACE) {
    $InterfaceMin = [int]$env:RC_PLUGIN_INTERFACE
    $InterfaceMax = $InterfaceMin
} else {
    $LatestLog = Get-ChildItem -LiteralPath $LogDir -Filter "ModLoader*.log" `
        -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | `
        Select-Object -First 1
    if ($LatestLog) {
        $Matches = [regex]::Matches(
            [System.IO.File]::ReadAllText($LatestLog.FullName),
            'modloader expects \[(\d+),\s*(\d+)\]',
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if ($Matches.Count -gt 0) {
            $Range = $Matches[$Matches.Count - 1]
            $InterfaceMin = [int]$Range.Groups[1].Value
            $InterfaceMax = [int]$Range.Groups[2].Value
        }
    }
}
if ($null -eq $InterfaceMin -or $null -eq $InterfaceMax) {
    throw "Could not detect the Mod Loader interface. Launch StarRupture once and retry."
}

if ($InterfaceMin -le 47 -and $InterfaceMax -ge 47) {
    $Variant = "Legacy v47"
    $DllAsset = "RuptureCompanion-Legacy.dll"
    $ManifestUrl = "$ReleaseBase/RuptureCompanion-legacy-manifest.json"
} elseif ($InterfaceMin -le 60 -and $InterfaceMax -ge 60) {
    $Variant = "Current v60"
    $DllAsset = "RuptureCompanion-Client.dll"
    $ManifestUrl = "$ReleaseBase/RuptureCompanion-client-manifest.json"
} else {
    throw "Unsupported Mod Loader interface range: [$InterfaceMin, $InterfaceMax]"
}

$Temporary = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("rupture-companion-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Temporary | Out-Null
try {
    $DownloadedDll = Join-Path $Temporary "RuptureCompanion.dll"
    Invoke-WebRequest -UseBasicParsing -Uri "$ReleaseBase/$DllAsset" `
        -OutFile $DownloadedDll
    $Stream = [System.IO.File]::OpenRead($DownloadedDll)
    try {
        if ($Stream.ReadByte() -ne 0x4d -or $Stream.ReadByte() -ne 0x5a) {
            throw "Downloaded plugin is not a Windows DLL"
        }
    } finally {
        $Stream.Dispose()
    }

    Copy-Item -LiteralPath $DownloadedDll `
        -Destination (Join-Path $PluginDir "RuptureCompanion.dll") -Force
    $Sidecar = @{ manifest_url = $ManifestUrl } | ConvertTo-Json
    Write-Utf8NoBom (Join-Path $PluginDir "RuptureCompanion.json") `
        ($Sidecar + [Environment]::NewLine)
    New-Item -ItemType Directory -Force `
        -Path (Join-Path $BinaryDir "RuptureCompanion") | Out-Null

    $AutoUpdateStatus = "Mod Loader plugin auto-update was left unchanged."
    if ($env:RC_ENABLE_AUTO_UPDATE -ne "0" -and (Test-Path -LiteralPath $ConfigFile)) {
        $Lines = [System.IO.File]::ReadAllLines($ConfigFile)
        $InAutoUpdate = $false
        $Changed = $false
        for ($Index = 0; $Index -lt $Lines.Length; $Index++) {
            if ($Lines[$Index] -match '^\s*\[([^]]+)\]\s*$') {
                $InAutoUpdate = $Matches[1] -eq "AutoUpdate"
            } elseif ($InAutoUpdate -and $Lines[$Index] -match '^\s*Enabled\s*=') {
                $Lines[$Index] = "Enabled=1"
                $Changed = $true
            }
        }
        if (-not $Changed) { throw "Could not enable auto-update in $ConfigFile" }
        [System.IO.File]::WriteAllLines($ConfigFile, $Lines, $Utf8NoBom)
        $AutoUpdateStatus = "Mod Loader plugin auto-update is enabled."
    }
} finally {
    Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
}

$Launcher = Join-Path $PSScriptRoot "run-with-companion.cmd"
Write-Host "Installed RuptureCompanion.dll ($Variant) in $PluginDir"
Write-Host $AutoUpdateStatus
Write-Host "Use this Steam launch option:"
Write-Host ('"{0}" %command%' -f $Launcher)
