from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

RELEASE_BASE = "https://github.com/Lucenx9/rupturecompanion/releases/latest/download"
LEGACY_MANIFEST_URL = f"{RELEASE_BASE}/RuptureCompanion-legacy-manifest.json"
CURRENT_MANIFEST_URL = f"{RELEASE_BASE}/RuptureCompanion-client-manifest.json"
CURRENT_DLL_URL = f"{RELEASE_BASE}/RuptureCompanion-Client.dll"
LEGACY_DLL_URL = f"{RELEASE_BASE}/RuptureCompanion-Legacy.dll"
MAX_PLUGIN_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024

INTERFACE_PATTERNS = (
    re.compile(r"modloader expects \[(\d+),\s*(\d+)\]", re.IGNORECASE),
    re.compile(r"loader supports \[(\d+),\s*(\d+)\]", re.IGNORECASE),
    re.compile(r"supported range \[(\d+),\s*(\d+)\]", re.IGNORECASE),
)


@dataclass(frozen=True)
class PluginVariant:
    name: str
    interface: int
    dll_url: str
    manifest_url: str


VARIANTS = (
    PluginVariant("Current v60", 60, CURRENT_DLL_URL, CURRENT_MANIFEST_URL),
    PluginVariant("Legacy v47", 47, LEGACY_DLL_URL, LEGACY_MANIFEST_URL),
)


class PluginUpdateError(Exception):
    pass


def detect_interface_range(log_text: str) -> tuple[int, int] | None:
    matches = (
        (match.start(), int(match.group(1)), int(match.group(2)))
        for pattern in INTERFACE_PATTERNS
        for match in pattern.finditer(log_text)
    )
    try:
        _, interface_min, interface_max = max(matches)
    except ValueError:
        return None
    return interface_min, interface_max


