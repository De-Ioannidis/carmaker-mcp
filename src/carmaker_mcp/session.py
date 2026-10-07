"""Session: ties backend, project files, backups and the change log together."""

from __future__ import annotations

import fnmatch
import functools
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import log as sessionlog
from . import outquants
from .backend import Backend, BackendError, BackendTimeout, Status, check_tcl_allowed, tcl_word
from .config import Config
from .guard import GuardError, PathGuard
from .infofile import InfoFile
from .movie import MOVIE_EXE, Movie, missing_quantities, missing_size
from .project import Project
from .results import Erg, list_results
from .state import StateStore
from .wincapture import CaptureError, capture_window

_VAR = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
_MODEL_PATH = re.compile(r"^[A-Za-z_]\w*(/.+)?$")

# Separators (ASCII unit / record separator) for the pop-up records built by _POPUP_DRAIN.
_US, _RS = chr(31), chr(30)
# Empties the GUI's pop-up buffer (at most five messages) into "type US answer US text RS" records.
_POPUP_DRAIN = (
    "set __o {}; for {set __i 0} {$__i < 8} {incr __i} {set __m [PopupCtrl nextmsg]; "
    "if {![llength $__m]} break; "
    "append __o [lindex $__m 0] [format %c 31] [lindex $__m 2] [format %c 31] [lindex $__m 1] "
    "[format %c 30]}; "
    "set __o"
)
# GUI storage mode (HIL(SaveMode)) -> argument of the ScriptControl command ``SaveMode``.
_SAVE_MODE_ARG = {"collect": "collect", "save": "save", "hist_10": "hist_10s", "hist_30": "hist_30s",
                  "hist_60": "hist_60s", "hist_max": "hist_max"}


def _locked(fn):
    """Run a Session method under the session lock."""

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)

    return wrapper


