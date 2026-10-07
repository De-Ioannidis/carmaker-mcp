# Setup

## Requirements

| | |
|---|---|
| Operating system | Windows 10 or 11 |
| CarMaker | a release with CarMaker for Simulink and the Python API (`cmapi`); developed on 14.1.1 |
| MATLAB / Simulink | a release your CarMaker version supports (CarMaker 14.1.1: up to R2024b); only needed for the MATLAB-connected session |
| Python | one of the versions your CarMaker ships `cmapi` for, 3.10 or newer (CarMaker 14.1: 3.10, 3.11, 3.12) |
| Tooling | [uv](https://docs.astral.sh/uv/) (recommended) or pip |

Only the listed development setup is tested, see [verification.md](verification.md).

## 1. Check the machine

```bash
uvx carmaker-mcp doctor
```

It looks for the CarMaker install, the Python versions it supports, the installed MATLAB releases and the one
your CarMaker supports, the matching `matlabengine` version, a shared MATLAB session, the CarMaker GUI and the
project folder. Every failed check comes with its fix, and the last lines give the `uvx` arguments for this
machine. Run it again with those arguments to check the environment the server will really run in:

```bash
uvx --python 3.12 --with "matlabengine==24.2.*" carmaker-mcp doctor
```

`matlabengine` can only be installed where the matching MATLAB release is installed.

## 2. Register the server

```bash
uvx carmaker-mcp config --client claude-code     # or claude-desktop, vscode, codex, antigravity, cursor
```

prints the registration with the detected values. Set `CM_PROJECT` to your CarMaker project folder before
running it, or edit the printed entry. Where the entry goes:

| Client | Where |
|---|---|
| Claude Code | run the printed `claude mcp add ...` command, or put the JSON into a project `.mcp.json` |
| Claude Desktop | Settings, Developer, Edit Config (`claude_desktop_config.json`), under `mcpServers` |
| VS Code | `.vscode/mcp.json`, under `servers` |
| Codex | `codex mcp add ...`, or `~/.codex/config.toml` under `[mcp_servers.carmaker]` |
| Antigravity | agent panel, MCP Servers, Manage MCP Servers, View raw config (`mcp_config.json`), under `mcpServers` |
| Cursor | `.cursor/mcp.json`, under `mcpServers` |

All settings are environment variables in the entry's `env` block; the README lists them.

## 3. Start a session

Either let the agent do it, or do it yourself.

**By the agent.** `cm_session_start` brings up what is missing: a MATLAB desktop started in
`<project>/src_cm4sl` (it runs `cmenv` and shares its engine), the Simulink model named in `CM_MODEL` or in
the call, and the CarMaker GUI. It never closes or restarts anything. MATLAB can take a minute to start; the
tool returns `ready=false` with what it is waiting for and is simply called again.

Which model? CarMaker does not record which Simulink model belongs to a test run or vehicle, so the server
cannot look it up. If neither the call nor `CM_MODEL` names one, `cm_session_start` opens none and returns
the models that are already loaded and the model files it finds under the start folder (most recently
changed first), so that the agent can pick the obvious one or ask you. Set `CM_MODEL` to skip that step.

**By hand.** Open MATLAB, change to your project's `src_cm4sl`, open the model and the CarMaker GUI as you
always do, then once per MATLAB session:

```matlab
matlab.engine.shareEngine('cm_mcp')
```

Put that line into `startup.m` to make it permanent.

Standalone runs (`cm_standalone_*`) need neither MATLAB nor an open GUI.

## Updating and pinning

`uvx` caches the environment. To move to the newest release run `uvx carmaker-mcp@latest --version` once; to
stay on one release write `carmaker-mcp==0.1.0` in the `args`.

## Fewer tools

Each tool description costs the model some context. If you never use a group, switch it off:
`CM_DISABLE=standalone,study` (also `matlab`, `movie`). Raw Tcl and the experimental Simulink structure tools are off
unless you set `CM_ENABLE=tcl,experimental`.

## Without uv

```bash
py -3.12 -m venv .venv
.venv\Scripts\pip install carmaker-mcp "matlabengine==24.2.*"
.venv\Scripts\carmaker-mcp doctor
```

and register `"command": "C:\\path\\to\\.venv\\Scripts\\carmaker-mcp.exe"` without `args`.

## One-click bundle (Claude Desktop)

Releases carry `.mcpb` bundles. Download the one for your MATLAB release (for example
`carmaker-mcp-<version>-R2024b-py312.mcpb`), open it with Claude Desktop (Settings, Extensions) and fill in
the project folder. The host creates the Python environment itself; MATLAB of that release must be
installed, because the MATLAB engine package is built against it. The bundle without MATLAB
(`...-standalone-...`) only offers standalone runs.

## Single-file executable

For a machine without Python, `packaging\build_exe.ps1` builds one `carmaker-mcp.exe` (about 40 MB) with
PyInstaller. It is tied to the Python version it was built with (your CarMaker must ship `cmapi` for it) and
to the MATLAB release and install folder of the build machine, so build it per setup.
