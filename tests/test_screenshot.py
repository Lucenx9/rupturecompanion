import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import screenshot


def test_windows_capture_saves_png(monkeypatch, tmp_path):
    output = tmp_path / "shot.png"
    observed: dict[str, object] = {}

    class FakeImage:
        def save(self, path: Path, image_format: str) -> None:
            observed["save"] = (path, image_format)
            path.write_bytes(b"png")

    def fake_grab(**kwargs):
        observed["grab"] = kwargs
        return FakeImage()

    monkeypatch.setattr(
        screenshot.ImageGrab,
        "grab",
        fake_grab,
    )

    screenshot._capture_windows(output)

    assert observed["grab"] == {"all_screens": True}
    assert observed["save"] == (output, "PNG")
    assert output.read_bytes() == b"png"


def test_linux_capture_uses_spectacle_and_sanitizes_environment(monkeypatch, tmp_path):
    observed: dict[str, object] = {}
    monkeypatch.setattr(screenshot, "_SHOT_DIR", tmp_path)
    monkeypatch.setattr(
        screenshot,
        "os",
        SimpleNamespace(name="posix", environ=os.environ),
    )
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/game/libs")
    monkeypatch.setenv("LD_PRELOAD", "overlay.so")
    monkeypatch.setattr(screenshot, "_capture_with_kwin_helper", lambda *args: False)

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"png")

    monkeypatch.setattr(screenshot.subprocess, "run", fake_run)

    output = screenshot.capture(timeout=3)

    assert output.read_bytes() == b"png"
    assert observed["command"] == [
        "spectacle",
        "-b",
        "-n",
        "-o",
        str(tmp_path / "shot.png"),
    ]
    environment = observed["kwargs"]["env"]
    assert "QT_QPA_PLATFORM" not in environment
    assert "LD_LIBRARY_PATH" not in environment
    assert "LD_PRELOAD" not in environment


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError(), "No Linux screenshot backend is available"),
        (
            subprocess.CalledProcessError(1, ["spectacle"]),
            "Spectacle failed",
        ),
    ],
)
def test_linux_capture_reports_spectacle_errors(monkeypatch, tmp_path, error, message):
    monkeypatch.setattr(screenshot, "_SHOT_DIR", tmp_path)
    monkeypatch.setattr(
        screenshot,
        "os",
        SimpleNamespace(name="posix", environ=os.environ),
    )
    monkeypatch.setattr(screenshot, "_capture_with_kwin_helper", lambda *args: False)

    def fake_run(*args, **kwargs):
        raise error

    monkeypatch.setattr(screenshot.subprocess, "run", fake_run)

    with pytest.raises(screenshot.ScreenshotError, match=message):
        screenshot.capture()

    assert not (tmp_path / "shot.png").exists()


def test_linux_capture_prefers_kwin_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(screenshot, "_SHOT_DIR", tmp_path)
    monkeypatch.setattr(
        screenshot,
        "os",
        SimpleNamespace(name="posix", environ=os.environ),
    )

    def fake_kwin(path, timeout, environment):
        assert timeout == 3
        path.write_bytes(b"png")
        return True

    monkeypatch.setattr(screenshot, "_capture_with_kwin_helper", fake_kwin)

    def forbidden_spectacle(*args, **kwargs):
        pytest.fail("Spectacle should not start when direct KWin capture succeeds")

    monkeypatch.setattr(screenshot.subprocess, "run", forbidden_spectacle)

    output = screenshot.capture(timeout=3)

    assert output.read_bytes() == b"png"


def test_save_kwin_image_decodes_bgra_rows(tmp_path):
    output = tmp_path / "shot.png"
    metadata = {
        "type": ("s", "raw"),
        "width": ("u", 2),
        "height": ("u", 1),
        "stride": ("u", 8),
        "format": ("u", 6),
    }
    raw = bytes((0, 0, 255, 255, 0, 255, 0, 255))

    screenshot._save_kwin_image(output, metadata, raw)

    with screenshot.Image.open(output) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0, 255)
        assert image.getpixel((1, 0)) == (0, 255, 0, 255)


def test_capture_for_analysis_removes_the_temporary_image(monkeypatch, tmp_path):
    output = tmp_path / "shot.png"
    output.write_bytes(b"png")
    monkeypatch.setattr(screenshot, "capture", lambda: output)

    with screenshot.capture_for_analysis() as captured:
        assert captured == output
        assert output.exists()

    assert not output.exists()
