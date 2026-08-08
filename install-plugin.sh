#!/usr/bin/env bash
# Install the release matching the interface exposed by the local Mod Loader.
set -euo pipefail

default_game_root="/mnt/storage/SteamLibrary/steamapps/common/StarRupture"
game_root="${RC_GAME_DIR:-${1:-$default_game_root}}"
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
installer_python="$installer_dir/.venv/bin/python"
if [[ ! -x "$installer_python" ]]; then
    installer_python="$(command -v python3 || true)"
fi
binary_dir="$game_root/StarRupture/Binaries/Win64"
plugin_dir="$binary_dir/ModLoader/Plugins"
log_dir="$binary_dir/ModLoader/Logs"
config_file="$binary_dir/ModLoader/modloader.ini"
release_base="https://github.com/Lucenx9/rupturecompanion/releases/latest/download"

if [[ ! -f "$binary_dir/StarRuptureGameSteam-Win64-Shipping.exe" ]]; then
    echo "StarRupture was not found under: $game_root" >&2
    echo "Usage: $0 [StarRupture installation directory]" >&2
    exit 1
fi
if [[ ! -f "$binary_dir/dwmapi.dll" || ! -d "$plugin_dir" ]]; then
    echo "AlienX's StarRupture Mod Loader is not installed in $binary_dir" >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2
    exit 1
fi
if [[ -z "$installer_python" ]]; then
    echo "Python 3 is required" >&2
    exit 1
fi

interface_min=""
interface_max=""
if [[ -n "${RC_PLUGIN_INTERFACE:-}" ]]; then
    interface_min="$RC_PLUGIN_INTERFACE"
    interface_max="$RC_PLUGIN_INTERFACE"
else
    latest_log="$(find "$log_dir" -maxdepth 1 -type f -name 'ModLoader*.log' \
        -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)"
    if [[ -n "$latest_log" ]]; then
        interface_range="$(sed -nE 's/.*(modloader expects|loader supports|supported range) \[([0-9]+),[[:space:]]*([0-9]+)\].*/\2 \3/p' \
            "$latest_log" | tail -1)"
        read -r interface_min interface_max <<<"$interface_range" || true
    fi
fi

if [[ -z "$interface_min" || -z "$interface_max" ]]; then
    echo "Could not detect the Mod Loader plugin interface." >&2
    echo "Launch StarRupture once, then rerun this installer." >&2
    exit 1
fi

variant=""
dll_asset=""
manifest_url=""
if (( interface_min <= 60 && interface_max >= 60 )); then
    variant="Current v60"
    dll_asset="RuptureCompanion-Client.dll"
    manifest_url="$release_base/RuptureCompanion-client-manifest.json"
elif (( interface_min <= 47 && interface_max >= 47 )); then
    variant="Legacy v47"
    dll_asset="RuptureCompanion-Legacy.dll"
    manifest_url="$release_base/RuptureCompanion-legacy-manifest.json"
else
    echo "Unsupported Mod Loader interface range: [$interface_min, $interface_max]" >&2
    echo "Update Rupture Companion or the StarRupture Mod Loader first." >&2
    exit 1
fi

temporary="$(mktemp -d "${TMPDIR:-/tmp}/rupture-companion-install.XXXXXX")"
staged_dll="$(mktemp "$plugin_dir/.RuptureCompanion.dll.update.XXXXXX")"
staged_sidecar="$(mktemp "$plugin_dir/.RuptureCompanion.json.update.XXXXXX")"
rollback_prepare="$(mktemp "$plugin_dir/.RuptureCompanion.dll.rollback.XXXXXX")"
rollback_dll="$plugin_dir/RuptureCompanion.dll.rollback"
installed_dll="$plugin_dir/RuptureCompanion.dll"
installed_sidecar="$plugin_dir/RuptureCompanion.json"
cleanup() {
    rm -rf -- "$temporary"
    rm -f -- "$staged_dll" "$staged_sidecar" "$rollback_prepare"
}
trap cleanup EXIT
curl -fL --retry 2 -o "$temporary/RuptureCompanion.dll" \
    "$release_base/$dll_asset"
if [[ "$(od -An -N2 -tc "$temporary/RuptureCompanion.dll" | tr -d ' ')" != "MZ" ]]; then
    echo "Downloaded plugin is not a Windows DLL" >&2
    exit 1
fi

installed_manifest=""
if [[ -f "$installed_sidecar" ]]; then
    installed_manifest="$("$installer_python" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as sidecar:
    value = json.load(sidecar).get("manifest_url", "")
print(value if isinstance(value, str) else "")
' "$installed_sidecar" 2>/dev/null || true)"
fi
if [[ -f "$rollback_dll" ]]; then
    if [[ "$installed_manifest" == "$manifest_url" ]]; then
        rm -f -- "$rollback_dll"
    else
        cp -p -- "$rollback_dll" "$rollback_prepare"
        mv -f -- "$rollback_prepare" "$installed_dll"
    fi
fi
install -m 0644 "$temporary/RuptureCompanion.dll" "$staged_dll"
printf '{\n  "manifest_url": "%s"\n}\n' "$manifest_url" \
    >"$staged_sidecar"
had_installed_dll=0
if [[ -f "$installed_dll" ]]; then
    cp -p -- "$installed_dll" "$rollback_prepare"
    mv -f -- "$rollback_prepare" "$rollback_dll"
    had_installed_dll=1
fi
mv -f -- "$staged_dll" "$installed_dll"
if ! mv -f -- "$staged_sidecar" "$installed_sidecar"; then
    echo "Could not commit the plugin sidecar; restoring the previous DLL." >&2
    if (( had_installed_dll )); then
        mv -f -- "$rollback_dll" "$installed_dll"
    else
        rm -f -- "$installed_dll"
    fi
    exit 1
fi
rm -f -- "$rollback_dll"
mkdir -p "$binary_dir/RuptureCompanion"

auto_update_status="Mod Loader plugin auto-update was left unchanged."
if [[ "${RC_ENABLE_AUTO_UPDATE:-1}" == "1" && -f "$config_file" ]]; then
    awk '
        /^\[AutoUpdate\][[:space:]]*$/ { in_auto_update=1; print; next }
        /^\[/ { in_auto_update=0 }
        in_auto_update && /^Enabled=/ { print "Enabled=1"; changed=1; next }
        { print }
        END { if (!changed) exit 2 }
    ' "$config_file" >"$temporary/modloader.ini" || {
        echo "Could not enable Mod Loader auto-update in $config_file" >&2
        exit 1
    }
    install -m 0644 "$temporary/modloader.ini" "$config_file"
    auto_update_status="Mod Loader plugin auto-update is enabled."
fi

launcher="$(realpath "$installer_dir/run-with-companion.sh")"
echo "Installed RuptureCompanion.dll ($variant) in $plugin_dir"
echo "$auto_update_status"
echo "Use this Steam launch option:"
echo "WINEDLLOVERRIDES=\"dwmapi=n,b\" $launcher %command%"
