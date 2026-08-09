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
    $seenRoots = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    if ($env:RC_STEAM_ROOT -and $seenRoots.Add($env:RC_STEAM_ROOT)) {
        $steamRoots.Add($env:RC_STEAM_ROOT)
    }
    foreach ($registryPath in
        "HKCU:\Software\Valve\Steam",
        "HKLM:\Software\WOW6432Node\Valve\Steam",
        "HKLM:\Software\Valve\Steam") {
        try {
            $properties = Get-ItemProperty $registryPath
            $steamPath = $properties.SteamPath
            if (-not $steamPath) { $steamPath = $properties.InstallPath }
            if ($steamPath -and $seenRoots.Add($steamPath)) {
                $steamRoots.Add($steamPath)
            }
        } catch {
        }
    }
    if (${env:ProgramFiles(x86)}) {
        $defaultSteam = Join-Path ${env:ProgramFiles(x86)} "Steam"
        if ((Test-Path -LiteralPath $defaultSteam) -and
            $seenRoots.Add($defaultSteam)) {
            $steamRoots.Add($defaultSteam)
        }
    }

    foreach ($steamRoot in $steamRoots) {
        $libraries = [System.Collections.Generic.List[string]]::new()
        $seenLibraries = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::OrdinalIgnoreCase
        )
        if ($seenLibraries.Add($steamRoot)) { $libraries.Add($steamRoot) }
        $vdf = Join-Path $steamRoot "steamapps\libraryfolders.vdf"
        if (Test-Path -LiteralPath $vdf) {
            $vdfText = [System.IO.File]::ReadAllText($vdf)
            foreach ($match in [regex]::Matches($vdfText, '"path"\s+"([^"]+)"')) {
                $library = $match.Groups[1].Value.Replace("\\", "\")
                if ($seenLibraries.Add($library)) { $libraries.Add($library) }
            }
        }
        foreach ($library in $libraries) {
            $manifest = Join-Path $library "steamapps\appmanifest_1631270.acf"
            $installDir = "StarRupture"
            if (Test-Path -LiteralPath $manifest) {
                $manifestText = [System.IO.File]::ReadAllText($manifest)
                $installMatch = [regex]::Match(
                    $manifestText, '"installdir"\s+"([^"]+)"'
                )
                if ($installMatch.Success) {
                    $installDir = $installMatch.Groups[1].Value
                }
            }
            $candidate = Join-Path $library "steamapps\common\$installDir"
            $candidateExe = Join-Path $candidate `
                "StarRupture\Binaries\Win64\StarRuptureGameSteam-Win64-Shipping.exe"
            if (Test-Path -LiteralPath $candidateExe) {
                return [System.IO.Path]::GetFullPath($candidate)
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
            '(?:modloader expects|loader supports|supported range) \[(\d+),\s*(\d+)\]',
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

if ($InterfaceMin -le 60 -and $InterfaceMax -ge 60) {
    $Variant = "Current v60"
    $DllAsset = "RuptureCompanion-Client.dll"
    $ManifestUrl = "$ReleaseBase/RuptureCompanion-client-manifest.json"
} elseif ($InterfaceMin -le 47 -and $InterfaceMax -ge 47) {
    $Variant = "Legacy v47"
    $DllAsset = "RuptureCompanion-Legacy.dll"
    $ManifestUrl = "$ReleaseBase/RuptureCompanion-legacy-manifest.json"
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

    $InstalledDll = Join-Path $PluginDir "RuptureCompanion.dll"
    $InstalledSidecar = Join-Path $PluginDir "RuptureCompanion.json"
    $RollbackDll = Join-Path $PluginDir "RuptureCompanion.dll.rollback"
    $StagedDll = Join-Path $PluginDir `
        (".RuptureCompanion.dll.update." + [guid]::NewGuid())
    $StagedSidecar = Join-Path $PluginDir `
        (".RuptureCompanion.json.update." + [guid]::NewGuid())
    $RollbackPrepare = Join-Path $PluginDir `
        (".RuptureCompanion.dll.rollback." + [guid]::NewGuid())

    $InstalledManifestUrl = $null
    if (Test-Path -LiteralPath $InstalledSidecar) {
        try {
            $InstalledManifestUrl = (
                [System.IO.File]::ReadAllText($InstalledSidecar) | ConvertFrom-Json
            ).manifest_url
        } catch {
        }
    }
    if (Test-Path -LiteralPath $RollbackDll) {
        if ($InstalledManifestUrl -eq $ManifestUrl) {
            Remove-Item -LiteralPath $RollbackDll -Force
        } else {
            Copy-Item -LiteralPath $RollbackDll `
                -Destination $RollbackPrepare -Force
            Move-Item -LiteralPath $RollbackPrepare `
                -Destination $InstalledDll -Force
        }
    }
    Copy-Item -LiteralPath $DownloadedDll -Destination $StagedDll
    $Sidecar = @{ manifest_url = $ManifestUrl } | ConvertTo-Json
    Write-Utf8NoBom $StagedSidecar ($Sidecar + [Environment]::NewLine)
    $HadInstalledDll = Test-Path -LiteralPath $InstalledDll
    if ($HadInstalledDll) {
        Copy-Item -LiteralPath $InstalledDll -Destination $RollbackPrepare
        Move-Item -LiteralPath $RollbackPrepare -Destination $RollbackDll -Force
    }
    Move-Item -LiteralPath $StagedDll -Destination $InstalledDll -Force
    try {
        Move-Item -LiteralPath $StagedSidecar `
            -Destination $InstalledSidecar -Force
    } catch {
        if ($HadInstalledDll) {
            Move-Item -LiteralPath $RollbackDll -Destination $InstalledDll -Force
        } else {
            Remove-Item -LiteralPath $InstalledDll -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    Remove-Item -LiteralPath $RollbackDll -Force -ErrorAction SilentlyContinue
    $ObsoleteRemoved = 0
    foreach ($ObsoleteName in
        "RuptureCompanion-Client.dll",
        "RuptureCompanion-Client.json",
        "RuptureCompanion-Legacy.dll",
        "RuptureCompanion-Legacy.json") {
        $ObsoletePath = Join-Path $PluginDir $ObsoleteName
        if (Test-Path -LiteralPath $ObsoletePath) {
            Remove-Item -LiteralPath $ObsoletePath -Force
            $ObsoleteRemoved++
        }
    }
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
    foreach ($Path in $StagedDll, $StagedSidecar, $RollbackPrepare) {
        if ($Path) {
            Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
}

$Launcher = Join-Path $PSScriptRoot "run-with-companion.cmd"
Write-Host "Installed RuptureCompanion.dll ($Variant) in $PluginDir"
if ($ObsoleteRemoved -gt 0) {
    Write-Host "Removed $ObsoleteRemoved obsolete duplicate plugin file(s)."
}
Write-Host $AutoUpdateStatus
Write-Host "Use this Steam launch option:"
Write-Host ('"{0}" %command%' -f $Launcher)
