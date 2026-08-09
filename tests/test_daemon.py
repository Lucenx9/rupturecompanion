import json
from contextlib import contextmanager

import pytest

import daemon


def live_snapshot(**overrides):
    snapshot = {
        "schema_version": 1,
        "captured_at_unix_ms": 1_700_000_000_000,
        "source": {
            "kind": "client_observed",
            "game_sdk_build": "CL121391",
            "sample_interval_ms": 750,
        },
        "status": {
            "available": True,
            "partial": False,
            "missing_sections": [],
            "truncated_sections": [],
        },
        "player": {"vitals": {"health": {"current": 75, "max": 100}}},
    }
    snapshot.update(overrides)
    return snapshot


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


def test_parse_request_validates_and_normalizes_live_context():
    snapshot = json.dumps(live_snapshot(), indent=2).replace("\n", " ")
    request = (
        "v1|8|session-live|How am I doing?\n"
        "Session mode: Standalone\n"
        "__RC_LIVE_CONTEXT_V1__\n"
        f"{snapshot}\n"
        "__RC_END__\n"
    )

    parsed = daemon.parse_request(request)

    assert parsed is not None
    assert parsed[:3] == (8, "session-live", "How am I doing?")
    assert parsed[3].startswith("Session mode: Standalone\n__RC_LIVE_CONTEXT_V1__\n")
    normalized = json.loads(parsed[3].splitlines()[-1])
    assert normalized["player"]["vitals"]["health"]["current"] == 75


@pytest.mark.parametrize(
    "live_context",
    [
        "not-json",
        "[]",
        '{"schema_version":2}',
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":1,"captured_at_unix_ms":NaN}',
    ],
)
def test_parse_request_discards_invalid_live_context(live_context):
    request = (
        "v1|9|session-live|Question\n"
        "Session mode: Standalone\n"
        "__RC_LIVE_CONTEXT_V1__\n"
        f"{live_context}\n"
        "__RC_END__\n"
    )

    assert daemon.parse_request(request) == (
        9,
        "session-live",
        "Question",
        "Session mode: Standalone",
    )


def test_parse_request_discards_duplicate_live_context_markers():
    snapshot = json.dumps(live_snapshot(), separators=(",", ":"))
    request = (
        "v1|10|session-live|Question\n"
        "Session mode: Standalone\n"
        "__RC_LIVE_CONTEXT_V1__\n"
        f"{snapshot}\n"
        "__RC_LIVE_CONTEXT_V1__\n"
        f"{snapshot}\n"
        "__RC_END__\n"
    )

    assert daemon.parse_request(request) == (
        10,
        "session-live",
        "Question",
        "Session mode: Standalone",
    )


def test_parse_request_preserves_unicode_line_separators_inside_live_json():
    snapshot = live_snapshot(
        player={"inventory": {"items": [{"name": "Iron\u2028Ore", "amount": 2}]}}
    )
    request = (
        "v1|11|session-live|Question\n"
        f"{daemon.LIVE_CONTEXT_MARKER}\n"
        f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}\n"
        f"{daemon.END_MARKER}\n"
    )

    parsed = daemon.parse_request(request)

    assert parsed is not None
    assert "Iron\u2028Ore" in parsed[3]


@pytest.mark.parametrize(
    "header",
    [
        "v1|1|bad session|Question",
        f"v1|1|{'a' * (daemon.MAX_SESSION_ID_BYTES + 1)}|Question",
        f"v1|1|session|{'q' * (daemon.MAX_QUESTION_BYTES + 1)}",
    ],
)
def test_parse_request_rejects_unbounded_identity_fields(header):
    assert daemon.parse_request(f"{header}\n{daemon.END_MARKER}\n") is None


