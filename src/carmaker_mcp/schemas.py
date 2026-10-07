"""Shapes of the most used tool results, so that MCP clients receive an output schema.

Every key a tool can return must be listed here: FastMCP drops keys that are not part of the declared
type (``tests/unit/test_server_surface.py`` checks that nothing gets lost).
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict  # pydantic needs this one before Python 3.12


class Popup(TypedDict, total=False):
    type: str  # info, warn, err, question, busy or bug
    text: str
    answer: int | str  # index of the answer that was given


class StatusResult(TypedDict, total=False):
    connected: bool
    sim_status: str  # idle, running, preprocessing, ...
    sim_status_code: int | None
    matlab_simstate: str | None
    end_status: str | None
    active_model: str | None
    project_dir: str | None
    session_id: str | None
    details: dict[str, Any]
    gui_all_saved: bool  # false: the GUI holds unsaved data and asks before a load (true: no guarantee)
    save_mode: str  # the GUI's storage mode: collect, save, hist_10, ...


class StartResult(TypedDict, total=False):
    started: bool
    state: str
    after_s: float
    save_mode: str | None
    note: str  # set when the run was still being prepared at the time limit
    popups: list[Popup]


class WaitResult(TypedDict, total=False):
    finished: bool
    stop_requested: bool
    # finished
    end_status: str | None
    elapsed_s: float
    testrun: str
    sim_time_s: float
    distance_m: float
    result_file: str | None
    log_errors: list[str]
    simulink_error: str  # why Simulink did not compile or run the model
    logs_file: str  # MAT file with Simulink's logged data (start with save_logs)
    logs_error: str  # why that file could not be written
    popups: list[Popup]
    # still running
    state: str
    waited_s: float
    live: dict[str, float | None]
    poll_errors: int
    note: str
