# Verification

What has been checked against real software, and what has not. Unit tests (mock backend, fake MATLAB
engine, fake `cmapi`) cover the logic of everything listed here; this page is only about **live**
checks with CarMaker and MATLAB.

**Tested environment (the only one):** Windows 11, CarMaker 14.1.1 with CarMaker for Simulink, MATLAB R2024b,
Python 3.12, `matlabengine` 24.2.2, FastMCP 4.0. One machine. Other CarMaker or MATLAB versions are untested.

Status words: **verified** (observed working live), **partly** (some of it observed, the rest listed),
**not yet** (code and unit tests exist, never run live).

## MATLAB-connected session (CarMaker for Simulink)

| Area | Status | What was observed | Still open |
|---|---|---|---|
| Attach to an open MATLAB (`shareEngine`), GUI Tcl, state queries | verified | `connect_matlab` works; `GetSimStatus` answers in about 0.07 s; `cmcmd` state queries work | |
| `cm_status` extras (`save_mode`, `gui_all_saved`) | partly | Read from the real GUI (`collect`, all saved). **Found:** `gui_all_saved` stayed true with an unsaved change in an open vehicle editor, although the GUI then asked about it at the next load; the field is CarMaker's `GUI allsaved` and is documented as no guarantee | A case in which it turns false |
| `cm_load_testrun` | verified | Loads in about 1.5 s without deadlock; a missing test run comes back as an error with the pop-up text (CarMaker answers `failed` with Tcl status 0) | `cancel` and `incomplete` answers; `force` |
| `cm_start_sim`, `cm_wait_end` | verified | Runs of a 29 s test run with a Simulink controller (wall time about 10 s after a 9 to 35 s start): the storage mode was switched to "Save all" and put back by `cm_revert_all`; the run completed and `cm_wait_end` returned end status, simulation time, distance and the result file, which the reader opened (2881 rows); a wait with a time limit returned `finished: false` with live values, and the next one the result. Status polls were answered during the run (0 failed) Progress: a client connected over stdio received three progress notifications with the simulation time during a 113 s test run (about 25 s wall time) **Found:** after a restart of the server without an undo the GUI's storage mode stayed on "Save all"; the server now puts the GUI settings it changed back when it exits (unit test only) | Whether a given client displays them; the first one comes after about 5 s, so short runs send none |
| `cm_stop_sim` | verified | Stopped mid-run: idle after 1.5 s, end status `aborted`, partial result file written | |
| Repeated load / start / stop cycles | verified | Three load / start / stop cycles in a row, about 15 s each, no hang; later a further ten runs in the same session | |
| `cm_live` | verified | Values changing during a run (time and speed) | |
| `cm_dva_write`, `cm_dva_release` | verified | Throttle 0 and brake 1 written during a run: the live values showed them and the car stopped; after the release the driver took over again | |
| `cm_log` | verified | Reads the real session log; the `SIM_END` line is parsed (time, distance) | Error lines of a failing run |
| Pop-ups (`cm_popups`, `cm_popup_timeout`) | verified | With an unsaved change in the open vehicle editor and a time-out of 3 s, a load without `force` raised "Vehicle not saved. All changes will be lost. OK to continue?", which answered itself with its first choice; the load went through and the tool returned the message (type `warn`, answer 0). Earlier: an error pop-up closed itself after 2 s and its text came back in the tool error. `cm_revert_all` put the time-out back to -1 | Reading a pop-up **while** it waits for a click |
| `cm_edit` picked up by the next run | verified | A test run was cloned, the time limit of its manoeuvre was edited from 100 s to 8 s, and the next run ended at 8.00 s instead of 28.8 s. `cm_revert_all` then moved the clone to the trash folder and left no change in the project's data | A vehicle file edit |
| Workspace and Simulink parameter edits with revert | verified | `tests/live/test_live_scratch.py` (5 tests, scratch model): model-workspace and base-workspace set / revert including variables created by the edit, `set_param` revert, `cm_model_save` refused outside the project | On a production model (deliberately only a scratch model so far) |
| Structure edits (`cm_model_add_block` ...) | partly, experimental | `add_block` and revert by reload on the scratch model | Off by default (`CM_ENABLE=experimental`) |
| `cm_session_start` | verified | Through a real client with MATLAB and CarMaker closed: the first call started a MATLAB desktop in the project's `src_cm4sl` folder and returned "waiting for MATLAB" after 45 s; a later call found the engine shared, `cmenv` done and the model loaded, opened the CarMaker GUI with `CM_Simulink` and returned ready; `cm_status` was connected and idle. With everything open it changes nothing. Found and fixed: while a large model was still opening, a call failed with MATLAB's own time-out error instead of "not ready yet" Later checks: with MATLAB open and the GUI closed it reopened only the GUI and listed the loaded model and the model and script files it found; with `init` it ran the project's own setup script (10 s). **Found:** a model opened without that script did not compile (folders missing from the MATLAB path), the Simulink error was invisible, and the start waited for its whole time-out; hence `init` / `CM_MATLAB_INIT`, the Simulink error in failed starts and runs, and the early failure. Also found: MATLAB's command-window output reached the server's stdout (the MCP channel); it is now captured The slow path, with everything closed and a wait of 15 to 20 s per call: "waiting for MATLAB" twice, then "waiting for MATLAB to finish" the setup script, then ready; `cm_status` connected and idle, and runs worked afterwards | A model so large that opening it alone outlasts a call |
| Simulink errors (`simulink_error`) | verified | A model made uncompilable on purpose (a text in a numeric workspace variable): `cm_start_sim` failed after 5.5 s with Simulink's own message naming the block and the parameter. The GUI goes through "simulink initialization" before it falls back to idle with end status `failed`, so the start now waits through that phase. **Found:** `sllasterror` returned an error that a block of the test model raises and handles during every initialisation, instead of the real one; the errors are now read from a log of the Diagnostic Viewer (`sldiagviewer.diary`), which holds only the real one and can be read while MATLAB is busy. The log is switched off after the run (checked). A normal run afterwards gave the same result as before | An error during a run (not at the start); a MATLAB set to another language |
| `cm_doctor`, `carmaker-mcp doctor / config` | verified | All checks correct on the test machine, including picking R2024b although a newer, unsupported MATLAB is installed | A machine where something is wrong |
| Parameter studies in the session (`cm_study_*`) | verified | Three variations in 60 s: a controller flag in the model workspace on and off, and a vehicle key set in memory with `KeyValue`. All three completed with different results (28.8 s, 29.8 s, 30.4 s), so both kinds of change reach the simulation. Afterwards the workspace value was restored, no `KeyValue` was left, and a plain run reproduced the first result exactly Cancel during the second of four runs: that run was stopped (`aborted` at 23 s), the other two were not started, the workspace value was back at once, and other changes were refused while the study ran | |
| Simulink logs to a MAT file (`cm_model_logs_save`) | verified | After a 29 s run with a controller model that marks 101 signals for logging: one call wrote a 21 MB file next to the result file in about 3 s. Read back in MATLAB: a Dataset with 101 elements at full rate (28809 samples at 1 kHz), the base workspace (8 variables) and the model workspace (93, including lookup-table objects) as one struct each, and the run's name. The plain-struct format gave 107 entries (the Data Inspector counts the channels of vector signals one by one). An existing file and a path outside the project were refused. Nothing stayed on the MATLAB path or in the base workspace. On a scratch model a To Workspace block appeared in the Data Inspector's run without any extra setting (R2024b) **Automatic saving** (`save_logs`): a study of two runs with a controller flag on and off wrote one file per run next to its result file, each with that run's own value of the flag in the model workspace (1 and 0) and its own Data Inspector run; a single run started with `save_logs` returned `logs_file` from `cm_wait_end` | A model that uses To Workspace blocks in a CarMaker run; buses; runs long enough to need a larger file format; another MATLAB release |
| Output quantities (`cm_output_quantities`, `cm_output_quantities_edit`) | verified | Read from the real project (165 entries, three rates). 52 names added, among them one that does not exist: the next run stored the real ones (236 instead of 185 quantities in the result file) and left the unknown one out without an error. `cm_revert_all` restored the file byte for byte | A GUI that uses another quantities file; rates other than normal |
| IPGMovie (`cm_movie_open`, `cm_movie_snapshot`) | verified | Opened from the GUI. After a run: pictures at 5 s, 20 s and the end, with three camera views; a time beyond the run gave a clear error after 15 s. From a result file: moving car at two times once the motion quantities were stored. **Found:** a picture during a run makes IPGMovie stop recording that run; export options are remembered, so a time without data blocked later exports (the time is now always given, exports run asynchronously); `Movie settime` does not move an export; a result file without the vehicle's motion quantities replays a parked car and raises a warning window that neither `PopupCtrl` nor `Movie status` shows (such files are now refused beforehand) **During a run:** the user confirmed that IPGMovie's window freezes after an export; no command (view unfreeze, attach, timing, camera) brings it back and an export from a second window freezes it too. Pictures during a run are now read from the window through the Windows API: three in one run, one of them after a camera change, all different, IPGMovie following the run throughout, and afterwards every moment of the run was still available. The image arrived and was displayed in a client (Claude Code) | Movie NX; traffic and sensors; a window on a second monitor or a scaled display |
| `cm_revert_all` twice | verified after a fix | **Found:** a second revert restored a cloned and edited file from its backup, so a removed scratch file came back. Files the session created are no longer backed up, and a revert does not restore a file twice | |

