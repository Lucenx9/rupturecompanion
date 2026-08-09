import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

import ai_backend
import plugin_updater
import screenshot

REQUEST_PREFIX = "v1|"
END_MARKER = "__RC_END__"
LIVE_CONTEXT_MARKER = "__RC_LIVE_CONTEXT_V1__"
MAX_REQUEST_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 56 * 1024
MAX_LIVE_CONTEXT_BYTES = ai_backend.MAX_LIVE_CONTEXT_BYTES
MAX_SESSION_ID_BYTES = 128
MAX_QUESTION_BYTES = 8 * 1024
MAX_HISTORY_TURNS = ai_backend.HISTORY_TURNS
POLL_SECONDS = 0.25
READY_PROTOCOL_VERSION = 1
LEGACY_MIGRATION_GRACE_SECONDS = 5.0
STEAM_APP_ID = "1631270"
STEAM_ROOTS = (
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
)
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class DaemonAlreadyRunning(Exception):
    pass


@dataclass(frozen=True)
class PendingAnswer:
    sequence: int
    session_id: str
    question: str
    text: str
    is_error: bool
    remember: bool


@dataclass
class Conversation:
    session_id: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)
    web_enabled: bool = True
    pending: PendingAnswer | None = None

    def switch_session(self, session_id: str) -> bool:
        if self.session_id == session_id:
            return False
        self.session_id = session_id
        self.history.clear()
        self.web_enabled = True
        self.pending = None
        return True

    def add_turn(self, question: str, answer: str) -> None:
        self.history.append((question, answer))
        self.history = self.history[-MAX_HISTORY_TURNS:]


def _steam_libraries(root: Path) -> list[Path]:
    libraries = [root]
    file = root / "steamapps/libraryfolders.vdf"
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries
    for raw_path in re.findall(r'"path"\s+"([^"]+)"', text):
        path = Path(raw_path.replace("\\\\", "\\"))
        if path not in libraries:
            libraries.append(path)
    return libraries


def _bridge_under_install(install: Path) -> Path:
    return install / "StarRupture/Binaries/Win64/RuptureCompanion"


def bridge_dir() -> Path:
    configured = os.environ.get("RC_BRIDGE_DIR")
    if configured:
        return Path(configured).expanduser()
    install = os.environ.get("STEAM_COMPAT_INSTALL_PATH") or os.environ.get(
        "RC_GAME_DIR"
    )
    if install:
        return _bridge_under_install(Path(install))
    for root in STEAM_ROOTS:
        for library in _steam_libraries(root):
            manifest = library / f"steamapps/appmanifest_{STEAM_APP_ID}.acf"
            if manifest.is_file():
                return _bridge_under_install(library / "steamapps/common/StarRupture")
    return Path.home() / ".local/share/rupture-companion/bridge"


