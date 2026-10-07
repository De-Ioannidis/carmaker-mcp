"""IPGMovie through the CarMaker GUI's ScriptControl ``Movie`` commands: open it and export pictures.

What IPGMovie 14.1 does, as observed (see docs/verification.md):

* It records a run only while it is open, and can show any moment of the last run afterwards.
* Exporting a picture during a run works, but it then stops following and recording that run (its
  window freezes). No command brings it back, and exporting from a second window does the same.
  Pictures during a run are therefore read from its window instead (wincapture.py).
* Export options are remembered between calls, so the time is always given, and a time it has no
  data for produces no file and no error: exports run asynchronously and are polled for the file.
* A result file is replayed with ``Movie loadsimdata``. If quantities are missing, IPGMovie shows a
  warning dialog of its own (not a CarMaker pop-up) and lists them in MISSING_QUANTITIES.txt.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from .backend import BackendError, tcl_word

# Camera views every IPGMovie has; sensors, traffic objects and Camera.cfg add more.
CAMERAS = ("DEFAULT", "Bird's Eye View", "Left Side", "Right Side", "Sitting In Vehicle (A)",
           "Sitting In Vehicle (B)")
MISSING_FILE = "MISSING_QUANTITIES.txt"
MOVIE_EXE = "Movie.exe"  # the program whose window is read for pictures during a run
_KEEP = 20  # pictures kept in the state folder

Tcl = Callable[[str, int], tuple[int, str]]


class Movie:
    def __init__(self, tcl: Tcl, folder: Path, sleep: Callable[[float], None] = time.sleep):
        self._tcl, self.folder, self._sleep = tcl, folder, sleep
        self._n = 0

    # ---- state ----------------------------------------------------------------
    def _ask(self, command: str) -> str | None:
        """Answer of a ``Movie`` command, or None when IPGMovie is not running."""
        status, result = self._tcl(command, 10000)
        return result.strip() if status == 0 else None

    def _do(self, command: str) -> str:
        status, result = self._tcl(command, 15000)
        if status != 0:
            raise BackendError(result.strip() or f"`{command}` failed (status {status})")
        return result.strip()

    def is_open(self) -> bool:
        return self._ask("Movie status initialized") == "1"

    def status(self) -> dict:
        if not self.is_open():
            return {"open": False}
        out: dict = {"open": True, "follows_simulation": self._ask("Movie status online") == "1"}
        m = re.search(r"Name\s+(\{[^}]*\}|\S+)", self._ask("Movie window info") or "")
        if m:
            out["camera"] = m.group(1).strip("{}")
        return out

    def open(self, max_wait_s: float = 30.0) -> dict:
        """Start IPGMovie, or take over one that is running, and wait until it is ready."""
        started = False
        if not self.is_open():
            if self._ask("Movie attach") != "1":
                self._do("Movie start")
                started = True
            t0 = time.time()
            while not self.is_open():
                if time.time() - t0 > max_wait_s:
                    raise BackendError(f"IPGMovie did not get ready within {max_wait_s:.0f}s")
                self._sleep(0.5)
        return {**self.status(), "started": started, "cameras": list(CAMERAS)}

    # ---- pictures -------------------------------------------------------------
    def camera(self, name: str) -> None:
        status, result = self._tcl(f"Movie camera select {tcl_word(name)}", 10000)
        if status != 0:
            raise BackendError(f"IPGMovie has no camera {name!r}. Built in: {', '.join(CAMERAS)}; sensors, "
                               "traffic objects and the project's Movie/Camera.cfg add more")

    def load(self, erg: Path) -> None:
        self._do(f"Movie loadsimdata {tcl_word(erg.as_posix())}")
        self._sleep(2.0)  # the command returns before the scene is built

    def export(self, when: float | None, width: int, height: int, timeout_s: float = 15.0) -> Path:
        """Write one picture of the main window: at ``when`` seconds, or at the latest moment held."""
        target = self._new_file(".jpg")
        t = "end" if when is None else f"{when:g}"
        self._do(f"Movie export window {tcl_word(target.as_posix())} 0 -start {t} -end {t} "
                 f"-width {int(width)} -height {int(height)} -format jpeg -overwrite -async")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            state = self._ask("Movie export status -detailed") or "inactive"
            if state == "inactive" and target.is_file() and target.stat().st_size > 0:
                self._prune()
                return target
            if state != "inactive" and not state.startswith("exporting"):
                raise BackendError(f"IPGMovie could not export the picture: {state}")
            self._sleep(0.2)
        at = "" if when is None else f" for t = {when:g} s"
        raise BackendError(
            f"IPGMovie wrote no picture{at}. It only holds what it recorded: it must be open during the "
            "run, and a picture exported with a time during a run ends its recording. For another run pass "
            "its result file as erg")

    def store(self, data: bytes, suffix: str) -> Path:
        """Keep a picture that was not made by IPGMovie's export (a capture of its window)."""
        target = self._new_file(suffix)
        target.write_bytes(data)
        self._prune()
        return target

    def _new_file(self, suffix: str) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._n += 1
        return self.folder / f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}-{self._n}{suffix}"

    def _prune(self) -> None:
        files = sorted(self.folder.glob("snapshot-*"), key=lambda p: p.stat().st_mtime)
        for p in files[:-_KEEP]:
            p.unlink(missing_ok=True)


def missing_quantities(project: Path, erg: Path, since_size: int = 0) -> list[str]:
    """Quantities IPGMovie reported missing for ``erg`` in the part of MISSING_QUANTITIES.txt written
    after ``since_size`` bytes."""
    f = project / MISSING_FILE
    try:
        text = f.read_bytes()[since_size:].decode("utf-8", errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for block in text.split("---"):
        if erg.name not in block or "Missing" not in block:
            continue
        body = block.split("Missing", 1)[1].split("\n", 1)[-1]
        tokens = body.split()
        heads = {t.split(".", 1)[0] for t in tokens}
        split = re.compile(r"(?<=[A-Za-z0-9_])(?=(?:" + "|".join(map(re.escape, sorted(heads))) + r")\.)")
        for tok in tokens:  # columns that ran into each other: 'Vhcl.RL.FyVhcl.RR.rot'
            names.extend(p for p in split.split(tok) if p)
    return list(dict.fromkeys(names))


def missing_size(project: Path) -> int:
    try:
        return (project / MISSING_FILE).stat().st_size
    except OSError:
        return 0