class Session:
    def __init__(self, backend: Backend, config: Config | None = None, session_id: str | None = None):
        self.backend = backend
        self.cfg = config or Config()
        self._session_id = session_id
        self._store: StateStore | None = None
        self._project: Project | None = None
        self._guard: PathGuard | None = None
        self._we_started = False
        self._reverted: set[str] = set()
        self._popup_timeout_orig: float | None = None  # GUI value before we first changed it
        self._save_mode_orig: str | None = None  # likewise for the storage mode
        self._lock = threading.RLock()  # one state-changing call at a time (tools run in threads)
        self._run_started: float | None = None
        self._log_mark: sessionlog.LogMark | None = None
        self._result_dirs: set[Path] = set()  # folders the GUI reported result files in
        self._launcher: Any = None
        self._movie: Movie | None = None
        self._movie_erg: Path | None = None  # result file IPGMovie is replaying (None: the last run)
        self._movie_missing: list[str] = []
        self._logs_pending = False  # save Simulink's logs when the run in progress has ended
        self._window_capture = capture_window  # (exe name) -> PNG data, width, height
        # Thread of a running parameter study; other threads may not change state meanwhile.
        self.study_owner: int | None = None
        b: Any = backend
        if getattr(b, "diag_file", "") is None:  # a backend that can log Simulink's diagnostics
            b.diag_file = self.cfg.state_dir / "simulink-diagnostics.txt"

    # ---- lazy project setup ------------------------------------------------
    def _ensure(self) -> Project:
        if self._project is None:
            root = self.cfg.project
            if root is None:
                st = self.backend.status()
                if not st.project_dir:
                    raise BackendError(
                        "Project directory unknown. Set CM_PROJECT or start the CarMaker GUI."
                    )
                root = Path(st.project_dir)
            self._guard = PathGuard(root, forbidden=(self.cfg.state_dir,))
            self._store = StateStore(self.cfg.state_dir, root, self._session_id)
            self._project = Project(root, self._guard, self._store)
        return self._project

    @property
    def project(self) -> Project:
        return self._ensure()

    @property
    def store(self) -> StateStore:
        self._ensure()
        return self._store  # type: ignore[return-value]

    @property
    def guard(self) -> PathGuard:
        self._ensure()
        return self._guard  # type: ignore[return-value]

    # ---- status and run control -------------------------------------------
    def status(self) -> dict:
        st = self.backend.status()
        out = {
            "connected": st.connected, "sim_status": st.sim_status_text, "sim_status_code": st.sim_status,
            "matlab_simstate": st.simstate, "end_status": st.end_status or None,
            "active_model": st.active_model, "project_dir": st.project_dir,
            "session_id": self._session_id or (self._store.session if self._store else None),
            **({"details": st.extra} if st.extra else {}),
        }
        if st.connected:
            out.update(self._gui_state())
        return out

    def _gui_state(self) -> dict:
        """Whether the GUI holds unsaved data (loading a test run then asks a question) and the
        storage mode. Empty if the GUI does not answer these."""
        try:
            vals = self._tcl(
                "list [GUI allsaved] [if {[info exists ::HIL(SaveMode)]} {set ::HIL(SaveMode)}]"
            ).split()
        except BackendError:
            return {}
        out: dict[str, Any] = {}
        if vals and vals[0] in ("0", "1"):
            out["gui_all_saved"] = vals[0] == "1"
        if len(vals) > 1 and vals[1] != "{}":
            out["save_mode"] = vals[1]
        return out

    def require_idle(self, what: str) -> Status:
        if self.study_owner not in (None, threading.get_ident()):
            raise BackendError(f"cannot {what} while a parameter study is running "
                               "(cm_study_status, cm_study_cancel)")
        st = self.backend.status()
        if not st.connected:
            raise BackendError(f"cannot {what}: CarMaker not reachable ({st.extra.get('error')})")
        if not st.idle:
            raise BackendError(f"cannot {what} while the simulation state is '{st.sim_status_text}'")
        return st

    @_locked
    def load_testrun(self, name: str, force: bool = False) -> dict:
        self.require_idle("load a test run")
        self._popups_begin()
        self._log_mark = self._mark_log()
        try:
            self.backend.load_testrun(name, force)
        except BackendError as e:
            raise BackendError(self._diagnose(str(e))) from e
        return self._with_popups({"loaded": name})

    @_locked
    def start_sim(self, start_timeout_s: float = 90.0, save: str = "save", save_logs: bool = False) -> dict:
        if save not in ("save", "collect", "keep"):
            raise BackendError("save must be 'save', 'collect' or 'keep'")
        if save_logs and not hasattr(self.backend, "save_logs"):
            raise BackendError("save_logs needs the MATLAB-connected session")
        self.require_idle("start a simulation")
        self._logs_pending = bool(save_logs)  # saved when the end of this run is reported
        self._popups_begin()
        if save != "keep":
            self._set_save_mode(save)
        self._log_mark = self._mark_log()
        self._run_started = time.time()
        self._simulink_error(clear=True)
        self._movie_erg, self._movie_missing = None, []  # IPGMovie follows the new run
        self.backend.start_sim()
        t0 = time.time()
        strikes, preparing, st = 0, False, None
        while time.time() - t0 < start_timeout_s:
            try:
                st = self.backend.status()
            except BackendError:
                st = None
            if st is None or not st.connected:
                pass
            elif st.sim_status is not None and st.sim_status >= 0:
                return self._started(st, t0)
            elif not st.idle:
                preparing = True  # the application is starting, Simulink is compiling the model
            elif preparing:
                # Idle again without having been seen running: the preparation failed (typically
                # the model did not compile), or the run was over between two looks.
                if st.end_status == "failed":
                    return self._start_failed("the simulation did not start", self._simulink_error())
                return self._started(st, t0)
            elif time.time() - t0 > 1.0:
                # Never left idle. If Simulink has reported an error since the start AND the model
                # is stopped (not compiling), the run is not going to begin: say so now instead of
                # waiting for the time-out. Two looks in a row, to be sure.
                sl = self._simulink_error(certain=True)
                strikes = strikes + 1 if sl and self._model_stopped(st.active_model) else 0
                if sl and strikes >= 2:
                    return self._start_failed("the simulation did not start", sl)
            time.sleep(0.5)
        if preparing and st is not None and st.connected and not st.idle:
            return self._started(st, t0, f"still '{st.sim_status_text}' after {start_timeout_s:.0f}s: "
                                         "cm_wait_end waits for the run and reports a failure")
        return self._start_failed(f"simulation did not leave 'idle' within {start_timeout_s:.0f}s",
                                  self._simulink_error())

    def _started(self, st: Status, t0: float, note: str | None = None) -> dict:
        self._we_started = True
        out: dict[str, Any] = {"started": True, "state": st.sim_status_text,
                               "after_s": round(time.time() - t0, 1), "save_mode": self._save_mode_quiet()}
        if note:
            out["note"] = note
        return self._with_popups(out)

    def _start_failed(self, error: str, simulink: str | None) -> dict:
        self._logs_pending = False
        self._simulink_capture_stop()
        raise BackendError(self._diagnose(error, simulink=simulink))

    def stop_sim(self, wait_s: float = 30.0) -> dict:
        """Not locked on purpose: stopping must work while another call is still starting a run."""
        self.backend.stop_sim()
        out: dict[str, Any] = {"stop_requested": True}
        if wait_s > 0:
            out.update(self.wait_end(wait_s, 0.5))
        return out

    # ---- cold start ----------------------------------------------------------
    @_locked
    def session_start(self, model: str | None = None, max_wait_s: float = 45.0,
                      init: str | None = None) -> dict:
        """Bring up MATLAB, the model and the CarMaker GUI as far as they are missing."""
        if not hasattr(self.backend, "matlab"):
            return {"ready": True, "steps": {}, "note": "this backend has no MATLAB session to start"}
        if self._launcher is None:
            from .launcher import Launcher

            self._launcher = Launcher()
        try:
            return self._launcher.ensure(self.backend, self.cfg, model, max_wait_s, init)
        except BackendTimeout as e:  # MATLAB is still busy with an earlier step
            return {"ready": False, "waiting_for": f"MATLAB ({e})", "note": "call again to keep waiting"}

    def doctor(self) -> dict:
        from . import doctor

        rep = doctor.diagnose(self.cfg, gui_status=self.backend.status)
        return {**rep.as_dict(), "uvx_args": doctor.uvx_args(rep)}

    # ---- storage of results ------------------------------------------------
    # The GUI's "Storage of Results" mode decides whether a run writes a result file. ScriptControl
    # sets it with ``SaveMode``; the current value is only available from the GUI variable HIL(SaveMode).
    def save_mode(self) -> str | None:
        return self._tcl("if {[info exists ::HIL(SaveMode)]} {set ::HIL(SaveMode)}").strip() or None

    def _save_mode_quiet(self) -> str | None:
        try:
            return self.save_mode()
        except BackendError:
            return None

    def _set_save_mode(self, mode: str) -> None:
        old = self._save_mode_quiet()
        if old == mode:
            return
        self._tcl(f"SaveMode {mode}")
        if self._save_mode_orig is None and old in _SAVE_MODE_ARG:
            self._save_mode_orig = old
        self.store.log("save_mode", "gui", old=old, new=mode)

    def _last_result_file(self) -> str | None:
        """Result file of the run this session started last, if the GUI wrote one."""
        try:
            name = self._tcl("GetLastResultFName").strip()
        except BackendError:
            return None
        if not name:
            return None
        p = Path(name)
        if not p.is_absolute():
            p = self.project.root / p
        try:
            if not p.exists() or (self._run_started and p.stat().st_mtime < self._run_started - 2):
                return None
        except OSError:
            return None
        self._result_dirs.add(p.parent)
        return str(p)

    # ---- session log -------------------------------------------------------
    def _mark_log(self) -> sessionlog.LogMark | None:
        try:
            return sessionlog.mark(self.project.root)
        except (BackendError, OSError):
            return None

    def _log_since(self) -> list[str]:
        if self._log_mark is None:
            return []
        try:
            return sessionlog.since(self.project.root, self._log_mark)
        except (BackendError, OSError):
            return []

    def log_tail(self, lines: int = 50, level: str = "all") -> dict:
        try:
            return sessionlog.tail(self.project.root, lines, level)
        except ValueError as e:
            raise BackendError(str(e)) from e

    # ---- GUI pop-ups -------------------------------------------------------
    # The GUI records the pop-ups it raises for remote commands (ScriptControl ``PopupCtrl``). With the
    # default timeout of -1 such a pop-up waits for a click in the GUI; with a timeout it answers itself
    # with its default choice. Either way the text ends up in a small buffer that is read here.
    def _tcl(self, command: str, timeout_ms: int = 5000) -> str:
        status, result = self.backend.gui_tcl(command, timeout_ms)
        if status != 0:
            raise BackendError(f"Tcl `{command}` failed (status {status}): {result}")
        return result

    def popups(self) -> list[dict]:
        """Buffered pop-up messages, oldest first. Reading removes them from the GUI's buffer."""
        out = []
        for rec in self._tcl(_POPUP_DRAIN).split(_RS):
            parts = rec.split(_US, 2)
            if len(parts) == 3:
                kind, answer, text = parts
                out.append({"type": kind, "text": text,
                            "answer": int(answer) if answer.isdigit() else answer})
        return out

    def popup_timeout(self, seconds: float | None = None) -> float:
        """Current pop-up timeout in seconds (-1 = wait for the user). Sets it when ``seconds`` is given."""
        if seconds is None:
            return float(self._tcl("PopupCtrl timeout"))
        if seconds < 0 and seconds != -1:
            raise BackendError("popup timeout must be >= 0, or -1 to wait for the user")
        old = float(self._tcl(f"PopupCtrl timeout {seconds:g}"))
        if self._popup_timeout_orig is None:
            self._popup_timeout_orig = old
        return old

    def popup_status(self) -> dict:
        return {"timeout_s": self.popup_timeout(), "messages": self.popups()}

    @_locked
    def set_popup_timeout(self, seconds: float) -> dict:
        old = self.popup_timeout(seconds)
        self.store.log("popup_timeout", "gui", old=old, new=seconds)
        return {"timeout_s": seconds, "previous_timeout_s": old}

    def _popups_begin(self) -> None:
        """Before a GUI action: apply CM_POPUP_TIMEOUT once and drop messages of earlier commands."""
        try:
            if self.cfg.popup_timeout is not None and self._popup_timeout_orig is None:
                self.popup_timeout(self.cfg.popup_timeout)
            self.popups()
        except BackendError:
            pass

    def _popups_quiet(self) -> list[dict]:
        try:
            return self.popups()
        except BackendError:
            return []

    def _with_popups(self, out: dict) -> dict:
        msgs = self._popups_quiet()
        if msgs:
            out["popups"] = msgs
        return out

    def _diagnose(self, error: str, simulink: str | None = None) -> str:
        """Extend an error with what was said about it: Simulink's error, GUI pop-ups, new log errors."""
        parts = [error]
        sl = simulink  # only when the caller knows it belongs to this action
        if sl:
            parts.append("Simulink: " + sl)
        msgs = self._popups_quiet()
        if msgs:
            parts.append("GUI pop-ups: " + "; ".join(f"[{m['type']}] {m['text']}" for m in msgs))
        errs = sessionlog.dedupe(sessionlog.filter_level(self._log_since(), "error"), 3)
        if errs:
            parts.append("Session log: " + " | ".join(errs))
        if not msgs and not errs and not sl:
            parts.append("If the CarMaker GUI is showing a pop-up, answer it there, or let pop-ups "
                         "answer themselves with cm_popup_timeout; cm_log shows the session log")
        return ". ".join(parts)

    def wait_end(self, timeout_s: float = 45.0, poll_s: float = 1.0,
                 on_poll: Callable[[float, dict], None] | None = None) -> dict:
        """Wait until the simulation is idle, at most ``timeout_s``. Not locked: other calls (live
        values, stop) stay possible. ``on_poll(elapsed_s, live)`` is called about every 5 s."""
        t0 = time.time()
        errors, last_cb, st = 0, 0.0, None
        while True:
            try:
                st = self.backend.status()
            except BackendError:
                st = None
            if st is not None and st.connected and st.idle:
                self._we_started = False
                return self._with_popups(self._run_summary(st, t0, errors))
            if st is None or not st.connected:
                errors += 1
            elapsed = time.time() - t0
            if elapsed >= timeout_s:
                break
            if on_poll is not None and elapsed - last_cb >= 5:
                last_cb = elapsed
                on_poll(elapsed, self._live_quiet())
            time.sleep(min(poll_s, max(0.0, timeout_s - elapsed)))
        return {"finished": False, "state": st.sim_status_text if st is not None else "unknown",
                "waited_s": round(time.time() - t0, 1), "live": self._live_quiet(), "poll_errors": errors,
                "note": "still running: call again to keep waiting"}

    def _live_quiet(self) -> dict:
        try:
            return self.backend.quantity_read(["Time", "Car.v"])
        except BackendError:
            return {}

    def _run_summary(self, st: Status, t0: float, poll_errors: int) -> dict:
        out: dict[str, Any] = {"finished": True, "end_status": st.end_status or None,
                               "elapsed_s": round(time.time() - t0, 1), "poll_errors": poll_errors}
        new = self._log_since()
        out.update(sessionlog.sim_end(new) or {})
        out["result_file"] = self._last_result_file()
        if out["result_file"] is None and self._save_mode_quiet() == "collect":
            out["note"] = "no result file: the storage mode is 'collect only' (start with save='save')"
        errs = sessionlog.dedupe(sessionlog.filter_level(new, "error"))
        if errs:
            out["log_errors"] = errs
        if out["end_status"] == "failed":  # not for a stopped run: blocks raise harmless errors too
            sl = self._simulink_error()
            if sl:
                out["simulink_error"] = sl
        self._simulink_capture_stop()
        if self._logs_pending and self.study_owner in (None, threading.get_ident()):
            self._logs_pending = False
            if out["end_status"] in ("completed", "aborted"):  # otherwise Simulink logged nothing new
                try:
                    saved = self.model_logs_save(model=st.active_model)
                    out["logs_file"] = saved["file"]
                except (BackendError, GuardError) as e:
                    out["logs_error"] = str(e)
        return out

    def _model_stopped(self, model: str | None) -> bool:
        fn = getattr(self.backend, "model_stopped", None)
        if fn is None or not model:
            return False
        try:
            return bool(fn(model))
        except BackendError:
            return False  # MATLAB is busy, so something is happening

    def _simulink_error(self, clear: bool = False, certain: bool = False) -> str | None:
        """Simulink's errors for this start or run (None for backends without Simulink).
        ``clear`` begins a new capture; ``certain`` leaves out errors that may have been handled."""
        fn = getattr(self.backend, "simulink_error", None)
        if fn is None:
            return None
        try:
            return fn(clear=clear, certain=certain)
        except BackendError:
            return None

    def _simulink_capture_stop(self) -> None:
        fn = getattr(self.backend, "simulink_capture_stop", None)
        if fn is not None:
            fn()

    def gui_tcl(self, command: str, timeout_ms: int = 10000) -> dict:
        check_tcl_allowed(command)
        status, result = self.backend.gui_tcl(command, timeout_ms)
        self.store.log("tcl", "gui", command=command, status=status, result=result[:500])
        return {"status": status, "result": result}

    # ---- project files -----------------------------------------------------
    @_locked
    def edit_file(self, kind: str, name: str, overrides: dict[str, Any]) -> dict:
        self.require_idle("edit project files")
        return self.project.edit(kind, name, overrides)

    @_locked
    def clone_file(self, kind: str, base: str, new_name: str) -> dict:
        return self.project.clone(kind, base, new_name)

    # ---- MATLAB workspace --------------------------------------------------
    def workspace_list(self, scope: str = "base", pattern: str | None = None) -> dict:
        _check_scope(scope)
        items = self.backend.workspace_list(scope)
        if pattern:
            items = [i for i in items if fnmatch.fnmatch(i["name"].lower(), pattern.lower())]
        return {"scope": scope, "count": len(items), "variables": items}

    def workspace_get(self, name: str, scope: str = "base") -> Any:
        _check_var(name)
        _check_scope(scope)
        return self.backend.workspace_get(name, scope)

    @_locked
    def workspace_set(self, name: str, value: Any, scope: str = "base") -> dict:
        _check_var(name)
        _check_scope(scope)
        self._ensure()
        self.require_idle("change a workspace variable")
        try:
            old, existed = self.backend.workspace_get(name, scope), True
        except BackendError:
            old, existed = None, False
        if isinstance(old, dict) and "class" in old and "note" in old:
            raise BackendError(
                f"{name} is a {old['class']} object; set one of its fields instead (e.g. {name}.Value)"
            )
        self.backend.workspace_set(name, value, scope)
        new = self.backend.workspace_get(name, scope)
        self.store.log("workspace", name, scope=scope, old=old, new=new, existed=existed)
        note = None if scope == "base" else "changed in memory only; save the model to persist"
        return {"scope": scope, "name": name, "old": old, "new": new, "existed": existed, "note": note}

    @_locked
    def workspace_clear(self, name: str, scope: str = "base") -> None:
        _check_var(name)
        _check_scope(scope)
        self.require_idle("clear a workspace variable")
        self.backend.workspace_clear(name, scope)
        self.store.log("workspace_clear", name, scope=scope)

    @_locked
    def key_values(self, keys: dict[str, Any]) -> None:
        """In-memory infofile overrides for the next simulation (ScriptControl ``KeyValue``): replaces
        all earlier ones; an empty dict removes them. No file is changed."""
        self._tcl("KeyValue reset")
        for k, v in keys.items():
            text = " ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
            self._tcl(f"KeyValue set {tcl_word(str(k))} {tcl_word(text)}")

    # ---- Simulink model ----------------------------------------------------
    def model_get(self, path: str, param: str) -> Any:
        _check_model_path(path)
        return self.backend.model_get(path, param)

    @_locked
    def model_set(self, path: str, param: str, value: Any, save: bool = False) -> dict:
        _check_model_path(path)
        self._ensure()
        self.require_idle("edit the Simulink model")
        old = self.backend.model_get(path, param)
        self.backend.model_set(path, param, value)
        new = self.backend.model_get(path, param)
        self.store.log("model_param", path, param=param, old=old, new=new)
        out = {"path": path, "param": param, "old": old, "new": new}
        if save:
            out["saved"] = self.model_save(path.split("/")[0])
        return out

    @_locked
    def model_save(self, model: str) -> dict:
        _check_model_path(model)
        self._ensure()
        self.require_idle("save the Simulink model")
        f = self.backend.model_file(model)
        if not f:
            raise BackendError(f"model {model!r} has no file on disk")
        p = self.guard.resolve(f)  # refuses files outside the project
        self.store.backup_file(p)
        self.backend.model_save(model)
        self.store.log("model_save", self.guard.relative(p))
        return {"saved": self.guard.relative(p)}

    @_locked
    def model_logs_save(self, file: str | None = None, model: str | None = None, logs: str = "inspector",
                        fmt: str = "dataset", base_workspace: bool = True, model_workspace: bool = True,
                        overwrite: bool = False, max_var_mb: float = 50.0, timeout_s: float = 120.0) -> dict:
        """Save what Simulink logged in the last run, and the workspaces, to a MAT file in the project."""
        if logs not in ("inspector", "workspace", "both"):
            raise BackendError("logs must be 'inspector', 'workspace' or 'both'")
        if fmt not in ("dataset", "struct"):
            raise BackendError("format must be 'dataset' or 'struct'")
        fn = getattr(self.backend, "save_logs", None)
        if fn is None:
            raise BackendError("this backend has no MATLAB session to save logged data from")
        self._ensure()
        st = self.require_idle("save the logged data")
        model = model or st.active_model
        if not model or not re.fullmatch(r"[A-Za-z_]\w*", model):
            raise BackendError(f"not a model name: {model!r}" if model else "no Simulink model is active")
        if file:
            p = Path(file if file.lower().endswith(".mat") else file + ".mat")
            if not p.is_absolute():
                p = self.project.root / p
        else:
            last = self._last_result_file()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            p = Path(last).with_suffix(".mat") if last else (
                self.project.root / "SimOutput" / "SimulinkLogs" / f"{model}_{stamp}.mat")
            n = 1
            while not last and p.exists():  # two runs within a second
                n += 1
                p = p.with_name(f"{model}_{stamp}-{n}.mat")
        p = self.guard.resolve(p)  # refuses a place outside the project
        if p.exists() and not overwrite:
            raise BackendError(f"{self.guard.relative(p)} exists already (pass overwrite=true to replace it)")
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = fn(p, model, logs, fmt, base_workspace, model_workspace, max_var_mb * 1e6,
                   self.cfg.state_dir / "matlab", timeout_s)
        except BackendTimeout as e:
            raise BackendError(f"MATLAB did not finish writing the file within {timeout_s:.0f}s and was "
                               "interrupted; give it more time with timeout_s") from e
        if not p.is_file():
            raise BackendError(f"MATLAB reported success but {p} does not exist")
        out: dict[str, Any] = {
            "file": str(p), "size_mb": round(p.stat().st_size / 1e6, 2), "model": model,
            "variables": r.get("variables", []), "run": r.get("run") or None, "signals": r.get("signals", 0),
            "base_variables": r.get("base_variables", 0), "model_variables": r.get("model_variables", 0),
        }
        for key in ("workspace_logs", "skipped", "notes"):
            value = r.get(key)
            if value:
                out[key] = value if isinstance(value, list) else [value]
        return out

    @_locked
    def model_struct(self, op: str, **kw: Any) -> dict:
        self._ensure()
        self.require_idle("restructure the Simulink model")
        ref = kw.get("dest") or kw.get("system") or kw.get("path") or ""
        _check_model_path(ref)
        model = ref.split("/")[0]
        f = self.backend.model_file(model)
        self.backend.model_struct(op, **kw)
        args = {k: str(v) for k, v in kw.items()}
        self.store.log("model_struct", ref, op=op, model=model, file=f, args=args)
        return {"op": op, "model": model, "unsaved": True}

    # ---- results -----------------------------------------------------------
    def result_dirs(self) -> list[Path]:
        root = self.project.root
        dirs = [root / "SimOutput", *sorted(self._result_dirs)]
        for d in self.cfg.result_dirs:
            p = Path(d)
            dirs.append(p if p.is_absolute() else root / p)
        return dirs

    def results_list(self, limit: int = 30) -> list[dict]:
        return list_results(self.result_dirs(), limit)

    def erg(self, path: str) -> Erg:
        p = Path(path)
        if not p.is_absolute():
            p = self.project.root / p
        return Erg(p)

    # ---- output quantities ---------------------------------------------------
    def _outquants_file(self) -> str:
        """Name of the active output-quantities file below Data/Config (the GUI can use another)."""
        try:
            name = self._tcl("OutQuantsGetFName").strip()
        except BackendError:
            name = ""
        return name or "OutputQuantities"

    def output_quantities(self) -> dict:
        name = self._outquants_file()
        p = self.project.path("config", name)
        if not p.is_file():
            raise BackendError(f"no output-quantities file Data/Config/{name}")
        return {"file": self.project.guard.relative(p), **outquants.describe(InfoFile.load(p))}

    @_locked
    def output_quantities_edit(self, add: list[str] | None = None, remove: list[str] | None = None,
                               rate: str = "normal", preset: str | None = None) -> dict:
        if preset is not None and preset not in outquants.PRESETS:
            raise BackendError(f"unknown preset {preset!r}; there is: {', '.join(outquants.PRESETS)}")
        add = [*(add or []), *outquants.PRESETS.get(preset or "", ())]
        if not add and not remove:
            raise BackendError("nothing to add or remove")
        self.require_idle("change the output quantities")
        name = self._outquants_file()
        p = self.project.path("config", name)
        if not p.is_file():
            raise BackendError(f"no output-quantities file Data/Config/{name}")
        changes, report = outquants.plan(InfoFile.load(p), add, list(remove or []), rate)
        if changes:
            self.project.edit("config", name, dict(changes))
        return {"changed": bool(changes), "file": self.project.guard.relative(p), **report,
                "note": "applies from the next start; a name CarMaker does not know is left out of the "
                        "result file without an error"}

    # ---- IPGMovie ---------------------------------------------------------------
    def _movie_ctl(self) -> Movie:
        if self._movie is None:
            self._movie = Movie(self.backend.gui_tcl, self.cfg.state_dir / "movie")
        return self._movie

    def movie_open(self) -> dict:
        out = self._movie_ctl().open()
        out["note"] = ("IPGMovie records a run only while it is open. After the run cm_movie_snapshot can "
                       "show any moment of it")
        return out

    def movie_snapshot(self, time_s: float | None = None, camera: str | None = None,
                       erg: str | None = None, width: int = 960, height: int = 540) -> dict:
        """Export one picture. Returns its description; the file is ``out['file']``."""
        m = self._movie_ctl()
        out: dict[str, Any] = {}
        if erg:
            e = self.erg(erg)
            lacking = [q for q in outquants.MOVIE_CORE if q not in e.names]
            if lacking:  # loading it would only raise IPGMovie's warning dialog and show a parked car
                raise BackendError(
                    f"{e.path.name} cannot be replayed in IPGMovie: it does not hold the vehicle's motion "
                    f"({', '.join(lacking)}). Add the quantities with cm_output_quantities_edit"
                    "(preset='movie') and run again, or take the picture of the last run without erg")
            m.open()
            if self._movie_erg != e.path:
                root = self.project.root
                size = missing_size(root)
                m.load(e.path)
                self._movie_erg = e.path
                self._movie_missing = missing_quantities(root, e.path, size)
            if self._movie_missing:
                out["missing_quantities"] = self._movie_missing[:40]
                out["note"] = ("IPGMovie showed a warning: the result file lacks these quantities, so parts "
                               "of the scene do not move")
        elif not m.is_open():
            raise BackendError("IPGMovie is not open. Call cm_movie_open before the run so that it records "
                               "it, or pass a result file as erg")
        if camera:
            m.camera(camera)
        try:
            running = not self.backend.status().idle
        except BackendError:
            running = False
        if running and not erg and time_s is None:
            # IPGMovie's export would stop it following the run: read its window instead.
            try:
                if camera:
                    time.sleep(0.3)  # let the new view be drawn
                data, w, h = self._window_capture(MOVIE_EXE)
                now = self._live_quiet().get("Time")
                return {"file": str(m.store(data, ".png")), "time_s": now if now is not None else "now",
                        "source": "IPGMovie's window during the run", "width": w, "height": h, **m.status(),
                        "note": "the window as it is on screen, in its own size; IPGMovie keeps following "
                                "the run"}
            except (CaptureError, OSError) as e:
                out["capture_error"] = str(e)
        path = m.export(time_s, width, height)
        out = {"file": str(path), "time_s": "latest" if time_s is None else time_s,
               "source": self._movie_erg.name if self._movie_erg else "the last run", **m.status(), **out}
        if running and not erg:
            out["note"] = ("exported during a run: IPGMovie has stopped following and recording this run "
                           "(its window stays frozen until the next start), so later moments of it cannot "
                           "be shown")
        return out

    # ---- change log, revert ------------------------------------------------
    def changelog(self, session: str | None = None, limit: int = 100) -> list[dict]:
        return self.store.entries(session)[-limit:]

    @_locked
    def revert_all(self) -> dict:
        self._ensure()
        entries = [e for e in self.store.entries(self.store.session)
                   if e["kind"] in ("workspace", "model_param", "model_struct")
                   and e["id"] not in self._reverted]
        if entries:
            self.require_idle("revert changes")
        done, failed = [], []
        reloaded: set[str] = set()
        for e in entries:
            if e["kind"] == "model_struct" and e["model"] not in reloaded and e.get("file"):
                try:
                    self.backend.model_reload(e["model"], e["file"])
                    reloaded.add(e["model"])
                except BackendError as ex:
                    failed.append({"id": e["id"], "error": str(ex)})
        for e in reversed(entries):
            try:
                if e["kind"] == "workspace":
                    scope = e.get("scope", "base")
                    if e.get("existed", True):
                        self.backend.workspace_set(e["target"], e["old"], scope)
                    else:
                        self.backend.workspace_clear(e["target"], scope)
                elif e["kind"] == "model_param":
                    if e["target"].split("/")[0] in reloaded:
                        pass  # the reload already restored the on-disk state
                    else:
                        self.backend.model_set(e["target"], e["param"], e["old"])
                done.append(e["id"])
                self._reverted.add(e["id"])
            except BackendError as ex:
                failed.append({"id": e["id"], "error": str(ex)})
        failed += self.restore_gui_settings()
        files = self.store.restore_files()
        self.store.log("revert", "session", reverted=done, failed=[f["id"] for f in failed])
        return {"reverted": done, "failed": failed, **files}

    def restore_gui_settings(self) -> list[dict]:
        """Put the GUI settings this session changed (pop-up time-out, storage mode) back as they were.
        Also called when the server exits, so that a restart does not leave them behind."""
        failed = []
        if self._popup_timeout_orig is not None:
            try:
                self.popup_timeout(self._popup_timeout_orig)
                self._popup_timeout_orig = None
            except BackendError as ex:
                failed.append({"id": "popup_timeout", "error": str(ex)})
        if self._save_mode_orig is not None:
            try:
                self._tcl(f"SaveMode {_SAVE_MODE_ARG[self._save_mode_orig]}")
                self._save_mode_orig = None
            except BackendError as ex:
                failed.append({"id": "save_mode", "error": str(ex)})
        return failed

    def at_exit(self) -> None:
        """Leave the GUI as it was found, unless a simulation is running (it may still be saving)."""
        if self._popup_timeout_orig is None and self._save_mode_orig is None:
            return
        try:
            if self.backend.status().idle:
                self.restore_gui_settings()
        except Exception:  # the server is going down: nothing useful can be done with an error
            pass

    @_locked
    def restore(self, session: str) -> dict:
        """Restore files from the backups of an earlier session (files only)."""
        self._ensure()
        self.require_idle("restore files")
        res = self.store.restore_files(session)
        self.store.log("restore", session, **res)
        return res


def _check_scope(scope: str) -> None:
    if scope != "base" and not re.fullmatch(r"[A-Za-z_]\w*", scope):
        raise BackendError(f"invalid scope {scope!r}: use 'base' or a model name")


def _check_var(name: str) -> None:
    if not _VAR.match(name):
        raise BackendError(f"invalid MATLAB variable name: {name!r}")


def _check_model_path(path: str) -> None:
    if not _MODEL_PATH.match(path) or "\n" in path:
        raise BackendError(f"invalid Simulink path: {path!r}")
