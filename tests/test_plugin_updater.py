import json
import os
import subprocess
import threading
import time
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


INSTALLER_RECOVERY_CASES = (
    (
        plugin_updater.CURRENT_MANIFEST_URL,
        b"MZcurrent",
        b"MZcurrent",
        False,
    ),
    (
        plugin_updater.LEGACY_MANIFEST_URL,
        b"MZinterrupted",
        b"MZlegacy",
        True,
    ),
)


def make_installer_recovery_state(
    tmp_path: Path, installed_manifest: str, installed_dll: bytes
) -> tuple[Path, Path, Path]:
    game_root = tmp_path / "StarRupture"
    binary_dir = game_root / "StarRupture/Binaries/Win64"
    plugin_dir = binary_dir / "ModLoader/Plugins"
    log_dir = binary_dir / "ModLoader/Logs"
    plugin_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    (binary_dir / "StarRuptureGameSteam-Win64-Shipping.exe").write_bytes(b"")
    (binary_dir / "dwmapi.dll").write_bytes(b"MZ")
    (log_dir / "ModLoader.log").write_text(
        "loader supports [60, 60]\n", encoding="utf-8"
    )
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    rollback = plugin_dir / "RuptureCompanion.dll.rollback"
    dll.write_bytes(installed_dll)
    sidecar.write_text(
        json.dumps({"manifest_url": installed_manifest}, separators=(",", ":")),
        encoding="utf-8",
    )
    rollback.write_bytes(b"MZlegacy")
    return game_root, dll, rollback


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


def test_sync_plugin_cancellation_keeps_existing_pair(tmp_path, monkeypatch):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    original_sidecar = json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL})
    dll.write_bytes(b"MZlegacy")
    sidecar.write_text(original_sidecar, encoding="utf-8")
    cancelled = threading.Event()

    def cancel_after_download(_url: str, destination: Path) -> None:
        destination.write_bytes(b"MZcurrent")
        cancelled.set()

    monkeypatch.setattr(plugin_updater, "download_plugin", cancel_after_download)

    with pytest.raises(plugin_updater.PluginUpdateError, match="deferred"):
        plugin_updater.sync_plugin(
            bridge,
            cancel_event=cancelled,
            commit_lock=threading.Lock(),
        )

    assert dll.read_bytes() == b"MZlegacy"
    assert sidecar.read_text(encoding="utf-8") == original_sidecar
    assert not (plugin_dir / "RuptureCompanion.dll.rollback").exists()


def test_recover_plugin_restores_pending_rollback_before_migration(tmp_path):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    rollback = plugin_dir / "RuptureCompanion.dll.rollback"
    dll.write_bytes(b"MZinterrupted")
    sidecar.write_text(
        json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL}),
        encoding="utf-8",
    )
    rollback.write_bytes(b"MZlegacy")

    plugin_updater.recover_plugin(bridge)

    assert dll.read_bytes() == b"MZlegacy"
    assert not rollback.exists()


def test_recover_plugin_cancellation_preserves_pending_rollback(tmp_path):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    rollback = plugin_dir / "RuptureCompanion.dll.rollback"
    dll.write_bytes(b"MZinterrupted")
    sidecar.write_text(
        json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL}),
        encoding="utf-8",
    )
    rollback.write_bytes(b"MZlegacy")
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(plugin_updater.PluginUpdateError, match="deferred"):
        plugin_updater.recover_plugin(
            bridge,
            cancel_event=cancelled,
            commit_lock=threading.Lock(),
        )

    assert dll.read_bytes() == b"MZinterrupted"
    assert rollback.read_bytes() == b"MZlegacy"


