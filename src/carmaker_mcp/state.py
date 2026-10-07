"""Server state: first-write backups and an append-only change log.

Everything lives in ``state_dir`` (outside the CarMaker project and outside the repo):

    <state_dir>/backups/<session>/<project-relative path>   copy of a file before its first edit
    <state_dir>/created/<session>.json                       files this session created
    <state_dir>/trash/<session>/...                          files moved away by a revert
    <state_dir>/changes.jsonl                                one JSON object per change
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path


class StateStore:
    def __init__(self, state_dir: Path, project_root: Path | None = None, session: str | None = None):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(project_root) if project_root else None
        self.session = session or time.strftime("%Y%m%d-%H%M%S")
        self._lock = threading.Lock()
        self._backed_up: set[str] = set()
        self._created: set[str] = set()  # files this session created (never backed up or restored)
        self._restored: set[str] = set()  # files a revert put back and we have not edited since
        self._seq = 0

    # ---- change log --------------------------------------------------------
    @property
    def log_path(self) -> Path:
        return self.dir / "changes.jsonl"

    def log(self, kind: str, target: str, **fields) -> dict:
        """Append a change record. ``kind`` is one of file_key, file_create, workspace,
        model_param, model_struct."""
        with self._lock:
            self._seq += 1
            rec = {
                "id": f"{self.session}:{self._seq}",
                "session": self.session,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "target": target,
                **fields,
            }
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            return rec

    def entries(self, session: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if session is None or rec.get("session") == session:
                out.append(rec)
        return out

    def sessions(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.entries():
            seen.setdefault(r["session"], None)
        return list(seen)

    # ---- backups -----------------------------------------------------------
    def _rel(self, path: Path) -> Path:
        if self.root is None:
            return Path(path.name)
        return Path(path).relative_to(self.root)

    def backup_file(self, path: Path) -> Path | None:
        """Copy ``path`` aside before its first modification in this session."""
        path = Path(path)
        key = str(path).lower()
        with self._lock:
            if key in self._backed_up or key in self._created or not path.exists():
                return None  # a file this session created holds nothing of the user's to keep
            dest = self.dir / "backups" / self.session / self._rel(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            self._backed_up.add(key)
            self._restored.discard(key)
            return dest

    def record_created(self, path: Path) -> None:
        with self._lock:
            f = self.dir / "created" / f"{self.session}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            cur = json.loads(f.read_text()) if f.exists() else []
            cur.append(str(path))
            f.write_text(json.dumps(cur))
            self._created.add(str(Path(path)).lower())

    def restore_files(self, session: str | None = None) -> dict:
        """Put every backed-up file of ``session`` back and move files it created to trash.
        Nothing is deleted."""
        session = session or self.session
        own = session == self.session
        restored, trashed = [], []
        bdir = self.dir / "backups" / session
        if bdir.exists() and self.root is not None:
            for src in bdir.rglob("*"):
                if src.is_file():
                    target = self.root / src.relative_to(bdir)
                    key = str(target).lower()
                    if own and key in self._restored:
                        continue  # put back by an earlier revert and not edited by us since
                    shutil.copy2(src, target)
                    restored.append(str(target))
                    if own:
                        self._restored.add(key)
        cf = self.dir / "created" / f"{session}.json"
        if cf.exists():
            for p in json.loads(cf.read_text()):
                pp = Path(p)
                if pp.exists() and self.root is not None:
                    dest = self.dir / "trash" / session / pp.relative_to(self.root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(pp), str(dest))
                    trashed.append(p)
            cf.unlink()
        if own:
            self._backed_up.clear()
            self._created.clear()
        return {"restored": restored, "moved_to_trash": trashed}
