import json
import subprocess

import pytest

import ai_backend


def response_envelope(
    advice: str,
    *,
    web_used: bool = False,
    sources: list[dict[str, str]] | None = None,
    web_requests: int = 0,
) -> str:
    return json.dumps(
        {
            "structured_output": {
                "advice": advice,
                "web_used": web_used,
                "sources": sources or [],
            },
            "usage": {
                "server_tool_use": {
                    "web_search_requests": web_requests,
                    "web_fetch_requests": 0,
                }
            },
        }
    )


def test_build_prompt_uses_english_star_rupture_context():
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

    answer = ai_backend.parse_structured_response(response, web_tools_enabled=True)

    assert answer == (
        "Update 2 changed that recipe.\n\n"
        "Sources:\n1. Steam — https://store.steampowered.com/news/app/1631270"
    )


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

    assert ai_backend.ask("What is the bottleneck?", str(screenshot), []) == (
        "Build a second smelter."
    )
    command = observed["command"]
    allowed = command[command.index("--allowedTools") + 1]
    assert f"Read({screenshot.as_posix()})" in allowed
    assert "WebSearch" in allowed
    assert "WebFetch(domain:store.steampowered.com)" in allowed
    assert "Bash" not in command[command.index("--tools") + 1]
    assert observed["kwargs"]["cwd"] == screenshot.parent
