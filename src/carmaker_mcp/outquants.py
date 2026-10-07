"""The list of quantities CarMaker writes to result files (``Data/Config/OutputQuantities``).

The file is an infofile with one multi-line list per storage rate::

    DStore.dt.normal = 0.01
    DStore.Quantities.normal:
            Car.v
            Car.Fx*

Names may contain wildcards. The simulation program reads the file at the start of every test run.
"""

from __future__ import annotations

import fnmatch
import re

from .infofile import InfoFile
from .project import ProjectError

RATES = ("fast", "normal", "slow")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.*?\[\]-]*")

# What IPGMovie needs in a result file to replay the vehicle (it lists what it misses in
# MISSING_QUANTITIES.txt in the project folder). Without the first group nothing moves.
MOVIE_CORE = ("Vhcl.Fr1.x", "Vhcl.Fr1.y", "Vhcl.Fr1.z", "Vhcl.Yaw")
MOVIE_QUANTITIES = (
    *MOVIE_CORE, "Vhcl.Pitch", "Vhcl.Roll", "Vhcl.v", "Vhcl.Distance", "Vhcl.Steer.Ang", "Vhcl.Steer.Trq",
    *(f"Vhcl.{w}.{q}" for w in ("FL", "FR", "RL", "RR")
      for q in ("tx", "ty", "tz", "rx", "ry", "rz", "rot", "Fx", "Fy", "Fz")),
)
PRESETS = {"movie": MOVIE_QUANTITIES}


class OutQuantsError(ProjectError):
    pass


def key(rate: str) -> str:
    if rate not in RATES:
        raise OutQuantsError(f"rate must be one of {', '.join(RATES)}")
    return f"DStore.Quantities.{rate}"


def lists(info: InfoFile) -> dict[str, list[str]]:
    """The quantity names per storage rate, in file order."""
    return {r: [ln.strip() for ln in (info.get(key(r)) or "").split("\n") if ln.strip()] for r in RATES}


def covers(patterns: list[str], name: str) -> bool:
    """True if ``name`` is stored: listed itself or matched by a listed wildcard pattern."""
    return any(p == name or fnmatch.fnmatchcase(name, p) for p in patterns)


def describe(info: InfoFile) -> dict:
    qs = lists(info)
    every = [q for r in RATES for q in qs[r]]
    return {
        "format": info.get("DStore.Format") or "erg",
        "sample_time_s": {r: _num(info.get(f"DStore.dt.{r}")) for r in RATES},
        "quantities": qs,
        "count": len(every),
        "movie_replay": all(covers(every, q) for q in MOVIE_CORE),
    }


def plan(info: InfoFile, add: list[str], remove: list[str], rate: str) -> tuple[dict[str, str], dict]:
    """New text per changed key, and a report of what happens to every requested name."""
    for name in [*add, *remove]:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise OutQuantsError(f"not a quantity name: {name!r}")
    key(rate)
    qs = lists(info)
    before = {r: list(v) for r, v in qs.items()}
    removed, not_listed = [], []
    for name in dict.fromkeys(remove):
        hit = [r for r in RATES if name in qs[r]]
        for r in hit:
            qs[r].remove(name)
        (removed if hit else not_listed).append(name)
    added, already = [], []
    for name in dict.fromkeys(add):
        if any(name in qs[r] for r in RATES):
            already.append(name)
        else:
            qs[rate].append(name)
            added.append(name)
    changes = {key(r): "\n".join(qs[r]) for r in RATES if qs[r] != before[r]}
    report = {"rate": rate, "added": added, "already_listed": already, "removed": removed,
              "not_listed": not_listed, "count": sum(len(v) for v in qs.values())}
    return changes, {k: v for k, v in report.items() if v not in ([], None)}


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None
