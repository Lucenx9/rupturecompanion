import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
LAUNCHER = ROOT / "run-with-companion.sh"


def make_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="Bash launcher is Linux-only")
@pytest.mark.parametrize("supports_ready_protocol", [True, False])
def test_launcher_waits_for_daemon_readiness_before_starting_game(
    tmp_path, supports_ready_protocol
):
    launcher = tmp_path / LAUNCHER.name
    shutil.copy2(LAUNCHER, launcher)
    bridge = tmp_path / "bridge"
    args_file = tmp_path / "game-args.txt"
    migration_file = tmp_path / "migration-complete"
    protocol_file = tmp_path / "ready-protocol"
    make_executable(
        tmp_path / ".venv/bin/python",
        "#!/usr/bin/env bash\n"
        '[[ "$1" == *updater.py ]] && exit 0\n'
        'if [[ "$1" == -c ]]; then\n'
        '  grep -q \'"ready_protocol":1\' "$3" 2>/dev/null && echo 1 || echo 0\n'
        "  exit 0\n"
        "fi\n"
        'printf \'%s\' "$RC_DAEMON_READY_PROTOCOL" > "$PROTOCOL_FILE"\n'
        'mkdir -p "$RC_BRIDGE_DIR"\n'
        'exec 9> "$RC_BRIDGE_DIR/daemon.lock"\n'
        "flock -n 9 || exit 0\n"
        'if [[ "$RC_DAEMON_READY_PROTOCOL" == 1 ]]; then\n'
        '  [[ -n "$RC_DAEMON_READY_NONCE" ]] || exit 11\n'
        '  identity="$$|$RC_DAEMON_READY_NONCE"\n'
        '  echo "$identity" > "$RC_BRIDGE_DIR/daemon.lock"\n'
        "  sleep 10.25\n"
        '  printf migrated > "$MIGRATION_FILE"\n'
        '  echo "$identity" > "$RC_BRIDGE_DIR/daemon.ready"\n'
        "else\n"
        "  sleep 0.25\n"
        '  printf migrated > "$MIGRATION_FILE"\n'
        '  echo $$ > "$RC_BRIDGE_DIR/daemon.lock"\n'
        "fi\n"
        "trap 'exit 0' TERM INT\n"
        "while true; do sleep 0.05; done\n",
    )
    (tmp_path / "daemon.py").write_text("", encoding="utf-8")
    if supports_ready_protocol:
        (tmp_path / "daemon-capabilities.json").write_text(
            '{"ready_protocol":1}\n', encoding="utf-8"
        )
    make_executable(
        tmp_path / "fake-game",
        "#!/usr/bin/env bash\n"
        '[[ -f "$MIGRATION_FILE" ]] || exit 42\n'
        'printf \'%s\' "$1" > "$GAME_ARGS_FILE"\n'
        "exit 7\n",
    )
    env = os.environ | {
        "RC_AUTO_UPDATE": "0",
        "RC_BRIDGE_DIR": str(bridge),
        "GAME_ARGS_FILE": str(args_file),
        "MIGRATION_FILE": str(migration_file),
        "PROTOCOL_FILE": str(protocol_file),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    result = subprocess.run(
        [str(launcher), str(tmp_path / "fake-game"), "argument with spaces"],
        env=env,
        timeout=20,
    )

    assert result.returncode == 7
    assert protocol_file.read_text(encoding="utf-8") == (
        "1" if supports_ready_protocol else "0"
    )
    assert args_file.read_text(encoding="utf-8") == "argument with spaces"
    assert (tmp_path / "state/rupture-companion/daemon.log").exists()


@pytest.mark.skipif(os.name != "nt", reason="PowerShell launcher is Windows-only")
@pytest.mark.parametrize("supports_ready_protocol", [True, False])
def test_powershell_launcher_waits_for_migration(tmp_path, supports_ready_protocol):
    local_app_data = tmp_path / "local-app-data"
    backend = local_app_data / "RuptureCompanion/backend"
    backend.mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", backend)
    shutil.copy2(ROOT / "uv.lock", backend)
    (backend / "daemon.py").write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "bridge = Path(os.environ['RC_BRIDGE_DIR'])\n"
        "bridge.mkdir(parents=True, exist_ok=True)\n"
        "nonce = os.environ.get('RC_DAEMON_READY_NONCE', '')\n"
        "identity = f'{os.getpid()}|{nonce}' if nonce else str(os.getpid())\n"
        "if nonce:\n"
        "    (bridge / 'daemon.lock').write_text(identity)\n"
        "time.sleep(10.25)\n"
        "Path(os.environ['MIGRATION_FILE']).write_text('migrated')\n"
        "if not nonce:\n"
        "    (bridge / 'daemon.lock').write_text(identity)\n"
        "else:\n"
        "    (bridge / 'daemon.ready').write_text(identity)\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    if supports_ready_protocol:
        shutil.copy2(ROOT / "daemon-capabilities.json", backend)
    migration_file = tmp_path / "migration-complete"
    env = os.environ | {
        "LOCALAPPDATA": str(local_app_data),
        "MIGRATION_FILE": str(migration_file),
        "RC_AUTO_UPDATE": "0",
        "RC_BRIDGE_DIR": str(tmp_path / "bridge"),
    }

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "run-with-companion.ps1"),
            "cmd.exe",
            "/d",
            "/c",
            "exit 7",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 7, result.stderr
    assert migration_file.read_text(encoding="utf-8") == "migrated"
    assert (
        "|" in (tmp_path / "bridge/daemon.lock").read_text(encoding="utf-8")
    ) is supports_ready_protocol
