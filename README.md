# carmaker-mcp

<!-- mcp-name: io.github.De-Ioannidis/carmaker-mcp -->

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent drive **IPG CarMaker** on your
computer: start a session, run test runs, read the results, change vehicle data and Simulink controller
parameters, and undo what it changed.

> Unofficial community project, not affiliated with or endorsed by IPG Automotive or MathWorks. You need your
> own CarMaker and MATLAB/Simulink licences. This repository contains no IPG or MathWorks code or data.

**Status: alpha.** Developed and tested on one setup (CarMaker 14.1.1, MATLAB R2024b, Windows 11).
[docs/verification.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/verification.md) lists
what has been checked against real software and what has not.

**Contents:** [What it does](#what-it-does) · [What it does not cover](#what-it-does-not-cover) ·
[Requirements](#requirements) · [Setup](#setup) ·
[With the MATLAB MCP server](#using-it-with-the-matlab-mcp-server) · [Settings](#settings) · [Tools](#tools) ·
[Safety and privacy](#safety-and-privacy) · [Troubleshooting](#troubleshooting) ·
[How it works](#how-it-works)

## What it does

Things you can ask an agent once the server is connected:

* "Run the braking test run and tell me the stopping distance."
* "Set `Kp_yaw` to 1.5 in the controller model, run the slalom again and compare the yaw rate."
* "Try the lane change with a vehicle mass of 280, 300 and 320 kg and tabulate the lateral acceleration."
* "The last run aborted. Why?"
* "Undo everything you changed."

It works in two ways, and the agent picks the one that fits the vehicle:

| | MATLAB-connected session | Standalone run |
|---|---|---|
| Use it for | vehicles whose controller is a Simulink model (CarMaker for Simulink) | vehicles with CarMaker's built-in controllers |
| Needs | MATLAB with your model, and the CarMaker GUI (the server can start both) | only CarMaker; no MATLAB, no window |

More worked examples:
[docs/examples.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/examples.md).

## What it does not cover

The server was written for vehicle-dynamics and controller work in a Formula Student team, where every run was
either a standalone run or a CarMaker for Simulink run. CarMaker can do much more than that, and the rest is
outside what this server does or has been tried with.

**Not supported** (there are no tools for it):

* **Other platforms and products:** Linux; TruckMaker and MotorcycleMaker; CarMaker HIL and CarMaker Office
  Extended (real-time hardware, Fail Safe Tester, bus interfaces such as CAN, FlexRay, SOME/IP, XCP).
* **CarMaker's own test automation:** Test Manager and test series, test reports, parallel (HPC) execution,
  batch mode with start-up files. The server runs its own parameter studies instead, one run after another.
* **Building scenarios:** the Scenario Editor and roads, traffic, sensors, the environment, OpenSCENARIO import.
  `cm_edit` changes keys in files that exist; it does not author these things.
* **Most visualisation:** Movie NX, IPGControl, Instruments, video export, Model Check. Of IPGMovie, only single
  pictures.
* **Other result formats:** only `.erg` files are read, not MDF or ASCII.
* **Rebuilding the simulation program:** models in C code or generated with Simulink Coder.
* **Remote machines:** CarMaker and MATLAB must run on the computer the server runs on.

**May work, never tested:**

* FMUs, CarMaker exported as an FMU, third-party tyre models and other co-simulation tools.
* Encrypted or protected data files.
* CarMaker and MATLAB versions other than the ones named above; more than one MATLAB session or CarMaker GUI
  at a time.
* Several standalone runs in parallel, pausing and resuming a standalone run, parameter studies on standalone
  runs.
* Test runs with traffic, sensors or driver-assistance functions. Nothing stands in their way, but every check
  so far used vehicle-dynamics runs without them.

The state of each tool is in
[docs/verification.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/verification.md). Reports
from other setups are welcome.

## Requirements

* Windows 10 or 11
* IPG CarMaker with its Python API (developed on 14.1.1)
* For the MATLAB-connected session: CarMaker for Simulink and a MATLAB release your CarMaker supports
* [uv](https://docs.astral.sh/uv/) (it fetches the server and its Python environment; pip also works, see
  [docs/setup.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/setup.md))
* An MCP client: Claude Code, Claude Desktop, VS Code with GitHub Copilot, Codex, Antigravity, Cursor, ...

## Setup

### 1. Check your machine

```bash
uvx carmaker-mcp doctor
```

This finds your CarMaker and MATLAB installs, tells you what does not fit and how to fix it, and prints the
`uvx` arguments for your machine. They look like this:

```text
uvx --python 3.12 --with matlabengine==24.2.* carmaker-mcp
```

The two extra arguments matter, which is why the tool works them out for you:

* `--python 3.12` selects a Python version that your CarMaker ships its Python API for (CarMaker 14.1: up to
  3.12). Without it `uvx` may pick a newer Python that CarMaker cannot work with.
* `--with "matlabengine==24.2.*"` adds the MATLAB engine package for **your** MATLAB release (R2023b `23.2.*`,
  R2024a `24.1.*`, R2024b `24.2.*`, R2025a `25.1.*`). Leave it out if you only use standalone runs.

To look at the tools without CarMaker installed: `uvx carmaker-mcp --mock`.

### 2. Register the server in your MCP client

Every client needs the same three things: the command `uvx`, the arguments from step 1, and your settings as
environment variables. The two settings you will normally set:

* `CM_PROJECT`: your CarMaker project folder.
* `CM_MATLAB_INIT`: if you normally start work by running a MATLAB script of your project (one that adds
  folders to the path and opens the model), name it here so that the agent runs it too.
* `CM_MODEL`: the Simulink model the agent opens when it starts a session. A name on the MATLAB path, or a
  path relative to the project's `src_cm4sl` folder. If you leave it out, the agent is shown the models it
  finds and picks one or asks you.

Pick your client below. The examples assume MATLAB R2024b and Python 3.12; replace the arguments with the ones
`doctor` printed. Or let the server write the entry for you, with every `CM_` variable set in your shell
filled in:

```bash
uvx carmaker-mcp config --client vscode     # or claude-code, claude-desktop, codex, antigravity, cursor
```

<details>
<summary><b>Claude Code</b></summary>

Run in a terminal:

```bash
claude mcp add carmaker --env CM_PROJECT="C:\CM_Projects\my-project" --env CM_MODEL="MyModel" -- uvx --python 3.12 --with "matlabengine==24.2.*" carmaker-mcp
```

Add further settings with more `--env NAME="value"` before the `--`. With `--scope project` the entry goes
into a `.mcp.json` in the current folder, where you can edit its `env` block later; otherwise remove and add
it again (`claude mcp remove carmaker`). `claude mcp list` shows whether the server connects.

</details>

<details>
<summary><b>Claude Desktop</b></summary>

**One-click bundle.** Download the `.mcpb` file for your MATLAB release from the
[latest release](https://github.com/De-Ioannidis/carmaker-mcp/releases/latest), double-click it and click
**Install**. Fill in the project folder and the model in the dialog; **Settings > Extensions > Configure**
changes them later.

**By hand.** **Settings > Developer > Edit Config** opens `claude_desktop_config.json`. Add the entry and
restart Claude Desktop:

```json
{
  "mcpServers": {
    "carmaker": {
      "command": "uvx",
      "args": ["--python", "3.12", "--with", "matlabengine==24.2.*", "carmaker-mcp"],
      "env": {
        "CM_PROJECT": "C:\\CM_Projects\\my-project",
        "CM_MODEL": "MyModel"
      }
    }
  }
}
```

</details>

<details>
<summary><b>GitHub Copilot in Visual Studio Code</b></summary>

Create `.vscode/mcp.json` in your workspace (for all workspaces: **MCP: Open User Configuration** in the
Command Palette):

```json
{
  "servers": {
    "carmaker": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--python", "3.12", "--with", "matlabengine==24.2.*", "carmaker-mcp"],
      "env": {
        "CM_PROJECT": "C:\\CM_Projects\\my-project",
        "CM_MODEL": "MyModel"
      }
    }
  }
}
```

Click **Start** above the entry, then use Copilot Chat in agent mode.

</details>

<details>
<summary><b>Codex</b></summary>

Run in a terminal:

```bash
codex mcp add carmaker --env CM_PROJECT="C:\CM_Projects\my-project" --env CM_MODEL="MyModel" -- uvx --python 3.12 --with "matlabengine==24.2.*" carmaker-mcp
```

or edit `C:\Users\<username>\.codex\config.toml`:

```toml
[mcp_servers.carmaker]
command = "uvx"
args = ["--python", "3.12", "--with", "matlabengine==24.2.*", "carmaker-mcp"]
env_vars = ["WINDIR"]

[mcp_servers.carmaker.env]
CM_PROJECT = "C:\\CM_Projects\\my-project"
CM_MODEL = "MyModel"
```

Codex starts servers with a reduced environment; `env_vars = ["WINDIR"]` passes on a Windows variable that
MATLAB's libraries need.

</details>

<details>
<summary><b>Antigravity</b></summary>

In the agent panel open the **...** menu, then **MCP Servers > Manage MCP Servers > View raw config**. Add the
entry to the `mcp_config.json` that opens, save, and press **Refresh** in the server list:

```json
{
  "mcpServers": {
    "carmaker": {
      "command": "uvx",
      "args": ["--python", "3.12", "--with", "matlabengine==24.2.*", "carmaker-mcp"],
      "env": {
        "CM_PROJECT": "C:\\CM_Projects\\my-project",
        "CM_MODEL": "MyModel"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Cursor and other clients</b></summary>

Cursor: create `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for all projects) with the same
content as shown for Antigravity. Most other clients use that `mcpServers` format too.

</details>

In JSON and TOML, write Windows paths with double backslashes or with forward slashes. So far only Claude Code
has been used with this server; the other entries follow each client's documented format.

### 3. Start a session

Ask the agent, for example "start CarMaker and run the braking test run". It opens MATLAB, your model and the
CarMaker GUI as far as they are not open yet, and never closes anything.

If you would rather open MATLAB and CarMaker yourself, run this once per MATLAB session (or put it into
`startup.m`) so that the server can attach:

```matlab
matlab.engine.shareEngine('cm_mcp')
```

Standalone runs need neither.

## Using it with the MATLAB MCP server

For CarMaker for Simulink work it is worth connecting MathWorks'
[MATLAB MCP Core Server](https://github.com/matlab/matlab-mcp-core-server) as well. It is optional, and
standalone runs do not need it. The two servers do different jobs:

| Server | Use it for |
|---|---|
| carmaker-mcp | The CarMaker side: session, test runs, run control, results and project data, and controller parameters with a change log and undo |
| MATLAB MCP server | The MATLAB side: writing, checking and running MATLAB code (post-processing scripts, plots) and looking into the Simulink model |

Things to know when you use both:

* Let both work on the same MATLAB session (set the MATLAB server up to use your open MATLAB, see its README),
  so that they see the same model and workspace. This is how the pair was used during development.
* MATLAB does one thing at a time. While a model compiles or a simulation runs, calls through the MATLAB server
  wait. This server's run tools are built around that (time-limited waits that are simply repeated).
* Start and stop runs with this server's tools, so that it can follow the run (result file, errors, storage
  mode).
* What is changed through the MATLAB server is not in this server's change log, and `cm_revert_all` does not
  undo it.

## Settings

All settings are environment variables in the client entry, set like `CM_PROJECT` and `CM_MODEL` above.

**Common**

| Variable | Default | Meaning |
|---|---|---|
| `CM_PROJECT` | read from the running CarMaker GUI | CarMaker project folder. The server writes nowhere else. Needed to start a session from scratch |
| `CM_MODEL` | none | Simulink model to open when a session is started |
| `CM_MATLAB_INIT` | none | Your project's own MATLAB setup script (in `src_cm4sl`, or a full path), run once when a session is started. Use it if you normally run a script that adds folders to the path and opens the model: without it the model may not compile |
| `CM_POPUP_TIMEOUT` | not set | Seconds after which CarMaker pop-ups answer themselves with their default choice, so that a question cannot block a run. Not set: they wait for your click. See [Safety](#safety-and-privacy) |
| `CM_DISABLE` | none | Tool groups to switch off, to give the model a shorter tool list: `standalone`, `study`, `matlab`, `movie` |
| `CM_ENABLE` | none | Optional tools to switch on: `tcl` (raw Tcl in the CarMaker GUI), `experimental` (add and delete Simulink blocks) |

**Advanced**

| Variable | Default | Meaning |
|---|---|---|
| `CM_HOME` | newest `C:\IPG\carmaker\win64-*` | CarMaker install folder |
| `CM_MATLAB_EXE` | newest installed MATLAB that your CarMaker supports | MATLAB executable used to start a session |
| `CM_MATLAB_DIR` | `<project>/src_cm4sl` | Folder MATLAB starts in (where `cmenv.m` is) |
| `CM_MATLAB_SESSION` | `cm_mcp` | Name under which MATLAB shares its engine |
| `CM_RESULT_DIRS` | none | Extra folders to search for result files |
| `CM_STATE_DIR` | `%LOCALAPPDATA%\carmaker-mcp` | Where backups, the change log and `server.log` are kept (never inside the project) |
| `CM_ENGINE_TIMEOUT` | `30` | Seconds before a call into MATLAB is given up |
| `CM_LOG_LEVEL` | `INFO` | Detail of `server.log` |

## Tools

Each tool tells the client whether it is read-only, changes state, or is destructive, so that clients can ask
for confirmation where it matters. Every parameter is described in
[docs/tools.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/tools.md).

**Session and runs**

| Tool | What it does |
|---|---|
| `cm_session_start` | Start MATLAB, the model and the CarMaker GUI as far as they are missing |
| `cm_doctor` | Check the setup and say how to fix what is wrong |
| `cm_status` | Simulation state, active model, project folder |
| `cm_load_testrun` | Load a test run into the CarMaker GUI |
| `cm_start_sim`, `cm_stop_sim` | Start and stop the simulation; a run writes a result file by default |
| `cm_wait_end` | Wait for the end of the run; returns end status, simulation time, distance and the result file |
| `cm_live` | Read quantities while the simulation runs |
| `cm_dva_write`, `cm_dva_release` | Overwrite a quantity during a run (Direct Variable Access) |
| `cm_log` | Read CarMaker's session log, for example after an aborted run |
| `cm_popups`, `cm_popup_timeout` | See what the CarMaker GUI asked or reported, and let pop-ups answer themselves |

**Results**

| Tool | What it does |
|---|---|
| `cm_results_list` | Newest result files |
| `cm_results_summary` | First, last, min, max and mean of quantities in a result file; search for quantity names |
| `cm_results_read` | Time series from a result file |
| `cm_output_quantities`, `cm_output_quantities_edit` | See which quantities runs write to result files, and add or remove some |
| `cm_movie_open`, `cm_movie_snapshot` | Open IPGMovie and get a picture of the 3D view: any moment of the last run, or of a result file |

**Project data and parameters**

| Tool | What it does |
|---|---|
| `cm_list`, `cm_read` | List and read test runs, vehicles, drivers, tyres and other project files |
| `cm_edit`, `cm_clone` | Change keys in a project file (only the edited lines change), or copy a file |
| `cm_list_workspace_vars`, `cm_get_workspace_var`, `cm_set_workspace_var` | MATLAB base workspace and Simulink model workspace, where controller parameters usually are |
| `cm_model_get`, `cm_model_set`, `cm_model_save` | Simulink block and model parameters; save the model |
| `cm_model_logs_save` | After a run, save what Simulink logged (logged signals, To Workspace blocks) and both workspaces to a MAT file. `save_logs` on `cm_start_sim` and `cm_study_start` does it automatically for every run |
| `cm_study_start`, `cm_study_status`, `cm_study_cancel` | Run one test run with several parameter sets and tabulate the results, without changing any file |

**Standalone runs (no MATLAB)**

| Tool | What it does |
|---|---|
| `cm_standalone_launch` | Start an independent CarMaker process and run a test run on it |
| `cm_standalone_status`, `cm_standalone_wait_end`, `cm_standalone_results` | Follow the run and get its result files |
| `cm_standalone_control`, `cm_standalone_dva_write` | Pause, resume or stop; overwrite a quantity |
| `cm_standalone_servers`, `cm_standalone_attach` | Find and attach to CarMaker programs that are already running |
| `cm_standalone_close` | Stop the process |

**History**

| Tool | What it does |
|---|---|
| `cm_changelog` | What this session changed, with old and new values |
| `cm_revert_all` | Undo this session's changes |
| `cm_restore` | Restore the files of an earlier session from its backups |

The server also provides resources (`carmaker://guide`, `carmaker://status`, `carmaker://changelog`,
`carmaker://log`) and three prompts (`run_and_summarise`, `compare_settings`, `undo_session`).

## Safety and privacy

This server can start simulations and **edit your project files and the live Simulink model in place**. Connect
it only to agents you trust.

What protects your work:

* Every file is backed up before its first change, every change is logged with its old value, and
  `cm_revert_all` undoes a session.
* Files are only written inside the project folder, and only while the simulation is idle. No tool deletes
  project files.
* Raw Tcl and Simulink structure edits are off unless you switch them on with `CM_ENABLE`.
* CarMaker pop-ups wait for your click by default. If you set `CM_POPUP_TIMEOUT`, a question such as "Vehicle
  not saved. All changes will be lost. OK to continue?" is answered with its default, which discards unsaved
  changes in the CarMaker GUI.

Runs write result and log files into the project's `SimOutput` folder; a revert does not remove those.

The server collects no telemetry and makes no network connections of its own. It only talks to MATLAB and
CarMaker on your machine. Details:
[docs/safety.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/safety.md).

## Troubleshooting

Run `uvx carmaker-mcp doctor` first: it names most setup problems together with their fix.

| Symptom | Cause and fix |
|---|---|
| "No shared MATLAB session" | MATLAB is closed or has not shared its engine. Let the agent start the session, or run `matlab.engine.shareEngine('cm_mcp')` in MATLAB |
| `uvx` fails while installing `matlabengine` | The pin must match your MATLAB release, and that MATLAB must be installed. Use the arguments `doctor` prints |
| "CarMaker ships no cmapi for Python 3.x" | Add `--python 3.12` (or another version the message lists) |
| A tool call hangs at load or start | The CarMaker GUI is showing a question. Answer it, or see `CM_POPUP_TIMEOUT` |
| "too many licenses in use" on a standalone run | An open MATLAB with CarMaker for Simulink holds the licence. Close that model or MATLAB |
| The wait tool returns `finished: false` | Not an error: waits are limited to 45 s per call and are simply repeated |

More: [docs/troubleshooting.md](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/troubleshooting.md).
The server's own log is `server.log` in the state folder.

## How it works

* **MATLAB-connected session:** the server attaches to your MATLAB through the MATLAB Engine for Python and
  sends commands to the CarMaker GUI through CarMaker for Simulink's own `cmguicmd` (the GUI's Tcl / ScriptControl
  interface).
* **Standalone runs:** it uses IPG's Python API `cmapi`, loaded from your CarMaker install, to start and
  control a separate CarMaker process.
* **Project files** are edited by a byte-preserving editor for CarMaker's infofile format, and **results** are
  read by a NumPy reader for `.erg` files (checked against MATLAB's `cmread`).

Nothing of IPG's or MathWorks' is bundled or redistributed.

## Documentation and contributing

* [Setup in detail](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/setup.md) (pip, updating,
  the single-file executable)
* [Example sessions](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/examples.md)
* [Tools reference](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/tools.md)
* [What is verified](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/docs/verification.md)
* [Contributing](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/CONTRIBUTING.md): test reports from
  other CarMaker and MATLAB versions are especially welcome
* [Security policy](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/SECURITY.md)

Related work: [pycarmaker](https://github.com/gmnvh/pycarmaker) talks to the command port of a standalone
CarMaker program. This project also covers CarMaker for Simulink, uses IPG's own interfaces, and shares no
code with it.

## Licence

MIT, see [LICENSE](https://github.com/De-Ioannidis/carmaker-mcp/blob/main/LICENSE).
