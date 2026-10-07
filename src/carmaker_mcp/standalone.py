"""Independent (standalone) CarMaker instances, not connected to MATLAB.

Uses IPG's own Python API (``cmapi``, shipped with CarMaker) to start a stock ``CarMaker.win64``
simulation program as a separate process, run a test run on it and control it over APO
(pause, DVA, live quantities). It can also attach to a CarMaker program that a GUI started.

``cmapi`` is IPG's code and is imported from your CarMaker install (``CM_HOME``); it is not
bundled here. cmapi is asyncio based, so everything runs on one background event loop and the
public methods are plain blocking calls.

Test-run parameters can be overridden *in memory* (``overrides``); no project file is changed.
"""

from __future__ import annotations

import asyncio
import glob
import inspect
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import log as sessionlog
from .backend import BackendError

DEFAULT_QUANTITIES = ["Time", "Car.v", "Car.YawRate", "Car.ax", "Car.ay", "Car.Distance"]


def find_cm_home(explicit: str | Path | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise BackendError(f"CM_HOME does not exist: {p}")
    cands = []
    for pat in (r"C:\IPG\carmaker\win64-*", "/opt/ipg/carmaker/linux64-*"):
        cands += [Path(x) for x in glob.glob(pat)]

    def ver(p: Path):
        m = re.search(r"-(\d+(?:\.\d+)*)$", p.name)
        return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)

    if not cands:
        raise BackendError("CarMaker install not found; set CM_HOME to its folder")
    return sorted(cands, key=ver)[-1]


def import_cmapi(cm_home: Path):
    pydir = cm_home / "Python" / f"python3.{sys.version_info.minor}"
    if not pydir.exists():
        have = sorted(p.name for p in (cm_home / "Python").glob("python3.*"))
        raise BackendError(
            f"CarMaker ships no cmapi for Python 3.{sys.version_info.minor} (available: {have}). "
            "Run this server with one of those Python versions."
        )
    if str(pydir) not in sys.path:
        sys.path.insert(0, str(pydir))
    try:
        import cmapi  # type: ignore
    except ImportError as e:
        raise BackendError(f"cannot import cmapi from {pydir}: {e}") from e
    return cmapi


async def _maybe(x):
    return await x if inspect.isawaitable(x) else x


def _launch_failure(error: Exception, log_lines: list[str]) -> str:
    """Why a launched CarMaker program could not be used, from its own log if it wrote one."""
    errs = [" ".join(ln.split()) for ln in sessionlog.filter_level(log_lines, "error")]
    errs = [e for e in errs if "FATAL ERROR - Application stopped" not in e]
    if not errs:
        return f"CarMaker did not start: {type(error).__name__}: {str(error).splitlines()[0][:300]}"
    msg = "CarMaker started but stopped with an error: " + " | ".join(errs[:3])
    if any("license" in e.lower() for e in errs):
        msg += (". Another CarMaker program is holding the licence, for example the CarMaker for Simulink "
                "engine of an open MATLAB (closing the CarMaker GUI window does not release it): close "
                "that model or MATLAB, or use the MATLAB-connected session instead")
    return msg


def _server_spec(info) -> dict:
    """Fields of a cmapi ``ApoServerInfo`` (pid, identity, description...). cmapi keeps them in a
    ``spec`` dict, not as attributes."""
    spec = getattr(info, "spec", None)
    return dict(spec) if isinstance(spec, dict) else {}


@dataclass
class Instance:
    id: str
    kind: str  # "launched" or "attached"
    sc: Any
    master: Any
    var: Any
    pid: int | None
    testrun: str | None
    started_at: float = field(default_factory=time.time)
    stop_after_s: float | None = None
    finished: Any = None  # asyncio.Event set by the watcher
    stop_reason: str | None = None


