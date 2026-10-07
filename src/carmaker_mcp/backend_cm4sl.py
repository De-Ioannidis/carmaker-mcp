"""CarMaker for Simulink backend.

Talks to the user's already-open MATLAB session through the MATLAB Engine for Python and
uses the functions IPG ships with CM4SL:

* ``cmguicmd(tcl, timeout_ms)``  runs a Tcl / ScriptControl command in the CarMaker GUI
* ``cmcmd('simstate' | 'endstatus' | 'getprojectdir' | ...)``  MATLAB-side state

MATLAB must share its engine once:  ``matlab.engine.shareEngine('cm_mcp')``.
Every engine call runs in the background with a timeout, so a MATLAB that is busy (for
example during a simulation) cannot hang the server.
"""

from __future__ import annotations

import concurrent.futures
import io
import threading
from pathlib import Path
from typing import Any

from .backend import (
    BackendError,
    BackendTimeout,
    NotSupported,
    Status,
    sim_status_text,
    tcl_word,
)

# GUI commands that call back into MATLAB must not block MATLAB's interpreter (deadlock),
# so they are sent without waiting and the state is polled instead.
_NOWAIT = 0
# Keywords LoadTestRun returns instead of an empty string.
_LOAD_ERRORS = {
    "failed": "CarMaker could not load it (wrong name, or a file it refers to cannot be read)",
    "cancel": "a question of the GUI was answered with Cancel",
    "incomplete": "it was loaded incompletely (a referenced file is missing)",
}


