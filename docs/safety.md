# Safety

## What the server can do
* Start MATLAB and the CarMaker GUI (`cm_session_start`); it never closes or restarts them.
* Start and stop simulations in the open CarMaker GUI, and switch the GUI's storage mode so that runs write
  result files.
* Overwrite keys in project data files (test runs, vehicles, drivers, tyres...) in place, and change the list
  of quantities that runs store (`Data/Config/OutputQuantities`).
* Write a MAT file with Simulink's logged data and the workspaces into the project (`cm_model_logs_save`;
  it never overwrites unless asked). For that one call the server puts a folder of its own with one
  MATLAB function on the MATLAB path and removes it again.
* Open IPGMovie, change its camera view and let it replay a result file. During a run it reads the pixels of
  IPGMovie's window through the Windows API (of that window only) and restores the window if it is minimised. Pictures are written to
  `<CM_STATE_DIR>/movie` (the newest 20 are kept). IPGMovie itself writes `MISSING_QUANTITIES.txt` into the
  project folder when a result file lacks quantities it expects.
* Change variables in the MATLAB base workspace and in a Simulink model's workspace, change block parameters
  and save the model file.
* Start independent CarMaker processes (`cm_standalone_launch`) that use a CarMaker licence.
* Make GUI pop-ups answer themselves with their default choice (`cm_popup_timeout`), which for a question like
  "Vehicle not saved. All changes will be lost. OK to continue?" discards unsaved data in the GUI.
* Only when enabled with `CM_ENABLE`: run Tcl in the CarMaker GUI (`tcl`), add or delete Simulink blocks and
  lines (`experimental`).

Runs write result and log files into the project's `SimOutput` folder. From a start to the end of the run the server lets Simulink log its Diagnostic Viewer messages to a file in `<CM_STATE_DIR>` (`sldiagviewer.diary`), to be able to report why a model did not compile; if you use `sldiagviewer.diary` yourself, a start switches your log off.

Anyone who can call these tools can change your project. Only connect the server to agents and clients you trust.

## Safeguards
1. **Backup before first write.** Before a file is modified for the first time in a server session, it is copied to
   `<CM_STATE_DIR>/backups/<session>/<project-relative path>`. Models are backed up before `cm_model_save`.
2. **Change log.** Every change is appended to `<CM_STATE_DIR>/changes.jsonl` with old and new value
   (`cm_changelog` reads it). This includes GUI settings the server changed (storage mode, pop-up timeout).
3. **Revert.** `cm_revert_all` replays this session's workspace, parameter and structure changes backwards, restores
   backed-up files and puts the GUI settings back. Files created by `cm_clone` are moved to
   `<CM_STATE_DIR>/trash/`, never deleted. `cm_restore(session)` restores files from an earlier session's backups.
4. **Write confinement.** File writes must resolve inside the project directory (symlinks and `..` are resolved first),
   never inside `.git` or the state directory, and `cm_edit` / `cm_clone` stay inside the matching `Data/<Kind>`
   folder. `cm_model_save` refuses model files outside the project.
5. **Idle only, one at a time.** Loading, editing, saving and reverting require the simulation to be idle, and
   state-changing calls are serialised. While a parameter study runs, other state-changing calls are refused.
6. **No deletion tools.** There is no tool that deletes project files.
7. **Nothing permanent in studies.** Parameter studies apply infofile keys in memory and restore workspace values
   after every run.
8. **Client hints.** Every tool is marked read-only, state-changing or destructive, so that MCP clients can ask
   for confirmation where it matters.
9. **Raw Tcl is off by default.** When enabled, `cm_gui_tcl` rejects `exit`, `exec`, `cd`, `open`, `source`,
   `socket`, `rm`, `file delete|rename|copy|mkdir|attributes` and `interp`. **This deny-list is a speed bump
   against accidents, not a sandbox**: Tcl is dynamic and can be obfuscated. Calls are logged.

## Things to know
* Pop-ups wait for a click unless you set a timeout. That is deliberate: the default answer of a "not saved"
  question throws the unsaved data away.
* Standalone launches have a default safety stop (`stop_after_s`, 600 s of simulation time). Processes launched by the
  server are stopped by `cm_standalone_close` and when the server exits; attached processes are only disconnected.
* Result and log files created by runs are not tracked or reverted by `cm_revert_all`.
* Model-workspace edits live in memory until the model is saved; the model shows as modified. `cm_revert_all` restores
  the values without touching the file.
* Structural edits are experimental. Revert closes the model without saving and reloads it from disk, which also
  discards any other unsaved edits to that model.
* If the project is under git, in-place edits show up in `git status`; commit or `git checkout` as you see fit.
* A crash can leave a model half-edited in memory, or a study's workspace values not restored; the backups and
  `changes.jsonl` are the recovery path.
* The server makes no network connections of its own and collects no telemetry.
