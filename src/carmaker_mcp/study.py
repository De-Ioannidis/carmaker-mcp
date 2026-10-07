"""Parameter studies: run one test run several times with different values, one after another.

A study runs on a background thread, so the tool call that starts it returns at once and the agent
polls. Nothing is written to project files: infofile keys are applied in memory (ScriptControl
``KeyValue`` in the MATLAB-connected session, cmapi overrides for standalone runs) and MATLAB
workspace values are put back after each run.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backend import BackendError
from .results import Erg, ErgError
from .session import Session

_KEY = re.compile(r"^[A-Za-z0-9_]+(:[A-Za-z0-9_.]+)?(\.[A-Za-z0-9_]+)*$")
_ALLOWED = {"label", "keys", "workspace"}
DEFAULT_QUANTITIES = ["Time", "Car.v"]


def check_variations(variations: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    out = []
    for i, v in enumerate(variations):
        if not isinstance(v, dict) or set(v) - _ALLOWED:
            raise BackendError(f"variation {i + 1}: use only the fields {sorted(_ALLOWED)}")
        keys, ws = v.get("keys") or {}, v.get("workspace") or {}
        if not isinstance(keys, dict) or not isinstance(ws, dict):
            raise BackendError(f"variation {i + 1}: 'keys' and 'workspace' must be objects")
        for k in keys:
            if not _KEY.match(str(k)):
                raise BackendError(f"variation {i + 1}: invalid infofile key {k!r}")
        if ws and mode == "standalone":
            raise BackendError("standalone runs have no MATLAB workspace: use 'keys' only, or mode 'cm4sl'")
        out.append({"label": str(v.get("label") or f"run {i + 1}"), "keys": dict(keys),
                    "workspace": dict(ws)})
    return out


class StudyRunner:
    def __init__(self, session: Session, standalone: Callable[[], Any]):
        self.session = session
        self._standalone = standalone
        self._studies: dict[str, dict] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._n = 0

    # ---- public --------------------------------------------------------------------
    def start(self, testrun: str, variations: list[dict[str, Any]], quantities: list[str] | None = None,
              mode: str = "cm4sl", scope: str = "base", max_run_s: float = 900.0,
              save_logs: bool = False) -> dict:
        if mode not in ("cm4sl", "standalone"):
            raise BackendError("mode must be 'cm4sl' or 'standalone'")
        if save_logs and mode != "cm4sl":
            raise BackendError("save_logs needs the MATLAB-connected session (mode 'cm4sl')")
        runs = check_variations(variations, mode)
        with self._lock:
            active = [s["id"] for s in self._studies.values() if s["state"] == "running"]
            if active:
                raise BackendError(f"study {active[0]} is still running (cm_study_status, cm_study_cancel)")
            if mode == "cm4sl":
                self.session.require_idle("start a study")
            self._n += 1
            sid = f"{self.session.store.session}-s{self._n}"
            st = {
                "id": sid, "state": "running", "testrun": testrun, "mode": mode, "scope": scope,
                "quantities": list(quantities or DEFAULT_QUANTITIES), "max_run_s": max_run_s,
                "save_logs": bool(save_logs),
                "started": time.strftime("%Y-%m-%d %H:%M:%S"), "total": len(runs), "current": 0,
                "current_label": None, "rows": [], "variations": runs,
            }
            self._studies[sid] = st
            self._cancel[sid] = threading.Event()
            self._save(st)
            t = threading.Thread(target=self._work, args=(st,), name=f"study-{sid}", daemon=True)
            self._threads[sid] = t
            t.start()
        return {"study": sid, "runs": len(runs), "state": "running",
                "note": "poll cm_study_status until state is no longer 'running'"}

    def status(self, study: str | None = None) -> dict:
        st = self._get(study)
        out = {k: v for k, v in st.items() if k != "variations"}
        out["done"] = len(st["rows"])
        return out

    def cancel(self, study: str | None = None) -> dict:
        st = self._get(study)
        if st["state"] != "running":
            return {"study": st["id"], "state": st["state"], "note": "not running"}
        self._cancel[st["id"]].set()
        self._threads[st["id"]].join(90)
        return {"study": st["id"], "state": st["state"], "done": len(st["rows"]), "total": st["total"]}

    def wait(self, study: str | None = None, timeout: float = 60.0) -> dict:
        """Block until the study has ended (tests and scripts; tools poll instead)."""
        st = self._get(study)
        self._threads[st["id"]].join(timeout)
        return self.status(st["id"])

    # ---- internals -------------------------------------------------------------------
    def _get(self, study: str | None) -> dict:
        if not self._studies:
            raise BackendError("no study has been started in this session")
        if study is None:
            return list(self._studies.values())[-1]
        if study not in self._studies:
            raise BackendError(f"unknown study {study!r}; known: {', '.join(self._studies)}")
        return self._studies[study]

    def _save(self, st: dict) -> None:
        try:
            d = Path(self.session.store.dir) / "studies"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{st['id']}.json").write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        except OSError:
            pass

    def _work(self, st: dict) -> None:
        cancel = self._cancel[st["id"]]
        one = self._one_cm4sl if st["mode"] == "cm4sl" else self._one_standalone
        if st["mode"] == "cm4sl":
            self.session.study_owner = threading.get_ident()
        try:
            for i, var in enumerate(st["variations"]):
                if cancel.is_set():
                    break
                st["current"], st["current_label"] = i + 1, var["label"]
                st["rows"].append(one(st, var, cancel))
                self._save(st)
            st["state"] = "cancelled" if cancel.is_set() else "done"
        except Exception as e:  # a bug must not leave the study 'running' for ever
            st["state"], st["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            self.session.study_owner = None
            st["current_label"] = None
            st["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save(st)

    def _summarise(self, row: dict, st: dict) -> None:
        if not row.get("result_file"):
            return
        try:
            erg = Erg(Path(row["result_file"]))
            names = [q for q in st["quantities"] if q in erg.names]
            row["quantities"] = erg.summary(names)["quantities"]
            missing = [q for q in st["quantities"] if q not in erg.names]
            if missing:
                row["missing_quantities"] = missing
        except (ErgError, OSError) as e:
            row["result_error"] = str(e)

    def _one_cm4sl(self, st: dict, var: dict, cancel: threading.Event) -> dict:
        s = self.session
        row: dict[str, Any] = {"label": var["label"], "keys": var["keys"], "workspace": var["workspace"]}
        changed: list[tuple[str, Any, bool]] = []
        try:
            for name, value in var["workspace"].items():
                r = s.workspace_set(name, value, st["scope"])
                changed.append((name, r["old"], r["existed"]))
            s.load_testrun(st["testrun"])
            s.key_values(var["keys"])
            # The logs are saved when the end of the run is reported, that is before the workspace
            # values are put back below: the file holds the values this run used.
            s.start_sim(start_timeout_s=min(st["max_run_s"], 300), save_logs=st.get("save_logs", False))
            t0 = time.time()
            while True:
                end = s.wait_end(timeout_s=2, poll_s=0.5)
                if end["finished"]:
                    break
                if cancel.is_set() or time.time() - t0 > st["max_run_s"]:
                    row["stopped"] = "cancelled" if cancel.is_set() else f"max_run_s ({st['max_run_s']:g} s)"
                    end = s.stop_sim(wait_s=60)
                    break
            for k in ("end_status", "sim_time_s", "distance_m", "result_file", "logs_file", "logs_error",
                      "log_errors", "popups"):
                if end.get(k) is not None:
                    row[k] = end[k]
            self._summarise(row, st)
        except BackendError as e:
            row["error"] = str(e)
        finally:
            problems = []
            for name, old, existed in reversed(changed):
                try:
                    if existed:
                        s.workspace_set(name, old, st["scope"])
                    else:
                        s.workspace_clear(name, st["scope"])
                except BackendError as e:
                    problems.append(f"{name}: {e}")
            if var["keys"]:
                try:
                    s.key_values({})
                except BackendError as e:
                    problems.append(f"KeyValue reset: {e}")
            if problems:
                row["restore_errors"] = problems
        return row

    def _one_standalone(self, st: dict, var: dict, cancel: threading.Event) -> dict:
        sa = self._standalone()
        row: dict[str, Any] = {"label": var["label"], "keys": var["keys"], "workspace": {}}
        inst = None
        try:
            store = sorted({"Time", "Car.v", "Car.Distance", *st["quantities"]})
            inst = sa.launch(st["testrun"], overrides=var["keys"] or None, quantities=store)["instance"]
            t0 = time.time()
            while True:
                end = sa.wait_end(inst, 2)
                if end["finished"]:
                    break
                if cancel.is_set() or time.time() - t0 > st["max_run_s"]:
                    row["stopped"] = "cancelled" if cancel.is_set() else f"max_run_s ({st['max_run_s']:g} s)"
                    sa.control("stop", inst)
                    end = sa.wait_end(inst, 60)
                    break
            row["end_status"] = "completed" if end.get("error_flag") in (0, None) and not row.get("stopped") \
                else "aborted"
            for src, dst in (("sim_time_s", "sim_time_s"), ("sim_dist_m", "distance_m"),
                             ("error_flag", "error_flag"), ("stop_reason", "stop_reason")):
                if end.get(src) is not None:
                    row[dst] = end[src]
            if end.get("log_errors"):
                row["log_errors"] = end["log_errors"]
            files = sa.results(inst).get("result_files") or []
            row["result_file"] = files[-1] if files else None
            self._summarise(row, st)
        except BackendError as e:
            row["error"] = str(e)
        finally:
            if inst is not None:
                try:
                    sa.close(inst)
                except BackendError as e:
                    row["restore_errors"] = [f"close: {e}"]
        return row
