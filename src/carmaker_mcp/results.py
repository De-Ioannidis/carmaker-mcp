"""Reader for CarMaker ``.erg`` result files (binary data + ``.erg.info`` description)."""

from __future__ import annotations

import fnmatch
import math
import time
from pathlib import Path

import numpy as np

_TYPES = {
    "Double": "f8", "Float": "f4", "LongLong": "i8", "ULongLong": "u8", "Long": "i4",
    "ULong": "u4", "Int": "i4", "UInt": "u4", "Short": "i2", "UShort": "u2", "Char": "i1",
    "UChar": "u1",
}
_HEADER = 16
_MAGIC = b"CM-ERG"


class ErgError(Exception):
    pass


def _parse_info(path: Path) -> tuple[dict[str, str], str]:
    kv: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    return kv, kv.get("File.ByteOrder", "LittleEndian")


class Erg:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        info_path = Path(str(self.path) + ".info")
        if not self.path.is_file() or not info_path.is_file():
            raise ErgError(f"need both {self.path.name} and {info_path.name}")
        kv, order = _parse_info(info_path)
        prefix = "<" if order.lower().startswith("little") else ">"
        self.names: list[str] = []
        self.units: dict[str, str] = {}
        fields = []
        i = 1
        while f"File.At.{i}.Name" in kv:
            name, typ = kv[f"File.At.{i}.Name"], kv.get(f"File.At.{i}.Type", "Double")
            if typ in _TYPES:
                dt = prefix + _TYPES[typ]
            elif typ.endswith("Bytes"):
                dt = f"S{int(typ.split()[0])}"
            else:
                raise ErgError(f"unsupported column type {typ!r} for {name}")
            fields.append((name, dt))
            self.names.append(name)
            self.units[name] = kv.get(f"Quantity.{name}.Unit", "")
            i += 1
        if not fields:
            raise ErgError("no columns found in info file")
        self.dtype = np.dtype(fields)
        with self.path.open("rb") as f:
            head = f.read(_HEADER)
        if len(head) < _HEADER or not head.startswith(_MAGIC):
            raise ErgError("not a CM-ERG file")
        rec = int.from_bytes(head[10:12], "little")
        if rec != self.dtype.itemsize:
            raise ErgError(
                f"record size mismatch: header says {rec} bytes, info file describes "
                f"{self.dtype.itemsize}"
            )
        payload = self.path.stat().st_size - _HEADER
        self.n_rows = payload // self.dtype.itemsize
        self.truncated = payload % self.dtype.itemsize != 0
        self._data = (
            np.memmap(self.path, dtype=self.dtype, mode="r", offset=_HEADER, shape=(self.n_rows,))
            if self.n_rows
            else np.empty(0, dtype=self.dtype)
        )

    def column(self, name: str) -> np.ndarray:
        if name not in self.names:
            raise ErgError(f"no quantity {name!r} (use search to find names)")
        return np.asarray(self._data[name])

    def search(self, text: str, limit: int | None = 100) -> list[str]:
        """Quantity names containing ``text``, or matching it if it has wildcards (``*``, ``?``)."""
        t = text.lower()
        if any(c in t for c in "*?["):
            hits = [n for n in self.names if fnmatch.fnmatchcase(n.lower(), t)]
        else:
            hits = [n for n in self.names if t in n.lower()]
        return hits if limit is None else hits[:limit]

    def read(
        self, names: list[str], t_min: float | None = None, t_max: float | None = None,
        max_points: int = 500,
    ) -> dict:
        idx = np.arange(self.n_rows)
        if (t_min is not None or t_max is not None) and "Time" in self.names:
            t = self.column("Time")
            m = np.ones(self.n_rows, dtype=bool)
            if t_min is not None:
                m &= t >= t_min
            if t_max is not None:
                m &= t <= t_max
            idx = idx[m]
        stride = max(1, math.ceil(len(idx) / max(1, max_points)))
        idx = idx[::stride]
        cols = {n: _clean(self.column(n)[idx]) for n in names}
        return {
            "file": self.path.name, "rows_total": self.n_rows, "rows_returned": len(idx),
            "stride": stride, "units": {n: self.units.get(n, "") for n in names}, "data": cols,
        }

    def summary(self, names: list[str]) -> dict:
        out = {}
        for n in names:
            c = self.column(n)
            if c.dtype.kind == "S" or len(c) == 0:
                out[n] = {"unit": self.units.get(n, "")}
                continue
            c = c.astype(float)
            f = c[np.isfinite(c)]
            out[n] = {
                "unit": self.units.get(n, ""),
                "first": _num(c[0]), "last": _num(c[-1]),
                "min": _num(f.min()) if len(f) else None, "max": _num(f.max()) if len(f) else None,
                "mean": _num(f.mean()) if len(f) else None,
            }
        t_end = float(self.column("Time")[-1]) if "Time" in self.names and self.n_rows else None
        return {"file": self.path.name, "rows": self.n_rows, "duration_s": t_end, "quantities": out}


def _num(x) -> float | None:
    x = float(x)
    return x if math.isfinite(x) else None


def _clean(a: np.ndarray) -> list:
    if a.dtype.kind == "S":
        return [v.decode("latin-1") for v in a]
    return [_num(v) for v in a.astype(float)]


def list_results(dirs: list[Path], limit: int = 30, include_modelcheck: bool = False) -> list[dict]:
    seen: dict[str, dict] = {}
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.erg"):
            if not include_modelcheck and "modelcheck" in (x.lower() for x in p.parts):
                continue
            if not Path(str(p) + ".info").exists():
                continue
            st = p.stat()
            seen[str(p)] = {"path": str(p), "name": p.name, "size_bytes": st.st_size, "mtime": st.st_mtime}
    items = sorted(seen.values(), key=lambda r: r["mtime"], reverse=True)[:limit]
    for r in items:
        r["modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.pop("mtime")))
    return items


__all__ = ["Erg", "ErgError", "list_results"]