## Standalone instances (no MATLAB)

| Area | Status | What was observed | Still open |
|---|---|---|---|
| Launch, run, live values, timed stop, results, close | verified | `tests/live/test_live_standalone.py`: the stock CarMaker program starts in about 5 s as its own process; 15 s of simulation, error flag 0, 1503 rows in the result file, no process left behind; 10x real time honoured. Again through the MCP tools of a real client, with MATLAB and the CarMaker GUI closed: a 29.5 s run at real time with the vehicle replaced by an in-memory override; `cm_standalone_status` showed live values changing during the run (0, then 18.5 s into it), `cm_standalone_wait_end` returned `finished: false` after its time limit and `finished: true` on the next call, the result file had 2953 rows, and closing left no process | |
| Launch without a free licence | verified | With the licence held by the CarMaker for Simulink engine of an open MATLAB, the launched program stopped with "too many licenses in use". Found and fixed: the launch reported an unreadable connection error and left the stopped process behind; it now quotes the program's log and stops the process | The fixed path against the real program |
| `cm_standalone_servers` | verified | Lists the running CarMaker program with pid and description | |
| `cm_standalone_attach` | partly | A read-only attach to a running program returned live values | Starting a run through a program that a CarMaker Office window started |
| `cm_standalone_control` (pause / resume), `cm_standalone_dva_write` | not yet | | |
| Parameter studies, standalone mode | not yet | | |
| A visible CarMaker window for a launched instance | not possible so far | Starting a second CarMaker Office from the command line exited without a window while another one had the project open | Open CarMaker Office by hand and attach |

