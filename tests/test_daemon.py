from contextlib import contextmanager

import pytest

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

    daemon.write_answer(9, "session-a", "First line\n__RC_END__\nLast line")

    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == (
        "v1|9|session-a|ok\nFirst line\n[__RC_END__]\nLast line\n__RC_END__\n"
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
        lambda question, path, history, **kwargs: daemon.ai_backend.AIResponse(
            "Add one more smelter.",
            used_web=False,
        ),
    )
    conversation = daemon.Conversation()
    conversation.switch_session("session-a")

    daemon.handle(5, "What is slow?", "Session mode: Standalone", conversation)

    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == (
        "v1|5|session-a|ok\nAdd one more smelter.\n__RC_END__\n"
    )
    assert conversation.history == [("What is slow?", "Add one more smelter.")]


def test_handle_does_not_remember_backward_compatible_web_answer(tmp_path, monkeypatch):
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
        lambda *args, **kwargs: daemon.ai_backend.AIResponse(
            "Answer.\n\nSources:\nSteam",
            used_web=True,
        ),
    )
    conversation = daemon.Conversation(session_id="session-a")

    daemon.handle(6, "What changed?", "Session mode: Standalone", conversation)

    assert conversation.history == []
    assert "Sources:\nSteam" in (tmp_path / "answer.txt").read_text(encoding="utf-8")


def test_handle_does_not_repeat_ai_work_when_answer_publish_retries(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    calls = 0

    @contextmanager
    def fake_capture():
        yield shot

    def fake_ask(*args, **kwargs):
        nonlocal calls
        calls += 1
        return daemon.ai_backend.AIResponse("Stored answer.", used_web=False)

    real_write_answer = daemon.write_answer
    publish_attempts = 0

    def flaky_write_answer(*args, **kwargs):
        nonlocal publish_attempts
        publish_attempts += 1
        if publish_attempts == 1:
            raise OSError("temporary bridge failure")
        real_write_answer(*args, **kwargs)

    monkeypatch.setattr(daemon.screenshot, "capture_for_analysis", fake_capture)
    monkeypatch.setattr(daemon.ai_backend, "ask", fake_ask)
    monkeypatch.setattr(daemon, "write_answer", flaky_write_answer)
    conversation = daemon.Conversation(session_id="session-a")

    with pytest.raises(OSError, match="temporary bridge failure"):
        daemon.handle(8, "Question", "Session mode: Standalone", conversation)
    daemon.handle(8, "Question", "Session mode: Standalone", conversation)

    assert calls == 1
    assert conversation.pending is None
    assert conversation.history == [("Question", "Stored answer.")]


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


def test_daemon_lock_excludes_a_second_process_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    first = daemon.acquire_lock()
    try:
        with pytest.raises(daemon.DaemonAlreadyRunning):
            daemon.acquire_lock()
    finally:
        first.close()


def test_cancellation_must_match_sequence_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    (tmp_path / "cancel.txt").write_text(
        "v1|12|session-a\n__RC_END__\n", encoding="utf-8"
    )

    assert daemon.cancellation_requested(12, "session-a")
    assert not daemon.cancellation_requested(11, "session-a")
    assert not daemon.cancellation_requested(12, "session-b")


def test_answer_identity_matches_session_aware_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    (tmp_path / "answer.txt").write_text(
        "v1|12|session-a|ok\nAnswer\n__RC_END__\n", encoding="utf-8"
    )

    assert daemon.read_answer_identity() == (12, "session-a")
