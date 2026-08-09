import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

RELEASE_URL = (
    "https://github.com/Lucenx9/rupturecompanion/releases/latest/download/"
    "RuptureCompanion-Backend.tar.gz"
)
REQUIRED_FILES = (
    "daemon.py",
    "ai_backend.py",
    "screenshot.py",
    "plugin_updater.py",
    "updater.py",
    "daemon-capabilities.json",
    "kwin-screenshot-helper",
    "VERSION",
)
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64


class UpdateError(Exception):
    pass


def _validate_kwin_helper(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError as error:
        raise UpdateError("invalid KWin screenshot helper") from error
    is_x86_64_elf = (
        len(header) == 20
        and header[:6] == b"\x7fELF\x02\x01"
        and int.from_bytes(header[18:20], "little") == 62
    )
    if not is_x86_64_elf:
        raise UpdateError("invalid KWin screenshot helper")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise UpdateError("KWin screenshot helper is not executable")
    if os.name != "nt":
        environment = os.environ.copy()
        for variable in ("QT_QPA_PLATFORM", "LD_LIBRARY_PATH", "LD_PRELOAD"):
            environment.pop(variable, None)
        try:
            probe = subprocess.run(
                [path.resolve()],
                capture_output=True,
                text=True,
                timeout=2,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise UpdateError("KWin screenshot helper cannot start") from error
        if probe.returncode != 2 or "usage:" not in probe.stderr:
            raise UpdateError("KWin screenshot helper cannot start")


def extract_backend(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UpdateError("backend archive contains too many files")
            total_size = 0
            for member in members:
                member_path = PurePosixPath(member.name)
                target = (destination / Path(*member_path.parts)).resolve()
                total_size += member.size
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or not target.is_relative_to(root)
                    or not (member.isfile() or member.isdir())
                ):
                    raise UpdateError(f"unsafe archive entry: {member.name}")
            if total_size > MAX_EXTRACTED_BYTES:
                raise UpdateError("backend archive is too large")
            archive.extractall(destination, filter="data")
        missing = [
            name for name in REQUIRED_FILES if not (destination / name).is_file()
        ]
        if missing:
            raise UpdateError(f"incomplete backend archive: {', '.join(missing)}")
        _validate_kwin_helper(destination / "kwin-screenshot-helper")
        for name in REQUIRED_FILES:
            if name.endswith(".py"):
                try:
                    source = (destination / name).read_text(encoding="utf-8")
                    compile(source, name, "exec")
                except (OSError, SyntaxError, UnicodeError) as error:
                    raise UpdateError(f"invalid Python file: {name}") from error
    except (OSError, tarfile.TarError, UpdateError):
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _download(archive_path: Path, etag_path: Path) -> str | None:
    headers = {"User-Agent": "RuptureCompanion-Updater"}
    try:
        current_etag = etag_path.read_text(encoding="utf-8").strip()
    except OSError:
        current_etag = ""
    if current_etag:
        headers["If-None-Match"] = current_etag
    request = urllib.request.Request(RELEASE_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > MAX_ARCHIVE_BYTES:
                raise UpdateError("backend download is too large")
            downloaded = 0
            with archive_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise UpdateError("backend download is too large")
                    output.write(chunk)
            return response.headers.get("ETag") or ""
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None
        raise


def update_backend(data_dir: Path) -> Path | None:
    installed = data_dir / "backend"
    if os.environ.get("RC_AUTO_UPDATE", "1") != "1":
        return installed if installed.is_dir() else None
    data_dir.mkdir(parents=True, exist_ok=True)
    etag_path = data_dir / "backend.etag"
    if not all((installed / name).is_file() for name in REQUIRED_FILES):
        etag_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="update-", dir=data_dir) as temporary_name:
        temporary = Path(temporary_name)
        archive = temporary / "backend.tar.gz"
        etag = _download(archive, etag_path)
        if etag is None:
            return installed if installed.is_dir() else None
        unpacked = temporary / "unpacked"
        extract_backend(archive, unpacked)
        previous = data_dir / "backend.previous"
        shutil.rmtree(previous, ignore_errors=True)
        if installed.is_dir():
            installed.replace(previous)
        try:
            unpacked.replace(installed)
        except OSError:
            if previous.is_dir() and not installed.exists():
                previous.replace(installed)
            raise
        etag_path.write_text(etag, encoding="utf-8")
    return installed


def confirm_backend(data_dir: Path) -> None:
    shutil.rmtree(data_dir / "backend.previous", ignore_errors=True)


def rollback_backend(data_dir: Path) -> None:
    installed = data_dir / "backend"
    previous = data_dir / "backend.previous"
    shutil.rmtree(installed, ignore_errors=True)
    if previous.is_dir():
        previous.replace(installed)
    (data_dir / "backend.etag").unlink(missing_ok=True)


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / (
            "RuptureCompanion"
        )
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / (
        "rupture-companion"
    )


def main() -> None:
    maintenance_requested = any(
        option in sys.argv[1:] for option in ("--confirm", "--rollback")
    )
    try:
        data_dir = default_data_dir()
        if "--confirm" in sys.argv[1:]:
            confirm_backend(data_dir)
        elif "--rollback" in sys.argv[1:]:
            rollback_backend(data_dir)
        else:
            update_backend(data_dir)
    except (OSError, UpdateError, urllib.error.URLError) as error:
        print(f"Rupture Companion update skipped: {error}", file=sys.stderr)
        if maintenance_requested:
            raise SystemExit(1) from error


if __name__ == "__main__":
    main()
