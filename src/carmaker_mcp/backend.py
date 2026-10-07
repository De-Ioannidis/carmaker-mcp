"""Backend interface: everything that talks to a live CarMaker for Simulink + MATLAB session."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# SimStatus values reported by the CarMaker GUI (>= 0 means a simulation is running).
SIM_STATUS_TEXT = {
    -1: "preprocessing", -2: "idle", -3: "postprocessing", -4: "model check",
    -5: "driver adaptation", -6: "fatal error", -7: "waiting for license",
    -8: "paused", -10: "starting application", -11: "simulink initialization",
}
IDLE = -2


def sim_status_text(code: int | None) -> str:
    if code is None:
        return "unknown"
    if code >= 0:
        return "running"
    return SIM_STATUS_TEXT.get(code, f"status {code}")


class BackendError(Exception):
    """A command failed or the session could not be reached."""


class BackendTimeout(BackendError):
    """The session did not answer in time; whatever was asked may still be in progress."""


class NotSupported(BackendError):
    pass


@dataclass
class Status:
    connected: bool
    sim_status: int | None = None
    sim_status_text: str = "unknown"
    simstate: str | None = None  # MATLAB-side state (cmcmd simstate)
    end_status: str | None = None
    active_model: str | None = None
    project_dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def idle(self) -> bool:
        return self.sim_status == IDLE


class Backend(Protocol):
    def status(self) -> Status: ...
    def gui_tcl(self, command: str, timeout_ms: int = 10000) -> tuple[int, str]: ...
    def load_testrun(self, name: str, force: bool = False) -> str: ...
    def start_sim(self) -> None: ...
    def stop_sim(self) -> None: ...
    def quantity_read(self, names: list[str]) -> dict[str, float | None]: ...
    def dva_write(self, name: str, value: float, duration_ms: int, mode: str) -> None: ...
    def dva_release(self) -> None: ...
    def workspace_get(self, name: str, scope: str = "base") -> Any: ...
    def workspace_set(self, name: str, value: Any, scope: str = "base") -> None: ...
    def workspace_clear(self, name: str, scope: str = "base") -> None: ...
    def workspace_list(self, scope: str = "base") -> list[dict]: ...
    def model_get(self, path: str, param: str) -> Any: ...
    def model_set(self, path: str, param: str, value: Any) -> None: ...
    def model_file(self, model: str) -> str | None: ...
    def model_save(self, model: str) -> None: ...
    def model_reload(self, model: str, file: str) -> None: ...
    def model_struct(self, op: str, **kw: Any) -> None: ...


# ---- Tcl helpers -----------------------------------------------------------
_SAFE = re.compile(r"^[A-Za-z0-9_./:+\-]+$")


def tcl_word(s: str) -> str:
    """Quote ``s`` as a single Tcl word."""
    if s and _SAFE.match(s):
        return s
    if _braces_balanced(s) and not s.endswith("\\"):
        return "{" + s + "}"
    out = []
    for ch in s:
        if ch in '\\{}[]$";' or ch.isspace():
            if ch.isspace():
                out.append({" ": "\\ ", "\t": "\\t", "\n": "\\n", "\r": "\\r"}.get(ch, " "))
            else:
                out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out) or "{}"


def _braces_balanced(s: str) -> bool:
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


# Guard rail for the raw Tcl tool. This is a speed bump against accidents, not a sandbox:
# Tcl is dynamic and can always be obfuscated, so only give the tool to agents you trust.
_DENY = re.compile(
    r"(^|[;\[\s])(exit|quit|exec|cd|open|source|socket|file\s+(delete|rename|copy|mkdir|attributes)"
    r"|rm|unset\s+::?env|interp)\b",
    re.IGNORECASE,
)


def check_tcl_allowed(command: str) -> None:
    m = _DENY.search(command)
    if m:
        raise BackendError(
            f"Tcl command blocked by the safety deny-list ({m.group(0).strip()!r}). "
            "Use the dedicated tools instead."
        )
