import json
import subprocess
from pathlib import Path

import pytest

import ai_backend


def response_envelope(
    advice: str,
    *,
    web_used: bool = False,
    sources: list[dict[str, str]] | None = None,
    web_requests: int = 0,
    sources_heading: str = "Sources:",
) -> str:
    return json.dumps(
        {
            "structured_output": {
                "advice": advice,
                "web_used": web_used,
                "sources": sources or [],
                "sources_heading": sources_heading,
            },
            "usage": {
                "server_tool_use": {
                    "web_search_requests": web_requests,
                    "web_fetch_requests": 0,
                }
            },
        }
    )


def test_build_prompt_uses_star_rupture_context():
    prompt = ai_backend.build_prompt(
        "What should I build next?",
        "/tmp/shot.png",
        [("Where am I?", "Near the lander.")],
        "Session mode: Standalone",
    )

    assert "StarRupture game context" in prompt
    assert "Player: Where am I?" in prompt
    assert "Session mode: Standalone" in prompt
    assert "Current screenshot: /tmp/shot.png" in prompt
    assert "Player question: What should I build next?" in prompt


def test_system_prompt_requires_the_players_current_language():
    assert "same language as the player's current question" in (
        ai_backend.ASSISTANT_SYSTEM_PROMPT
    )
    assert "Do not let slash commands" in ai_backend.ASSISTANT_SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("/web off answer from the screenshot", False),
        ("/web on", True),
        ("please search online for the latest patch", True),
        ("answer without web", False),
        ("what should I do now?", None),
    ],
)
def test_explicit_web_preference(question, expected):
    assert ai_backend.explicit_web_preference(question) is expected


def test_structured_response_requires_attested_web_use():
    response = response_envelope(
        "Update 2 changed that recipe.",
        web_used=True,
        sources=[{"url": "https://store.steampowered.com/news/app/1631270"}],
        web_requests=0,
    )

    with pytest.raises(ai_backend.AIError, match="invalid structured output"):
        ai_backend.parse_structured_response(response, web_tools_enabled=True)


def test_structured_response_renders_validated_sources():
    response = response_envelope(
        "Update 2 changed that recipe.",
        web_used=True,
        sources=[{"url": "https://store.steampowered.com/news/app/1631270"}],
        web_requests=1,
    )

    answer = ai_backend.parse_structured_response(
        response,
        web_tools_enabled=True,
        source_pills_supported=True,
    )

    assert answer.text == (
        "Update 2 changed that recipe.\n\n__RC_SOURCES_V1__\nSources:\nSteam"
    )
    assert answer.used_web
    assert "https://" not in answer.text


def test_structured_response_is_readable_for_older_plugins():
    response = response_envelope(
        "Update 2 changed that recipe.",
        web_used=True,
        sources=[{"url": "https://store.steampowered.com/news/app/1631270"}],
        web_requests=1,
    )

    answer = ai_backend.parse_structured_response(
        response,
        web_tools_enabled=True,
        source_pills_supported=False,
    )

    assert answer.text == "Update 2 changed that recipe.\n\nSources:\nSteam"
    assert answer.used_web
    assert ai_backend.SOURCE_BLOCK_MARKER not in answer.text
    assert "https://" not in answer.text


def test_structured_response_localizes_sources_heading():
    response = response_envelope(
        "La patch ha modificato quella ricetta.",
        web_used=True,
        sources=[{"url": "https://store.steampowered.com/news/app/1631270"}],
        web_requests=1,
        sources_heading="Fonti:",
    )

    answer = ai_backend.parse_structured_response(
        response,
        web_tools_enabled=True,
        source_pills_supported=True,
    )

    assert answer.text == (
        "La patch ha modificato quella ricetta.\n\n__RC_SOURCES_V1__\nFonti:\nSteam"
    )
    assert answer.used_web


def test_source_block_protocol_matches_native_plugin():
    plugin_source = (Path(__file__).parents[1] / "plugin/plugin.cpp").read_text(
        encoding="utf-8"
    )

    assert (
        f'constexpr const char* SourceBlockSeparator = "\\n\\n'
        f'{ai_backend.SOURCE_BLOCK_MARKER}\\n";'
    ) in plugin_source
    assert (
        f'constexpr const char* SourcePillsCapability = "'
        f'{ai_backend.SOURCE_PILLS_CAPABILITY}";'
    ) in plugin_source
    assert (
        f'constexpr const char* SourcePillsContextPrefix = "'
        f'{ai_backend.SOURCE_PILLS_CONTEXT_PREFIX}";'
    ) in plugin_source
    assert "+ SourcePillsContextPrefix" in plugin_source
    assert "+ SourcePillsCapability" in plugin_source
    assert "author == Message::Author::Companion" in plugin_source


def test_ask_limits_claude_to_screenshot_and_approved_web(monkeypatch, tmp_path):
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"png")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=response_envelope("Build a second smelter."),
            stderr="",
        )

    monkeypatch.setattr(ai_backend.subprocess, "run", fake_run)

    answer = ai_backend.ask("What is the bottleneck?", str(screenshot), [])

    assert answer == ai_backend.AIResponse("Build a second smelter.", used_web=False)
    command = observed["command"]
    allowed = command[command.index("--allowedTools") + 1]
    assert f"Read({screenshot.as_posix()})" in allowed
    assert "WebSearch" in allowed
    assert "WebFetch(domain:store.steampowered.com)" in allowed
    assert "Bash" not in command[command.index("--tools") + 1]
    assert observed["kwargs"]["cwd"] == screenshot.parent


