import filecmp
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab


class ScreenshotError(Exception):
    pass


_SHOT_TEMP = tempfile.TemporaryDirectory(
    prefix="rupture-companion-",
    ignore_cleanup_errors=True,
)
_SHOT_DIR = Path(_SHOT_TEMP.name)
_KWIN_HELPER_NAME = "rupture-companion-screenshot-helper"
_KWIN_INTERFACE = "org.kde.KWin.ScreenShot2"
_KWIN_SERVICE = "org.kde.KWin.ScreenShot2"
_KWIN_OBJECT = "/org/kde/KWin/ScreenShot2"
_KWIN_MAX_IMAGE_BYTES = 512 * 1024 * 1024


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


def _same_executable(source: Path, helper: Path) -> bool:
    try:
        if os.path.samestat(source.stat(), helper.stat()):
            return True
        return filecmp.cmp(source, helper, shallow=True)
    except OSError:
        return False


def _install_helper_executable(source: Path, helper: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=helper.parent,
        prefix=f".{_KWIN_HELPER_NAME}-",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        temporary.chmod(0o755)
        temporary.replace(helper)
    finally:
        temporary.unlink(missing_ok=True)


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

    try:
        source = Path(sys.executable).resolve(strict=True)
        helper = Path(sys.executable).parent / _KWIN_HELPER_NAME
        if not _same_executable(source, helper):
            _install_helper_executable(source, helper)

        identity = hashlib.sha256(str(helper).encode()).hexdigest()[:12]
        data_home = Path(
            environment.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        )
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
            cache_builder = shutil.which("kbuildsycoca6") or shutil.which(
                "kbuildsycoca5"
            )
            if cache_builder is None:
                return None
            subprocess.run(
                [cache_builder, "--noincremental"],
                check=True,
                capture_output=True,
                timeout=15,
                env=environment,
            )
            ready_marker.touch()
        return helper
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _variant_value(metadata: dict[str, Any], key: str) -> Any:
    variant = metadata.get(key)
    if not isinstance(variant, tuple) or len(variant) != 2:
        raise ScreenshotError(f"KWin returned invalid {key} metadata")
    return variant[1]


def _read_image_bytes(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    image_bytes = b"".join(chunks)
    if len(image_bytes) != expected_size:
        raise ScreenshotError(
            f"KWin returned {len(image_bytes)} of {expected_size} image bytes"
        )
    return image_bytes


def _save_kwin_image(path: Path, metadata: dict[str, Any], image_bytes: bytes) -> None:
    image_type = _variant_value(metadata, "type")
    width = _variant_value(metadata, "width")
    height = _variant_value(metadata, "height")
    stride = _variant_value(metadata, "stride")
    image_format = _variant_value(metadata, "format")
    if image_type != "raw":
        raise ScreenshotError(f"KWin returned unsupported image type: {image_type}")
    if not all(isinstance(value, int) for value in (width, height, stride)):
        raise ScreenshotError("KWin returned invalid image dimensions")
    if not (0 < width <= 32768 and 0 < height <= 32768):
        raise ScreenshotError("KWin returned unsafe image dimensions")
    if stride < width * 4:
        raise ScreenshotError("KWin returned an invalid image stride")
    expected_size = stride * height
    if expected_size > _KWIN_MAX_IMAGE_BYTES or len(image_bytes) != expected_size:
        raise ScreenshotError("KWin returned an invalid image size")
    if image_format not in (4, 5, 6):
        raise ScreenshotError(f"KWin returned unsupported QImage format {image_format}")

    image = Image.frombytes(
        "RGBA",
        (width, height),
        image_bytes,
        "raw",
        "BGRA",
        stride,
        1,
    )
    image.save(path, "PNG")


def _capture_kwin_workspace(path: Path, timeout: float) -> None:
    from jeepney import DBusAddress, new_method_call  # type: ignore[import-untyped]
    from jeepney.io.blocking import (  # type: ignore[import-untyped]
        open_dbus_connection,
    )
    from jeepney.low_level import MessageType  # type: ignore[import-untyped]

    read_descriptor, write_descriptor = os.pipe()
    try:
        address = DBusAddress(
            _KWIN_OBJECT,
            bus_name=_KWIN_SERVICE,
            interface=_KWIN_INTERFACE,
        )
        options = {
            "include-cursor": ("b", False),
            "native-resolution": ("b", False),
            "hide-caller-windows": ("b", False),
        }
        message = new_method_call(
            address,
            "CaptureWorkspace",
            "a{sv}h",
            (options, write_descriptor),
        )
        with open_dbus_connection(enable_fds=True) as connection:
            reply = connection.send_and_get_reply(message, timeout=timeout)
        os.close(write_descriptor)
        write_descriptor = -1
        if reply.header.message_type is not MessageType.method_return:
            raise ScreenshotError(f"KWin rejected the screenshot request: {reply.body}")
        if len(reply.body) != 1 or not isinstance(reply.body[0], dict):
            raise ScreenshotError("KWin returned invalid screenshot metadata")
        metadata = reply.body[0]
        stride = _variant_value(metadata, "stride")
        height = _variant_value(metadata, "height")
        if not isinstance(stride, int) or not isinstance(height, int):
            raise ScreenshotError("KWin returned invalid image dimensions")
        expected_size = stride * height
        if not (0 < expected_size <= _KWIN_MAX_IMAGE_BYTES):
            raise ScreenshotError("KWin returned an unsafe image size")
        image_bytes = _read_image_bytes(read_descriptor, expected_size)
        _save_kwin_image(path, metadata, image_bytes)
    finally:
        os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)


def _capture_with_kwin_helper(
    path: Path,
    timeout: float,
    environment: dict[str, str],
) -> bool:
    helper = _ensure_kwin_helper(environment)
    if helper is None:
        return False
    try:
        subprocess.run(
            [
                helper,
                Path(__file__).resolve(),
                "--kwin-capture",
                str(path),
                str(timeout),
            ],
            check=True,
            capture_output=True,
            timeout=timeout + 2,
            env=environment,
        )
        return path.exists() and path.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        path.unlink(missing_ok=True)
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


def _main(arguments: list[str]) -> int:
    if len(arguments) != 4 or arguments[1] != "--kwin-capture":
        return 2
    output = Path(arguments[2])
    try:
        timeout = float(arguments[3])
        _capture_kwin_workspace(output, timeout)
        return 0
    except (OSError, ScreenshotError, ValueError):
        output.unlink(missing_ok=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
