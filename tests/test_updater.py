import io
import struct
import tarfile
import urllib.error
from types import SimpleNamespace

import pytest

import updater

TRUNCATED_ELF_X86_64 = b"\x7fELF\x02\x01" + (b"\0" * 12) + b"\x3e\0"


def valid_elf_x86_64():
    identity = b"\x7fELF\x02\x01\x01" + (b"\0" * 9)
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        identity,
        3,
        62,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    load_segment = struct.pack("<IIQQQQQQ", 1, 5, 120, 0, 0, 1, 1, 1)
    return header + load_segment + b"\0"


def write_archive(path, files, helper_mode=0o755):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            encoded = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(encoded)
            if name == "kwin-screenshot-helper":
                info.mode = helper_mode
            archive.addfile(info, io.BytesIO(encoded))


def test_extract_backend_accepts_complete_release(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    destination = tmp_path / "backend"
    write_archive(
        archive,
        {
            "daemon.py": "daemon",
            "ai_backend.py": "backend",
            "plugin_updater.py": "plugin_updater = True",
            "screenshot.py": "screenshot",
            "updater.py": "updater",
            "daemon-capabilities.json": '{"ready_protocol": 1}',
            "kwin-screenshot-helper": valid_elf_x86_64(),
            "VERSION": "v0.1.0\n",
        },
    )

    updater.extract_backend(archive, destination)

    assert (destination / "VERSION").read_text() == "v0.1.0\n"


def test_extract_backend_rejects_invalid_kwin_helper(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    write_archive(
        archive,
        {
            "daemon.py": "daemon = True",
            "ai_backend.py": "backend = True",
            "plugin_updater.py": "plugin_updater = True",
            "screenshot.py": "screenshot = True",
            "updater.py": "updater = True",
            "daemon-capabilities.json": '{"ready_protocol": 1}',
            "kwin-screenshot-helper": b"not an ELF",
            "VERSION": "v0.1.0\n",
        },
    )

    with pytest.raises(updater.UpdateError, match="invalid KWin screenshot helper"):
        updater.extract_backend(archive, tmp_path / "backend")


def test_extract_backend_accepts_valid_kwin_helper_on_noexec_mount(
    tmp_path, monkeypatch
):
    archive = tmp_path / "backend.tar.gz"
    write_archive(
        archive,
        {
            "daemon.py": "daemon = True",
            "ai_backend.py": "backend = True",
            "plugin_updater.py": "plugin_updater = True",
            "screenshot.py": "screenshot = True",
            "updater.py": "updater = True",
            "daemon-capabilities.json": '{"ready_protocol": 1}',
            "kwin-screenshot-helper": valid_elf_x86_64(),
            "VERSION": "v0.1.0\n",
        },
    )
    monkeypatch.setattr(updater.os, "access", lambda *args: False)
    destination = tmp_path / "backend"
    updater.extract_backend(archive, destination)

    assert (destination / "kwin-screenshot-helper").is_file()


def test_extract_backend_rejects_truncated_kwin_helper(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    write_archive(
        archive,
        {
            "daemon.py": "daemon = True",
            "ai_backend.py": "backend = True",
            "plugin_updater.py": "plugin_updater = True",
            "screenshot.py": "screenshot = True",
            "updater.py": "updater = True",
            "daemon-capabilities.json": '{"ready_protocol": 1}',
            "kwin-screenshot-helper": TRUNCATED_ELF_X86_64,
            "VERSION": "v0.1.0\n",
        },
    )

    with pytest.raises(updater.UpdateError, match="invalid KWin screenshot helper"):
        updater.extract_backend(archive, tmp_path / "backend")


def test_extract_backend_rejects_helper_without_executable_archive_mode(tmp_path):
    archive = tmp_path / "backend.tar.gz"
    write_archive(
        archive,
        {
            "daemon.py": "daemon = True",
            "ai_backend.py": "backend = True",
            "plugin_updater.py": "plugin_updater = True",
            "screenshot.py": "screenshot = True",
            "updater.py": "updater = True",
            "daemon-capabilities.json": '{"ready_protocol": 1}',
            "kwin-screenshot-helper": valid_elf_x86_64(),
            "VERSION": "v0.1.0\n",
        },
        helper_mode=0o644,
    )

    with pytest.raises(updater.UpdateError, match="helper is not executable"):
        updater.extract_backend(archive, tmp_path / "backend")


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
            "plugin_updater.py": "plugin_updater = True",
            "screenshot.py": "screenshot = True",
            "updater.py": "updater = True",
            "daemon-capabilities.json": '{"ready_protocol": 1}',
            "kwin-screenshot-helper": valid_elf_x86_64(),
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


def test_download_streams_release_and_reuses_etag(tmp_path, monkeypatch):
    archive = tmp_path / "backend.tar.gz"
    etag = tmp_path / "backend.etag"
    etag.write_text('"old"', encoding="utf-8")
    observed = {}

    class FakeResponse:
        headers = {"Content-Length": "7", "ETag": '"new"'}

        def __init__(self):
            self.chunks = iter((b"payload", b""))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return next(self.chunks)

    def fake_urlopen(request, timeout):
        observed["etag"] = request.get_header("If-none-match")
        observed["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    assert updater._download(archive, etag) == '"new"'
    assert archive.read_bytes() == b"payload"
    assert observed == {"etag": '"old"', "timeout": 15}


def test_download_returns_none_for_not_modified(tmp_path, monkeypatch):
    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("url", 304, "unchanged", {}, None)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    assert updater._download(tmp_path / "archive", tmp_path / "etag") is None


def test_download_rejects_an_oversized_release(tmp_path, monkeypatch):
    class FakeResponse:
        headers = {"Content-Length": str(updater.MAX_ARCHIVE_BYTES + 1)}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(),
    )

    with pytest.raises(updater.UpdateError, match="download is too large"):
        updater._download(tmp_path / "archive", tmp_path / "etag")


@pytest.mark.parametrize(
    "content_length",
    [
        "not-a-number",
        "-1",
        "+12",
        "1_000",
        " 12",
        pytest.param("1" * 5000, id="too-many-digits"),
    ],
)
def test_download_rejects_malformed_content_length(
    tmp_path, monkeypatch, content_length
):
    class FakeResponse:
        headers = {"Content-Length": content_length}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(),
    )

    with pytest.raises(updater.UpdateError, match="invalid backend download size"):
        updater._download(tmp_path / "archive", tmp_path / "etag")


def test_update_backend_installs_release_and_keeps_rollback(tmp_path, monkeypatch):
    installed = tmp_path / "backend"
    installed.mkdir()
    (installed / "VERSION").write_text("v0.1.0\n", encoding="utf-8")

    def fake_download(archive, etag_path):
        assert not etag_path.exists()
        write_archive(
            archive,
            {
                "daemon.py": "daemon = True",
                "ai_backend.py": "backend = True",
                "plugin_updater.py": "plugin_updater = True",
                "screenshot.py": "screenshot = True",
                "updater.py": "updater = True",
                "daemon-capabilities.json": '{"ready_protocol": 1}',
                "kwin-screenshot-helper": valid_elf_x86_64(),
                "VERSION": "v0.2.0\n",
            },
        )
        return '"v0.2.0"'

    monkeypatch.setattr(updater, "_download", fake_download)

    result = updater.update_backend(tmp_path)

    assert result == installed
    assert (installed / "VERSION").read_text() == "v0.2.0\n"
    assert (tmp_path / "backend.previous/VERSION").read_text() == "v0.1.0\n"
    assert (tmp_path / "backend.etag").read_text() == '"v0.2.0"'


def test_update_backend_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_AUTO_UPDATE", "0")

    assert updater.update_backend(tmp_path) is None
    assert not (tmp_path / "backend").exists()


def test_confirm_backend_removes_rollback_copy(tmp_path):
    previous = tmp_path / "backend.previous"
    previous.mkdir()

    updater.confirm_backend(tmp_path)

    assert not previous.exists()


@pytest.mark.parametrize(
    ("name", "environment", "expected"),
    [
        ("posix", {"XDG_DATA_HOME": "/data"}, "/data/rupture-companion"),
        ("nt", {"LOCALAPPDATA": "C:/Data"}, "C:/Data/RuptureCompanion"),
    ],
)
def test_default_data_dir_is_platform_specific(
    monkeypatch, name, environment, expected
):
    monkeypatch.setattr(
        updater,
        "os",
        SimpleNamespace(name=name, environ=environment),
    )

    assert updater.default_data_dir().as_posix() == expected


@pytest.mark.parametrize(
    ("argument", "operation"),
    [("--confirm", "confirm_backend"), ("--rollback", "rollback_backend")],
)
def test_main_runs_requested_maintenance(monkeypatch, tmp_path, argument, operation):
    observed = []
    monkeypatch.setattr(updater.sys, "argv", ["updater.py", argument])
    monkeypatch.setattr(updater, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, operation, lambda path: observed.append(path))

    updater.main()

    assert observed == [tmp_path]


def test_main_reports_failed_maintenance(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(updater.sys, "argv", ["updater.py", "--rollback"])
    monkeypatch.setattr(updater, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        updater,
        "rollback_backend",
        lambda path: (_ for _ in ()).throw(OSError("locked")),
    )

    with pytest.raises(SystemExit, match="1"):
        updater.main()

    assert "update skipped: locked" in capsys.readouterr().err