Pitfalls found on the way: a test run without an end condition runs until something breaks (hence
`stop_after_s`); a test run that refers to a missing driver template aborts at start (`overrides` can patch
that in memory); a project's own compiled `CarMaker.win64.exe` may belong to an older CarMaker release than
the install, so the stock executable is the default.

## Data handling

| Area | Status | What was observed |
|---|---|---|
| Result reader (`.erg`) | verified | A real 69.5 s run (6952 rows, 163 columns): row count, first, last, min, max and mean of three quantities agree with MATLAB's `cmread` to 9 significant digits |
| Infofile editor | verified | 539 files of a real project: 330 text infofiles parse and re-serialise byte-identically; a single-key edit changed exactly one line in all 330; 209 other files (binary roads etc.) are detected and skipped |

## Packaging

| Area | Status | What was observed | Still open |
|---|---|---|---|
| Wheel through `uvx` | verified | Built wheel run with `uvx --python 3.12 --with "matlabengine==24.2.*"` against the real session: handshake, tools, MATLAB engine and `cmapi` calls Version 0.1.0 installed from PyPI with `uvx carmaker-mcp@latest`; the same from TestPyPI passed the setup check against the real session | Python 3.11 |
| Single-file executable | partly | PyInstaller build, about 39 MB, handshake about 2 s; read-only tools against the real session | Starting a run and edits through it; a second machine |
| One-click bundle (`.mcpb`) | verified | Manifest validated and packed by the official `mcpb` tool. Installed in Claude Desktop from the file: the extension folder was unpacked, Claude Desktop built its Python environment with uv, and the settings dialog stored the project folder, model, setup script and session name. The installed copy, started with the command and environment from its manifest as the host does, answered after 22 s with 46 tools; its setup check passed all eight points and it read the project's data After the extension was switched on, Claude Desktop's own log shows it starting the server, the handshake and the lists of tools, prompts and resources answered within 11 s A conversation in Claude Desktop then asked for the setup check: the log shows two tool calls through the extension (`cm_doctor`, `cm_status`), both answered in under a second | Starting a run and editing through the extension; a second machine |
| Registry entry (`server.json`) | verified | Published by the release workflow; the registry lists `io.github.De-Ioannidis/carmaker-mcp` 0.1.0 | Installing from a client's registry browser |
| FastMCP versions | unit tests only | The unit tests pass on FastMCP 2.14, 3 (in CI) and 4.0 | |
| GitHub workflows (CI, release) | verified | CI ran on GitHub: unit tests on Windows and Linux with Python 3.10 and 3.12, the type check, the tests against FastMCP 2.14 and 3, and the package build, all green. **Found by the first run:** on Python 3.10 a standalone wait that timed out was reported as an error (its time-out exceptions are separate classes there), and one test depended on the resolution of file times The release workflow ran for `v0.1.0`: build, upload to PyPI, registry entry and a draft release with two bundles, after a rehearsal on TestPyPI | |
| A second machine or user | not yet | | Planned with a teammate on the same versions |

## Run the live checks yourself

The live tests are opt-in because they act on your open session:

```bash
# scratch model only, no simulation
CM_LIVE=1 pytest -m live tests/live/test_live_scratch.py
# starts simulations in the open GUI (use a run of 20 s or more)
CM_LIVE=1 CM_LIVE_SIM=1 CM_TESTRUN="Examples/..." pytest -m live tests/live/test_live_sim.py -s
# starts an independent CarMaker process
CM_LIVE=1 CM_LIVE_STANDALONE=1 CM_PROJECT=... CM_TESTRUN="Examples/..." pytest -m live tests/live/test_live_standalone.py -s
# starts MATLAB and the CarMaker GUI as far as they are missing
CM_LIVE=1 CM_LIVE_COLDSTART=1 CM_PROJECT=... pytest -m live tests/live/test_live_coldstart.py -s
```

Each test prints what it saw; that output is what the tables above are filled from.