class StandaloneManager:
    def __init__(self, cm_home: str | Path | None = None, project: Path | None = None):
        self.cm_home = find_cm_home(cm_home)
        self.project = project
        self.instances: dict[str, Instance] = {}
        self._cmapi: Any = None  # IPG's cmapi module once imported
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._n = 0

    # ---- event loop plumbing ------------------------------------------------
    def _start(self):
        if self._thread:
            return
        self._cmapi = import_cmapi(self.cm_home)

        def run():
            async def serve():
                self._loop = asyncio.get_running_loop()
                self._ready.set()
                await asyncio.Event().wait()

            self._cmapi.Task.run_main_task(serve())

        self._thread = threading.Thread(target=run, name="cmapi-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(15):
            raise BackendError("cmapi event loop did not start")

    def _call(self, coro_fn, timeout: float = 120.0):
        self._start()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(timeout)
        except TimeoutError as e:
            fut.cancel()
            raise BackendError(f"CarMaker did not answer within {timeout:.0f}s") from e
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"{type(e).__name__}: {e}") from e

    def _inst(self, instance: str | None) -> Instance:
        if instance is None:
            if len(self.instances) == 1:
                return next(iter(self.instances.values()))
            raise BackendError(f"specify instance; known: {list(self.instances)}")
        if instance not in self.instances:
            raise BackendError(f"unknown instance {instance!r}; known: {list(self.instances)}")
        return self.instances[instance]

    def _project_path(self, project: str | Path | None) -> Path:
        p = Path(project) if project else self.project
        if p is None:
            raise BackendError("no project directory: set CM_PROJECT or pass project")
        return p

    # ---- discovery ------------------------------------------------------------
    def servers(self) -> list[dict]:
        self._start()
        out = []
        for s in self._cmapi.query_aposerverinfos("localhost"):
            spec = _server_spec(s)
            pid = spec.get("pid")
            out.append({
                "pid": pid, "identity": spec.get("identity"), "description": spec.get("description"),
                "appclass": spec.get("appclass"), "host": spec.get("hostname"),
                "managed_by_us": any(i.pid == pid for i in self.instances.values()),
            })
        return out

    # ---- launch / attach ------------------------------------------------------
    def launch(
        self, testrun: str, project: str | Path | None = None, executable: str | Path | None = None,
        overrides: dict[str, Any] | None = None, quantities: list[str] | None = None,
        realtime_factor: float = 1.0, stop_after_s: float | None = 600.0, start: bool = True,
    ) -> dict:
        proj = self._project_path(project)
        exe = Path(executable) if executable else self.cm_home / "bin" / "CarMaker.win64.exe"
        if not exe.exists():
            raise BackendError(f"CarMaker executable not found: {exe}")
        self._n += 1
        iid = f"sa{self._n}"

        async def go():
            cm = self._cmapi
            cm.Project.load(proj)
            tr = cm.Project.instance().load_testrun_parametrization(Path(testrun))
            for k, v in (overrides or {}).items():
                tr.set_parameter_value(k, str(v))
            var = cm.Variation.create_from_testrun(tr)
            var.set_storage_mode(cm.StorageMode.save)
            oq = cm.OutputQuantities()
            oq.add_quantities(quantities or DEFAULT_QUANTITIES)
            var.set_outputquantities(oq)
            var.set_initial_realtimefactor(float(realtime_factor))
            master = cm.CarMaker()
            master.set_executable_path(exe)
            sc = cm.SimControlInteractive()
            sc.set_variation(var)
            await sc.set_master(master)
            mark = sessionlog.mark(proj)
            try:
                await sc.start_and_connect()
            except Exception as e:
                # The program may have come up and stopped with an error (no licence, bad project):
                # do not leave it behind, and say what its log says.
                await self._abandon(sc, master)
                raise BackendError(_launch_failure(e, sessionlog.since(proj, mark))) from e
            inst = Instance(iid, "launched", sc, master, var, master.get_pid(), testrun,
                            stop_after_s=stop_after_s, finished=asyncio.Event())
            self.instances[iid] = inst
            self._watch(inst)
            if start:
                await sc.start_sim()
                self._arm_stop(inst)
            return inst

        inst = self._call(go, 120)
        return {"instance": inst.id, "pid": inst.pid, "testrun": testrun, "project": str(proj),
                "executable": str(exe), "started": start, "stop_after_s": stop_after_s,
                "overrides_in_memory": overrides or {}}

    def attach(self, pid: int, testrun: str | None = None, project: str | Path | None = None,
               overrides: dict[str, Any] | None = None, quantities: list[str] | None = None,
               realtime_factor: float = 1.0, stop_after_s: float | None = 600.0,
               start: bool = False) -> dict:
        """Attach to a CarMaker simulation program that is already running (for example one a
        CarMaker Office window started). If ``testrun`` is given the run can be started."""
        self._n += 1
        iid = f"sa{self._n}"

        async def go():
            cm = self._cmapi
            var = None
            if testrun:
                cm.Project.load(self._project_path(project))
                tr = cm.Project.instance().load_testrun_parametrization(Path(testrun))
                for k, v in (overrides or {}).items():
                    tr.set_parameter_value(k, str(v))
                var = cm.Variation.create_from_testrun(tr)
                var.set_storage_mode(cm.StorageMode.save)
                oq = cm.OutputQuantities()
                oq.add_quantities(quantities or DEFAULT_QUANTITIES)
                var.set_outputquantities(oq)
                var.set_initial_realtimefactor(float(realtime_factor))
            master = cm.ApoServer()
            master.set_sinfo(cm.ApoServerInfo(pid=int(pid)))
            master.set_host("localhost")
            sc = await cm.SimControlInteractive.create_with_master(master)
            if var is not None:
                sc.set_variation(var)
            await sc.connect()
            inst = Instance(iid, "attached", sc, master, var, int(pid), testrun,
                            stop_after_s=stop_after_s, finished=asyncio.Event())
            self.instances[iid] = inst
            self._watch(inst)
            if start and var is not None:
                await sc.start_sim()
                self._arm_stop(inst)
            return inst

        inst = self._call(go, 60)
        return {"instance": inst.id, "pid": inst.pid, "attached": True, "testrun": testrun,
                "started": bool(start and testrun)}

    async def _abandon(self, sc, master) -> None:
        """Stop a program that we started but could not connect to."""
        try:
            await asyncio.wait_for(_maybe(sc.stop_and_disconnect()), 10)
        except Exception:
            pass
        try:
            pid = master.get_pid()
        except Exception:
            pid = None
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)  # no-op if it is already gone
            except (OSError, ValueError):
                pass

    # ---- background helpers (run on the cmapi loop) ---------------------------
    def _watch(self, inst: Instance):
        async def w():
            try:
                await inst.sc.create_simstate_condition(self._cmapi.ConditionSimState.finished).wait()
            finally:
                inst.finished.set()

        asyncio.ensure_future(w())

    def _arm_stop(self, inst: Instance):
        if not inst.stop_after_s:
            return

        async def s():
            cond = inst.sc.create_quantity_condition(lambda t: t > inst.stop_after_s, "Time")
            await cond.wait()
            if not inst.finished.is_set():
                inst.stop_reason = f"stopped by server at Time > {inst.stop_after_s:g} s"
                await _maybe(inst.sc.stop_sim())

        asyncio.ensure_future(s())

    # ---- operations -----------------------------------------------------------
    def status(self, instance: str | None = None, quantities: list[str] | None = None) -> dict:
        inst = self._inst(instance)
        names = list(quantities or ["Time", "Car.v"])

        async def go():
            out: dict[str, Any] = {
                "instance": inst.id, "pid": inst.pid, "kind": inst.kind,
                "finished": inst.finished.is_set(), "stop_reason": inst.stop_reason,
                "age_s": round(time.time() - inst.started_at, 1),
            }
            try:
                vals = await asyncio.wait_for(inst.sc.simio.dva_read_async(*names), 5)
                out["live"] = dict(zip(names, [float(v) for v in vals], strict=False))
            except Exception as e:
                out["live_error"] = f"{type(e).__name__}: {e}"
            return out

        return self._call(go, 20)

    def control(self, action: str, instance: str | None = None) -> dict:
        inst = self._inst(instance)
        if action not in ("start", "pause", "resume", "stop"):
            raise BackendError("action must be start, pause, resume or stop")

        async def go():
            sc = inst.sc
            if action == "start":
                if inst.var is None:
                    raise BackendError("no test run configured for this instance")
                await _maybe(sc.start_sim())
                self._arm_stop(inst)
            elif action == "pause":
                await _maybe(sc.pause_sim())
            elif action == "resume":
                await _maybe(sc.resume_sim())
            else:
                inst.stop_reason = inst.stop_reason or "stopped by request"
                await _maybe(sc.stop_sim())
            return {"instance": inst.id, "action": action, "ok": True}

        return self._call(go, 60)

    def wait_end(self, instance: str | None = None, timeout_s: float = 600.0) -> dict:
        inst = self._inst(instance)

        async def go():
            try:
                await asyncio.wait_for(inst.finished.wait(), timeout_s)
            except TimeoutError:
                return {"instance": inst.id, "finished": False, "timeout_s": timeout_s}
            info = inst.var.get_simend_info() if inst.var is not None else None
            return {
                "instance": inst.id, "finished": True, "stop_reason": inst.stop_reason,
                "sim_time_s": getattr(info, "sim_time", None), "sim_dist_m": getattr(info, "sim_dist", None),
                "error_flag": getattr(info, "error_flag", None),
                "user_stop": getattr(info, "user_stop", None),
                "log_errors": [
                    str(e).split("Message: ", 1)[-1][:200]
                    for e in inst.sc.sessionlog.get_entries_with_pattern("*ERROR*")[-10:]
                ],
            }

        return self._call(go, timeout_s + 15)

    def dva_write(self, name: str, value: float, duration_ms: int = -1, instance: str | None = None) -> dict:
        inst = self._inst(instance)

        async def go():
            await _maybe(inst.sc.simio.dva_write_absolute_value(name, float(value), int(duration_ms)))
            return {"instance": inst.id, "dva_written": name, "value": value}

        return self._call(go, 20)

    def results(self, instance: str | None = None) -> dict:
        inst = self._inst(instance)
        paths = [str(p) for p in inst.var.get_result_file_paths()] if inst.var is not None else []
        return {"instance": inst.id, "finished": inst.finished.is_set(), "result_files": paths}

    def close(self, instance: str | None = None) -> dict:
        inst = self._inst(instance)

        async def go():
            try:
                if inst.kind == "launched":
                    await inst.sc.stop_and_disconnect()
                else:
                    await inst.sc.disconnect()
            finally:
                self.instances.pop(inst.id, None)
            return {"instance": inst.id, "closed": True, "process_stopped": inst.kind == "launched"}

        return self._call(go, 60)

    def close_all(self) -> None:
        for iid in list(self.instances):
            try:
                self.close(iid)
            except BackendError:
                pass
