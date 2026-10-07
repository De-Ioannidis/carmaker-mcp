"""FastMCP server: tool, resource and prompt registration. Logic lives in session.py and friends.

No ``from __future__ import annotations`` here: FastMCP reads the tool signatures, and older versions
cannot resolve annotations that are only strings.
"""

import argparse
import asyncio
import base64
import functools
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__, guide
from .backend import BackendError
from .config import Config
from .guard import GuardError
from .infofile import InfoFileError
from .project import ProjectError
from .results import ErgError
from .schemas import StartResult, StatusResult, WaitResult
from .session import Session

log = logging.getLogger("carmaker_mcp")

# ---- shared state ------------------------------------------------------------------
_session: Session | None = None
_standalone = None
_studies = None


def set_session(s: Session | None) -> None:
    global _session, _studies
    _session, _studies = s, None


def S() -> Session:
    global _session
    if _session is None:
        import atexit

        from .backend_cm4sl import Cm4slBackend

        cfg = Config.from_env()
        _session = Session(Cm4slBackend(cfg.matlab_session, cfg.engine_timeout), cfg)
        atexit.register(_session.at_exit)
    return _session


def set_standalone(m) -> None:
    global _standalone
    _standalone = m


def SA():
    """Manager for independent CarMaker instances (created on first use)."""
    global _standalone
    if _standalone is None:
        import atexit

        from .standalone import StandaloneManager

        cfg = S().cfg
        project = cfg.project
        if project is None:
            try:
                project = S().project.root
            except BackendError:
                project = None
        _standalone = StandaloneManager(cfg.cm_home, project)
        atexit.register(_standalone.close_all)
    return _standalone


def ST():
    """Parameter-study runner (created on first use)."""
    global _studies
    if _studies is None:
        from .study import StudyRunner

        _studies = StudyRunner(S(), SA)
    return _studies


