"""Path guard: every file write must resolve inside the CarMaker project root."""

from __future__ import annotations

import os
from pathlib import Path


class GuardError(Exception):
    pass


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def is_within(path: Path, root: Path) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


class PathGuard:
    """Resolves user-supplied paths and refuses anything outside ``root``.

    Rejects ``..`` escapes, anything inside ``.git`` and anything inside ``forbidden``
    (the server's own state directory, so backups cannot be edited through the tools).
    """

    def __init__(self, root: Path, forbidden: tuple[Path, ...] = ()):
        self.root = Path(os.path.abspath(str(root)))
        self.forbidden = tuple(Path(os.path.abspath(str(f))) for f in forbidden)

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        p = Path(os.path.realpath(os.path.abspath(str(p))))
        if not is_within(p, Path(os.path.realpath(str(self.root)))):
            raise GuardError(f"path is outside the project root: {path}")
        if ".git" in {part.lower() for part in p.parts}:
            raise GuardError(f"refusing to touch .git: {path}")
        for f in self.forbidden:
            if is_within(p, f):
                raise GuardError(f"path is inside a protected folder: {path}")
        return p

    def relative(self, path: Path) -> str:
        return os.path.relpath(str(path), str(Path(os.path.realpath(str(self.root))))).replace(
            "\\", "/"
        )
