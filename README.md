# Rupture Companion

Rupture Companion is an English in-game AI chat for **StarRupture**. Press
**F10**, ask a question, and the companion captures the current screen and
returns concise advice inside the game.

It is a port of the companion workflow from Project Zomboid to
[AlienX's StarRupture Mod Loader](https://github.com/AlienXAXS/StarRupture-ModLoader).
The plugin is read-only: it observes the screenshot and session type but never
changes inventory, buildings, characters, or saves.

## Features

- Native ImGui chat panel loaded by StarRupture Mod Loader
- F10 toggle, Enter-to-send, retry, cancel, and confirmed new-chat reset
- Screenshot-aware answers through the logged-in Claude Code CLI
- Six-turn conversational context and persistent `/web on` / `/web off` mode
- Selective web research with validated StarRupture, Creepy Jar, Steam, GitHub,
  and community-wiki sources
- Atomic local bridge between the Proton game plugin and the Linux daemon
- Automatic DLL updates through the Mod Loader and automatic backend updates
  through the launcher
- Current and legacy release channels, including interface v47 for Mod Loader
  v1.15.x installations such as the local RuptureHUD setup

## Requirements

- Linux with Python 3.12+, [`uv`](https://docs.astral.sh/uv/), `curl`, `tar`,
  and `util-linux` (`flock` and `setsid`)
- KDE Plasma with Spectacle
- Steam/Proton and StarRupture
- [StarRupture Mod Loader](https://github.com/AlienXAXS/StarRupture-ModLoader)
- A logged-in `claude` CLI supporting `--json-schema` and
  `--output-format json`

## Install

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

## Updates

`install-plugin.sh` enables `[AutoUpdate] Enabled=1` in `ModLoader/modloader.ini`
and installs a sidecar manifest for the detected interface channel. On game
startup, the Mod Loader replaces the plugin DLL when a newer GitHub release is
available.

Before starting the game, `run-with-companion.sh` downloads the matching latest
backend archive into `${XDG_DATA_HOME:-~/.local/share}/rupture-companion/`.
If GitHub is unavailable, the last valid downloaded backend—or the checked-out
copy—is used. Set `RC_AUTO_UPDATE=0` to disable backend updates, or
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

Web-derived answers are shown in the transcript but are not added to later
conversation context.

## Architecture

The Windows plugin writes a complete request to
`StarRupture/Binaries/Win64/RuptureCompanion/question.txt`. The Linux daemon
captures a temporary screenshot, invokes Claude in safe mode, and atomically
publishes `answer.txt`. Both files use a sequence number, chat session ID, and
final marker so neither side consumes partial data.

Claude can read only the current screenshot. Bash, file editing, and game
mutation tools are not available. Web sources are restricted by domain and
validated before they reach the transcript.

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

- Companion daemon:
  `${XDG_STATE_HOME:-~/.local/state}/rupture-companion/daemon.log`
- Mod Loader:
  `StarRupture/Binaries/Win64/ModLoader/Logs/ModLoader.log`
- Plugin config:
  `StarRupture/Binaries/Win64/ModLoader/Plugins/config/RuptureCompanion.ini`

If F10 does nothing, confirm the DLL appears in the Mod Loader log and that its
reported interface matches the loader range. If the panel reports a timeout,
check the daemon log and verify `claude --version` and `spectacle --version`.