# ---- registration ------------------------------------------------------------------
# Hints for MCP clients (permission prompts, auto-approval of read-only calls). Everything here acts
# on the local CarMaker / MATLAB session, hence openWorldHint is false throughout.
# (model_validate with the protocol's own names works with every version of the MCP SDK)
READ = ToolAnnotations.model_validate({"readOnlyHint": True, "openWorldHint": False})
WRITE = ToolAnnotations.model_validate(
    {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
DESTRUCTIVE = ToolAnnotations.model_validate(
    {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False})

# Tool groups that are off unless named in CM_ENABLE (comma separated).
FEATURES = {
    "tcl": "raw Tcl in the CarMaker GUI (cm_gui_tcl)",
    "experimental": "Simulink structure edits (cm_model_add_block, cm_model_add_line, cm_model_delete_block)",
}
# Tool groups that are on unless named in CM_DISABLE: a shorter tool list costs the model less context.
GROUPS = {
    "standalone": "independent CarMaker processes without MATLAB (cm_standalone_*)",
    "study": "parameter studies (cm_study_*)",
    "matlab": "MATLAB workspace and Simulink parameters (cm_*_workspace_*, cm_model_*)",
    "movie": "IPGMovie pictures (cm_movie_*)",
}


def _group(tool: str) -> str | None:
    if tool.startswith("cm_standalone_"):
        return "standalone"
    if tool.startswith("cm_study_"):
        return "study"
    if tool.startswith("cm_movie_"):
        return "movie"
    if tool.startswith("cm_model_") or "_workspace_" in tool:
        return "matlab"
    return None


_DOMAIN_ERRORS = (BackendError, GuardError, ProjectError, InfoFileError, ErgError)


@dataclass
class _Spec:
    fn: Any
    annotations: ToolAnnotations
    title: str
    feature: str | None


_TOOLS: list[_Spec] = []


def _guarded(fn):
    """Turn our domain errors into clean tool errors and log every call."""
    name = fn.__name__

    def done(t0: float, err: Exception | None) -> None:
        ms = (time.perf_counter() - t0) * 1000
        if err is None:
            log.info("%s ok %.0f ms", name, ms)
        else:
            log.warning("%s failed after %.0f ms: %s", name, ms, err)

    @functools.wraps(fn)
    async def async_wrapper(*a, **kw):
        t0 = time.perf_counter()
        try:
            out = await fn(*a, **kw)
        except _DOMAIN_ERRORS as e:
            done(t0, e)
            raise ToolError(str(e)) from e
        done(t0, None)
        return out

    @functools.wraps(fn)
    def sync_wrapper(*a, **kw):
        t0 = time.perf_counter()
        try:
            out = fn(*a, **kw)
        except _DOMAIN_ERRORS as e:
            done(t0, e)
            raise ToolError(str(e)) from e
        done(t0, None)
        return out

    return async_wrapper if inspect.iscoroutinefunction(fn) else sync_wrapper


def _tool(annotations: ToolAnnotations, title: str, feature: str | None = None):
    """Declare a tool. It is registered by create_server (optional groups only when enabled)."""

    def deco(fn):
        _TOOLS.append(_Spec(_guarded(fn), annotations, title, feature))
        return fn

    return deco


def _progress(ctx: Context, total: float):
    """Callback for long waits (called from a worker thread): forwards MCP progress to the client."""
    loop = asyncio.get_running_loop()

    def report(elapsed: float, live: dict) -> None:
        t = live.get("Time")
        msg = f"simulation time {t:.1f} s" if isinstance(t, (int, float)) else "waiting"
        asyncio.run_coroutine_threadsafe(ctx.report_progress(elapsed, total, msg), loop)

    return report


# ---- parameter types ---------------------------------------------------------------
Kind = Annotated[
    Literal["testrun", "vehicle", "driver", "tire", "road", "trailer", "kinematics", "config", "chassis",
            "misc", "sensor", "traffic", "script"],
    Field(description="Kind of project data; selects the folder below <project>/Data"),
]
Scope = Annotated[str, Field(description="'base' for the MATLAB base workspace, or the name of a loaded "
                                         "Simulink model for that model's workspace")]
Instance = Annotated[str | None, Field(description="Standalone instance id such as 'sa1' (may be "
                                                   "omitted while there is only one)")]
ErgPath = Annotated[str, Field(description="Result file (.erg): absolute path, or relative to the project")]
Quantities = Annotated[list[str], Field(description="CarMaker quantity names, e.g. ['Time', 'Car.v']")]
MaxWait = Annotated[float, Field(description="Longest time to wait in seconds; keep it below your "
                                             "client's tool time-out and call again if not finished",
                                 gt=0)]
Overrides = Annotated[dict[str, Any] | None, Field(description="Test-run keys to change in memory only, "
                                                               "e.g. {'Vehicle.DriverTemplate.FName': 'X'}")]


# ---- setup ---------------------------------------------------------------------------
@_tool(WRITE, "Start the CarMaker for Simulink session")
async def cm_session_start(
    model: Annotated[str | None, Field(description="Simulink model to open: a name on the MATLAB path, "
                                                   "a path as listed in models_found, or a full path "
                                                   "(default: CM_MODEL)")] = None,
    max_wait_s: MaxWait = 45.0,
    init: Annotated[str | None, Field(description="The project's own MATLAB setup script, run once per "
                                                  "MATLAB session before the model is opened (default: "
                                                  "CM_MATLAB_INIT). A name from init_scripts_found, or "
                                                  "a full path")] = None,
) -> dict:
    """Bring up a CarMaker for Simulink session as far as it is missing: start MATLAB (visible, in the
    project's src_cm4sl folder, engine shared), run cmenv, open the Simulink model and open the CarMaker
    GUI. Nothing is restarted or closed. Returns ready=true, or ready=false with what it is waiting
    for: call again until ready. Without a model it opens none and lists models_loaded and
    models_found to choose from. Many projects need their own setup script before the model compiles:
    if init_scripts_found lists one, pass it as init."""
    return await asyncio.to_thread(S().session_start, model, max_wait_s, init)


@_tool(READ, "Check the setup")
def cm_doctor() -> dict:
    """Check the setup: CarMaker install, Python version, MATLAB release and engine, shared MATLAB
    session, GUI, project folder. Each failed check comes with its fix."""
    return S().doctor()


# ---- status and run control -----------------------------------------------------------
@_tool(READ, "Simulation status")
def cm_status() -> StatusResult:
    """Simulation state, active Simulink model, project directory, last end status, the GUI's storage
    mode (save_mode) and gui_all_saved (false: the GUI holds unsaved data and will ask before a load;
    true is no guarantee, an edit in an open editor window is not counted)."""
    return S().status()  # type: ignore[return-value]


@_tool(WRITE, "Load a test run")
def cm_load_testrun(
    name: Annotated[str, Field(description="Test run relative to Data/TestRun, e.g. "
                                           "'Examples/BasicFunctions/Driver/BackAndForth'")],
    force: Annotated[bool, Field(description="Do not let the GUI ask about unsaved data; that data is "
                                             "discarded")] = False,
) -> dict:
    """Load a test run into the CarMaker GUI. Requires an idle simulation. Find names with
    cm_list('testrun')."""
    return S().load_testrun(name, force)


@_tool(WRITE, "Start the simulation")
def cm_start_sim(
    start_timeout_s: Annotated[float, Field(description="How long the start may take (a Simulink model "
                                                        "may compile first)", gt=0)] = 90.0,
    save: Annotated[Literal["save", "collect", "keep"],
                    Field(description="save: write a result file (GUI storage mode becomes 'Save all', "
                                      "cm_revert_all puts it back). collect: buffer only. keep: leave "
                                      "the GUI setting alone")] = "save",
    save_logs: Annotated[bool, Field(description="Also save what Simulink logged in this run to a MAT "
                                                 "file next to the result file, as cm_model_logs_save "
                                                 "does; cm_wait_end returns it as logs_file")] = False,
) -> StartResult:
    """Start the loaded test run. Returns once it is running; fails at once, with Simulink's error,
    if the model does not compile. Then call cm_wait_end until it reports finished."""
    return S().start_sim(start_timeout_s, save, save_logs)  # type: ignore[return-value]


@_tool(DESTRUCTIVE, "Stop the simulation")
def cm_stop_sim(
    wait_s: Annotated[float, Field(description="Wait this long for the simulation to become idle "
                                               "(0: do not wait)", ge=0)] = 30.0,
) -> WaitResult:
    """Stop the running simulation. With wait_s > 0 it returns what cm_wait_end returns."""
    return S().stop_sim(wait_s)  # type: ignore[return-value]


@_tool(READ, "Wait for the simulation to end")
async def cm_wait_end(
    ctx: Context,
    timeout_s: MaxWait = 45.0,
    poll_s: Annotated[float, Field(description="Seconds between status polls", gt=0)] = 1.0,
) -> WaitResult:
    """Wait until the simulation is idle again. finished=true: end status, simulation time and
    distance, result_file, new log errors and pop-ups. finished=false: current state and live values;
    call again to keep waiting."""
    return await asyncio.to_thread(S().wait_end, timeout_s, poll_s, _progress(ctx, timeout_s))  # type: ignore[return-value]


@_tool(READ, "Read the CarMaker session log")
def cm_log(
    lines: Annotated[int, Field(description="Number of lines from the end", ge=1, le=500)] = 50,
    level: Annotated[Literal["all", "warning", "error"],
                     Field(description="warning: warnings and errors only; error: errors only")] = "all",
) -> dict:
    """Last lines of the newest CarMaker session log of the project. Use it when a run aborts or does
    not start."""
    return S().log_tail(lines, level)


@_tool(READ, "Read live quantities")
def cm_live(quantities: Quantities) -> dict:
    """Current values of CarMaker quantities during a run (or the last values after it)."""
    return S().backend.quantity_read(quantities)


@_tool(WRITE, "Overwrite a quantity (DVA)")
def cm_dva_write(
    name: Annotated[str, Field(description="Quantity to overwrite, e.g. 'DM.Gas'")],
    value: Annotated[float, Field(description="Value, offset or factor, depending on mode")],
    duration_ms: Annotated[int, Field(description="How long the override lasts in ms (-1: until "
                                                  "released or the run ends)")] = -1,
    mode: Annotated[Literal["Abs", "Off", "Fac", "AbsRamp", "OffRamp", "FacRamp"],
                    Field(description="Absolute value, offset or factor; Ramp variants blend in")] = "Abs",
) -> dict:
    """Overwrite a quantity during a run via Direct Variable Access."""
    S().backend.dva_write(name, value, duration_ms, mode)
    return {"dva_written": name, "value": value}


@_tool(WRITE, "Release DVA overrides")
def cm_dva_release() -> dict:
    """Release all quantities from DVA control."""
    S().backend.dva_release()
    return {"released": True}


@_tool(WRITE, "Read GUI pop-ups")
def cm_popups() -> dict:
    """Pop-up messages the CarMaker GUI raised for this server's commands (type info/warn/err/question,
    text, index of the answer given) and the current pop-up timeout. Reading empties the GUI's buffer
    (it keeps five). cm_load_testrun, cm_start_sim and cm_wait_end already return new ones as 'popups'."""
    return S().popup_status()


@_tool(DESTRUCTIVE, "Let GUI pop-ups answer themselves")
def cm_popup_timeout(
    seconds: Annotated[float, Field(description="Seconds until a pop-up answers itself; 0 = never shown; "
                                                "-1 = wait for a click in the GUI (the normal setting)")],
) -> dict:
    """Let GUI pop-ups raised by this server's commands answer themselves with their DEFAULT choice, so
    that a question such as 'Vehicle not saved. All changes will be lost. OK to continue?' cannot block
    a run. The default answer there discards the unsaved changes. Read what was answered with
    cm_popups. Stays until set back, cm_revert_all or a GUI restart."""
    return S().set_popup_timeout(seconds)


@_tool(DESTRUCTIVE, "Run Tcl in the CarMaker GUI", feature="tcl")
def cm_gui_tcl(
    command: Annotated[str, Field(description="Tcl / ScriptControl command")],
    timeout_ms: Annotated[int, Field(description="Time-out in ms (0: do not wait for the result)",
                                     ge=0)] = 10000,
) -> dict:
    """Run a Tcl / ScriptControl command in the CarMaker GUI. Escape hatch with a small deny-list
    (exit, exec, cd, file delete...). Logged. Prefer the dedicated tools."""
    return S().gui_tcl(command, timeout_ms)


# ---- results --------------------------------------------------------------------------
@_tool(READ, "List result files")
def cm_results_list(
    limit: Annotated[int, Field(description="Maximum number of files", ge=1, le=200)] = 30,
) -> list[dict]:
    """Newest .erg result files under the project's SimOutput folder, CM_RESULT_DIRS and the folders
    this session's runs wrote to."""
    return S().results_list(limit)


@_tool(READ, "Summarise a result file")
def cm_results_summary(
    erg: ErgPath,
    quantities: Annotated[list[str] | None, Field(description="Quantities to summarise (default: Time, "
                                                              "Car.v)")] = None,
    search: Annotated[str | None, Field(description="Instead of a summary, list the quantity names that "
                                                    "contain this text ('PT.') or match this wildcard "
                                                    "pattern ('Car.v*'; '*' lists all)")] = None,
) -> dict:
    """First / last / min / max / mean and unit of quantities in a result file, plus rows and duration."""
    e = S().erg(erg)
    if search:
        hits = e.search(search, None)
        return {"matches": hits[:300], "n_matches": len(hits), "n_quantities": len(e.names)}
    return e.summary(quantities or [n for n in ("Time", "Car.v") if n in e.names])


@_tool(READ, "Read time series from a result file")
def cm_results_read(
    erg: ErgPath,
    quantities: Quantities,
    t_min: Annotated[float | None, Field(description="Start of the time window in s")] = None,
    t_max: Annotated[float | None, Field(description="End of the time window in s")] = None,
    max_points: Annotated[int, Field(description="Samples are thinned to at most this many per quantity",
                                     ge=2, le=5000)] = 500,
) -> dict:
    """Time series from a result file, decimated to at most max_points samples."""
    return S().erg(erg).read(quantities, t_min, t_max, max_points)


@_tool(READ, "Read the output quantities")
def cm_output_quantities() -> dict:
    """The quantities the next run writes to its result file, per storage rate (names may hold
    wildcards), with the sample times. movie_replay says whether such a result file can be replayed in
    IPGMovie. What a finished result file holds: cm_results_summary(erg, search='*')."""
    return S().output_quantities()


@_tool(DESTRUCTIVE, "Change the output quantities")
def cm_output_quantities_edit(
    add: Annotated[list[str] | None, Field(description="Quantities to store as well, e.g. ['Car.az', "
                                                       "'PT.Motor*.Trq']; wildcards * and ? allowed")] = None,
    remove: Annotated[list[str] | None, Field(description="Entries to take out, spelled as they are "
                                                          "listed")] = None,
    rate: Annotated[Literal["fast", "normal", "slow"],
                    Field(description="Storage rate for added quantities (sample times: "
                                      "cm_output_quantities)")] = "normal",
    preset: Annotated[Literal["movie"] | None,
                      Field(description="movie: add what IPGMovie needs to replay the vehicle from a result "
                                        "file")] = None,
) -> dict:
    """Add quantities to, or remove them from, the project's output-quantities file (backed up first,
    logged, restored by cm_revert_all). Applies from the next start. Requires an idle simulation. Find
    names with cm_live (it reads any quantity) or in another result file."""
    return S().output_quantities_edit(add, remove, rate, preset)


# ---- IPGMovie ---------------------------------------------------------------------------
@_tool(WRITE, "Open IPGMovie")
def cm_movie_open() -> dict:
    """Open IPGMovie, CarMaker's 3D animation window (or take over one that is open). Open it BEFORE a
    run: it records a run only while it is open, and cm_movie_snapshot can then show any moment of it."""
    return S().movie_open()


@_tool(WRITE, "Take a picture in IPGMovie")
def cm_movie_snapshot(
    time_s: Annotated[float | None, Field(description="Simulation time of the picture in s (default: the "
                                                      "latest moment IPGMovie holds)", ge=0)] = None,
    camera: Annotated[str | None, Field(description="Camera view: 'DEFAULT', \"Bird's Eye View\", 'Left "
                                                    "Side', 'Right Side', 'Sitting In Vehicle (A)', or one "
                                                    "from the project (default: keep the view)")] = None,
    erg: Annotated[str | None, Field(description="Replay this result file instead of the last run "
                                                 "(absolute path, or relative to the project)")] = None,
    width: Annotated[int, Field(description="Picture width in pixels (not during a run)", ge=160,
                                le=1920)] = 960,
    height: Annotated[int, Field(description="Picture height in pixels (not during a run)", ge=120,
                                 le=1080)] = 540,
) -> list:
    """A picture of IPGMovie's 3D view, returned as an image. After a run it shows any moment of the last
    run (time_s), provided IPGMovie was open during that run. During a run, without time_s, it shows the
    IPGMovie window as it is at that moment (in the window's own size). With erg it replays a saved
    result file, which must hold the vehicle's motion (cm_output_quantities_edit(preset='movie') before
    that run). Do not pass time_s during a run: that makes IPGMovie stop following the run."""
    out = S().movie_snapshot(time_s, camera, erg, width, height)
    with open(out["file"], "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    mime = "image/png" if out["file"].lower().endswith(".png") else "image/jpeg"
    # (model_validate with the protocol's own names works with every version of the MCP SDK)
    return [TextContent.model_validate({"type": "text", "text": json.dumps(out)}),
            ImageContent.model_validate({"type": "image", "data": data, "mimeType": mime})]


# ---- project data -----------------------------------------------------------------------
@_tool(READ, "List project data files")
def cm_list(
    kind: Kind,
    pattern: Annotated[str | None, Field(description="Wildcard filter on the name, e.g. '*Brak*'")] = None,
    include_hidden: Annotated[bool, Field(description="Also list names starting with a dot")] = False,
) -> dict:
    """Names of project data files of one kind (test runs, vehicles, drivers, tyres...)."""
    return S().project.list(kind, pattern, include_hidden)


@_tool(READ, "Read a project data file")
def cm_read(
    kind: Kind,
    name: Annotated[str, Field(description="File name relative to the kind's folder, as cm_list returns it")],
    keys: Annotated[list[str] | None, Field(description="Only these keys")] = None,
    prefix: Annotated[str | None, Field(description="Only keys starting with this, e.g. 'Vehicle' or "
                                                    "'DrivMan.'")] = None,
) -> dict:
    """Key / value pairs of a CarMaker infofile. Large files are truncated: filter by keys or prefix."""
    return S().project.read(kind, name, keys, prefix)


@_tool(DESTRUCTIVE, "Edit a project data file")
def cm_edit(
    kind: Kind,
    name: Annotated[str, Field(description="File name relative to the kind's folder")],
    overrides: Annotated[dict[str, Any], Field(description="Keys to set, e.g. {'Road.FName': "
                                                           "'SKIDPAD.rd5'}; a null value removes the key")],
) -> dict:
    """Change keys in a project file in place (backed up first, logged; only the edited lines change).
    Requires an idle simulation; load the test run again afterwards."""
    return S().edit_file(kind, name, overrides)


@_tool(WRITE, "Copy a project data file")
def cm_clone(
    kind: Kind,
    base: Annotated[str, Field(description="Existing file to copy")],
    new_name: Annotated[str, Field(description="Name of the copy (must not exist)")],
) -> dict:
    """Copy a project file to a new name in the same kind's folder. Never overwrites."""
    return S().clone_file(kind, base, new_name)


# ---- MATLAB workspace and Simulink -------------------------------------------------------
@_tool(READ, "List workspace variables")
def cm_list_workspace_vars(
    scope: Scope = "base",
    pattern: Annotated[str | None, Field(description="Wildcard filter on the name, e.g. 'Kp_*'")] = None,
) -> dict:
    """Variables (name, class, size) of the MATLAB base workspace or of a Simulink model's workspace.
    Controller parameters usually live in the model workspace."""
    return S().workspace_list(scope, pattern)


@_tool(READ, "Read a workspace variable")
def cm_get_workspace_var(
    name: Annotated[str, Field(description="Variable or field, e.g. 'Kp_12' or 'Table.Value'")],
    scope: Scope = "base",
) -> Any:
    """Value of a variable or field. MATLAB objects are described, not converted: read their fields."""
    return S().workspace_get(name, scope)


@_tool(DESTRUCTIVE, "Set a workspace variable")
def cm_set_workspace_var(
    name: Annotated[str, Field(description="Variable or field, e.g. 'Kp_12' or 'Table.Value'")],
    value: Annotated[Any, Field(description="Number, string, list or nested list (matrix)")],
    scope: Scope = "base",
) -> dict:
    """Set a variable or field. The old value is logged and cm_revert_all restores it. Model-workspace
    changes live in memory until cm_model_save. Requires an idle simulation."""
    return S().workspace_set(name, value, scope)


@_tool(READ, "Read a Simulink parameter")
def cm_model_get(
    path: Annotated[str, Field(description="Model or block path, e.g. 'MyModel/Controller/Gain'")],
    param: Annotated[str, Field(description="Parameter name, e.g. 'Gain'")],
) -> Any:
    """get_param on a loaded Simulink model or block."""
    return S().model_get(path, param)


@_tool(DESTRUCTIVE, "Set a Simulink parameter")
def cm_model_set(
    path: Annotated[str, Field(description="Model or block path")],
    param: Annotated[str, Field(description="Parameter name")],
    value: Annotated[Any, Field(description="New value (numbers are converted to text as Simulink expects)")],
    save: Annotated[bool, Field(description="Also back up and save the model file")] = False,
) -> dict:
    """set_param on a loaded Simulink model or block. Logged; cm_revert_all restores the old value."""
    return S().model_set(path, param, value, save)


@_tool(DESTRUCTIVE, "Save a Simulink model")
def cm_model_save(model: Annotated[str, Field(description="Name of the loaded model")]) -> dict:
    """Save a loaded Simulink model. The file must be inside the project; it is backed up first."""
    return S().model_save(model)


@_tool(WRITE, "Save Simulink's logged data to a MAT file")
def cm_model_logs_save(
    file: Annotated[str | None, Field(description="MAT file to write, inside the project (default: next to "
                                                  "the last result file, with the same name)")] = None,
    model: Annotated[str | None, Field(description="Loaded Simulink model (default: the active one)")] = None,
    logs: Annotated[Literal["inspector", "workspace", "both"],
                    Field(description="inspector: every signal the Simulation Data Inspector holds for "
                                      "the last run (logged signals and To Workspace blocks, complete). "
                                      "workspace: the logging variables in the base workspace as they "
                                      "are")] = "inspector",
    format: Annotated[Literal["dataset", "struct"],
                      Field(description="dataset: a Simulink.SimulationData.Dataset. struct: plain arrays "
                                        "(name, time, values, units, block) that load without "
                                        "Simulink")] = "dataset",
    base_workspace: Annotated[bool, Field(description="Include the base workspace's variables")] = True,
    model_workspace: Annotated[bool, Field(description="Include the model workspace's variables")] = True,
    overwrite: Annotated[bool, Field(description="Replace the file if it exists")] = False,
    timeout_s: Annotated[float, Field(description="Longest time MATLAB may take to write the file",
                                      gt=0)] = 120.0,
) -> dict:
    """After a CarMaker for Simulink run: write what Simulink logged (signals marked for logging and
    To Workspace blocks) to a MAT file, with the base workspace and the model workspace as one struct
    each (variables: signals, base_workspace, model_workspace, info). Requires an idle simulation.
    The result file (.erg) holds CarMaker's quantities; this holds the Simulink side."""
    return S().model_logs_save(file, model, logs, format, base_workspace, model_workspace, overwrite,
                               timeout_s=timeout_s)


@_tool(WRITE, "Add a Simulink block (experimental)", feature="experimental")
def cm_model_add_block(
    src: Annotated[str, Field(description="Library block, e.g. 'simulink/Math Operations/Gain'")],
    dest: Annotated[str, Field(description="New block path, e.g. 'MyModel/Sub/Gain1'")],
    params: Annotated[dict[str, Any] | None, Field(description="Block parameters to set")] = None,
) -> dict:
    """EXPERIMENTAL. add_block. Unsaved until cm_model_save; a revert reloads the model from disk."""
    return S().model_struct("add_block", src=src, dest=dest, params=params or {})


@_tool(WRITE, "Add a Simulink line (experimental)", feature="experimental")
def cm_model_add_line(
    system: Annotated[str, Field(description="System that contains both blocks, e.g. 'MyModel/Sub'")],
    out: Annotated[str, Field(description="Source port as 'Block/1'")],
    inp: Annotated[str, Field(description="Destination port as 'Block2/1'")],
) -> dict:
    """EXPERIMENTAL. add_line. Unsaved until cm_model_save."""
    return S().model_struct("add_line", system=system, out=out, inp=inp)


@_tool(DESTRUCTIVE, "Delete a Simulink block (experimental)", feature="experimental")
def cm_model_delete_block(path: Annotated[str, Field(description="Block path")]) -> dict:
    """EXPERIMENTAL. delete_block. Unsaved until cm_model_save; a revert reloads the model from disk."""
    return S().model_struct("delete_block", path=path)


# ---- standalone instances (independent of MATLAB) ------------------------------------------
@_tool(READ, "List running CarMaker programs")
def cm_standalone_servers() -> list[dict]:
    """CarMaker simulation programs running on this machine (pid, description, whether this server
    manages it). A CarMaker Office window you opened shows up once its application is started."""
    return SA().servers()


@_tool(WRITE, "Launch a standalone CarMaker run")
def cm_standalone_launch(
    testrun: Annotated[str, Field(description="Test run relative to Data/TestRun")],
    overrides: Overrides = None,
    quantities: Annotated[list[str] | None, Field(description="Quantities to store (default: Time, Car.v, "
                                                              "Car.YawRate, Car.ax, Car.ay, "
                                                              "Car.Distance)")] = None,
    realtime_factor: Annotated[float, Field(description="Simulation speed relative to real time "
                                                        "(larger is faster)", gt=0)] = 1.0,
    stop_after_s: Annotated[float | None, Field(description="Safety stop at this simulation time in s "
                                                            "(null: none)")] = 600.0,
    executable: Annotated[str | None, Field(description="CarMaker executable (default: the stock one of "
                                                        "the install)")] = None,
    start: Annotated[bool, Field(description="Start the run at once")] = True,
) -> dict:
    """Start a NEW independent CarMaker simulation program (not connected to MATLAB) and run a test run
    on it. Only for test runs whose vehicle does not need a Simulink controller. Results go to the
    project's SimOutput; see cm_standalone_results."""
    return SA().launch(testrun, overrides=overrides, quantities=quantities, realtime_factor=realtime_factor,
                       stop_after_s=stop_after_s, executable=executable, start=start)


@_tool(WRITE, "Attach to a running CarMaker program")
def cm_standalone_attach(
    pid: Annotated[int, Field(description="Process id from cm_standalone_servers")],
    testrun: Annotated[str | None, Field(description="Test run to configure (needed to start a run)")] = None,
    overrides: Overrides = None,
    quantities: Annotated[list[str] | None, Field(description="Quantities to store")] = None,
    realtime_factor: Annotated[float, Field(description="Speed relative to real time", gt=0)] = 1.0,
    stop_after_s: Annotated[float | None, Field(description="Safety stop at this simulation time "
                                                            "in s")] = 600.0,
    start: Annotated[bool, Field(description="Start the run at once (needs testrun)")] = False,
) -> dict:
    """Attach to a CarMaker simulation program that is already running, for example one started from a
    CarMaker Office window."""
    return SA().attach(pid, testrun, overrides=overrides, quantities=quantities,
                       realtime_factor=realtime_factor, stop_after_s=stop_after_s, start=start)


@_tool(READ, "Standalone instance status")
def cm_standalone_status(
    instance: Instance = None,
    quantities: Annotated[list[str] | None, Field(description="Live quantities to read (default: Time, "
                                                              "Car.v)")] = None,
) -> dict:
    """State of a standalone instance plus live values."""
    return SA().status(instance, quantities)


@_tool(DESTRUCTIVE, "Control a standalone run")
def cm_standalone_control(
    action: Annotated[Literal["start", "pause", "resume", "stop"], Field(description="What to do")],
    instance: Instance = None,
) -> dict:
    """Start, pause, resume or stop the simulation of a standalone instance."""
    return SA().control(action, instance)


@_tool(READ, "Wait for a standalone run to end")
async def cm_standalone_wait_end(instance: Instance = None, timeout_s: MaxWait = 45.0) -> dict:
    """Wait until the instance's simulation has finished. finished=true: simulation time and distance,
    error flag and log errors. finished=false: call again to keep waiting."""
    return await asyncio.to_thread(SA().wait_end, instance, timeout_s)


@_tool(WRITE, "Overwrite a quantity in a standalone run (DVA)")
def cm_standalone_dva_write(
    name: Annotated[str, Field(description="Quantity to overwrite")],
    value: Annotated[float, Field(description="Absolute value")],
    duration_ms: Annotated[int, Field(description="How long the override lasts in ms (-1: until the run "
                                                  "ends)")] = -1,
    instance: Instance = None,
) -> dict:
    """Overwrite a quantity in a running standalone simulation (Direct Variable Access)."""
    return SA().dva_write(name, value, duration_ms, instance)


@_tool(READ, "Result files of a standalone run")
def cm_standalone_results(instance: Instance = None) -> dict:
    """Result .erg files of the instance's run (read them with cm_results_summary / cm_results_read)."""
    return SA().results(instance)


@_tool(DESTRUCTIVE, "Close a standalone instance")
def cm_standalone_close(instance: Instance = None) -> dict:
    """Disconnect; for instances this server launched, also stop the CarMaker process."""
    return SA().close(instance)


# ---- parameter studies ---------------------------------------------------------------------
@_tool(WRITE, "Start a parameter study")
def cm_study_start(
    testrun: Annotated[str, Field(description="Test run relative to Data/TestRun")],
    variations: Annotated[list[dict[str, Any]], Field(
        description="One entry per run: {'label': 'heavy', 'keys': {'Body.mass': 320}, 'workspace': "
                    "{'Kp_yaw': 1.5}}. 'keys' are infofile keys applied in memory for that run only "
                    "(prefix a place such as 'Vehicle:' if needed); 'workspace' are MATLAB variables in "
                    "'scope', restored after the run. Both are optional", min_length=1, max_length=50)],
    quantities: Annotated[list[str] | None, Field(description="Quantities to summarise per run (default: "
                                                              "Time, Car.v)")] = None,
    mode: Annotated[Literal["cm4sl", "standalone"],
                    Field(description="cm4sl: the MATLAB-connected session. standalone: a separate "
                                      "CarMaker process per run (no 'workspace' values)")] = "cm4sl",
    scope: Scope = "base",
    max_run_s: Annotated[float, Field(description="A run is stopped after this wall time in s",
                                      gt=0)] = 900.0,
    save_logs: Annotated[bool, Field(description="After every run, save what Simulink logged and the "
                                                 "workspaces as that run used them to a MAT file next "
                                                 "to its result file (mode cm4sl; logs_file per "
                                                 "row)")] = False,
) -> dict:
    """Run one test run several times with different parameter values, one after another, in the
    background. Returns a study id at once; poll cm_study_status. Nothing is written to project files."""
    return ST().start(testrun, variations, quantities, mode, scope, max_run_s, save_logs)


@_tool(READ, "Parameter study progress and results")
def cm_study_status(
    study: Annotated[str | None, Field(description="Study id (default: the latest)")] = None,
) -> dict:
    """State of a study (running, done, cancelled, failed), the run in progress and one row per finished
    run: end status, simulation time, distance, result file, statistics of the requested quantities."""
    return ST().status(study)


@_tool(DESTRUCTIVE, "Cancel a parameter study")
def cm_study_cancel(
    study: Annotated[str | None, Field(description="Study id (default: the latest)")] = None,
) -> dict:
    """Stop the run in progress, skip the remaining ones and restore the changed workspace values."""
    return ST().cancel(study)


# ---- history -------------------------------------------------------------------------------
@_tool(READ, "Change log")
def cm_changelog(
    session: Annotated[str | None, Field(description="Session id (default: this session)")] = None,
    limit: Annotated[int, Field(description="Newest entries to return", ge=1, le=1000)] = 100,
) -> list[dict]:
    """Recorded changes (file keys, workspace variables, model parameters, GUI settings) with old and
    new values."""
    return S().changelog(session, limit)


@_tool(DESTRUCTIVE, "Undo this session's changes")
def cm_revert_all() -> dict:
    """Undo every change made in this server session: workspace values and model parameters in reverse
    order, backed-up files put back (created files go to a trash folder), GUI settings restored."""
    return S().revert_all()


@_tool(DESTRUCTIVE, "Restore files of an earlier session")
def cm_restore(session: Annotated[str, Field(description="Session id as shown by cm_changelog")]) -> dict:
    """Restore project files from the backups of an earlier session. Files only."""
    return S().restore(session)


# ---- server ----------------------------------------------------------------------------------
def _names_from_env(var: str, known: dict[str, str]) -> frozenset[str]:
    names = {n.strip().lower() for n in os.environ.get(var, "").split(",") if n.strip()}
    if names - set(known):
        log.warning("%s: unknown name(s) %s (known: %s)", var, sorted(names - set(known)), sorted(known))
    return frozenset(names & set(known))


def features_from_env() -> frozenset[str]:
    return _names_from_env("CM_ENABLE", FEATURES)


def disabled_from_env() -> frozenset[str]:
    return _names_from_env("CM_DISABLE", GROUPS)


def create_server(features: frozenset[str] | set[str] = frozenset(),
                  disabled: frozenset[str] | set[str] = frozenset()) -> FastMCP:
    """Build the MCP server. ``features`` switches on optional tools (FEATURES), ``disabled`` switches
    off tool groups (GROUPS)."""
    m = FastMCP("carmaker", version=__version__, instructions=guide.INSTRUCTIONS)
    for spec in _TOOLS:
        if spec.feature is not None and spec.feature not in features:
            continue
        if _group(spec.fn.__name__) in disabled:
            continue
        m.tool(spec.fn, annotations=spec.annotations, title=spec.title)

    @m.resource("carmaker://guide", name="guide", mime_type="text/markdown",
                description="How to use this server: workflows, parameters, troubleshooting")
    def guide_resource() -> str:
        return guide.GUIDE

    @m.resource("carmaker://status", name="status", mime_type="application/json",
                description="Current simulation status (same as cm_status)")
    def status_resource() -> str:
        return json.dumps(S().status(), default=str)

    @m.resource("carmaker://changelog", name="changelog", mime_type="application/json",
                description="Changes made by this server session (same as cm_changelog)")
    def changelog_resource() -> str:
        return json.dumps(S().changelog(None, 200), default=str)

    @m.resource("carmaker://log", name="log", mime_type="text/plain",
                description="Last 200 lines of the newest CarMaker session log")
    def log_resource() -> str:
        return "\n".join(S().log_tail(200)["lines"])

    @m.prompt(name="run_and_summarise", description="Run a test run and summarise the result")
    def run_and_summarise(testrun: str, quantities: str = "Car.v, Car.ax, Car.ay") -> str:
        return guide.prompt_run_and_summarise(testrun, quantities)

    @m.prompt(name="compare_settings", description="Run a test run with two values of one parameter and "
                                                   "compare the results")
    def compare_settings(testrun: str, variable: str, value_a: str, value_b: str, scope: str = "base") -> str:
        return guide.prompt_compare_settings(testrun, variable, value_a, value_b, scope)

    @m.prompt(name="undo_session", description="Show what this session changed and undo it")
    def undo_session() -> str:
        return guide.prompt_undo_session()

    return m


mcp = create_server(features_from_env(), disabled_from_env())


def _setup_logging(cfg: Config) -> None:
    """Log to stderr (MCP clients show it) and to <state_dir>/server.log. Level: CM_LOG_LEVEL."""
    level = getattr(logging, os.environ.get("CM_LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(cfg.state_dir / "server.log", maxBytes=1_000_000,
                                            backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    log.setLevel(level)
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)


def _setup_cli(args) -> int:
    """`carmaker-mcp doctor` and `carmaker-mcp config`: print and exit (nothing is changed)."""
    from . import doctor
    from .backend_cm4sl import Cm4slBackend

    cfg = Config.from_env()
    gui = Cm4slBackend(cfg.matlab_session, min(cfg.engine_timeout, 10)).status
    rep = doctor.diagnose(cfg, gui_status=gui if args.command == "doctor" else None)
    if args.command == "config":
        try:
            print(doctor.client_config(args.client, rep, cfg))
        except ValueError as e:
            print(e)
            return 2
        return 0
    print(json.dumps(rep.as_dict(), indent=2) if args.json else doctor.format_report(rep))
    return 0 if rep.ok else 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="carmaker-mcp",
        description="MCP server (stdio) for IPG CarMaker for Simulink. Configure via CM_* env vars.",
    )
    p.add_argument("command", nargs="?", default="serve", choices=("serve", "doctor", "config"),
                   help="serve: run the MCP server (default). doctor: check this machine's setup. "
                        "config: print the registration for an MCP client")
    p.add_argument("--mock", action="store_true", help="use an in-memory backend (no CarMaker needed)")
    p.add_argument("--client", default="claude-code", help="for config: claude-code, claude-desktop, "
                                                           "vscode, codex, antigravity or cursor")
    p.add_argument("--json", action="store_true", help="for doctor: print the report as JSON")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if args.command != "serve":
        raise SystemExit(_setup_cli(args))
    cfg = Config.from_env()
    _setup_logging(cfg)
    if args.mock:
        from .backend_mock import MockBackend

        set_session(Session(MockBackend(), cfg))
    log.info("carmaker-mcp %s starting (features: %s)", __version__, sorted(features_from_env()) or "none")
    mcp.run(show_banner=False)  # stdio transport; keep stderr quiet for MCP clients


if __name__ == "__main__":
    main()