def test_sync_plugin_rolls_back_dll_when_sidecar_commit_fails(tmp_path, monkeypatch):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    original_sidecar = json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL})
    dll.write_bytes(b"MZlegacy")
    sidecar.write_text(original_sidecar, encoding="utf-8")
    monkeypatch.setattr(
        plugin_updater,
        "download_plugin",
        lambda _url, destination: destination.write_bytes(b"MZcurrent"),
    )
    real_replace = plugin_updater.os.replace

    def fail_sidecar_commit(source, destination):
        if Path(source).suffixes[-2:] == [".json", ".update"]:
            raise OSError("sidecar is locked")
        real_replace(source, destination)

    monkeypatch.setattr(plugin_updater.os, "replace", fail_sidecar_commit)

    with pytest.raises(OSError, match="sidecar is locked"):
        plugin_updater.sync_plugin(bridge)

    assert dll.read_bytes() == b"MZlegacy"
    assert sidecar.read_text(encoding="utf-8") == original_sidecar
    assert not list(plugin_dir.glob("RuptureCompanion.*.update"))
    assert not list(plugin_dir.glob("RuptureCompanion.*.backup"))
    assert not (plugin_dir / "RuptureCompanion.dll.rollback").exists()


def test_sync_plugin_retains_and_recovers_backup_when_rollback_fails(
    tmp_path, monkeypatch
):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    rollback = plugin_dir / "RuptureCompanion.dll.rollback"
    dll.write_bytes(b"MZlegacy")
    sidecar.write_text(
        json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        plugin_updater,
        "download_plugin",
        lambda _url, destination: destination.write_bytes(b"MZcurrent"),
    )
    real_replace = plugin_updater.os.replace

    def fail_commit_and_rollback(source, destination):
        source = Path(source)
        if source.suffixes[-2:] == [".json", ".update"] or source == rollback:
            raise OSError("transaction interrupted")
        real_replace(source, destination)

    monkeypatch.setattr(plugin_updater.os, "replace", fail_commit_and_rollback)

    with pytest.raises(OSError, match="transaction interrupted"):
        plugin_updater.sync_plugin(bridge)

    assert dll.read_bytes() == b"MZcurrent"
    assert rollback.read_bytes() == b"MZlegacy"

    monkeypatch.setattr(plugin_updater.os, "replace", real_replace)

    assert plugin_updater.sync_plugin(bridge) == "Current v60"
    assert dll.read_bytes() == b"MZcurrent"
    assert not rollback.exists()


def test_sync_plugin_serializes_concurrent_migrations(tmp_path, monkeypatch):
    bridge = make_modloader_install(tmp_path, "loader supports [60, 60]\n")
    plugin_dir = bridge.parent / "ModLoader/Plugins"
    (plugin_dir / "RuptureCompanion.dll").write_bytes(b"MZlegacy")
    (plugin_dir / "RuptureCompanion.json").write_text(
        json.dumps({"manifest_url": plugin_updater.LEGACY_MANIFEST_URL}),
        encoding="utf-8",
    )
    download_started = threading.Event()
    release_download = threading.Event()
    downloads = []
    results = []

    def blocked_download(url: str, destination: Path) -> None:
        downloads.append(url)
        download_started.set()
        assert release_download.wait(timeout=2)
        destination.write_bytes(b"MZcurrent")

    def migrate() -> None:
        results.append(plugin_updater.sync_plugin(bridge))

    monkeypatch.setattr(plugin_updater, "download_plugin", blocked_download)
    first = threading.Thread(target=migrate)
    second = threading.Thread(target=migrate)
    first.start()
    assert download_started.wait(timeout=2)
    second.start()
    assert second.is_alive()
    release_download.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert downloads == [plugin_updater.CURRENT_DLL_URL]
    assert sorted(result for result in results if result is not None) == ["Current v60"]


def test_daemon_signals_readiness_after_plugin_sync(tmp_path, monkeypatch):
    events = []
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    monkeypatch.setenv("RC_DAEMON_READY_PROTOCOL", "1")
    monkeypatch.setenv("RC_DAEMON_READY_NONCE", "test-nonce")
    monkeypatch.setattr(daemon, "bridge_dir", lambda: bridge)

    class FakeLock:
        def close(self):
            events.append(("close", None))

    def acquire_lock(identity):
        events.append(("lock", identity))
        return FakeLock()

    monkeypatch.setattr(daemon, "acquire_lock", acquire_lock)
    monkeypatch.setattr(
        daemon.plugin_updater,
        "sync_plugin",
        lambda bridge: events.append(("sync", bridge)),
    )

    def stop_after_ready(_seconds):
        events.append(("ready", (bridge / "daemon.ready").read_text().strip()))
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", stop_after_ready)

    daemon.main()

    assert events == [
        ("lock", f"{os.getpid()}|test-nonce"),
        ("sync", bridge),
        ("ready", f"{os.getpid()}|test-nonce"),
        ("close", None),
    ]
    assert not (bridge / "daemon.ready").exists()


