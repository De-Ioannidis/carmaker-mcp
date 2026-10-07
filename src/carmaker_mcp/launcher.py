"""Cold start of a CarMaker for Simulink session.

Brings up whatever is missing, in this order: a MATLAB desktop that shares its engine, the CarMaker
search path (``cmenv``), the Simulink model, the CarMaker GUI. Each step is skipped when it is already
done, so the function can be called again at any time; it never closes anything.

MATLAB is started as a normal, visible desktop process that keeps running when the server exits.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backend import BackendError, BackendTimeout
from .config import Config
from .doctor import matlab_installs, release_name, shared_sessions
from .standalone import find_cm_home

# How long a MATLAB that we started may take to share its engine before we would start another one.
_STARTUP_GRACE_S = 600


def matlab_exe(cfg: Config) -> Path:
    """The MATLAB to start: CM_MATLAB_EXE, else the newest installed release this CarMaker supports."""
    if cfg.matlab_exe:
        if not Path(cfg.matlab_exe).exists():
            raise BackendError(f"CM_MATLAB_EXE does not exist: {cfg.matlab_exe}")
        return Path(cfg.matlab_exe)
    installs = {release_name(v): root for v, root in matlab_installs().items()}
    try:
        supported = {p.name for p in (find_cm_home(cfg.cm_home) / "Matlab").glob("R20*")}
    except BackendError:
        supported = set()
    usable = sorted(r for r in installs if not supported or r in supported)
    if not usable:
        raise BackendError("no usable MATLAB installation found; set CM_MATLAB_EXE to matlab.exe")
    exe = Path(installs[usable[-1]]) / "bin" / ("matlab.exe" if sys.platform == "win32" else "matlab")
    if not exe.exists():
        raise BackendError(f"MATLAB executable not found: {exe}")
    return exe


def matlab_dir(cfg: Config) -> Path:
    """Folder MATLAB starts in: CM_MATLAB_DIR, else <project>/src_cm4sl (where cmenv.m lives)."""
    if cfg.matlab_dir:
        return Path(cfg.matlab_dir)
    if cfg.project is None:
        raise BackendError("set CM_PROJECT (or CM_MATLAB_DIR) so that MATLAB can be started in the project")
    return Path(cfg.project) / "src_cm4sl"


def find_models(workdir: Path, limit: int = 20) -> list[str]:
    """Simulink models below the MATLAB start folder, most recently changed first, as paths relative
    to it. CarMaker does not record which model belongs to a test run, so this is what there is to
    choose from."""
    found = [p for pat in ("*.slx", "*.mdl") for p in workdir.rglob(pat) if "slprj" not in p.parts]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.relative_to(workdir).as_posix() for p in found[:limit]]


def find_init_scripts(workdir: Path) -> list[str]:
    """MATLAB scripts directly in the start folder (cmenv.m aside): candidates for a project's own
    setup script."""
    return sorted(p.name for p in workdir.glob("*.m") if p.name.lower() != "cmenv.m")


def _loaded_models(backend: Any) -> list[str]:
    try:
        out = backend.matlab("find_system('SearchDepth', 0, 'BlockDiagramType', 'model')")
    except BackendError:
        return []
    return [str(m) for m in out] if isinstance(out, (list, tuple)) else ([str(out)] if out else [])


def _q(s: str | Path) -> str:
    """A MATLAB single-quoted string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def startup_code(workdir: Path, session: str) -> str:
    return (f"try, cd({_q(workdir)}); cmenv; catch cm_mcp_err, disp(cm_mcp_err.message); end; "
            f"clear cm_mcp_err; matlab.engine.shareEngine({_q(session)});")