def test_read_request_rejects_oversized_or_invalid_utf8(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    request = tmp_path / "question.txt"
    request.write_bytes(b"x" * (daemon.MAX_REQUEST_BYTES + 1))

    assert daemon.read_request() == ""

    request.write_bytes(b"\xff")
    assert daemon.read_request() == ""


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


def test_handle_skips_desktop_capture_when_fresh_live_state_is_available(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    snapshot = live_snapshot(captured_at_unix_ms=int(daemon.time.time() * 1000))
    game_state = (
        "Session mode: Standalone\n"
        f"{daemon.LIVE_CONTEXT_MARKER}\n"
        f"{json.dumps(snapshot, separators=(',', ':'))}"
    )
    observed = {}

    @contextmanager
    def forbidden_capture():
        raise AssertionError("fresh live state must not trigger a desktop capture")
        yield

    def fake_ask(question, path, history, **kwargs):
        observed["path"] = path
        return daemon.ai_backend.AIResponse("Use the nearby base core.", used_web=False)

    monkeypatch.setattr(daemon.screenshot, "capture_for_analysis", forbidden_capture)
    monkeypatch.setattr(daemon.ai_backend, "ask", fake_ask)
    conversation = daemon.Conversation(session_id="session-a")

    daemon.handle(6, "Where should I go?", game_state, conversation)

    assert observed["path"] is None
    assert conversation.history == [("Where should I go?", "Use the nearby base core.")]


def test_handle_screen_directive_forces_capture_with_fresh_live_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    snapshot = live_snapshot(captured_at_unix_ms=int(daemon.time.time() * 1000))
    game_state = (
        "Session mode: Standalone\n"
        f"{daemon.LIVE_CONTEXT_MARKER}\n"
        f"{json.dumps(snapshot, separators=(',', ':'))}"
    )
    observed = {}

    @contextmanager
    def fake_capture():
        yield shot

    def fake_ask(question, path, history, **kwargs):
        observed["path"] = path
        return daemon.ai_backend.AIResponse("The crater is ahead.", used_web=False)

    monkeypatch.setattr(daemon.screenshot, "capture_for_analysis", fake_capture)
    monkeypatch.setattr(daemon.ai_backend, "ask", fake_ask)
    conversation = daemon.Conversation(session_id="session-a")

    daemon.handle(7, "/screen Where should I go?", game_state, conversation)

    assert observed["path"] == str(shot)


def test_handle_keeps_automatic_windows_capture_with_fresh_live_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "WINDOWS_SCREENSHOT_DEFAULT", True)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    snapshot = live_snapshot(captured_at_unix_ms=int(daemon.time.time() * 1000))
    game_state = (
        f"{daemon.LIVE_CONTEXT_MARKER}\n{json.dumps(snapshot, separators=(',', ':'))}"
    )
    observed = {}

    @contextmanager
    def fake_capture():
        yield shot

    def fake_ask(question, path, history, **kwargs):
        observed["path"] = path
        return daemon.ai_backend.AIResponse("Use both inputs.", used_web=False)

    monkeypatch.setattr(daemon.screenshot, "capture_for_analysis", fake_capture)
    monkeypatch.setattr(daemon.ai_backend, "ask", fake_ask)
    conversation = daemon.Conversation(session_id="session-a")

    daemon.handle(8, "What should I do?", game_state, conversation)

    assert observed["path"] == str(shot)


def test_handle_windows_capture_failure_falls_back_to_fresh_live_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RC_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "WINDOWS_SCREENSHOT_DEFAULT", True)
    snapshot = live_snapshot(captured_at_unix_ms=int(daemon.time.time() * 1000))
    game_state = (
        f"{daemon.LIVE_CONTEXT_MARKER}\n{json.dumps(snapshot, separators=(',', ':'))}"
    )
    observed = {}

    @contextmanager
    def failed_capture():
        raise daemon.screenshot.ScreenshotError("capture failed")
        yield

    def fake_ask(question, path, history, **kwargs):
        observed["path"] = path
        return daemon.ai_backend.AIResponse("Live data is enough.", used_web=False)

    monkeypatch.setattr(daemon.screenshot, "capture_for_analysis", failed_capture)
    monkeypatch.setattr(daemon.ai_backend, "ask", fake_ask)
    conversation = daemon.Conversation(session_id="session-a")

    daemon.handle(9, "What should I do?", game_state, conversation)

    assert observed["path"] is None
    assert "Live data is enough." in (tmp_path / "answer.txt").read_text(
        encoding="utf-8"
    )


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
