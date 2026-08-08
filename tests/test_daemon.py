from contextlib import contextmanager

import daemon


def test_parse_request_requires_complete_marker():
    complete = (
        "v1|42|session-a|What should I craft?\nSession mode: Standalone\n__RC_END__\n"
    )

    assert daemon.parse_request(complete) == (
        42,
        "session-a",
        "What should I craft?",
        "Session mode: Standalone",
    )
    assert daemon.parse_request(complete.removesuffix("__RC_END__\n")) is None


def test_parse_request_preserves_pipes_in_question():
    request = "v1|7|session-b|iron | copper?\n__RC_END__\n"

    assert daemon.parse_request(request) == (
        7,
        "session-b",
        "iron | copper?",
        "",
    )


def test_write_answer_is_atomic_and_escapes_reserved_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))

    daemon.write_answer(9, "First line\n__RC_END__\nLast line")

    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == (
        "9|ok\nFirst line\n[__RC_END__]\nLast line\n__RC_END__\n"
    )
    assert not (tmp_path / "answer.tmp").exists()


def test_handle_captures_screenshot_and_remembers_non_web_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")

    @contextmanager
    def fake_capture():
        yield shot

    monkeypatch.setattr(daemon.screenshot, "capture_for_analysis", fake_capture)
    monkeypatch.setattr(
        daemon.ai_backend,
        "ask",
        lambda question, path, history, **kwargs: "Add one more smelter.",
    )
    conversation = daemon.Conversation()
    conversation.switch_session("session-a")

    daemon.handle(5, "What is slow?", "Session mode: Standalone", conversation)

    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == (
        "5|ok\nAdd one more smelter.\n__RC_END__\n"
    )
    assert conversation.history == [("What is slow?", "Add one more smelter.")]


def test_switch_session_resets_history_and_web_mode():
    conversation = daemon.Conversation(
        session_id="old",
        history=[("question", "answer")],
        web_enabled=False,
    )

    assert conversation.switch_session("new")
    assert conversation.history == []
    assert conversation.web_enabled
    assert not conversation.switch_session("new")


def test_discover_bridge_dir_from_steam_library(tmp_path, monkeypatch):
    steam = tmp_path / "Steam"
    library = tmp_path / "Library"
    (steam / "steamapps").mkdir(parents=True)
    (library / "steamapps").mkdir(parents=True)
    (library / "steamapps/appmanifest_1631270.acf").write_text(
        '"installdir" "StarRupture"', encoding="utf-8"
    )
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"path" "{library}"', encoding="utf-8"
    )
    monkeypatch.delenv("RC_BRIDGE_DIR", raising=False)
    monkeypatch.setattr(daemon, "STEAM_ROOTS", (steam,))

    assert daemon.bridge_dir() == (
        library
        / "steamapps/common/StarRupture/StarRupture/Binaries/Win64"
        / "RuptureCompanion"
    )
