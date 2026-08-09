import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageGrab


class ScreenshotError(Exception):
    pass


_SHOT_TEMP = tempfile.TemporaryDirectory(
    prefix="rupture-companion-",
    ignore_cleanup_errors=True,
)
_SHOT_DIR = Path(_SHOT_TEMP.name)
_KWIN_HELPER_NAME = "kwin-screenshot-helper"
_KWIN_INTERFACE = "org.kde.KWin.ScreenShot2"
_KWIN_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_KWIN_RETRY_SECONDS = 30
_kwin_direct_ready: bool | None = None
_kwin_retry_after = 0.0


@dataclass(frozen=True)
class _KWinImageInfo:
    width: int
    height: int
    stride: int
    image_format: int

    @classmethod
    def parse(cls, output: str) -> "_KWinImageInfo":
        fields = output.strip().split()
        if len(fields) != 5 or fields[0] != "raw":
            raise ScreenshotError("KWin helper returned invalid image metadata")
        try:
            info = cls(*(int(value) for value in fields[1:]))
        except ValueError as error:
            raise ScreenshotError(
                "KWin helper returned invalid image metadata"
            ) from error
        info.validate()
        return info

    @property
    def size(self) -> int:
        return self.stride * self.height

    def validate(self) -> None:
        if not (0 < self.width <= 32768 and 0 < self.height <= 32768):
            raise ScreenshotError("KWin returned unsafe image dimensions")
        if self.stride < self.width * 4:
            raise ScreenshotError("KWin returned an invalid image stride")
        if not (0 < self.size <= _KWIN_MAX_IMAGE_BYTES):
            raise ScreenshotError("KWin returned an unsafe image size")
        if self.image_format not in (4, 5, 6):
            raise ScreenshotError(
                f"KWin returned unsupported QImage format {self.image_format}"
            )


def _sanitized_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in ("QT_QPA_PLATFORM", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        environment.pop(variable, None)
    return environment


def _kwin_session_available(environment: dict[str, str]) -> bool:
    desktop = environment.get("XDG_CURRENT_DESKTOP", "").casefold()
    kde_session = environment.get("KDE_FULL_SESSION", "").casefold() == "true"
    return bool(environment.get("DBUS_SESSION_BUS_ADDRESS")) and (
        kde_session or "kde" in desktop or "plasma" in desktop
    )


def _desktop_exec_path(path: Path) -> str:
    value = str(path)
    if "\n" in value or "\r" in value:
        raise ValueError("invalid helper path")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, f"\\{character}")
    return f'"{value}"'


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except (FileNotFoundError, OSError, UnicodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}-",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _ensure_kwin_helper(environment: dict[str, str]) -> Path | None:
    if not _kwin_session_available(environment):
        return None

    helper = Path(__file__).with_name(_KWIN_HELPER_NAME).resolve()
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise ScreenshotError(f"fixed-purpose KWin helper is unavailable: {helper}")

    identity = hashlib.sha256(str(helper).encode()).hexdigest()[:12]
    data_home = Path(environment.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    desktop_file = (
        data_home
        / "applications"
        / f"rupture-companion-screenshot-helper-{identity}.desktop"
    )
    desktop_entry = "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Name=Rupture Companion Screenshot Helper",
            "NoDisplay=true",
            f"Exec={_desktop_exec_path(helper)}",
            f"X-KDE-DBUS-Restricted-Interfaces={_KWIN_INTERFACE}",
            "",
        )
    )
    changed = _write_if_changed(desktop_file, desktop_entry)
    ready_marker = helper.with_name(f".{helper.name}-{identity}.ready")
    if changed:
        ready_marker.unlink(missing_ok=True)
    if not ready_marker.exists():
        cache_builder = shutil.which("kbuildsycoca6") or shutil.which("kbuildsycoca5")
        if cache_builder is None:
            raise ScreenshotError("kbuildsycoca is unavailable")
        subprocess.run(
            [cache_builder, "--noincremental"],
            check=True,
            capture_output=True,
            timeout=15,
            env=environment,
        )
        ready_marker.touch()
    return helper


