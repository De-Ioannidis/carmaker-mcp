# Troubleshooting

Start with `carmaker-mcp doctor` (or the `cm_doctor` tool): it names most setup problems and their fix. The
server's own log is `server.log` in the state folder (`%LOCALAPPDATA%\carmaker-mcp` unless `CM_STATE_DIR` is
set); set `CM_LOG_LEVEL=DEBUG` for more.

## Setup

**"No shared MATLAB session named 'cm_mcp'"**
MATLAB is not running or has not shared its engine. Call `cm_session_start`, or run
`matlab.engine.shareEngine('cm_mcp')` in MATLAB. Sharing ends when MATLAB closes.

**"matlabengine is not installed"**, or `uvx` fails while building `matlabengine`
The engine package must have the version of your MATLAB release and can only be installed where that MATLAB is
installed. Use the pin that `carmaker-mcp doctor` prints, for example `--with "matlabengine==24.2.*"` for
R2024b. If several MATLAB releases are installed, the pin must match the one CarMaker uses.

**"CarMaker ships no cmapi for Python 3.x"**
`uvx` chose a Python your CarMaker has no Python API for. Add `--python 3.12` (or another version the message
lists).

**`cm_status` says not connected although MATLAB is shared**
The CarMaker GUI is not open or not connected to this MATLAB. Open it from the Simulink model, or call
`cm_session_start`.

**MATLAB takes long to answer, calls time out**
Raise `CM_ENGINE_TIMEOUT`. While a simulation compiles or runs, MATLAB can be busy; status polls that fail for
that reason are counted (`poll_errors`), not treated as errors.

## Running

**The tool call hangs at load or start, and the GUI shows a question**
The GUI asks before it discards unsaved data ("Vehicle not saved. All changes will be lost. OK to continue?")
and waits for a click. Answer it, or let such pop-ups answer themselves: `cm_popup_timeout(5)` or
`CM_POPUP_TIMEOUT=5`. The default answer is the first button, which here discards the unsaved data.
`cm_load_testrun(force=true)` skips the question. `cm_status` reports `gui_all_saved: false` for unsaved data
the GUI knows of, but not for an edit in an editor window that is still open: there the question comes
although the status said everything was saved.

**"the simulation did not start"** or **"simulation did not leave 'idle'"**
The error already quotes Simulink's error, new pop-ups and log errors. Typical causes: the Simulink model does
not compile (often because the project's setup script was not run: see `CM_MATLAB_INIT`), a file the test run
refers to does not exist, no licence. `cm_log(level="error")` shows more. A model that is still compiling
after `start_timeout_s` is not an error: `cm_start_sim` returns with a note and `cm_wait_end` goes on waiting.

**`cm_wait_end` returns `finished: false`**
That is not an error: the wait is limited (45 s by default) so that no tool call outlives the client's
time-out. Call it again until `finished` is true.

**No `result_file`**
The GUI's storage mode was "collect only". `cm_start_sim` switches it to "Save all" unless called with
`save="keep"` or `save="collect"`. Which quantities are stored is decided by the project's `OutputQuantities`
file (`cm_read("config", "OutputQuantities")`).

**"Unable to obtain CarMaker license: too many licenses in use"** when launching a standalone run
Another CarMaker program holds the licence. Typically that is the CarMaker for Simulink engine inside an open
MATLAB: it keeps the licence after its first simulation, also when the CarMaker GUI window is closed. Close
that model or MATLAB, or run the test in the MATLAB-connected session. `cm_standalone_servers` lists the
CarMaker programs that are running.

**"IPGMovie wrote no picture"**
IPGMovie only holds what it recorded. It must be open before the run starts (`cm_movie_open`), and a picture
exported with `time_s` while a run is going on ends its recording of that run. For any other run, replay the
result file (`erg=...`).

**IPGMovie's window stands still during a run**
IPGMovie stops following a run as soon as it is told to export a picture, and nothing brings it back before
the next start. `cm_movie_snapshot` therefore reads the window itself during a run and leaves IPGMovie
alone; it only exports, and says so in its result, when `time_s` is given during a run or when the window
cannot be read (`capture_error`). A minimised IPGMovie window is restored for the picture.

**"cannot be replayed in IPGMovie"**, or an IPGMovie warning about expected quantities
The result file does not hold what IPGMovie animates the scene with (for the vehicle: `Vhcl.Fr1.x`, `Vhcl.Yaw`
and so on). Add them with `cm_output_quantities_edit(preset="movie")` and run again. The server refuses such
a file before IPGMovie can raise its warning; if only other objects (traffic, for example) are incomplete,
IPGMovie still warns, and the tool result lists what is missing. That warning is IPGMovie's own window: it
does not show up in `cm_popups`.

**A standalone run never ends**
The test run has no end condition. `cm_standalone_launch` stops at `stop_after_s` of simulation time
(600 s by default); `cm_standalone_control("stop")` stops it now.

**A standalone run aborts at once**
`cm_standalone_wait_end` returns the log errors. Often the test run names a driver or tyre file that does not
exist; `overrides` can point it to an existing one without editing the file. Vehicles whose controller is a
Simulink model cannot run standalone.

## Editing

**"cannot ... while the simulation state is 'running'"**
Edits, loads and reverts need an idle simulation. Wait for the end or call `cm_stop_sim`.

**"... is outside the project root"**
Writes are confined to the project folder (and to the right `Data/<Kind>` folder). This is intended.

**A workspace change did not survive a MATLAB restart**
Model-workspace changes live in memory until `cm_model_save`.

**I want everything back as it was**
`cm_changelog` lists the changes, `cm_revert_all` undoes this session's, `cm_restore(session)` restores the
files of an earlier session. Backups are in the state folder under `backups/<session>/`.

## Reporting a problem

Open an issue with the output of `carmaker-mcp doctor`, the versions of CarMaker and MATLAB, the tool call and
its error, and the relevant lines of `server.log`. Remove project names and paths you do not want to publish.
