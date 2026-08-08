import os
import shutil
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
REQUIRED_FILES = ("daemon.py", "ai_backend.py", "screenshot.py", "VERSION")


class UpdateError(Exception):
    pass


def extract_backend(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                target = (destination / Path(*name.parts)).resolve()
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not target.is_relative_to(root)
                    or not (member.isfile() or member.isdir())
                ):
                    raise UpdateError(f"unsafe archive entry: {member.name}")
            archive.extractall(destination, filter="data")
        missing = [
            name for name in REQUIRED_FILES if not (destination / name).is_file()
        ]
        if missing:
            raise UpdateError(f"incomplete backend archive: {', '.join(missing)}")
    except (OSError, tarfile.TarError):
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
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
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
        shutil.rmtree(previous, ignore_errors=True)
        etag_path.write_text(etag, encoding="utf-8")
    return installed


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / (
            "RuptureCompanion"
        )
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / (
        "rupture-companion"
    )


def main() -> None:
    try:
        update_backend(default_data_dir())
    except (OSError, UpdateError, urllib.error.URLError) as error:
        print(f"Rupture Companion update skipped: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
