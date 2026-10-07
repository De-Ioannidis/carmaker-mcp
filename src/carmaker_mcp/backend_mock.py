"""In-memory backend for tests and for trying the server without CarMaker."""

from __future__ import annotations

import re
from typing import Any

from .backend import IDLE, BackendError, Status, sim_status_text


class MockBackend:
    def __init__(self, project_dir: str = "C:/mock/project"):
        self.project_dir = project_dir
        self.sim_status = IDLE
        self.simstate = "terminated"
        self.end_status = ""
        self.loaded: str | None = None
        self.workspace: dict[tuple[str, str], Any] = {("base", "Kp"): 2.0, ("mdl", "TV_ON"): 1.0}
        self.params: dict[tuple[str, str], Any] = {("mdl/Gain", "Gain"): "1"}
        self.quantities = {"Car.v": 12.5, "Time": 3.0}
        self.calls: list[tuple] = []
        self.polls_until_idle = 0  # simulate a run lasting N status polls
        self.model_files = {"mdl": None}
        self.struct_ops: list[tuple] = []
        self._polls = 0
        self.dva: list[tuple] = []
        self.popup_timeout = -1.0
        self.save_mode = "collect"  # GUI storage mode
        self.gui_all_saved = True
        self.last_result = ""  # what GetLastResultFName answers
        self.popup_msgs: list[tuple[str, str, int]] = []  # (type, text, answer) the GUI "raised"
        self.outquants_file = "OutputQuantities"
        self.logged_signals = 12  # signals the Simulation Data Inspector holds for the last run
        self.movie_open, self.movie_frozen = False, False  # IPGMovie
        self.movie_camera, self.movie_loaded = "DEFAULT", ""
        self.movie_cameras = {"DEFAULT", "Bird's Eye View", "Left Side", "Right Side"}
        self.movie_recorded_s = 100.0  # it holds a run of this length

    def status(self) -> Status:
        if self.sim_status >= 0 and self.polls_until_idle > 0:
            self._polls += 1
            if self._polls >= self.polls_until_idle:
                self.sim_status, self.simstate, self.end_status = IDLE, "terminated", "completed"
        return Status(
            connected=True, sim_status=self.sim_status, sim_status_text=sim_status_text(self.sim_status),
            simstate=self.simstate, end_status=self.end_status, active_model="mdl",
            project_dir=self.project_dir,
        )

    def gui_tcl(self, command: str, timeout_ms: int = 10000) -> tuple[int, str]:
        self.calls.append(("tcl", command))
        if command.startswith("PopupCtrl timeout"):
            old, arg = self.popup_timeout, command.split()[2:]
            if arg:
                self.popup_timeout = float(arg[0])
            return 0, f"{old:g}"
        if command.startswith("SaveMode "):
            self.save_mode = command.split()[1]
            return 0, ""
        if "GUI allsaved" in command:  # Session._gui_state
            return 0, f"{int(self.gui_all_saved)} {self.save_mode}"
        if "HIL(SaveMode)" in command:
            return 0, self.save_mode
        if command == "GetLastResultFName":
            return 0, self.last_result
        if command == "OutQuantsGetFName":
            return 0, self.outquants_file
        if command.startswith("Movie "):
            return self._movie(command)
        if "PopupCtrl nextmsg" in command:  # the drain script of Session.popups
            out = "".join(f"{t}{chr(31)}{a}{chr(31)}{text}{chr(30)}" for t, text, a in self.popup_msgs)
            self.popup_msgs = []
            return 0, out
        return 0, ""

    def _movie(self, command: str) -> tuple[int, str]:
        """IPGMovie as the GUI's ``Movie`` commands show it."""
        if command == "Movie start":
            self.movie_open = True
            return 0, ""
        if command == "Movie attach":
            return 0, "0"
        if not self.movie_open:
            return -1, "IPGMovie not available. Call 'Movie start' first."
        if command == "Movie status initialized":
            return 0, "1"
        if command == "Movie status online":
            return 0, str(int(self.sim_status >= 0 and not self.movie_frozen))
        if command == "Movie window info":
            name = self.movie_camera if " " not in self.movie_camera else "{" + self.movie_camera + "}"
            return 0, f"0 {{Rows 1 Columns 1 0 {{Width 700 Height 317 Name {name}}}}}"
        if command.startswith("Movie camera select "):
            name = command[len("Movie camera select "):].strip("{}").replace("\\", "")
            if name not in self.movie_cameras:
                return -1, f"IPGMovie: Couldn't find camera {name}"
            self.movie_camera = name
            return 0, ""
        if command.startswith("Movie loadsimdata "):
            self.movie_loaded = command[len("Movie loadsimdata "):].strip("{}")
            return 0, ""
        if command.startswith("Movie export status"):
            return 0, "inactive"
        m = re.match(r"Movie export window (\{[^}]*\}|\S+) 0 -start (\S+) ", command)
        if m:
            self.movie_frozen = self.sim_status >= 0  # a picture ends the recording of a running sim
            when = m.group(2)
            if when == "end" or float(when) <= self.movie_recorded_s:
                with open(m.group(1).strip("{}"), "wb") as f:
                    f.write(b"\xff\xd8\xff\xe0 mock picture at " + when.encode() + b" \xff\xd9")
            return 0, "1"
        return 0, ""

    def load_testrun(self, name: str, force: bool = False) -> str:
        self.calls.append(("load", name, force) if force else ("load", name))
        self.loaded = name
        return ""

    def start_sim(self) -> None:
        self.calls.append(("start",))
        self.sim_status, self.simstate, self.end_status, self._polls = 3, "running", "", 0

    def stop_sim(self) -> None:
        self.calls.append(("stop",))
        self.sim_status, self.simstate, self.end_status = IDLE, "terminated", "aborted"

    def quantity_read(self, names: list[str]) -> dict[str, float | None]:
        return {n: self.quantities.get(n) for n in names}

    def dva_write(self, name: str, value: float, duration_ms: int, mode: str) -> None:
        self.dva.append((name, value, duration_ms, mode))

    def dva_release(self) -> None:
        self.dva.append(("release",))

    def workspace_get(self, name: str, scope: str = "base") -> Any:
        if (scope, name) not in self.workspace:
            raise BackendError(f"undefined: {name}")
        return self.workspace[(scope, name)]

    def workspace_set(self, name: str, value: Any, scope: str = "base") -> None:
        self.workspace[(scope, name)] = value

    def workspace_clear(self, name: str, scope: str = "base") -> None:
        self.workspace.pop((scope, name), None)

    def workspace_list(self, scope: str = "base") -> list[dict]:
        return [
            {"name": n, "class": "double", "size": [1, 1]}
            for (sc, n) in sorted(self.workspace) if sc == scope
        ]

    def model_get(self, path: str, param: str) -> Any:
        return self.params[(path, param)]

    def model_set(self, path: str, param: str, value: Any) -> None:
        self.params[(path, param)] = value if isinstance(value, str) else repr(value)

    def model_file(self, model: str) -> str | None:
        return self.model_files.get(model)

    def model_save(self, model: str) -> None:
        self.calls.append(("save", model))

    def save_logs(self, file, model: str, logs: str, fmt: str, base: bool, model_ws: bool,
                  max_bytes: float, helper_dir, timeout: float) -> dict:
        self.calls.append(("save_logs", str(file), model, logs, fmt, base, model_ws))
        if model not in self.model_files:
            raise BackendError(f"MATLAB error: no model {model!r} is loaded")
        variables = (["signals"] if logs != "workspace" and self.logged_signals else []) \
            + (["workspace_logs"] if logs != "inspector" else []) \
            + (["base_workspace"] if base else []) + (["model_workspace"] if model_ws else []) + ["info"]
        with open(file, "wb") as f:
            f.write(b"MATLAB 5.0 MAT-file (mock)")
        n_base = sum(1 for s, _ in self.workspace if s == "base")
        n_model = sum(1 for s, _ in self.workspace if s == model)
        return {"run": f"Run 1: {model}" if self.logged_signals else "", "signals": self.logged_signals,
                "workspace_logs": ["logsout"] if logs != "inspector" else [],
                "base_variables": n_base if base else 0, "model_variables": n_model if model_ws else 0,
                "skipped": [], "bytes": 26, "variables": variables,
                "notes": [] if self.logged_signals or logs == "workspace"
                else ["the Simulation Data Inspector holds no run of this model"]}

    def model_reload(self, model: str, file: str) -> None:
        self.calls.append(("reload", model, file))

    def model_struct(self, op: str, **kw: Any) -> None:
        self.struct_ops.append((op, kw))