def start_matlab(exe: Path, workdir: Path, session: str) -> int:
    """Start a MATLAB desktop that changes to ``workdir``, runs cmenv and shares its engine."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([str(exe), "-r", startup_code(workdir, session)], cwd=str(workdir),
                            creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)
    return proc.pid


class Launcher:
    """State of a cold start across calls (so that a second call does not start a second MATLAB)."""

    def __init__(self, find: Callable[[], list[str] | None] = shared_sessions,
                 start: Callable[[Path, Path, str], int] = start_matlab,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self._find, self._start, self._sleep, self._clock = find, start, sleep, clock
        self._started_at: float | None = None

    def ensure(self, backend: Any, cfg: Config, model: str | None = None, max_wait_s: float = 45.0,
               init: str | None = None) -> dict:
        """Do the missing steps, waiting at most ``max_wait_s`` in total. Returns ``ready`` plus what
        was done; when not ready yet, ``waiting_for`` says what for and the call can be repeated."""
        t0 = self._clock()
        steps: dict[str, Any] = {}
        model = model or cfg.model
        init = init or cfg.matlab_init

        def left() -> float:
            return max_wait_s - (self._clock() - t0)

        extra: dict[str, Any] = {}  # what there is to choose from when no model was named

        def not_ready(what: str) -> dict:
            return {"ready": False, "waiting_for": what, "steps": steps, **extra,
                    "note": "call again to keep waiting"}

        # 1. a MATLAB that shares its engine
        sessions = self._find()
        if sessions is None:
            raise BackendError("matlabengine is not installed; see `carmaker-mcp doctor`")
        if cfg.matlab_session in sessions:
            steps["matlab"] = "shared session found"
        else:
            if self._started_at is None or self._clock() - self._started_at > _STARTUP_GRACE_S:
                workdir = matlab_dir(cfg)
                if not workdir.is_dir():
                    raise BackendError(f"MATLAB start folder does not exist: {workdir}")
                pid = self._start(matlab_exe(cfg), workdir, cfg.matlab_session)
                self._started_at = self._clock()
                steps["matlab"] = f"started (pid {pid})"
            else:
                steps["matlab"] = "starting"
            while cfg.matlab_session not in (self._find() or []):
                if left() <= 0:
                    return not_ready("MATLAB to start and share its engine")
                self._sleep(2.0)
        self._started_at = None

        # 2. CarMaker for Simulink on the MATLAB path
        if float(backend.matlab("exist('cmguicmd')")) > 0:
            steps["cmenv"] = "already set up"
        else:
            workdir = matlab_dir(cfg)
            backend.matlab(f"cd({_q(workdir)}); cmenv", nargout=0, timeout=60)
            steps["cmenv"] = f"ran cmenv in {workdir}"

        # 2b. the project's own setup script, once per MATLAB session (it may add folders to the path
        # that the model needs to compile, and often opens the model and the GUI itself)
        if init:
            script = Path(init) if Path(init).is_absolute() else matlab_dir(cfg) / init
            if not script.is_file():
                raise BackendError(f"MATLAB init script not found: {script}")
            if float(backend.matlab("exist('cm_mcp_init', 'var')")) > 0:
                steps["init"] = f"{script.name} already run in this MATLAB session"
            else:
                try:  # mark first: if the script outlasts this call it must not be started twice
                    backend.matlab(f"cm_mcp_init = {_q(script)}; run(cm_mcp_init);", nargout=0,
                                   timeout=max(left(), 30))
                except BackendTimeout:
                    steps["init"] = f"running {script.name}"
                    return not_ready(f"MATLAB to finish {script.name}")
                steps["init"] = f"ran {script.name}"
        else:
            try:
                found = find_init_scripts(matlab_dir(cfg))
            except (BackendError, OSError):
                found = []
            if found:
                extra["init_scripts_found"] = found
                extra["init_note"] = ("if the project has its own setup script, pass it as init= (or set "
                                      "CM_MATLAB_INIT): a model may not compile without it")

        # 3. the Simulink model
        if model:
            name = Path(model).stem
            if bool(backend.matlab(f"bdIsLoaded({_q(name)})")):
                steps["model"] = f"{name} already loaded"
            else:
                if left() < 5:
                    return not_ready("time to open the Simulink model")
                target = model
                try:  # a path relative to the start folder, as models_found lists them
                    if (matlab_dir(cfg) / model).is_file():
                        target = str(matlab_dir(cfg) / model)
                except BackendError:
                    pass
                try:
                    backend.matlab(f"open_system({_q(target)})", nargout=0, timeout=max(left(), 30))
                except BackendTimeout:  # a large model; MATLAB keeps loading it
                    steps["model"] = f"opening {name}"
                    return not_ready(f"MATLAB to finish opening {name}")
                steps["model"] = f"opened {name}"
        else:
            steps["model"] = "none requested"
            extra["models_loaded"] = _loaded_models(backend)
            try:
                extra["models_found"] = find_models(matlab_dir(cfg))
            except (BackendError, OSError):
                extra["models_found"] = []
            if not extra["models_loaded"]:
                extra["model_note"] = ("no Simulink model is loaded: call again with model=<one of "
                                       "models_found>, or ask the user which one")

        # 4. the CarMaker GUI
        if backend.gui_tcl("GetSimStatus", 3000)[0] == 0:
            steps["gui"] = steps.get("gui", "already open")
            return {"ready": True, "steps": steps, **extra}
        try:
            backend.matlab("CM_Simulink", nargout=0, timeout=max(left(), 20))
            steps["gui"] = "opened with CM_Simulink"
        except BackendError as e:  # may only mean that MATLAB is still busy starting the GUI
            steps["gui"] = f"CM_Simulink sent ({e})"
        while backend.gui_tcl("GetSimStatus", 3000)[0] != 0:
            if left() <= 0:
                return not_ready("the CarMaker GUI to connect to MATLAB")
            self._sleep(1.0)
        return {"ready": True, "steps": steps, **extra}
