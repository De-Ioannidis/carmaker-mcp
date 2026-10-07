"""Byte-preserving reader/editor for CarMaker infofiles.

Only the lines that are edited change; comments, ordering, spacing and the original line
endings (CRLF or LF) of every other line stay as they were. This is an independent
implementation of the plain-text format and does not use IPG code.

Supported syntax:
    Key = value            single-line value (the value may be empty)
    Key:                   multi-line text, following lines start with a tab or space
    # comment              ignored
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_KV = re.compile(r"^(?P<key>[^\s=:#][^\s=]*)(?P<sep>\s*=)(?P<val>.*)$")
_BLOCK = re.compile(r"^(?P<key>[^\s=:#][^\s=:]*):\s*$")


class InfoFileError(Exception):
    pass


@dataclass
class Entry:
    key: str
    kind: str  # "kv" or "text"
    start: int  # first line index
    end: int  # last line index (inclusive)


def _split_eol(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


class InfoFile:
    def __init__(self, text: str):
        self.lines: list[str] = text.splitlines(keepends=True)
        self.eol = "\r\n" if "\r\n" in text else "\n"
        self._entries: list[Entry] | None = None

    # ---- io ----------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> InfoFile:
        data = Path(path).read_bytes()
        return cls(data.decode("utf-8", errors="surrogateescape"))

    def dumps(self) -> str:
        return "".join(self.lines)

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(self.dumps().encode("utf-8", errors="surrogateescape"))

    @property
    def is_infofile(self) -> bool:
        return bool(self.lines) and self.lines[0].lstrip("﻿").startswith("#INFOFILE")

    # ---- parsing -----------------------------------------------------------
    def _parse(self) -> list[Entry]:
        if self._entries is not None:
            return self._entries
        out: list[Entry] = []
        i, n = 0, len(self.lines)
        while i < n:
            body, _ = _split_eol(self.lines[i])
            if not body.strip() or body.lstrip().startswith("#"):
                i += 1
                continue
            m = _BLOCK.match(body)
            if m:
                j = i
                while j + 1 < n and _split_eol(self.lines[j + 1])[0][:1] in ("\t", " "):
                    j += 1
                out.append(Entry(m["key"], "text", i, j))
                i = j + 1
                continue
            m = _KV.match(body)
            if m:
                out.append(Entry(m["key"], "kv", i, i))
            i += 1
        self._entries = out
        return out

    def _invalidate(self) -> None:
        self._entries = None

    def _last(self, key: str) -> Entry | None:
        found = None
        for e in self._parse():
            if e.key == key:
                found = e
        return found

    # ---- reading -----------------------------------------------------------
    def keys(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self._parse():
            seen.setdefault(e.key, None)
        return list(seen)

    def get(self, key: str) -> str | None:
        e = self._last(key)
        if e is None:
            return None
        if e.kind == "kv":
            body, _ = _split_eol(self.lines[e.start])
            return _KV.match(body)["val"].strip()  # type: ignore[index]
        return "\n".join(
            _split_eol(self.lines[k])[0][1:] for k in range(e.start + 1, e.end + 1)
        )

    def to_dict(self) -> dict[str, str]:
        return {k: self.get(k) or "" for k in self.keys()}

    # ---- editing -----------------------------------------------------------
    def set(self, key: str, value: str) -> None:
        """Set a single-line value. Creates the key at the end of the file if missing."""
        if "\n" in value or "\r" in value:
            raise InfoFileError("single-line values cannot contain line breaks")
        if not re.fullmatch(r"[^\s=:#][^\s=:]*", key):
            raise InfoFileError(f"invalid key: {key!r}")
        e = self._last(key)
        if e is not None and e.kind == "text":
            raise InfoFileError(f"{key} is a multi-line text key; use set_text")
        if e is None:
            if self.lines and not _split_eol(self.lines[-1])[1]:
                self.lines[-1] += self.eol
            self.lines.append(f"{key} = {value}".rstrip() + self.eol if value else f"{key} ={self.eol}")
        else:
            old, eol = _split_eol(self.lines[e.start])
            prefix = old[: _KV.match(old).end("sep")]  # type: ignore[union-attr]
            self.lines[e.start] = (f"{prefix} {value}" if value else prefix) + eol
        self._invalidate()

    def is_text(self, key: str) -> bool:
        """True if the key exists as a multi-line text key (``Key:``)."""
        e = self._last(key)
        return e is not None and e.kind == "text"

    def set_text(self, key: str, text: str) -> None:
        """Set a multi-line text key (``Key:`` followed by tab-indented lines; none for '')."""
        e = self._last(key)
        body = text.split("\n") if text else []
        new = [f"{key}:{self.eol}"] + [f"\t{ln}{self.eol}" for ln in body]
        if e is None:
            if self.lines and not _split_eol(self.lines[-1])[1]:
                self.lines[-1] += self.eol
            self.lines.extend(new)
        else:
            _, eol = _split_eol(self.lines[e.start])
            new = [f"{key}:{eol}"] + [f"\t{ln}{eol}" for ln in body]
            self.lines[e.start : e.end + 1] = new
        self._invalidate()

    def unset(self, key: str) -> bool:
        removed = False
        for e in reversed([x for x in self._parse() if x.key == key]):
            del self.lines[e.start : e.end + 1]
            removed = True
        if removed:
            self._invalidate()
        return removed


def diff_values(before: InfoFile, after: InfoFile) -> list[dict]:
    """Key-level diff between two versions of the same file."""
    b, a = before.to_dict(), after.to_dict()
    out = []
    for k in list(b) + [k for k in a if k not in b]:
        if b.get(k) != a.get(k):
            out.append({"key": k, "old": b.get(k), "new": a.get(k)})
    return out