def acquire_lock(identity: str | None = None) -> TextIO:
    directory = bridge_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "daemon.lock").open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write("0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(  # type: ignore[attr-defined]
                lock.fileno(),
                msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                1,
            )
        else:
            fcntl.flock(  # type: ignore[attr-defined]
                lock.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
    except (BlockingIOError, OSError) as error:
        lock.close()
        raise DaemonAlreadyRunning("another daemon is already running") from error
    try:
        (directory / "daemon.ready").unlink(missing_ok=True)
        lock.seek(0)
        lock.truncate()
        lock.write(f"{identity or os.getpid()}\n")
        lock.flush()
    except OSError:
        lock.close()
        raise
    return lock


def _validated_live_context(raw: str) -> str | None:
    return ai_backend.validate_live_context(raw, max_input_bytes=MAX_LIVE_CONTEXT_BYTES)


def _protocol_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def normalize_context(context: str) -> str:
    lines = _protocol_lines(context)
    marker_indexes = [
        index for index, line in enumerate(lines) if line == LIVE_CONTEXT_MARKER
    ]
    if not marker_indexes:
        return ai_backend.normalize_session_metadata(context)
    first_marker = marker_indexes[0]
    metadata = ai_backend.normalize_session_metadata("\n".join(lines[:first_marker]))
    safe_metadata = metadata
    if len(marker_indexes) != 1 or first_marker + 2 != len(lines):
        return safe_metadata
    live_context = _validated_live_context(lines[first_marker + 1])
    if live_context is None:
        return safe_metadata
    normalized = (
        f"{metadata}\n{LIVE_CONTEXT_MARKER}\n{live_context}"
        if metadata
        else f"{LIVE_CONTEXT_MARKER}\n{live_context}"
    )
    return (
        normalized
        if len(normalized.encode("utf-8")) <= MAX_CONTEXT_BYTES
        else safe_metadata
    )


def parse_request(text: str) -> tuple[int, str, str, str] | None:
    lines = _protocol_lines(text)
    if len(lines) < 2 or lines[-1] != END_MARKER:
        return None
    fields = lines[0].split("|", 3)
    if len(fields) != 4 or fields[0] != "v1":
        return None
    _, sequence, session_id, question = fields
    session_id = session_id.strip()
    question = question.strip()
    if (
        not session_id
        or len(session_id.encode("utf-8")) > MAX_SESSION_ID_BYTES
        or SESSION_ID_PATTERN.fullmatch(session_id) is None
        or not question
        or len(question.encode("utf-8")) > MAX_QUESTION_BYTES
    ):
        return None
    try:
        parsed_sequence = int(sequence)
    except ValueError:
        return None
    if parsed_sequence < 0:
        return None
    context = normalize_context("\n".join(lines[1:-1]).strip())
    return parsed_sequence, session_id, question, context


def _safe_answer(text: str) -> str:
    return "\n".join(
        f"[{END_MARKER}]" if line == END_MARKER else line
        for line in _protocol_lines(text.strip())
    )


def write_answer(
    sequence: int, session_id: str, text: str, *, error: bool = False
) -> None:
    directory = bridge_dir()
    directory.mkdir(parents=True, exist_ok=True)
    answer = directory / "answer.txt"
    temporary = directory / "answer.tmp"
    status = "error" if error else "ok"
    content = (
        f"v1|{sequence}|{session_id}|{status}\n{_safe_answer(text)}\n{END_MARKER}\n"
    )
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, answer)
    finally:
        temporary.unlink(missing_ok=True)