def prepare() -> None:
    global _kwin_direct_ready, _kwin_retry_after
    if os.name == "nt":
        return
    environment = _sanitized_environment()
    if not _kwin_session_available(environment):
        _kwin_direct_ready = False
        _kwin_retry_after = float("inf")
        print(
            "Rupture Companion screenshot backend: Spectacle fallback",
            file=sys.stderr,
        )
        return
    try:
        helper = _ensure_kwin_helper(environment)
        if helper is None:
            raise ScreenshotError("KWin helper is unavailable")
        probe = _SHOT_DIR / "kwin-probe.png"
        probe.unlink(missing_ok=True)
        try:
            _capture_kwin_workspace(probe, helper, 3, environment)
        finally:
            probe.unlink(missing_ok=True)
    except (OSError, ScreenshotError, subprocess.SubprocessError, ValueError) as error:
        _kwin_direct_ready = False
        _kwin_retry_after = time.monotonic() + _KWIN_RETRY_SECONDS
        print(
            f"Rupture Companion KWin setup failed: {error}; using Spectacle",
            file=sys.stderr,
        )
        return
    _kwin_direct_ready = True
    _kwin_retry_after = 0.0
    print("Rupture Companion screenshot backend: direct KWin capture")


def _save_kwin_image(path: Path, info: _KWinImageInfo, image_bytes: bytes) -> None:
    info.validate()
    if len(image_bytes) != info.size:
        raise ScreenshotError("KWin returned an invalid image size")
    image = Image.frombytes(
        "RGBA",
        (info.width, info.height),
        image_bytes,
        "raw",
        "BGRA",
        info.stride,
        1,
    )
    image.save(path, "PNG")


def _capture_kwin_workspace(
    path: Path,
    helper: Path,
    timeout: float,
    environment: dict[str, str],
) -> None:
    raw_path = path.with_suffix(".kwin.raw")
    raw_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [helper, raw_path, str(max(1, round(timeout * 1000)))],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            env=environment,
        )
        info = _KWinImageInfo.parse(result.stdout)
        if raw_path.stat().st_size != info.size:
            raise ScreenshotError("KWin helper returned an incomplete image")
        _save_kwin_image(path, info, raw_path.read_bytes())
    finally:
        raw_path.unlink(missing_ok=True)


def _fallback_reason(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        stderr = error.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        if isinstance(stderr, str):
            return stderr.strip().splitlines()[-1]
    return str(error)


def _capture_with_kwin_helper(
    path: Path,
    timeout: float,
    environment: dict[str, str],
) -> bool:
    global _kwin_direct_ready, _kwin_retry_after
    if not _kwin_session_available(environment):
        return False
    if _kwin_direct_ready is False and time.monotonic() < _kwin_retry_after:
        return False
    try:
        helper = _ensure_kwin_helper(environment)
        if helper is None:
            return False
        _capture_kwin_workspace(path, helper, min(timeout, 1), environment)
        _kwin_direct_ready = True
        _kwin_retry_after = 0.0
        return path.exists() and path.stat().st_size > 0
    except (OSError, ScreenshotError, subprocess.SubprocessError, ValueError) as error:
        _kwin_direct_ready = False
        _kwin_retry_after = time.monotonic() + _KWIN_RETRY_SECONDS
        path.unlink(missing_ok=True)
        print(
            f"Rupture Companion direct KWin capture failed: "
            f"{_fallback_reason(error)}; using Spectacle",
            file=sys.stderr,
        )
        return False


def _capture_windows(path: Path) -> None:
    image = ImageGrab.grab(all_screens=True)
    image.save(path, "PNG")


def capture(timeout: float = 10) -> Path:
    path = _SHOT_DIR / "shot.png"
    path.unlink(missing_ok=True)
    environment = _sanitized_environment()
    captured = False
    try:
        if os.name == "nt":
            _capture_windows(path)
        elif not _capture_with_kwin_helper(path, timeout, environment):
            subprocess.run(
                ["spectacle", "-b", "-n", "-o", str(path)],
                check=True,
                capture_output=True,
                timeout=timeout,
                env=environment,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size > 0:
                captured = True
                return path
            time.sleep(0.1)
        raise ScreenshotError("The screenshot backend did not create an image")
    except FileNotFoundError as error:
        raise ScreenshotError("No Linux screenshot backend is available") from error
    except OSError as error:
        platform = "Windows" if os.name == "nt" else "Linux"
        raise ScreenshotError(f"{platform} screenshot failed: {error}") from error
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ScreenshotError(f"Spectacle failed: {error}") from error
    finally:
        if not captured:
            path.unlink(missing_ok=True)


@contextmanager
def capture_for_analysis() -> Iterator[Path]:
    path = capture()
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
