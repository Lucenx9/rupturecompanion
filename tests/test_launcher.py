import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
LAUNCHER = ROOT / "run-with-companion.sh"


def make_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_launcher_runs_daemon_for_game_lifetime(tmp_path):
    launcher = tmp_path / LAUNCHER.name
    shutil.copy2(LAUNCHER, launcher)
    bridge = tmp_path / "bridge"
    args_file = tmp_path / "game-args.txt"
    make_executable(
        tmp_path / ".venv/bin/python",
        "#!/usr/bin/env bash\n"
        'mkdir -p "$RC_BRIDGE_DIR"\n'
        'exec 9> "$RC_BRIDGE_DIR/daemon.lock"\n'
        "flock -n 9 || exit 0\n"
        'echo $$ > "$RC_BRIDGE_DIR/daemon.lock"\n'
        "trap 'exit 0' TERM INT\n"
        "while true; do sleep 0.05; done\n",
    )
    (tmp_path / "daemon.py").write_text("", encoding="utf-8")
    make_executable(
        tmp_path / "fake-game",
        '#!/usr/bin/env bash\nprintf \'%s\' "$1" > "$GAME_ARGS_FILE"\nexit 7\n',
    )
    env = os.environ | {
        "RC_AUTO_UPDATE": "0",
        "RC_BRIDGE_DIR": str(bridge),
        "GAME_ARGS_FILE": str(args_file),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    result = subprocess.run(
        [str(launcher), str(tmp_path / "fake-game"), "argument with spaces"],
        env=env,
        timeout=5,
    )

    assert result.returncode == 7
    assert args_file.read_text(encoding="utf-8") == "argument with spaces"
    assert (tmp_path / "state/rupture-companion/daemon.log").exists()
