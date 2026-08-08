# Rupture Companion

Rupture Companion is an in-game AI chat for **StarRupture**. Press **F10**, ask
a question in your preferred language, and the companion captures the current
screen and returns concise advice in that same language inside the game.

It is a port of the companion workflow from Project Zomboid to
[AlienX's StarRupture Mod Loader](https://github.com/AlienXAXS/StarRupture-ModLoader).
The plugin is read-only: it observes the screenshot and session type but never
changes inventory, buildings, characters, or saves.

## Features

- Native ImGui chat panel loaded by StarRupture Mod Loader
- F10 toggle, Enter-to-send, retry, cancel, and confirmed new-chat reset
- Screenshot-aware answers through the logged-in Claude Code CLI
- Automatic response-language matching for each player message
- Six-turn conversational context and persistent `/web on` / `/web off` mode
- Selective web research with validated StarRupture, Creepy Jar, Steam, GitHub,
  and community-wiki sources
- Compact source pills that show site names without exposing full URLs
- Atomic local bridge between the game plugin and the Linux or Windows daemon
- Automatic DLL updates through the Mod Loader and automatic backend updates
  through the launcher
- Current and legacy release channels, including interface v47 for Mod Loader
  v1.15.x installations such as the local RuptureHUD setup

## Requirements

- Windows 10/11, or Linux with Steam/Proton and `util-linux` (`flock` and
  `setsid`)
- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- Spectacle on Linux; Windows screenshots use Pillow automatically
- Steam and StarRupture
- [StarRupture Mod Loader](https://github.com/AlienXAXS/StarRupture-ModLoader)
- A logged-in `claude` CLI supporting `--json-schema` and
  `--output-format json`

## Install on Linux

Clone the repository and create its Python environment:

```bash
git clone https://github.com/Lucenx9/rupturecompanion.git
cd rupturecompanion
uv sync --group dev
```

Install the plugin. The default matches the local Steam installation; pass a
different StarRupture directory as the first argument when needed.

```bash
./install-plugin.sh
```

The installer reads the Mod Loader log and chooses the compatible release:

- interface 47 → `RuptureCompanion-Legacy.dll`
- interface 60 → `RuptureCompanion-Client.dll`

It installs the DLL next to existing plugins such as `MapExtension_Plugin.dll`;
it does not replace or modify RuptureHUD.

Set the following Steam launch option, using the absolute path printed by the
installer:

```text
WINEDLLOVERRIDES="dwmapi=n,b" /absolute/path/run-with-companion.sh %command%
```

Launch StarRupture and press **F10**. The key can be changed in the Mod Loader
configuration screen.

## Install on Windows

Install Git, Python 3.12+, `uv`, and the Claude Code CLI, then open PowerShell:

```powershell
git clone https://github.com/Lucenx9/rupturecompanion.git
cd rupturecompanion
uv sync --group dev
powershell -ExecutionPolicy Bypass -File .\install-plugin.ps1
```

The installer discovers the Steam library and compatible loader interface. If
the game is in a nonstandard location, pass it explicitly:

```powershell
.\install-plugin.ps1 -GameRoot "D:\SteamLibrary\steamapps\common\StarRupture"
```

Copy the Steam launch option printed by the installer. It uses
`run-with-companion.cmd`, so no Proton override is needed on Windows. Launch the
game and press **F10**.

## Updates

`install-plugin.sh` enables `[AutoUpdate] Enabled=1` in `ModLoader/modloader.ini`
and installs a sidecar manifest for the detected interface channel. On game
startup, the Mod Loader replaces the plugin DLL when a newer GitHub release is
available.

Before starting the game, the platform launcher downloads the matching latest
backend archive into `${XDG_DATA_HOME:-~/.local/share}/rupture-companion/` on
Linux or `%LOCALAPPDATA%\RuptureCompanion` on Windows.
The backend also compares the newest Mod Loader log with the installed plugin
channel before the game starts. If a Mod Loader update changes its plugin
interface, the launcher switches both the DLL and sidecar manifest to the
compatible release channel. A failed sidecar commit rolls the DLL back; an
interrupted rollback keeps its recovery copy for the next launch. Incomplete
migrations are detected and retried before the game starts. The first launch
after a loader update may be needed to record its new interface; the next
launch performs the migration. Current launchers distinguish daemon liveness
from migration readiness and wait up to two minutes. When an independently
updated backend is started by an older launcher, migration gets a short grace
period and then defers without blocking the game; a later launch retries it.
If GitHub is unavailable, the last valid downloaded backend—or the checked-out
copy—is used. Its locked production environment is synchronized automatically,
so backend dependency changes are included in updates. Set `RC_AUTO_UPDATE=0`
to disable backend updates, or
`RC_ENABLE_AUTO_UPDATE=0` while running the installer to leave the Mod Loader's
global setting unchanged.

## Web mode

Web tools are available by default but used only for uncertain or current facts
such as patches, recipes, ratios, and mods. Immediate questions about the
screenshot stay local unless research is explicitly requested.

- `/web off` keeps the current chat offline.
- `/web on` re-enables selective research.
- “search online” forces research for one question.
- “answer without web” opts out for one question.

Web-derived answers show validated sources as compact site-name pills in the
transcript; full URLs are not displayed. These answers are not added to later
conversation context.

## Architecture

The native plugin writes a complete request to
`StarRupture/Binaries/Win64/RuptureCompanion/question.txt`. The Linux daemon
captures a temporary screenshot, invokes Claude in safe mode, and atomically
publishes `answer.txt`. Both files use a sequence number, chat session ID, and
final marker so neither side consumes partial data.

Claude can read only the current screenshot. Bash, file editing, and game
mutation tools are not available. Web sources are restricted by domain and
validated before they reach the transcript. The backend then appends a versioned
source-metadata block containing only the localized heading and approved site
labels when the native plugin advertises support, and the plugin renders those
labels as pills. Older plugin versions receive a readable site-name list instead,
so backend and DLL updates do not need to finish at exactly the same time.

## Development

Run the local checks:

```bash
uv run --group dev pytest -q
uv run --group dev ruff check .
uv run --group dev ruff format --check .
uv run --group dev mypy
shellcheck run-with-companion.sh install-plugin.sh
```

GitHub Actions compiles the C++20 plugin with Visual Studio against both the
current Plugin SDK and the legacy v47 header. A push to `main` creates a release
containing both DLL channels, their updater manifests and ZIPs, and the backend
archive.

The generated Plugin SDK interface header is vendored under `include/` for
reproducible local development; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Logs and troubleshooting

- Companion daemon on Linux:
  `${XDG_STATE_HOME:-~/.local/state}/rupture-companion/daemon.log`
- Companion daemon on Windows: `%LOCALAPPDATA%\RuptureCompanion\daemon.log`
  and `daemon-error.log`
- Mod Loader:
  `StarRupture/Binaries/Win64/ModLoader/Logs/ModLoader.log`
- Plugin config:
  `StarRupture/Binaries/Win64/ModLoader/Plugins/config/RuptureCompanion.ini`

If F10 does nothing, confirm the DLL appears in the Mod Loader log and that its
reported interface matches the loader range. If the panel reports a timeout,
check the daemon log and verify `claude --version`; on Linux also verify
`spectacle --version`.
