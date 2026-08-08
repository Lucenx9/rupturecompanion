#!/usr/bin/env bash
# Keep the companion daemon alive for exactly the lifetime of the Steam command.
set -uo pipefail

companion_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$companion_dir/.venv/bin/python"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
state_dir="$state_home/rupture-companion"
data_dir="$data_home/rupture-companion"
log_file="$state_dir/daemon.log"
backend_dir="$companion_dir"

if (( $# == 0 )); then
    echo "Usage: $0 <game command> [arguments...]" >&2
    exit 2
fi
if [[ ! -x "$python" ]]; then
    echo "Companion Python environment not found: $python" >&2
    echo "Run: uv sync --group dev" >&2
    exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
    echo "flock is required (util-linux package)" >&2
    exit 1
fi

mkdir -p "$state_dir" "$data_dir"
"$python" "$companion_dir/updater.py" 2>>"$log_file" || true
if [[ -f "$data_dir/backend/daemon.py" ]]; then
    if command -v uv >/dev/null 2>&1 \
            && uv sync --project "$data_dir/backend" --locked --no-dev \
                >>"$log_file" 2>&1; then
        backend_dir="$data_dir/backend"
        python="$backend_dir/.venv/bin/python"
    fi
fi

if [[ -z "${RC_BRIDGE_DIR:-}" ]]; then
    if [[ -n "${STEAM_COMPAT_INSTALL_PATH:-}" ]]; then
        RC_BRIDGE_DIR="$STEAM_COMPAT_INSTALL_PATH/StarRupture/Binaries/Win64/RuptureCompanion"
    else
        RC_BRIDGE_DIR="$(PYTHONPATH="$backend_dir" "$python" -c 'import daemon; print(daemon.bridge_dir())')"
    fi
fi
export RC_BRIDGE_DIR
mkdir -p "$RC_BRIDGE_DIR"
lock_file="$RC_BRIDGE_DIR/daemon.lock"
daemon_pid=""
game_pid=""

start_daemon() {
    (
        cd "$backend_dir" || exit 1
        exec setsid env -u LD_LIBRARY_PATH -u LD_PRELOAD -u QT_QPA_PLATFORM \
            PYTHONUNBUFFERED=1 RC_BRIDGE_DIR="$RC_BRIDGE_DIR" \
            "$python" "$backend_dir/daemon.py"
    ) >>"$log_file" 2>&1 &
    daemon_pid=$!
}

wait_for_daemon() {
    local lock_pid=""
    for _ in {1..200}; do
        if [[ -f "$lock_file" ]]; then
            read -r lock_pid < "$lock_file" || true
        fi
        if [[ "$lock_pid" == "$daemon_pid" ]] \
                && kill -0 "$daemon_pid" 2>/dev/null \
                && ! flock -n "$lock_file" -c true 2>/dev/null; then
            return 0
        fi
        kill -0 "$daemon_pid" 2>/dev/null || return 1
        sleep 0.05
    done
    return 1
}

# Called indirectly by cleanup's EXIT trap.
# shellcheck disable=SC2329
stop_daemon() {
    [[ -n "$daemon_pid" ]] || return
    if kill -0 -- "-$daemon_pid" 2>/dev/null; then
        kill -TERM -- "-$daemon_pid" 2>/dev/null || true
    fi
    wait "$daemon_pid" 2>/dev/null || true
    failed_daemon_pid="$daemon_pid"
    if kill -0 -- "-$failed_daemon_pid" 2>/dev/null; then
        kill -TERM -- "-$failed_daemon_pid" 2>/dev/null || true
    fi
    wait "$failed_daemon_pid" 2>/dev/null || true
    daemon_pid=""
}

# Called indirectly by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
    if [[ -n "$game_pid" ]] && kill -0 "$game_pid" 2>/dev/null; then
        kill "$game_pid" 2>/dev/null || true
        wait "$game_pid" 2>/dev/null || true
    fi
    stop_daemon
}
trap cleanup EXIT
trap 'if [[ -n "$game_pid" ]]; then kill -INT "$game_pid" 2>/dev/null || true; fi; exit 130' INT
trap 'if [[ -n "$game_pid" ]]; then kill -TERM "$game_pid" 2>/dev/null || true; fi; exit 143' TERM

start_daemon
if ! wait_for_daemon; then
    echo "Companion daemon did not start; check $log_file" >&2
    exit 1
fi

"$@" &
game_pid=$!
game_status=0
while true; do
    completed_pid=""
    wait -n -p completed_pid "$game_pid" "$daemon_pid"
    completed_status=$?
    if [[ "$completed_pid" == "$game_pid" ]]; then
        game_status=$completed_status
        break
    fi
    daemon_pid=""
    echo "Daemon stopped unexpectedly; restarting" >>"$log_file"
    start_daemon
    if ! wait_for_daemon; then
        echo "Companion daemon is unavailable; check $log_file" >&2
        wait "$game_pid"
        game_status=$?
        break
    fi
done
game_pid=""
exit "$game_status"
