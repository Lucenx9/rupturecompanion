import os
import re
import sys
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
MAX_HISTORY_TURNS = ai_backend.HISTORY_TURNS
POLL_SECONDS = 0.25
STEAM_APP_ID = "1631270"
STEAM_ROOTS = (
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
)


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


def acquire_lock() -> TextIO:
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
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def parse_request(text: str) -> tuple[int, str, str, str] | None:
    lines = text.splitlines()
    if len(lines) < 2 or lines[-1] != END_MARKER:
        return None
    fields = lines[0].split("|", 3)
    if len(fields) != 4 or fields[0] != "v1":
        return None
    _, sequence, session_id, question = fields
    if not session_id.strip() or not question.strip():
        return None
    try:
        parsed_sequence = int(sequence)
    except ValueError:
        return None
    if parsed_sequence < 0:
        return None
    context = "\n".join(lines[1:-1]).strip()
    return parsed_sequence, session_id.strip(), question.strip(), context


def _safe_answer(text: str) -> str:
    return "\n".join(
        f"[{END_MARKER}]" if line == END_MARKER else line
        for line in text.strip().splitlines()
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
    return text.splitlines() == [f"v1|{sequence}|{session_id}", END_MARKER]


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
        return (bridge_dir() / "question.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def read_answer_identity() -> tuple[int, str] | None:
    try:
        header = (
            (bridge_dir() / "answer.txt").read_text(encoding="utf-8").splitlines()[0]
        )
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


def main() -> None:
    bridge = bridge_dir()
    try:
        migrated = plugin_updater.sync_plugin(bridge)
        if migrated:
            print(f"Rupture Companion plugin migrated to {migrated}")
    except (OSError, plugin_updater.PluginUpdateError) as error:
        print(f"Rupture Companion plugin migration skipped: {error}", file=sys.stderr)
    try:
        lock = acquire_lock()
    except DaemonAlreadyRunning as error:
        print(f"Rupture Companion: {error}", file=sys.stderr)
        raise SystemExit(1) from error
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
    try:
        while True:
            request = parse_request(read_request())
            if (
                request is not None
                and (request[0], request[1]) != seen_request
                and process_request(request, conversation)
            ):
                seen_request = (request[0], request[1])
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        lock.close()


if __name__ == "__main__":
    main()