def _latest_interface(log_dir: Path) -> tuple[int, int] | None:
    try:
        latest = max(
            log_dir.glob("ModLoader*.log"), key=lambda path: path.stat().st_mtime
        )
        with latest.open("rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - MAX_LOG_BYTES))
            text = log.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return detect_interface_range(text)


def _select_variant(interface_min: int, interface_max: int) -> PluginVariant | None:
    return next(
        (
            variant
            for variant in VARIANTS
            if interface_min <= variant.interface <= interface_max
        ),
        None,
    )


def _installed_manifest(sidecar: Path) -> str:
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))["manifest_url"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def download_plugin(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "RuptureCompanion-PluginUpdater"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise PluginUpdateError("invalid plugin download size") from error
            if declared_size > MAX_PLUGIN_BYTES:
                raise PluginUpdateError("plugin download is too large")
        downloaded = 0
        with destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > MAX_PLUGIN_BYTES:
                    raise PluginUpdateError("plugin download is too large")
                output.write(chunk)
    try:
        with destination.open("rb") as plugin:
            header = plugin.read(2)
    except OSError as error:
        raise PluginUpdateError("could not verify downloaded plugin") from error
    if header != b"MZ":
        raise PluginUpdateError("downloaded plugin is not a Windows DLL")


def _temporary_path(directory: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix="RuptureCompanion.", suffix=suffix, dir=directory
    )
    os.close(descriptor)
    return Path(name)


@contextmanager
def _migration_lock(plugin_dir: Path) -> Iterator[None]:
    lock = (plugin_dir / ".RuptureCompanion.migration.lock").open("a+b")
    locked = False
    try:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(  # type: ignore[attr-defined]
                lock.fileno(),
                msvcrt.LK_LOCK,  # type: ignore[attr-defined]
                1,  # type: ignore[attr-defined]
            )
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock.fileno(),
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,  # type: ignore[attr-defined]
                )
            else:
                fcntl.flock(  # type: ignore[attr-defined]
                    lock.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        lock.close()


def _recover_pending_plugin(
    dll: Path,
    sidecar: Path,
    rollback_dll: Path,
    manifest_url: str,
) -> str:
    installed_manifest = _installed_manifest(sidecar)
    if rollback_dll.is_file():
        if dll.is_file() and installed_manifest == manifest_url:
            rollback_dll.unlink()
        elif installed_manifest != manifest_url:
            os.replace(rollback_dll, dll)
    return installed_manifest


def _sync_plugin_locked(
    modloader_dir: Path,
    plugin_dir: Path,
    cancel_event: threading.Event | None = None,
    commit_lock: threading.Lock | None = None,
) -> str | None:
    if cancel_event is not None and cancel_event.is_set():
        raise PluginUpdateError("plugin migration deferred")
    interface_range = _latest_interface(modloader_dir / "Logs")
    if interface_range is None:
        return None
    variant = _select_variant(*interface_range)
    if variant is None:
        return None

    dll = plugin_dir / "RuptureCompanion.dll"
    sidecar = plugin_dir / "RuptureCompanion.json"
    rollback_dll = plugin_dir / "RuptureCompanion.dll.rollback"
    commit_context = commit_lock if commit_lock is not None else nullcontext()
    with commit_context:
        if cancel_event is not None and cancel_event.is_set():
            raise PluginUpdateError("plugin migration deferred")
        installed_manifest = _recover_pending_plugin(
            dll,
            sidecar,
            rollback_dll,
            variant.manifest_url,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise PluginUpdateError("plugin migration deferred")
        if dll.is_file() and installed_manifest == variant.manifest_url:
            return None

    temporary_dll = _temporary_path(plugin_dir, ".dll.update")
    temporary_sidecar = _temporary_path(plugin_dir, ".json.update")
    backup_dll = (
        _temporary_path(plugin_dir, ".dll.backup")
        if dll.is_file() and not rollback_dll.is_file()
        else None
    )
    try:
        download_plugin(variant.dll_url, temporary_dll)
        temporary_sidecar.write_text(
            json.dumps({"manifest_url": variant.manifest_url}, indent=2) + "\n",
            encoding="utf-8",
        )
        commit_context = commit_lock if commit_lock is not None else nullcontext()
        with commit_context:
            if cancel_event is not None and cancel_event.is_set():
                raise PluginUpdateError("plugin migration deferred")
            if backup_dll is not None:
                shutil.copy2(dll, backup_dll)
                os.replace(backup_dll, rollback_dll)
            os.replace(temporary_dll, dll)
            try:
                os.replace(temporary_sidecar, sidecar)
            except OSError:
                if rollback_dll.is_file():
                    os.replace(rollback_dll, dll)
                else:
                    dll.unlink(missing_ok=True)
                raise
            rollback_dll.unlink(missing_ok=True)
    finally:
        temporary_dll.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
        if backup_dll is not None:
            backup_dll.unlink(missing_ok=True)
    return variant.name


def recover_plugin(
    bridge: Path,
    *,
    cancel_event: threading.Event | None = None,
    commit_lock: threading.Lock | None = None,
) -> None:
    modloader_dir = bridge.parent / "ModLoader"
    plugin_dir = modloader_dir / "Plugins"
    if not plugin_dir.is_dir():
        return
    with _migration_lock(plugin_dir):
        if cancel_event is not None and cancel_event.is_set():
            raise PluginUpdateError("plugin migration deferred")
        interface_range = _latest_interface(modloader_dir / "Logs")
        variant = (
            _select_variant(*interface_range) if interface_range is not None else None
        )
        if variant is None:
            return
        dll = plugin_dir / "RuptureCompanion.dll"
        sidecar = plugin_dir / "RuptureCompanion.json"
        rollback_dll = plugin_dir / "RuptureCompanion.dll.rollback"
        commit_context = commit_lock if commit_lock is not None else nullcontext()
        with commit_context:
            if cancel_event is not None and cancel_event.is_set():
                raise PluginUpdateError("plugin migration deferred")
            _recover_pending_plugin(
                dll,
                sidecar,
                rollback_dll,
                variant.manifest_url,
            )


def sync_plugin(
    bridge: Path,
    *,
    cancel_event: threading.Event | None = None,
    commit_lock: threading.Lock | None = None,
) -> str | None:
    modloader_dir = bridge.parent / "ModLoader"
    plugin_dir = modloader_dir / "Plugins"
    if not plugin_dir.is_dir():
        return None
    with _migration_lock(plugin_dir):
        return _sync_plugin_locked(
            modloader_dir,
            plugin_dir,
            cancel_event,
            commit_lock,
        )
