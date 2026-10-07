"""Setup check: finds CarMaker and MATLAB, says what does not fit and how to fix it, and prints the
``uvx`` arguments and the MCP client configuration for this machine.

Used by ``carmaker-mcp doctor``, ``carmaker-mcp config`` and the ``cm_doctor`` tool. Nothing here
changes the machine.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .backend import BackendError
from .config import Config
from .standalone import find_cm_home

CLIENTS = ("claude-code", "claude-desktop", "vscode", "codex", "antigravity", "cursor")


@dataclass
class Check:
    name: str
    ok: bool | None  # None: could not be checked / not applicable
    detail: str
    fix: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    python: str = ""  # e.g. "3.12"
    cm_home: str | None = None
    cmapi_pythons: list[str] = field(default_factory=list)  # e.g. ["3.10", "3.11", "3.12"]
    matlab_release: str | None = None  # the release to use, e.g. "R2024b"
    matlab_root: str | None = None
    engine_pin: str | None = None  # e.g. "matlabengine==24.2.*"
    recommended_python: str | None = None

    def add(self, name: str, ok: bool | None, detail: str, fix: str | None = None) -> None:
        self.checks.append(Check(name, ok, detail, None if ok else fix))

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


# ---- facts about the machine ---------------------------------------------------
# Version numbers follow the year only since R2023b (registry keys, matlabengine versions).
_OLD_VERSIONS = {"9.10": "R2021a", "9.11": "R2021b", "9.12": "R2022a", "9.13": "R2022b", "9.14": "R2023a"}
# Newest Python the engine package of a release installs on (metadata of matlabengine on PyPI).
ENGINE_MAX_PYTHON = {
    "R2021a": "3.8", "R2021b": "3.9", "R2022a": "3.9", "R2022b": "3.10", "R2023a": "3.10", "R2023b": "3.11",
    "R2024a": "3.11", "R2024b": "3.12", "R2025a": "3.12", "R2025b": "3.12", "R2026a": "3.13",
}
MIN_PYTHON = (3, 10)  # of this server


def _py(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split(".")[:2])


def release_name(version: str) -> str:
    """MATLAB version number to release name: '24.2' -> 'R2024b', '9.14' -> 'R2023a'."""
    major, minor = (int(x) for x in version.split(".")[:2])
    if f"{major}.{minor}" in _OLD_VERSIONS:
        return _OLD_VERSIONS[f"{major}.{minor}"]
    return f"R20{major:02d}{'a' if minor == 1 else 'b'}"


def engine_line(release: str) -> str:
    """Release name to the version line of its engine package: 'R2024b' -> '24.2', 'R2023a' -> '9.14'."""
    for version, name in _OLD_VERSIONS.items():
        if name == release:
            return version
    m = re.fullmatch(r"R20(\d\d)([ab])", release)
    if not m:
        raise ValueError(f"not a MATLAB release name: {release!r} (expected e.g. R2024b)")
    return f"{int(m[1])}.{1 if m[2] == 'a' else 2}"


def engine_python(release: str, available: list[str] | None = None) -> str | None:
    """Newest Python that both the release's engine package and this server run on, optionally among
    ``available`` versions. None if there is none (releases before R2022b)."""
    top = ENGINE_MAX_PYTHON.get(release)
    pool = available if available is not None else [f"3.{n}" for n in range(MIN_PYTHON[1], 15)]
    fit = [v for v in pool if _py(v) >= MIN_PYTHON and (top is None or _py(v) <= _py(top))]
    return max(fit, key=_py) if fit else None


def matlab_installs() -> dict[str, str]:
    """Installed MATLAB releases from the Windows registry: {'24.2': 'C:\\...\\R2024b'}."""
    try:
        import winreg
    except ImportError:  # not Windows
        return {}
    out: dict[str, str] = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\MathWorks\MATLAB", 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except OSError:
        return out
    i = 0
    while True:
        try:
            name = winreg.EnumKey(key, i)
        except OSError:
            break
        i += 1
        try:
            root, _ = winreg.QueryValueEx(winreg.OpenKey(key, name), "MATLABROOT")
        except OSError:
            continue
        if re.fullmatch(r"\d+\.\d+", name) and Path(root).exists():
            out[name] = str(root)
    return out


def cmapi_pythons(cm_home: Path) -> list[str]:
    """Python versions the CarMaker install ships cmapi for."""
    vers = [p.name.removeprefix("python") for p in (cm_home / "Python").glob("python3.*")]
    return sorted(vers, key=lambda v: tuple(int(x) for x in v.split(".")))


def cm_matlab_releases(cm_home: Path) -> list[str]:
    """MATLAB releases this CarMaker ships CarMaker for Simulink for (folders Matlab/R20xxx)."""
    return sorted(p.name for p in (cm_home / "Matlab").glob("R20*") if p.is_dir())


def shared_sessions() -> list[str] | None:
    """Names of shared MATLAB sessions; None if matlabengine is not installed."""
    try:
        import matlab.engine  # type: ignore
    except ImportError:
        return None
    try:
        return list(matlab.engine.find_matlab())
    except Exception:
        return []


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def engine_version() -> str | None:
    try:
        return importlib.metadata.version("matlabengine")
    except importlib.metadata.PackageNotFoundError:
        return None


# ---- the checks ------------------------------------------------------------------
_PROBE: Any = object()  # "ask the machine" (None is a valid answer for sessions and engine)


def diagnose(cfg: Config, installs: dict[str, str] | None = None, sessions: list[str] | None = _PROBE,
             engine: str | None = _PROBE, gui_status: Any = None) -> Report:
    """Collect all checks. The optional arguments replace the machine probes (for tests);
    ``gui_status`` is a callable returning a backend ``Status`` and is only used when the shared
    MATLAB session exists."""
    rep = Report(python=python_version())
    installs = matlab_installs() if installs is None else installs
    sessions = shared_sessions() if sessions is _PROBE else sessions
    engine = engine_version() if engine is _PROBE else engine

    # CarMaker install and its Python API
    cm_home = None
    try:
        cm_home = find_cm_home(cfg.cm_home)
        rep.cm_home = str(cm_home)
        rep.add("CarMaker install", True, str(cm_home))
    except BackendError as e:
        rep.add("CarMaker install", False, str(e),
                "set CM_HOME to the CarMaker folder, e.g. C:\\IPG\\carmaker\\win64-14.1.1")
    if cm_home is not None:
        rep.cmapi_pythons = cmapi_pythons(cm_home)
        usable = [v for v in rep.cmapi_pythons if tuple(int(x) for x in v.split(".")) >= (3, 10)]
        rep.recommended_python = usable[-1] if usable else None
        rep.add(
            "Python version", rep.python in rep.cmapi_pythons,
            f"running Python {rep.python}; CarMaker ships cmapi for {', '.join(rep.cmapi_pythons) or 'none'}",
            f"run the server with Python {rep.recommended_python} (uvx --python {rep.recommended_python} ...)"
            if rep.recommended_python else "this CarMaker has no cmapi for Python 3.10 or newer",
        )

    # MATLAB release that CarMaker for Simulink supports
    supported = cm_matlab_releases(cm_home) if cm_home is not None else []
    by_release = {release_name(v): (v, root) for v, root in installs.items()}
    usable_rel = sorted(r for r in by_release if not supported or r in supported)
    if usable_rel:
        rep.matlab_release = usable_rel[-1]
        version, rep.matlab_root = by_release[rep.matlab_release]
        others = sorted(set(by_release) - {rep.matlab_release})
        rep.add("MATLAB", True, f"{rep.matlab_release} at {rep.matlab_root}"
                + (f" (also installed, not used: {', '.join(others)})" if others else ""))
        # The engine package of a release only installs on some Python versions.
        top = ENGINE_MAX_PYTHON.get(rep.matlab_release)
        fit = engine_python(rep.matlab_release, rep.cmapi_pythons or None)
        if fit is None:
            rep.add("Python for the MATLAB engine", False,
                    f"the engine package of {rep.matlab_release} needs Python {top} or older, this server "
                    f"needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
                    "use a newer MATLAB release that this CarMaker supports; standalone runs work "
                    "without MATLAB")
        else:
            rep.engine_pin = f"matlabengine=={version}.*"
            rep.recommended_python = fit
            if top is not None and _py(rep.python) > _py(top):
                rep.add("Python for the MATLAB engine", False,
                        f"running Python {rep.python}; the engine package of {rep.matlab_release} "
                        f"installs on Python {top} at most",
                        f"run the server with Python {fit} (uvx --python {fit} ...)")
    elif installs:
        rep.add("MATLAB", False,
                f"installed: {', '.join(sorted(by_release))}; this CarMaker supports {', '.join(supported)}",
                "install a MATLAB release that this CarMaker version supports")
    else:
        rep.add("MATLAB", None, "no MATLAB found in the registry (fine for standalone instances and --mock)")

    # MATLAB engine for Python
    if rep.engine_pin:
        want = rep.engine_pin.split("==")[1].rstrip(".*")
        if engine is None:
            rep.add("matlabengine", False, "not installed in this environment",
                    f'add --with "{rep.engine_pin}" to the uvx arguments (or pip install "{rep.engine_pin}")')
        else:
            rep.add("matlabengine", engine.startswith(want + "."),
                    f"{engine} installed; {rep.matlab_release} needs {rep.engine_pin}",
                    f'use --with "{rep.engine_pin}"')

    # shared session and GUI
    if sessions is not None and rep.engine_pin:
        has = cfg.matlab_session in sessions
        rep.add("shared MATLAB session", has,
                f"{cfg.matlab_session!r} "
                + ("found" if has else f"not found (shared: {sessions or 'none'})"),
                f"run matlab.engine.shareEngine('{cfg.matlab_session}') in MATLAB, or call cm_session_start")
        if has and gui_status is not None:
            try:
                st = gui_status()
                rep.add("CarMaker GUI", bool(st.connected),
                        f"simulation state: {st.sim_status_text}" if st.connected
                        else str(st.extra.get("error", "no answer")),
                        "open the CarMaker GUI from the Simulink model (or call cm_session_start)")
            except BackendError as e:
                rep.add("CarMaker GUI", False, str(e), "open the CarMaker GUI (or call cm_session_start)")

    # project and state folders
    if cfg.project is None:
        rep.add("project folder", None, "CM_PROJECT not set (it is then read from the running GUI)")
    else:
        ok = (cfg.project / "Data" / "TestRun").is_dir()
        rep.add("project folder", ok, str(cfg.project) + ("" if ok else " has no Data/TestRun"),
                "set CM_PROJECT to a CarMaker project folder")
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=cfg.state_dir):
            pass
        rep.add("state folder", True, str(cfg.state_dir))
    except OSError as e:
        rep.add("state folder", False, f"{cfg.state_dir}: {e}", "set CM_STATE_DIR to a writable folder")
    return rep


# ---- client configuration ----------------------------------------------------------
def uvx_args(rep: Report, package: str = "carmaker-mcp") -> list[str]:
    args = []
    if rep.recommended_python:
        args += ["--python", rep.recommended_python]
    if rep.engine_pin:
        args += ["--with", rep.engine_pin]
    return [*args, package]


def _env(cfg: Config) -> dict[str, str]:
    env = {"CM_PROJECT": str(cfg.project) if cfg.project else "C:\\CM_Projects\\my-project"}
    for key in ("CM_RESULT_DIRS", "CM_MATLAB_SESSION", "CM_HOME", "CM_POPUP_TIMEOUT", "CM_MODEL"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def client_config(client: str, rep: Report, cfg: Config) -> str:
    """Ready-to-paste registration of the server for an MCP client."""
    args, env = uvx_args(rep), _env(cfg)
    if client == "claude-code":
        envs = " ".join(f'--env {k}="{v}"' for k, v in env.items())
        quoted = " ".join(f'"{a}"' if any(c in a for c in "=* ") else a for a in args)
        return f"claude mcp add carmaker {envs} -- uvx {quoted}"
    entry = {"command": "uvx", "args": args, "env": env}
    if client == "vscode":  # .vscode/mcp.json
        return json.dumps({"servers": {"carmaker": {"type": "stdio", **entry}}}, indent=2)
    if client in ("claude-desktop", "cursor", "antigravity"):
        # claude_desktop_config.json / .cursor/mcp.json / Antigravity's mcp_config.json
        return json.dumps({"mcpServers": {"carmaker": entry}}, indent=2)
    if client == "codex":  # ~/.codex/config.toml (JSON strings are valid TOML basic strings)
        return "\n".join([
            "[mcp_servers.carmaker]",
            'command = "uvx"',
            f"args = {json.dumps(args)}",
            'env_vars = ["WINDIR"]',
            "",
            "[mcp_servers.carmaker.env]",
            *[f"{k} = {json.dumps(v)}" for k, v in env.items()],
        ])
    raise ValueError(f"unknown client {client!r}; use one of {', '.join(CLIENTS)}")


def format_report(rep: Report) -> str:
    mark = {True: "ok  ", False: "FAIL", None: "--  "}
    lines = [f"[{mark[c.ok]}] {c.name}: {c.detail}" + (f"\n       fix: {c.fix}" if c.fix else "")
             for c in rep.checks]
    lines.append("")
    lines.append("uvx arguments for this machine:  uvx " + " ".join(uvx_args(rep)))
    lines.append("All checks passed." if rep.ok else "Some checks failed, see the fixes above.")
    return "\n".join(lines)
