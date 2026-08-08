import json
import subprocess
from pathlib import Path

import pytest

import daemon
import plugin_updater


@pytest.mark.parametrize(
    ("log_line", "expected"),
    [
        (
            "Plugin interface version 47 does not match; modloader expects [46, 47]",
            (46, 47),
        ),
        (
            "RuptureCompanion requires interface [46, 47], "
            "loader supports [60, 60] -- skipping",
            (60, 60),
        ),
        (
            "Plugin interface version 47 not in supported range [60, 60]",
            (60, 60),
        ),
    ],
)
def test_detect_interface_range_accepts_loader_log_formats(log_line, expected):
    assert plugin_updater.detect_interface_range(log_line) == expected


def make_modloader_install(tmp_path: Path, log_line: str) -> Path:
    binary_dir = tmp_path / "StarRupture/Binaries/Win64"
    bridge = binary_dir / "RuptureCompanion"
    plugins = binary_dir / "ModLoader/Plugins"
    logs = binary_dir / "ModLoader/Logs"
    bridge.mkdir(parents=True)
    plugins.mkdir(parents=True)
    logs.mkdir(parents=True)
    (logs / "ModLoader.log").write_text(log_line, encoding="utf-8")
    return bridge


def test_sync_plugin_migrates_legacy_install_to_current_variant(tmp_path, monkeypatch):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60] -- skipping\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    dll.write_bytes(b"MZlegacy")
    sidecar.write_text(
        json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL}),
        encoding="utf-8",
    )
    requested = []

    def fake_download(url: str, destination: Path) -> None:
        requested.append(url)
        destination.write_bytes(b"MZcurrent")

    monkeypatch.setattr(plugin_updater, "download_plugin", fake_download)

    assert plugin_updater.sync_plugin(bridge) == "Current v60"
    assert requested == [plugin_updater.CURRENT_DLL_URL]
    assert dll.read_bytes() == b"MZcurrent"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "manifest_url": plugin_updater.CURRENT_MANIFEST_URL
    }


def test_sync_plugin_does_not_download_matching_variant(tmp_path, monkeypatch):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    (plugin_dir / "RuptureCompanion.dll").write_bytes(b"MZcurrent")
    (plugin_dir / "RuptureCompanion.json").write_text(
        json.dumps({"manifest_url": plugin_updater.CURRENT_MANIFEST_URL}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        plugin_updater,
        "download_plugin",
        lambda *_args: pytest.fail("matching plugin must not be downloaded"),
    )

    assert plugin_updater.sync_plugin(bridge) is None


def test_sync_plugin_preserves_existing_pair_when_download_fails(tmp_path, monkeypatch):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    original_sidecar = json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL})
    dll.write_bytes(b"MZlegacy")
    sidecar.write_text(original_sidecar, encoding="utf-8")

    def failed_download(_url: str, destination: Path) -> None:
        destination.write_bytes(b"partial")
        raise plugin_updater.PluginUpdateError("network failed")

    monkeypatch.setattr(plugin_updater, "download_plugin", failed_download)

    with pytest.raises(plugin_updater.PluginUpdateError, match="network failed"):
        plugin_updater.sync_plugin(bridge)

    assert dll.read_bytes() == b"MZlegacy"
    assert sidecar.read_text(encoding="utf-8") == original_sidecar
    assert not list(plugin_dir.glob("RuptureCompanion.*.update"))


def test_daemon_syncs_plugin_before_acquiring_lock(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(daemon, "bridge_dir", lambda: tmp_path / "bridge")
    monkeypatch.setattr(
        daemon.plugin_updater,
        "sync_plugin",
        lambda bridge: events.append(("sync", bridge)),
    )

    def stop_after_sync():
        events.append(("lock", None))
        raise daemon.DaemonAlreadyRunning("test stop")

    monkeypatch.setattr(daemon, "acquire_lock", stop_after_sync)

    with pytest.raises(SystemExit):
        daemon.main()

    assert events == [("sync", tmp_path / "bridge"), ("lock", None)]


@pytest.mark.skipif(
    plugin_updater.os.name == "nt", reason="Bash installer is Linux-only"
)
def test_bash_installer_detects_new_loader_log_format(tmp_path):
    game_root = tmp_path / "StarRupture"
    binary_dir = game_root / "StarRupture/Binaries/Win64"
    plugin_dir = binary_dir / "ModLoader/Plugins"
    log_dir = binary_dir / "ModLoader/Logs"
    fake_bin = tmp_path / "bin"
    plugin_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    fake_bin.mkdir()
    (binary_dir / "StarRuptureGameSteam-Win64-Shipping.exe").write_bytes(b"")
    (binary_dir / "dwmapi.dll").write_bytes(b"MZ")
    (binary_dir / "ModLoader/modloader.ini").write_text(
        "[AutoUpdate]\nEnabled=1\n", encoding="utf-8"
    )
    (log_dir / "ModLoader.log").write_text(
        "Plugin interface version 47 not in supported range [60, 60]\n",
        encoding="utf-8",
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "while (( $# )); do\n"
        "  if [[ $1 == -o ]]; then output=$2; shift 2; continue; fi\n"
        "  url=$1; shift\n"
        "done\n"
        'printf MZcurrent > "$output"\n'
        'printf \'%s\' "$url" > "$CURL_URL_FILE"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    url_file = tmp_path / "curl-url.txt"

    result = subprocess.run(
        [str(Path(__file__).parent.parent / "install-plugin.sh"), str(game_root)],
        check=False,
        capture_output=True,
        text=True,
        env=plugin_updater.os.environ
        | {
            "PATH": f"{fake_bin}:{plugin_updater.os.environ['PATH']}",
            "CURL_URL_FILE": str(url_file),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Current v60" in result.stdout
    assert url_file.read_text(encoding="utf-8").endswith("/RuptureCompanion-Client.dll")
    assert plugin_updater.CURRENT_MANIFEST_URL in (
        plugin_dir / "RuptureCompanion.json"
    ).read_text(encoding="utf-8")


def test_windows_installer_recognizes_new_loader_log_messages():
    script = (Path(__file__).parent.parent / "install-plugin.ps1").read_text(
        encoding="utf-8"
    )

    assert "loader supports" in script
    assert "supported range" in script
