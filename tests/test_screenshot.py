from pathlib import Path

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
