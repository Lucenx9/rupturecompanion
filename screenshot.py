import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ScreenshotError(Exception):
    pass


_SHOT_TEMP = tempfile.TemporaryDirectory(
    prefix="rupture-companion-",
    ignore_cleanup_errors=True,
)
_SHOT_DIR = Path(_SHOT_TEMP.name)


def capture(timeout: float = 10) -> Path:
    path = _SHOT_DIR / "shot.png"
    path.unlink(missing_ok=True)
    environment = os.environ.copy()
    for variable in ("QT_QPA_PLATFORM", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        environment.pop(variable, None)
    captured = False
    try:
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
        raise ScreenshotError("Spectacle did not create a screenshot")
    except FileNotFoundError as error:
        raise ScreenshotError("Spectacle is not installed") from error
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
