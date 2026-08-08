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