def test_daemon_keeps_lock_as_readiness_for_legacy_launcher(tmp_path, monkeypatch):
    events = []
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    monkeypatch.delenv("RC_DAEMON_READY_PROTOCOL", raising=False)
    monkeypatch.setattr(daemon, "bridge_dir", lambda: bridge)

    class FakeLock:
        def close(self):
            events.append("close")

    monkeypatch.setattr(
        daemon.plugin_updater,
        "sync_plugin",
        lambda _bridge, **_kwargs: events.append("sync"),
    )
    monkeypatch.setattr(
        daemon,
        "acquire_lock",
        lambda *_args: events.append("lock") or FakeLock(),
    )
    monkeypatch.setattr(
        daemon.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    daemon.main()

    assert events == ["sync", "lock", "close"]
    assert not (bridge / "daemon.ready").exists()


def test_daemon_defers_slow_migration_for_legacy_launcher(tmp_path, monkeypatch):
    events = []
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    migration_cancelled = threading.Event()
    monkeypatch.delenv("RC_DAEMON_READY_PROTOCOL", raising=False)
    monkeypatch.setattr(daemon, "bridge_dir", lambda: bridge)
    monkeypatch.setattr(daemon, "LEGACY_MIGRATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        daemon.plugin_updater,
        "recover_plugin",
        lambda _bridge, **_kwargs: events.append("recover"),
    )

    class FakeLock:
        def close(self):
            events.append("close")

    def slow_sync(_bridge, *, cancel_event, commit_lock):
        events.append("sync")
        assert cancel_event.wait(timeout=1)
        with commit_lock:
            events.append("cancelled" if cancel_event.is_set() else "mutated")
            migration_cancelled.set()

    monkeypatch.setattr(daemon.plugin_updater, "sync_plugin", slow_sync)
    monkeypatch.setattr(
        daemon,
        "acquire_lock",
        lambda *_args: events.append("lock") or FakeLock(),
    )
    monkeypatch.setattr(
        daemon.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    daemon.main()

    assert migration_cancelled.wait(timeout=1)
    assert daemon.LEGACY_MIGRATION_GRACE_SECONDS < 10
    assert events[:2] == ["recover", "sync"]
    assert "cancelled" in events
    assert "mutated" not in events
    assert "lock" in events
    assert "close" in events


def test_legacy_launcher_grace_bounds_blocked_recovery(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    recovery_cancelled = threading.Event()

    def blocked_recovery(_bridge, *, cancel_event, commit_lock):
        assert cancel_event.wait(timeout=daemon.LEGACY_MIGRATION_GRACE_SECONDS + 1)
        with commit_lock:
            recovery_cancelled.set()

    monkeypatch.setattr(daemon.plugin_updater, "recover_plugin", blocked_recovery)
    monkeypatch.setattr(
        daemon.plugin_updater,
        "sync_plugin",
        lambda *_args, **_kwargs: pytest.fail("sync started after cancelled recovery"),
    )

    started = time.monotonic()
    assert daemon._sync_plugin_for_legacy_launcher(bridge) is None
    elapsed = time.monotonic() - started

    assert recovery_cancelled.wait(timeout=1)
    assert daemon.LEGACY_MIGRATION_GRACE_SECONDS == 5.0
    assert 4.5 <= elapsed < 10


@pytest.mark.skipif(os.name == "nt", reason="Bash installer is Linux-only")
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
        "Plugin interface version 47 not in supported range [46, 60]\n",
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
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_URL_FILE": str(url_file),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Current v60" in result.stdout
    assert url_file.read_text(encoding="utf-8").endswith("/RuptureCompanion-Client.dll")
    assert plugin_updater.CURRENT_MANIFEST_URL in (
        plugin_dir / "RuptureCompanion.json"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Bash installer is Linux-only")
def test_bash_installer_discovers_secondary_library_and_removes_obsolete_files(
    tmp_path,
):
    steam_root = tmp_path / "steam"
    secondary_library = tmp_path / "other SSD" / "SteamLibrary"
    game_root = secondary_library / "steamapps/common/StarRupture"
    binary_dir = game_root / "StarRupture/Binaries/Win64"
    plugin_dir = binary_dir / "ModLoader/Plugins"
    log_dir = binary_dir / "ModLoader/Logs"
    fake_bin = tmp_path / "bin"
    (steam_root / "steamapps").mkdir(parents=True)
    (secondary_library / "steamapps").mkdir(parents=True)
    plugin_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    fake_bin.mkdir()
    (steam_root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n  "1"\n  {{\n    "path" "{secondary_library}"\n  }}\n}}\n',
        encoding="utf-8",
    )
    (secondary_library / "steamapps/appmanifest_1631270.acf").write_text(
        '"AppState" { "appid" "1631270" "installdir" "StarRupture" }\n',
        encoding="utf-8",
    )
    (binary_dir / "StarRuptureGameSteam-Win64-Shipping.exe").write_bytes(b"")
    (binary_dir / "dwmapi.dll").write_bytes(b"MZ")
    (binary_dir / "ModLoader/modloader.ini").write_text(
        "[AutoUpdate]\nEnabled=1\n", encoding="utf-8"
    )
    (log_dir / "ModLoader.log").write_text(
        "loader supports [46, 60]\n", encoding="utf-8"
    )
    for obsolete in (
        "RuptureCompanion-Client.dll",
        "RuptureCompanion-Client.json",
        "RuptureCompanion-Legacy.dll",
        "RuptureCompanion-Legacy.json",
    ):
        (plugin_dir / obsolete).write_bytes(b"old")
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "while (( $# )); do\n"
        "  if [[ $1 == -o ]]; then output=$2; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        'printf MZdownloaded > "$output"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    result = subprocess.run(
        [str(Path(__file__).parent.parent / "install-plugin.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RC_STEAM_ROOT": str(steam_root),
            "RC_ENABLE_AUTO_UPDATE": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert str(plugin_dir) in result.stdout
    assert (plugin_dir / "RuptureCompanion.dll").read_bytes() == b"MZdownloaded"
    assert not any(
        (plugin_dir / obsolete).exists()
        for obsolete in (
            "RuptureCompanion-Client.dll",
            "RuptureCompanion-Client.json",
            "RuptureCompanion-Legacy.dll",
            "RuptureCompanion-Legacy.json",
        )
    )


@pytest.mark.parametrize(
    ("installed_manifest", "installed_dll", "expected_dll", "keeps_rollback"),
    INSTALLER_RECOVERY_CASES,
)
@pytest.mark.skipif(os.name == "nt", reason="Bash installer is Linux-only")
def test_bash_installer_keeps_recovery_state_when_staging_fails(
    tmp_path,
    installed_manifest,
    installed_dll,
    expected_dll,
    keeps_rollback,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    game_root, dll, rollback = make_installer_recovery_state(
        tmp_path, installed_manifest, installed_dll
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "while (( $# )); do\n"
        "  if [[ $1 == -o ]]; then output=$2; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        'printf MZdownloaded > "$output"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    failing_install = fake_bin / "install"
    failing_install.write_text("#!/usr/bin/env bash\nexit 43\n", encoding="utf-8")
    failing_install.chmod(0o755)

    result = subprocess.run(
        [str(Path(__file__).parent.parent / "install-plugin.sh"), str(game_root)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RC_ENABLE_AUTO_UPDATE": "0",
        },
    )

    assert result.returncode == 43
    assert dll.read_bytes() == expected_dll
    assert rollback.exists() is keeps_rollback


@pytest.mark.skipif(os.name == "nt", reason="Bash installer is Linux-only")
def test_bash_installer_restores_rollback_atomically(tmp_path):
    game_root, dll, rollback = make_installer_recovery_state(
        tmp_path, plugin_updater.LEGACY_MANIFEST_URL, b"MZinterrupted"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "while (( $# )); do\n"
        "  if [[ $1 == -o ]]; then output=$2; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        'printf MZdownloaded > "$output"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    failing_copy = fake_bin / "cp"
    failing_copy.write_text(
        "#!/usr/bin/env bash\n"
        "while [[ $1 == -* ]]; do shift; done\n"
        'printf partial > "$2"\n'
        "exit 44\n",
        encoding="utf-8",
    )
    failing_copy.chmod(0o755)

    result = subprocess.run(
        [str(Path(__file__).parent.parent / "install-plugin.sh"), str(game_root)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RC_ENABLE_AUTO_UPDATE": "0",
        },
    )

    assert result.returncode == 44
    assert dll.read_bytes() == b"MZinterrupted"
    assert rollback.read_bytes() == b"MZlegacy"


def test_windows_installer_recognizes_new_loader_log_messages():
    script = (Path(__file__).parent.parent / "install-plugin.ps1").read_text(
        encoding="utf-8"
    )

    assert "loader supports" in script
    assert "supported range" in script
    assert script.index("$InterfaceMin -le 60") < script.index("$InterfaceMin -le 47")
    assert "$env:RC_STEAM_ROOT" in script
    assert "RuptureCompanion-Client.dll" in script
    assert "RuptureCompanion-Legacy.dll" in script


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer is Windows-only")
def test_powershell_installer_prefers_current_for_overlapping_range(tmp_path):
    game_root = tmp_path / "StarRupture"
    binary_dir = game_root / "StarRupture/Binaries/Win64"
    plugin_dir = binary_dir / "ModLoader/Plugins"
    log_dir = binary_dir / "ModLoader/Logs"
    plugin_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    (binary_dir / "StarRuptureGameSteam-Win64-Shipping.exe").write_bytes(b"")
    (binary_dir / "dwmapi.dll").write_bytes(b"MZ")
    (binary_dir / "ModLoader/modloader.ini").write_text(
        "[AutoUpdate]\nEnabled=1\n", encoding="utf-8"
    )
    (log_dir / "ModLoader.log").write_text(
        "Plugin interface version 47 not in supported range [46, 60]\n",
        encoding="utf-8",
    )
    url_file = tmp_path / "download-url.txt"
    installer = Path(__file__).parent.parent / "install-plugin.ps1"

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Invoke-WebRequest { param([switch]$UseBasicParsing, "
        "[string]$Uri, [string]$OutFile); "
        "[IO.File]::WriteAllBytes($OutFile, [byte[]](0x4d,0x5a,0x00)); "
        "[IO.File]::WriteAllText($env:DOWNLOAD_URL_FILE, $Uri) }; "
        f"& '{quote(installer)}' -GameRoot '{quote(game_root)}'"
    )

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "DOWNLOAD_URL_FILE": str(url_file),
            "RC_ENABLE_AUTO_UPDATE": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Current v60" in result.stdout
    assert url_file.read_text(encoding="utf-8").endswith("/RuptureCompanion-Client.dll")
    assert plugin_updater.CURRENT_MANIFEST_URL in (
        plugin_dir / "RuptureCompanion.json"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer is Windows-only")
def test_powershell_installer_discovers_secondary_library_and_removes_obsolete_files(
    tmp_path,
):
    steam_root = tmp_path / "steam"
    secondary_library = tmp_path / "other SSD" / "SteamLibrary"
    game_root = secondary_library / "steamapps/common/StarRupture"
    binary_dir = game_root / "StarRupture/Binaries/Win64"
    plugin_dir = binary_dir / "ModLoader/Plugins"
    log_dir = binary_dir / "ModLoader/Logs"
    (steam_root / "steamapps").mkdir(parents=True)
    (secondary_library / "steamapps").mkdir(parents=True)
    plugin_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    escaped_library = str(secondary_library).replace("\\", "\\\\")
    (steam_root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n  "1"\n  {{\n    "path" "{escaped_library}"\n  }}\n}}\n',
        encoding="utf-8",
    )
    (secondary_library / "steamapps/appmanifest_1631270.acf").write_text(
        '"AppState" { "appid" "1631270" "installdir" "StarRupture" }\n',
        encoding="utf-8",
    )
    (binary_dir / "StarRuptureGameSteam-Win64-Shipping.exe").write_bytes(b"")
    (binary_dir / "dwmapi.dll").write_bytes(b"MZ")
    (binary_dir / "ModLoader/modloader.ini").write_text(
        "[AutoUpdate]\nEnabled=1\n", encoding="utf-8"
    )
    (log_dir / "ModLoader.log").write_text(
        "loader supports [46, 60]\n", encoding="utf-8"
    )
    obsolete_names = (
        "RuptureCompanion-Client.dll",
        "RuptureCompanion-Client.json",
        "RuptureCompanion-Legacy.dll",
        "RuptureCompanion-Legacy.json",
    )
    for obsolete in obsolete_names:
        (plugin_dir / obsolete).write_bytes(b"old")
    installer = Path(__file__).parent.parent / "install-plugin.ps1"

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Invoke-WebRequest { param([switch]$UseBasicParsing, "
        "[string]$Uri, [string]$OutFile); "
        "[IO.File]::WriteAllBytes($OutFile, [byte[]](0x4d,0x5a,0x00)) }; "
        f"& '{quote(installer)}'"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "RC_STEAM_ROOT": str(steam_root),
            "RC_ENABLE_AUTO_UPDATE": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert str(plugin_dir) in result.stdout
    assert (plugin_dir / "RuptureCompanion.dll").read_bytes().startswith(b"MZ")
    assert not any((plugin_dir / obsolete).exists() for obsolete in obsolete_names)


@pytest.mark.parametrize(
    ("installed_manifest", "installed_dll", "expected_dll", "keeps_rollback"),
    INSTALLER_RECOVERY_CASES,
)
@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer is Windows-only")
def test_powershell_installer_keeps_recovery_state_when_staging_fails(
    tmp_path,
    installed_manifest,
    installed_dll,
    expected_dll,
    keeps_rollback,
):
    game_root, dll, rollback = make_installer_recovery_state(
        tmp_path, installed_manifest, installed_dll
    )
    installer = Path(__file__).parent.parent / "install-plugin.ps1"

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Invoke-WebRequest { param([switch]$UseBasicParsing, "
        "[string]$Uri, [string]$OutFile); "
        "[IO.File]::WriteAllBytes($OutFile, [byte[]](0x4d,0x5a,0x00)) }; "
        "function Copy-Item { [CmdletBinding()] param("
        "[Parameter(Mandatory=$true)][string]$LiteralPath, "
        "[Parameter(Mandatory=$true)][string]$Destination, [switch]$Force); "
        "if ([IO.Path]::GetFileName($Destination) -like "
        "'.RuptureCompanion.dll.update.*') { throw 'staging failed' }; "
        "Microsoft.PowerShell.Management\\Copy-Item @PSBoundParameters }; "
        f"& '{quote(installer)}' -GameRoot '{quote(game_root)}'"
    )

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RC_ENABLE_AUTO_UPDATE": "0"},
    )

    assert result.returncode != 0
    assert dll.read_bytes() == expected_dll
    assert rollback.exists() is keeps_rollback


@pytest.mark.skipif(os.name != "nt", reason="PowerShell installer is Windows-only")
def test_powershell_installer_restores_rollback_atomically(tmp_path):
    game_root, dll, rollback = make_installer_recovery_state(
        tmp_path, plugin_updater.LEGACY_MANIFEST_URL, b"MZinterrupted"
    )
    installer = Path(__file__).parent.parent / "install-plugin.ps1"

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Invoke-WebRequest { param([switch]$UseBasicParsing, "
        "[string]$Uri, [string]$OutFile); "
        "[IO.File]::WriteAllBytes($OutFile, [byte[]](0x4d,0x5a,0x00)) }; "
        "function Copy-Item { [CmdletBinding()] param("
        "[Parameter(Mandatory=$true)][string]$LiteralPath, "
        "[Parameter(Mandatory=$true)][string]$Destination, [switch]$Force); "
        "if ([IO.Path]::GetFileName($LiteralPath) -eq "
        "'RuptureCompanion.dll.rollback') { "
        "[IO.File]::WriteAllText($Destination, 'partial'); throw 'copy failed' }; "
        "Microsoft.PowerShell.Management\\Copy-Item @PSBoundParameters }; "
        f"& '{quote(installer)}' -GameRoot '{quote(game_root)}'"
    )

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"RC_ENABLE_AUTO_UPDATE": "0"},
    )

    assert result.returncode != 0
    assert dll.read_bytes() == b"MZinterrupted"
    assert rollback.read_bytes() == b"MZlegacy"
