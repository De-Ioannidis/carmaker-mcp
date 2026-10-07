# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, tool names and
parameters may still change between minor versions.

## [Unreleased]

## [0.1.1] - 2026-10-07

### Fixed
- The setup check (`doctor`, `cm_doctor`, `config`) chose the wrong engine package and Python version for
  MATLAB releases other than R2024b: releases before R2023b have engine versions `9.x`, and each engine
  package installs only up to a certain Python version. A release whose engine needs a Python older than
  3.10 is now reported as such.

### Added
- One-click bundles for MATLAB R2022b, R2023a, R2023b and R2024a next to R2024b (untested).

## [0.1.0] - 2026-10-07

First version. Developed and tested with CarMaker 14.1.1 and MATLAB R2024b on Windows; what has been verified
against real software is listed in `docs/verification.md`.

### Added
- **MATLAB-connected session (CarMaker for Simulink):** `cm_status`, `cm_load_testrun`, `cm_start_sim`,
  `cm_wait_end`, `cm_stop_sim`, `cm_live`, `cm_dva_write`, `cm_dva_release`, through the MATLAB Engine for
  Python and the GUI's Tcl interface.
- **Results by default:** `cm_start_sim` switches the GUI's storage mode to "Save all" (restored by
  `cm_revert_all`); `cm_wait_end` returns the result file, simulation time, distance, new log errors and
  pop-ups, waits at most 45 s per call and reports MCP progress.
- **Diagnostics:** `cm_log` reads CarMaker's session log; `cm_popups` / `cm_popup_timeout` read the GUI's
  pop-ups and let them answer themselves; failed loads and starts quote pop-ups and log errors. A Simulink model
  that does not compile fails `cm_start_sim` at once with Simulink's own error message.
- **Cold start and setup:** `cm_session_start` brings up MATLAB, the model and the CarMaker GUI as far as they
  are missing; `carmaker-mcp doctor` and `carmaker-mcp config` (and `cm_doctor`) check the machine and print
  the client registration.
- **Standalone instances:** `cm_standalone_*` run test runs on independent CarMaker processes through IPG's
  `cmapi`, without MATLAB.
- **Project data:** `cm_list`, `cm_read`, `cm_edit`, `cm_clone` with a byte-preserving infofile editor.
- **MATLAB workspace and Simulink:** `cm_list_workspace_vars`, `cm_get_workspace_var`, `cm_set_workspace_var`,
  `cm_model_get`, `cm_model_set`, `cm_model_save`; `cm_model_logs_save` writes what Simulink logged in a
  run, with the base and the model workspace, to a MAT file; `save_logs` on `cm_start_sim` and
  `cm_study_start` does that for every run.
- **Parameter studies:** `cm_study_start`, `cm_study_status`, `cm_study_cancel` run one test run with several
  sets of in-memory infofile keys and workspace values and tabulate the results.
- **Results:** `cm_results_list`, `cm_results_summary`, `cm_results_read` with a pure-numpy `.erg` reader.
- **Output quantities:** `cm_output_quantities` and `cm_output_quantities_edit` show and change what runs
  write to result files.
- **IPGMovie:** `cm_movie_open` and `cm_movie_snapshot` return a picture of the 3D view at any moment of
  the last run, of a result file, or of the window while a run is going on.
- **History:** backup before the first write of every file, change log, `cm_changelog`, `cm_revert_all`,
  `cm_restore`.
- **MCP surface:** described and typed parameters, a title and read-only / destructive hints per tool, output
  schemas for status / start / wait, resources (`carmaker://guide`, `status`, `changelog`, `log`), three
  prompts, a log file. Raw Tcl and Simulink structure edits are opt-in (`CM_ENABLE`); tool groups can be
  switched off (`CM_DISABLE`).
- **Packaging:** PyPI package for `uvx`; MCP registry entry (`server.json`); one-click bundles for Claude
  Desktop (`packaging/build_mcpb.py`); release workflow with trusted publishing; optional single-file
  executable build (`packaging/build_exe.ps1`).
