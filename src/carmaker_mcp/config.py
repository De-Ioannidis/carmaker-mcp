"""Configuration from environment variables. Nothing project-specific is hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    return Path(base) / "carmaker-mcp"


@dataclass
class Config:
    # CarMaker project directory. If None, it is read from the running GUI.
    project: Path | None = None
    # Name under which MATLAB shares its engine (matlab.engine.shareEngine(name)).
    matlab_session: str = "cm_mcp"
    # Backups, change log and other server state. Never inside the CarMaker project.
    state_dir: Path = field(default_factory=_default_state_dir)
    # Extra folders searched for .erg result files (relative paths are project-relative).
    result_dirs: list[str] = field(default_factory=list)
    # Seconds before an engine call is abandoned (MATLAB can be busy during a simulation).
    engine_timeout: float = 30.0
    # CarMaker install folder (for standalone instances). Auto-detected when None.
    cm_home: Path | None = None
    # Seconds after which GUI pop-ups raised by our commands answer themselves with their default
    # choice (0 = never shown). None leaves the GUI's setting alone (normally: wait for the user).
    popup_timeout: float | None = None
    # Cold start (cm_session_start): MATLAB executable, the folder MATLAB starts in (default
    # <project>/src_cm4sl) and the Simulink model to open. All optional.
    matlab_exe: Path | None = None
    matlab_dir: Path | None = None
    model: str | None = None
    # MATLAB script a project uses to set itself up (paths, variables, opening the model); run once
    # per MATLAB session by cm_session_start. Relative to the MATLAB start folder.
    matlab_init: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        # Empty values count as "not set", and so do placeholders a host left unfilled ("${...}").
        env = {k: v for k, v in os.environ.items() if v.strip() and not v.startswith("${")}
        cfg = cls()
        if env.get("CM_PROJECT"):
            cfg.project = Path(env["CM_PROJECT"])
        if env.get("CM_MATLAB_SESSION"):
            cfg.matlab_session = env["CM_MATLAB_SESSION"]
        if env.get("CM_STATE_DIR"):
            cfg.state_dir = Path(env["CM_STATE_DIR"])
        if env.get("CM_RESULT_DIRS"):
            cfg.result_dirs = [p for p in env["CM_RESULT_DIRS"].split(os.pathsep) if p]
        if env.get("CM_HOME"):
            cfg.cm_home = Path(env["CM_HOME"])
        if env.get("CM_ENGINE_TIMEOUT"):
            cfg.engine_timeout = float(env["CM_ENGINE_TIMEOUT"])
        if env.get("CM_POPUP_TIMEOUT"):
            cfg.popup_timeout = float(env["CM_POPUP_TIMEOUT"])
        if env.get("CM_MATLAB_EXE"):
            cfg.matlab_exe = Path(env["CM_MATLAB_EXE"])
        if env.get("CM_MATLAB_DIR"):
            cfg.matlab_dir = Path(env["CM_MATLAB_DIR"])
        if env.get("CM_MODEL"):
            cfg.model = env["CM_MODEL"]
        if env.get("CM_MATLAB_INIT"):
            cfg.matlab_init = env["CM_MATLAB_INIT"]
        return cfg
