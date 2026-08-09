#!/usr/bin/env bash
# Classify the latest ChimeraMain live-context smoke test from Mod Loader logs.
set -euo pipefail

log_file="${1:-}"
if [[ -z "$log_file" || ! -f "$log_file" ]]; then
    echo "Usage: $0 /path/to/ModLoader.log" >&2
    exit 2
fi

world_line="$(rg -n '\[WorldBeginPlay\] ChimeraMain world begin play detected' \
    "$log_file" | tail -1 | cut -d: -f1)"
if [[ -z "$world_line" ]]; then
    echo "INCONCLUSIVE: ChimeraMain has not begun play" >&2
    exit 2
fi

segment="$(tail -n "+$world_line" "$log_file")"
if rg -q '\[CrashReporter\].*FATAL ENGINE CRASH' <<<"$segment" \
        && rg -q 'RuptureCompanion\.dll \+ 0x' <<<"$segment"; then
    echo "RED: RuptureCompanion crashed during the first ChimeraMain sample" >&2
    exit 1
fi

if rg -q '\[Plugin:RuptureCompanion\].*Live game-state sample captured' \
        <<<"$segment"; then
    echo "GREEN: RuptureCompanion captured its first ChimeraMain sample"
    exit 0
fi

echo "INCONCLUSIVE: no successful sample or RuptureCompanion crash yet" >&2
exit 2