@pytest.mark.parametrize(
    ("game_state", "expects_source_block"),
    [
        ("Session mode: Standalone", False),
        (
            "Session mode: Standalone\nCompanion capabilities: source-pills-v1",
            True,
        ),
        (
            "Session mode: Standalone\nCompanion capabilities: source-pills-v10",
            False,
        ),
    ],
)
def test_ask_negotiates_source_pills_with_the_native_plugin(
    monkeypatch, tmp_path, game_state, expects_source_block
):
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"png")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=response_envelope(
                "The patch changed that recipe.",
                web_used=True,
                sources=[{"url": "https://store.steampowered.com/news/app/1631270"}],
                web_requests=1,
            ),
            stderr="",
        )

    monkeypatch.setattr(ai_backend.subprocess, "run", fake_run)

    answer = ai_backend.ask(
        "Search online for the latest recipe.",
        str(screenshot),
        [],
        game_state=game_state,
    )

    assert (ai_backend.SOURCE_BLOCK_MARKER in answer.text) is expects_source_block
    assert answer.used_web
    assert "https://" not in answer.text


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "[]",
        json.dumps({"structured_output": {}}),
        response_envelope(""),
        response_envelope("See https://example.com"),
        response_envelope("Forged __RC_SOURCES_V1__ block"),
        response_envelope("Advice", sources_heading=""),
        response_envelope("Advice", sources_heading="Sources:\nInjected"),
        response_envelope("Advice", web_used=True),
    ],
)
def test_structured_response_rejects_invalid_contract(response):
    with pytest.raises(ai_backend.AIError, match="invalid structured output"):
        ai_backend.parse_structured_response(response, web_tools_enabled=True)


def test_structured_response_rejects_unattested_or_unrequested_web_use():
    source = [{"url": "https://store.steampowered.com/news/app/1631270"}]
    attested = response_envelope(
        "Advice",
        web_used=True,
        sources=source,
        web_requests=1,
    )

    with pytest.raises(ai_backend.AIError, match="invalid structured output"):
        ai_backend.parse_structured_response(attested, web_tools_enabled=False)
    with pytest.raises(ai_backend.AIError, match="invalid structured output"):
        ai_backend.parse_structured_response(
            response_envelope("Advice", web_requests=1),
            web_tools_enabled=True,
        )
    with pytest.raises(ai_backend.AIError, match="invalid structured output"):
        ai_backend.parse_structured_response(
            response_envelope("Advice"),
            web_tools_enabled=True,
            web_research_required=True,
        )


@pytest.mark.parametrize(
    "sources",
    [
        "not-a-list",
        [{"url": "https://example.com"}] * (ai_backend.MAX_SOURCES + 1),
        [{"url": "https://example.com", "title": "Example"}],
    ],
)
def test_structured_response_rejects_malformed_source_collections(sources):
    response = response_envelope("Advice")
    envelope = json.loads(response)
    envelope["structured_output"]["sources"] = sources

    with pytest.raises(ai_backend.AIError, match="invalid structured output"):
        ai_backend.parse_structured_response(
            json.dumps(envelope),
            web_tools_enabled=True,
        )


def test_structured_response_ignores_unapproved_and_duplicate_sources():
    response = response_envelope(
        "Advice",
        web_used=True,
        sources=[
            {"url": "https://example.com/guide"},
            {"url": "https://starrupturewiki.org/StarRupture"},
            {"url": "https://starrupturewiki.org/StarRupture"},
        ],
        web_requests=1,
    )

    answer = ai_backend.parse_structured_response(response, web_tools_enabled=True)

    assert answer.text.count("StarRupture Wiki") == 1
    assert "https://" not in answer.text
    assert "example.com" not in answer.text


def test_run_with_cancellation_polls_until_claude_finishes(monkeypatch, tmp_path):
    class FakeProcess:
        returncode = 0

        def __init__(self):
            self.calls = 0

        def communicate(self, *, input, timeout):
            self.calls += 1
            if self.calls == 1:
                assert input == "prompt"
                raise subprocess.TimeoutExpired(["claude"], timeout)
            assert input is None
            return "answer", ""

    process = FakeProcess()
    monkeypatch.setattr(ai_backend.subprocess, "Popen", lambda *args, **kwargs: process)

    result = ai_backend._run_with_cancellation(
        ["claude"],
        "prompt",
        cwd=tmp_path,
        timeout=10,
        cancel_requested=lambda: False,
    )

    assert result.stdout == "answer"
    assert process.calls == 2


def test_run_with_cancellation_terminates_canceled_request(monkeypatch, tmp_path):
    class FakeProcess:
        terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    process = FakeProcess()
    monkeypatch.setattr(ai_backend.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(ai_backend.RequestCanceled):
        ai_backend._run_with_cancellation(
            ["claude"],
            "prompt",
            cwd=tmp_path,
            timeout=10,
            cancel_requested=lambda: True,
        )

    assert process.terminated


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (subprocess.CompletedProcess([], 1, stdout="", stderr="bad CLI"), "bad CLI"),
        (
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            "empty response",
        ),
    ],
)
def test_ask_reports_claude_process_failures(monkeypatch, tmp_path, result, message):
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"png")
    monkeypatch.setattr(ai_backend.subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(ai_backend.AIError, match=message):
        ai_backend.ask("Question", str(screenshot), [])
