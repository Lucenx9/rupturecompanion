import io
import tarfile

import pytest

import updater


def write_archive(path, files):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            encoded = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))


def test_extract_backend_accepts_complete_release(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    destination = tmp_path / "backend"
    write_archive(
        archive,
        {
            "daemon.py": "daemon",
            "ai_backend.py": "backend",
            "screenshot.py": "screenshot",
            "updater.py": "updater",
            "VERSION": "v0.1.0\n",
        },
    )

    updater.extract_backend(archive, destination)

    assert (destination / "VERSION").read_text() == "v0.1.0\n"


def test_extract_backend_rejects_path_traversal(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    write_archive(archive, {"../escaped": "bad"})

    with pytest.raises(updater.UpdateError, match="unsafe archive entry"):
        updater.extract_backend(archive, tmp_path / "backend")

    assert not (tmp_path / "escaped").exists()


def test_extract_backend_rejects_invalid_python(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    write_archive(
        archive,
        {
            "daemon.py": "not valid Python !",
            "ai_backend.py": "backend = True",
            "screenshot.py": "screenshot = True",
            "updater.py": "updater = True",
            "VERSION": "v0.1.0\n",
        },
    )

    with pytest.raises(updater.UpdateError, match="invalid Python file: daemon.py"):
        updater.extract_backend(archive, tmp_path / "backend")


def test_rollback_restores_previous_backend(tmp_path):
    installed = tmp_path / "backend"
    previous = tmp_path / "backend.previous"
    installed.mkdir()
    previous.mkdir()
    (installed / "VERSION").write_text("bad")
    (previous / "VERSION").write_text("good")
    (tmp_path / "backend.etag").write_text("new")

    updater.rollback_backend(tmp_path)

    assert (installed / "VERSION").read_text() == "good"
    assert not previous.exists()
    assert not (tmp_path / "backend.etag").exists()
