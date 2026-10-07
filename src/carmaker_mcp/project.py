"""Read, edit and clone CarMaker project data files (test runs, vehicles, drivers, tyres...)."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .guard import GuardError, PathGuard, is_within
from .infofile import InfoFile, InfoFileError, diff_values
from .state import StateStore

# kind -> folder below <project>/Data
KINDS = {
    "testrun": "TestRun",
    "vehicle": "Vehicle",
    "driver": "Driver",
    "tire": "Tire",
    "road": "Road",
    "trailer": "Trailer",
    "kinematics": "Kinematics",
    "config": "Config",
    "chassis": "Chassis",
    "misc": "Misc",
    "sensor": "Sensor",
    "traffic": "Traffic",
    "script": "Script",
}


class ProjectError(Exception):
    pass


class Project:
    def __init__(self, root: Path, guard: PathGuard, store: StateStore):
        self.root = Path(root)
        self.guard = guard
        self.store = store

    # ---- paths -------------------------------------------------------------
    def kind_dir(self, kind: str) -> Path:
        if kind not in KINDS:
            raise ProjectError(f"unknown kind {kind!r}; choose one of {sorted(KINDS)}")
        return self.root / "Data" / KINDS[kind]

    def path(self, kind: str, name: str) -> Path:
        name = name.replace("\\", "/").strip("/")
        if not name:
            raise ProjectError("empty name")
        p = self.guard.resolve(self.kind_dir(kind) / name)
        base = self.guard.resolve(self.kind_dir(kind))
        if not is_within(p, base):
            raise GuardError(f"{name!r} is outside Data/{KINDS[kind]}")
        return p

    # ---- listing -----------------------------------------------------------
    def list(
        self, kind: str, pattern: str | None = None, include_hidden: bool = False, limit: int = 500
    ) -> dict:
        base = self.kind_dir(kind)
        if not base.exists():
            return {"kind": kind, "names": [], "total": 0}
        names = []
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(base).as_posix()
            parts = rel.split("/")
            if any(x.startswith(".tmp") for x in parts):
                continue
            if not include_hidden and any(x.startswith(".") for x in parts):
                continue
            if pattern and not fnmatch.fnmatch(rel.lower(), pattern.lower()):
                continue
            names.append(rel)
        return {"kind": kind, "names": names[:limit], "total": len(names), "truncated": len(names) > limit}

    # ---- reading -----------------------------------------------------------
    def _load(self, kind: str, name: str) -> tuple[Path, InfoFile]:
        p = self.path(kind, name)
        if not p.is_file():
            raise ProjectError(f"{kind} {name!r} not found")
        info = InfoFile.load(p)
        return p, info

    def read(
        self, kind: str, name: str, keys: list[str] | None = None, prefix: str | None = None,
        max_items: int = 300,
    ) -> dict:
        p, info = self._load(kind, name)
        if not info.is_infofile:
            return {"kind": kind, "name": name, "infofile": False, "size_bytes": p.stat().st_size}
        d = info.to_dict()
        if keys:
            d = {k: d[k] for k in keys if k in d}
        if prefix:
            d = {k: v for k, v in d.items() if k.startswith(prefix)}
        total = len(d)
        items = dict(list(d.items())[:max_items])
        return {
            "kind": kind, "name": name, "infofile": True, "total_keys": total,
            "returned": len(items), "values": items,
        }

    # ---- editing -----------------------------------------------------------
    def edit(self, kind: str, name: str, overrides: dict[str, str | None]) -> dict:
        """Change keys in place. ``None`` as a value removes the key. The file is backed up
        before its first edit in this session. A value with line breaks, or any value for a
        key that is a multi-line text key already, is written as a text key."""
        if not overrides:
            raise ProjectError("no overrides given")
        p, info = self._load(kind, name)
        if not info.is_infofile:
            raise ProjectError(f"{name!r} is not a text infofile; refusing to edit")
        before = InfoFile(info.dumps())
        for k, v in overrides.items():
            try:
                if v is None:
                    info.unset(k)
                elif isinstance(v, str) and ("\n" in v or info.is_text(k)):
                    info.set_text(k, v)
                else:
                    info.set(k, _fmt(v))
            except InfoFileError as e:
                raise ProjectError(str(e)) from e
        changes = diff_values(before, info)
        if not changes:
            return {"changed": False, "changes": []}
        self.store.backup_file(p)
        info.save(p)
        rel = self.guard.relative(p)
        for c in changes:
            self.store.log("file_key", rel, key=c["key"], old=c["old"], new=c["new"])
        return {"changed": True, "file": rel, "changes": changes}

    def clone(self, kind: str, base: str, new_name: str) -> dict:
        src = self.path(kind, base)
        if not src.is_file():
            raise ProjectError(f"{kind} {base!r} not found")
        dst = self.path(kind, new_name)
        if dst.exists():
            raise ProjectError(f"{new_name!r} already exists; refusing to overwrite")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        self.store.record_created(dst)
        rel = self.guard.relative(dst)
        self.store.log("file_create", rel, source=self.guard.relative(src))
        return {"created": rel, "from": self.guard.relative(src)}


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (list, tuple)):
        return " ".join(_fmt(x) for x in v)
    if isinstance(v, float):
        return repr(v)
    return str(v)


__all__ = ["KINDS", "GuardError", "Project", "ProjectError"]