def cancellation_requested(sequence: int, session_id: str) -> bool:
    try:
        text = (bridge_dir() / "cancel.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _protocol_lines(text) == [f"v1|{sequence}|{session_id}", END_MARKER]


def handle(
    sequence: int,
    question: str,
    game_state: str,
    conversation: Conversation,
) -> None:
    session_id = conversation.session_id or ""
    pending = conversation.pending
    if pending is None or (pending.sequence, pending.session_id) != (
        sequence,
        session_id,
    ):
        requested_web_mode = ai_backend.web_mode_directive(question)
        if requested_web_mode is not None:
            conversation.web_enabled = requested_web_mode
        try:
            with screenshot.capture_for_analysis() as shot:
                response = ai_backend.ask(
                    question,
                    str(shot),
                    conversation.history,
                    game_state=game_state,
                    web_tools_default=conversation.web_enabled,
                    cancel_requested=lambda: cancellation_requested(
                        sequence, session_id
                    ),
                )
            pending = PendingAnswer(
                sequence,
                session_id,
                question,
                response.text,
                False,
                not response.used_web,
            )
        except (screenshot.ScreenshotError, ai_backend.AIError) as error:
            pending = PendingAnswer(
                sequence, session_id, question, str(error), True, False
            )
        conversation.pending = pending
    write_answer(sequence, session_id, pending.text, error=pending.is_error)
    if pending.remember:
        conversation.add_turn(pending.question, pending.text)
    conversation.pending = None


def read_request() -> str:
    try:
        with (bridge_dir() / "question.txt").open("rb") as request:
            payload = request.read(MAX_REQUEST_BYTES + 1)
        if len(payload) > MAX_REQUEST_BYTES:
            return ""
        return payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return ""


def read_answer_identity() -> tuple[int, str] | None:
    try:
        header = _protocol_lines(
            (bridge_dir() / "answer.txt").read_text(encoding="utf-8")
        )[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    fields = header.split("|")
    if len(fields) != 4 or fields[0] != "v1" or fields[3] not in {"ok", "error"}:
        return None
    try:
        sequence = int(fields[1])
    except ValueError:
        return None
    return sequence, fields[2]


def process_request(
    request: tuple[int, str, str, str], conversation: Conversation
) -> bool:
    sequence, session_id, question, game_state = request
    try:
        if conversation.switch_session(session_id):
            print(f"[{sequence}] new chat {session_id}")
        print(f"[{sequence}] {question}")
        handle(sequence, question, game_state, conversation)
    except OSError as error:
        print(f"[{sequence}] transient error: {error}", file=sys.stderr)
        traceback.print_exc()
        return False
    except Exception as error:
        print(f"[{sequence}] unexpected error: {error}", file=sys.stderr)
        traceback.print_exc()
        try:
            write_answer(
                sequence,
                session_id,
                "Internal daemon error. Please retry.",
                error=True,
            )
        except OSError:
            return False
    return True


def _sync_plugin_for_legacy_launcher(bridge: Path) -> str | None:
    cancel_event = threading.Event()
    commit_lock = threading.Lock()
    results: list[str | None] = []
    errors: list[Exception] = []

    def migrate() -> None:
        try:
            plugin_updater.recover_plugin(
                bridge,
                cancel_event=cancel_event,
                commit_lock=commit_lock,
            )
            if cancel_event.is_set():
                raise plugin_updater.PluginUpdateError("plugin migration deferred")
            results.append(
                plugin_updater.sync_plugin(
                    bridge,
                    cancel_event=cancel_event,
                    commit_lock=commit_lock,
                )
            )
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=migrate, daemon=True)
    worker.start()
    worker.join(LEGACY_MIGRATION_GRACE_SECONDS)
    if worker.is_alive():
        cancel_event.set()
        with commit_lock:
            pass
        print(
            "Rupture Companion plugin migration deferred for legacy launcher",
            file=sys.stderr,
        )
        return None
    if errors:
        raise errors[0]
    return results[0]


def main() -> None:
    bridge = bridge_dir()
    ready = bridge / "daemon.ready"
    ready_nonce = os.environ.get("RC_DAEMON_READY_NONCE", "")
    ready_protocol = os.environ.get("RC_DAEMON_READY_PROTOCOL") == str(
        READY_PROTOCOL_VERSION
    ) and bool(ready_nonce)
    identity = f"{os.getpid()}|{ready_nonce}" if ready_protocol else str(os.getpid())
    lock: TextIO | None = None
    try:
        if ready_protocol:
            lock = acquire_lock(identity)
        try:
            migrated = (
                plugin_updater.sync_plugin(bridge)
                if ready_protocol
                else _sync_plugin_for_legacy_launcher(bridge)
            )
            if migrated:
                print(f"Rupture Companion plugin migrated to {migrated}")
        except (OSError, plugin_updater.PluginUpdateError) as error:
            print(
                f"Rupture Companion plugin migration skipped: {error}",
                file=sys.stderr,
            )
        screenshot.prepare()
        if lock is None:
            lock = acquire_lock(identity)
        if ready_protocol:
            ready.write_text(f"{identity}\n", encoding="utf-8")
        conversation = Conversation()
        initial_request = parse_request(read_request())
        initial_identity = (
            (initial_request[0], initial_request[1])
            if initial_request is not None
            else None
        )
        seen_request = (
            initial_identity if initial_identity == read_answer_identity() else None
        )
        print(f"Rupture Companion daemon watching {bridge_dir()}")
        while True:
            request = parse_request(read_request())
            if (
                request is not None
                and (request[0], request[1]) != seen_request
                and process_request(request, conversation)
            ):
                seen_request = (request[0], request[1])
            time.sleep(POLL_SECONDS)
    except DaemonAlreadyRunning as error:
        print(f"Rupture Companion: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        pass
    finally:
        if lock is not None:
            try:
                if ready_protocol:
                    ready.unlink(missing_ok=True)
            finally:
                lock.close()


if __name__ == "__main__":
    main()