def _diary_errors(path: Path, limit: int = 5) -> list[str]:
    """Error entries of a Diagnostic Viewer log: 'Error: ...' plus the lines that continue it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Error:"):
            current = [line[len("Error:"):].strip()]
            entries.append("")
        elif line.startswith("Warning:") or not line:
            current = None
        elif current is not None:
            current.append(line)
        if current is not None:
            entries[-1] = " ".join(current)
    return list(dict.fromkeys(e for e in entries if e))[:limit]


class Cm4slBackend:
    def __init__(self, session: str = "cm_mcp", timeout: float = 30.0):
        self.session = session
        self.timeout = timeout
        self.diag_file: Path | None = None  # where Simulink's diagnostics are logged during a run
        self._capturing = False  # diag_file holds the messages of the latest start
        self._diary_on = False  # MATLAB is writing to it
        self._eng: Any = None  # matlab.engine.MatlabEngine once connected
        self._lock = threading.RLock()
        self._status_cmd: str | None = None

    # ---- engine plumbing ---------------------------------------------------
    def _engine(self):
        with self._lock:
            if self._eng is None:
                try:
                    import matlab.engine  # type: ignore
                except ImportError as e:
                    raise BackendError(
                        "matlabengine is not installed. Install it with "
                        "`pip install matlabengine` (version matching your MATLAB)."
                    ) from e
                try:
                    self._eng = matlab.engine.connect_matlab(self.session)
                except Exception as e:  # matlab.engine.EngineError
                    raise BackendError(
                        f"No shared MATLAB session named {self.session!r}. Run "
                        f"`matlab.engine.shareEngine('{self.session}')` in the MATLAB that CarMaker uses."
                    ) from e
            return self._eng

    def _call(self, fn: str, *args, nargout: int = 1, timeout: float | None = None):
        timeout = self.timeout if timeout is None else timeout
        for attempt in (0, 1):
            eng = self._engine()
            fut: Any = None
            try:
                # MATLAB's command-window output must not reach our stdout: on the stdio transport
                # that is the MCP channel, and stray text there breaks the connection.
                fut = getattr(eng, fn)(*args, nargout=nargout, background=True,
                                       stdout=io.StringIO(), stderr=io.StringIO())
                return fut.result(timeout=timeout)
            except (TimeoutError, concurrent.futures.TimeoutError) as e:  # two classes on 3.10
                try:
                    fut.cancel()
                except Exception:
                    pass
                raise BackendTimeout(
                    f"MATLAB did not answer {fn} within {timeout:.0f}s (busy simulating?)"
                ) from e
            except Exception as e:
                msg = str(e)
                # the engine has its own TimeoutError class, which is not the built-in one
                if type(e).__name__ == "TimeoutError" or "timeout from execution" in msg.lower():
                    raise BackendTimeout(
                        f"MATLAB did not answer {fn} within {timeout:.0f}s (busy simulating?)"
                    ) from e
                if attempt == 0 and ("closed" in msg.lower() or "not connected" in msg.lower()):
                    self._eng = None  # reconnect once
                    continue
                raise BackendError(f"MATLAB error in {fn}: {msg}") from e
        raise BackendError("unreachable")

    def matlab(self, code: str, nargout: int = 1, timeout: float | None = None):
        """Evaluate MATLAB code in the shared session (internal use: cold start)."""
        return self._call("eval", code, nargout=nargout, timeout=timeout)

    def model_stopped(self, model: str) -> bool:
        """True if Simulink is neither compiling nor running the model."""
        return str(self._call("get_param", model, "SimulationStatus", timeout=5)) == "stopped"

    def simulink_error(self, clear: bool = False, certain: bool = False) -> str | None:
        """Errors Simulink reported for the current start or run, if any.

        ``clear`` begins a new capture: the Diagnostic Viewer's messages are logged to
        ``diag_file`` (``sldiagviewer.diary``), which this process can read even while MATLAB is
        busy compiling. Without a capture the answer is ``sllasterror``, which also holds errors
        that blocks raise and handle themselves; ``certain`` leaves that fallback out while a
        capture is on.
        """
        if clear:
            self._capturing = False
            if self.diag_file is not None:
                f = str(self.diag_file).replace("'", "''")
                try:
                    self.diag_file.parent.mkdir(parents=True, exist_ok=True)
                    self._call("eval", f"sldiagviewer.diary('off'); if isfile('{f}'), delete('{f}'); end; "
                                       f"sldiagviewer.diary('{f}', 'UTF-8');", nargout=0, timeout=10)
                    self._capturing = self._diary_on = True
                except (BackendError, OSError):
                    pass  # no Diagnostic Viewer log in this release: sllasterror only
            self._call("eval", "sllasterror([]);", nargout=0, timeout=10)
            return None
        if self._capturing and self.diag_file is not None:
            errors = _diary_errors(self.diag_file)
            if errors or certain:
                return " | ".join(errors) or None
        import json

        try:
            data = json.loads(str(self._call("eval", "jsonencode(sllasterror)", timeout=10)))
        except (BackendError, ValueError):
            return None
        items = data if isinstance(data, list) else [data]
        msgs = [str(i.get("Message", "")).strip() for i in items if isinstance(i, dict)]
        return " | ".join(m for m in msgs if m) or None

    def simulink_capture_stop(self) -> None:
        """Stop logging the Diagnostic Viewer (the errors captured so far stay readable)."""
        if not self._diary_on:
            return
        self._diary_on = False
        try:
            self._call("eval", "sldiagviewer.diary('off');", nargout=0, timeout=10)
        except BackendError:
            pass  # the next capture switches it off first

    # ---- Tcl in the GUI ----------------------------------------------------
    def gui_tcl(self, command: str, timeout_ms: int = 10000) -> tuple[int, str]:
        """Returns (status, result). status 0 = ok, -1 Tcl error, -2 no connection, -3 timeout."""
        wait = max(self.timeout, timeout_ms / 1000 + 5) if timeout_ms > 0 else self.timeout
        res = self._call("cmguicmd", command, float(timeout_ms), nargout=2, timeout=wait)
        result, status = res
        return int(status), str(result)

    def _tcl_ok(self, command: str, timeout_ms: int = 10000) -> str:
        status, result = self.gui_tcl(command, timeout_ms)
        if status != 0:
            raise BackendError(f"Tcl `{command}` failed (status {status}): {result}")
        return result

    # ---- state -------------------------------------------------------------
    def _sim_status(self) -> int:
        if self._status_cmd is None:  # GetSimStatus replaced SimStatus in CarMaker 12
            status, result = self.gui_tcl("GetSimStatus", 5000)
            if status == 0:
                self._status_cmd = "GetSimStatus"
                return int(result)
            if status != -1:  # not a Tcl error (unknown command), so the GUI itself is unreachable
                raise BackendError(f"Tcl `GetSimStatus` failed (status {status}): {result}")
            self._status_cmd = "SimStatus"
        return int(self._tcl_ok(self._status_cmd, 5000))

    def status(self) -> Status:
        try:
            code = self._sim_status()
        except (BackendError, ValueError) as e:
            return Status(connected=False, extra={"error": str(e)})
        st = Status(connected=True, sim_status=code, sim_status_text=sim_status_text(code))
        # cmcmd('activemodel') can return a block path inside the model; keep the model's name
        for attr, arg in (("simstate", "simstate"), ("active_model", "activemodel"),
                          ("end_status", "endstatus"), ("project_dir", "getprojectdir")):
            try:
                setattr(st, attr, str(self._call("cmcmd", arg, timeout=5)))
            except BackendError as e:
                st.extra[attr] = f"unavailable: {e}"
        if st.active_model:
            st.active_model = st.active_model.split("/", 1)[0]
        return st

    # ---- run control -------------------------------------------------------
    def load_testrun(self, name: str, force: bool = False) -> str:
        # force=1: the GUI does not ask about unsaved data (and discards it)
        res = self._tcl_ok(f"LoadTestRun {tcl_word(name)}{' 1' if force else ''}", 60000).strip()
        if res:  # the Tcl call itself succeeds (status 0); the reason comes back as a keyword
            reason = _LOAD_ERRORS.get(res.lower(), f"CarMaker answered {res!r}")
            raise BackendError(f"test run {name!r} not loaded: {reason}")
        return res

    def start_sim(self) -> None:
        self.gui_tcl("StartSim", _NOWAIT)

    def stop_sim(self) -> None:
        self.gui_tcl("StopSim", _NOWAIT)

    # ---- quantities / DVA --------------------------------------------------
    def quantity_read(self, names: list[str]) -> dict[str, float | None]:
        words = " ".join(tcl_word(n) for n in names)
        self._tcl_ok(f"QuantSubscribe {{{' '.join(names)}}}")
        out = self._tcl_ok(
            "set __r {}; foreach q {" + words + "} {"
            "if {[info exists ::Qu($q)]} {lappend __r [set ::Qu($q)]} else {lappend __r nan}}; "
            "set __r"
        )
        vals = out.split()
        res: dict[str, float | None] = {}
        for n, v in zip(names, vals, strict=False):
            try:
                f = float(v)
                res[n] = None if f != f else f
            except ValueError:
                res[n] = None
        return res

    def dva_write(self, name: str, value: float, duration_ms: int, mode: str) -> None:
        self._tcl_ok(f"DVAWrite {tcl_word(name)} {value!r} {int(duration_ms)} {tcl_word(mode)}")

    def dva_release(self) -> None:
        self._tcl_ok("DVAReleaseQuants")

    # ---- MATLAB workspaces (base, or the workspace of a model) ----------------
    @staticmethod
    def _mw(scope: str) -> str:
        return f"get_param('{scope}','ModelWorkspace')"

    @staticmethod
    def _expr(name: str, scope: str) -> str:
        return name if scope == "base" else f"evalin({Cm4slBackend._mw(scope)}, '{name}')"

    def workspace_get(self, name: str, scope: str = "base") -> Any:
        expr = self._expr(name, scope)
        try:
            return _py(self._call("eval", expr, nargout=1))
        except BackendError:
            try:  # values the engine cannot convert (Simulink.LookupTable, ...)
                cls = str(self._call("eval", f"class({expr})", nargout=1))
            except BackendError:
                raise
            return {"class": cls, "note": "MATLAB object; address a field, e.g. Name.Table.Value"}

    def workspace_set(self, name: str, value: Any, scope: str = "base") -> None:
        eng = self._engine()
        try:
            eng.workspace["cm_mcp_tmp"] = _to_matlab(value)
        except Exception as e:
            raise BackendError(f"cannot convert value for MATLAB: {e}") from e
        try:
            if scope == "base":
                self._call("eval", f"{name} = cm_mcp_tmp;", nargout=0)
            else:
                mw = self._mw(scope)
                self._call(
                    "eval",
                    f"assignin({mw}, 'cm_mcp_tmp', cm_mcp_tmp); "
                    f"evalin({mw}, '{name} = cm_mcp_tmp; clear cm_mcp_tmp');",
                    nargout=0,
                )
        finally:
            try:
                self._call("eval", "clear cm_mcp_tmp", nargout=0)
            except BackendError:
                pass

    def workspace_clear(self, name: str, scope: str = "base") -> None:
        if scope == "base":
            self._call("eval", f"clear {name}", nargout=0)
        else:
            self._call("eval", f"evalin({self._mw(scope)}, 'clear {name}')", nargout=0)

    def workspace_list(self, scope: str = "base") -> list[dict]:
        import json

        if scope == "base":
            code = (
                "jsonencode(cellfun(@(n) struct('name', n, 'class', evalin('base', ['class(' n ')']), "
                "'size', evalin('base', ['size(' n ')'])), who, 'UniformOutput', false))"
            )
        else:
            code = (
                "jsonencode(arrayfun(@(x) struct('name', x.Name, 'class', class(x.Value), "
                f"'size', size(x.Value)), {self._mw(scope)}.data, 'UniformOutput', false))"
            )
        out = json.loads(str(self._call("eval", code, nargout=1)))
        return out if isinstance(out, list) else [out]

    # ---- Simulink ----------------------------------------------------------
    def model_get(self, path: str, param: str) -> Any:
        return _py(self._call("get_param", path, param, nargout=1))

    def model_set(self, path: str, param: str, value: Any) -> None:
        self._call("set_param", path, param, value if isinstance(value, str) else repr(value), nargout=0)

    def model_file(self, model: str) -> str | None:
        f = self._call("get_param", model, "FileName", nargout=1)
        return str(f) or None

    def model_save(self, model: str) -> None:
        self._call("save_system", model, nargout=0)

    def save_logs(self, file: Path, model: str, logs: str, fmt: str, base: bool, model_ws: bool,
                  max_bytes: float, helper_dir: Path, timeout: float) -> dict:
        """Write Simulink's logged data and the workspaces to a MAT file; returns what was saved."""
        import json

        from .matlab_code import SAVE_LOGS, SAVE_LOGS_NAME

        helper_dir.mkdir(parents=True, exist_ok=True)
        source = helper_dir / f"{SAVE_LOGS_NAME}.m"
        if not source.is_file() or source.read_text(encoding="utf-8") != SAVE_LOGS:
            source.write_text(SAVE_LOGS, encoding="utf-8", newline="\n")
        self._call("addpath", str(helper_dir), nargout=0)
        try:
            out = self._call(SAVE_LOGS_NAME, str(file), model, logs, fmt, bool(base), bool(model_ws),
                             float(max_bytes), nargout=1, timeout=timeout)
        finally:
            try:
                self._call("rmpath", str(helper_dir), nargout=0)
            except BackendError:
                pass
        return json.loads(str(out))

    def model_reload(self, model: str, file: str) -> None:
        self._call("close_system", model, 0, nargout=0)
        self._call("load_system", file, nargout=0)

    def model_struct(self, op: str, **kw: Any) -> None:
        if op == "add_block":
            args = [kw["src"], kw["dest"]]
            for k, v in (kw.get("params") or {}).items():
                args += [k, v if isinstance(v, str) else repr(v)]
            self._call("add_block", *args, nargout=1)
        elif op == "add_line":
            self._call("add_line", kw["system"], kw["out"], kw["inp"], "autorouting", "on", nargout=1)
        elif op == "delete_block":
            self._call("delete_block", kw["path"], nargout=0)
        else:
            raise NotSupported(f"unknown structural operation {op!r}")


def _py(v: Any) -> Any:
    """Convert MATLAB engine values to plain JSON-friendly Python."""
    try:
        import matlab  # type: ignore
        if isinstance(v, (matlab.double, matlab.single, matlab.int8, matlab.int16, matlab.int32,
                          matlab.int64, matlab.uint8, matlab.uint16, matlab.uint32, matlab.uint64,
                          matlab.logical)):
            rows = [list(r) for r in v]
            if len(rows) == 1:
                rows = rows[0]
                return rows[0] if len(rows) == 1 else rows
            return rows
    except ImportError:
        pass
    if isinstance(v, dict):
        return {k: _py(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_py(x) for x in v]
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _to_matlab(v: Any) -> Any:
    import matlab  # type: ignore
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        if v and isinstance(v[0], (list, tuple)):
            return matlab.double([[float(x) for x in r] for r in v])
        return matlab.double([[float(x) for x in v]])
    if isinstance(v, dict):
        return {k: _to_matlab(x) for k, x in v.items()}
    return v
