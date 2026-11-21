"""Parse a git-style unified diff into FilePatch objects.

parse(text) never raises: if anything looks wrong it returns PARSE_FAILED, a marker the
selector treats as "run everything".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    # (tag, text) with tag in ' ', '-', '+'; text has no trailing newline.
    lines: list[tuple[str, str]] = field(default_factory=list)
    old_no_newline_at_eof: bool = False
    new_no_newline_at_eof: bool = False


@dataclass
class FilePatch:
    old_path: str | None = None  # None when the file is new
    new_path: str | None = None  # None when the file is deleted
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    is_rename: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        return self.new_path or self.old_path


class _ParseFailed:
    """Marker: the diff could not be understood -> FULL run."""

    def __init__(self, reason: str = ""):
        self.reason = reason

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"PARSE_FAILED({self.reason!r})"


PARSE_FAILED = _ParseFailed()

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git (\S+) (\S+)$")


class _Error(Exception):
    pass


def _unquote(path: str) -> str:
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        body = path[1:-1]
        raw = body.encode("latin-1", "backslashreplace").decode("unicode_escape")
        path = raw.encode("latin-1", "ignore").decode("utf-8", "replace")
    return path


def _strip_prefix(path: str) -> str | None:
    path = _unquote(path.split("\t", 1)[0])
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse(text: str) -> list[FilePatch]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    patches: list[FilePatch] = []
    cur: FilePatch | None = None
    i, n = 0, len(lines)

    def start() -> FilePatch:
        fp = FilePatch()
        patches.append(fp)
        return fp

    while i < n:
        line = lines[i].rstrip("\r")
        if line.startswith("diff --git "):
            cur = start()
            m = _DIFF_GIT_RE.match(line)
            if m:
                cur.old_path = _strip_prefix(m.group(1))
                cur.new_path = _strip_prefix(m.group(2))
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = start()
            old = _strip_prefix(line[4:])
            new = _strip_prefix(lines[i + 1].rstrip("\r")[4:])
            cur.old_path, cur.new_path = old, new
            if old is None:
                cur.is_new = True
            if new is None:
                cur.is_deleted = True
            i += 2
            continue
        if line.startswith("@@"):
            if cur is None:
                raise _Error("hunk before any file header")
            m = _HUNK_RE.match(line)
            if not m:
                raise _Error("bad hunk header")
            h = Hunk(
                int(m.group(1)),
                int(m.group(2)) if m.group(2) is not None else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) is not None else 1,
            )
            i += 1
            old_left, new_left = h.old_len, h.new_len
            last_tag = None
            while i < n and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
                raw = lines[i]
                if raw.startswith("\\"):
                    if last_tag == "-":
                        h.old_no_newline_at_eof = True
                    elif last_tag == "+":
                        h.new_no_newline_at_eof = True
                    elif last_tag == " ":
                        h.old_no_newline_at_eof = h.new_no_newline_at_eof = True
                    i += 1
                    continue
                if raw == "":
                    tag, body = " ", ""  # some tools drop the space on blank context lines
                else:
                    tag, body = raw[0], raw[1:]
                if body.endswith("\r"):
                    body = body[:-1]
                if tag == " ":
                    old_left -= 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                elif tag == "+":
                    new_left -= 1
                else:
                    raise _Error(f"bad hunk line tag {tag!r}")
                if old_left < 0 or new_left < 0:
                    raise _Error("hunk longer than its header")
                h.lines.append((tag, body))
                last_tag = tag
                i += 1
            if old_left != 0 or new_left != 0:
                raise _Error("hunk shorter than its header")
            cur.hunks.append(h)
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.is_new = True
            elif line.startswith("deleted file mode"):
                cur.is_deleted = True
            elif line.startswith("rename from "):
                cur.is_rename = True
                cur.old_path = _unquote(line[len("rename from "):])
            elif line.startswith("rename to "):
                cur.is_rename = True
                cur.new_path = _unquote(line[len("rename to "):])
            elif line.startswith("copy from ") or line.startswith("copy to "):
                raise _Error("copy not supported")
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur.is_binary = True
        i += 1

    for fp in patches:
        if fp.is_new:
            fp.old_path = None
        if fp.is_deleted:
            fp.new_path = None
        if fp.old_path is None and fp.new_path is None:
            raise _Error("file patch with no path")
        if fp.old_path is not None and fp.new_path is not None and fp.old_path != fp.new_path:
            fp.is_rename = True
    return patches


def parse(text: str):
    """Return list[FilePatch], or PARSE_FAILED if the diff is malformed or empty."""
    try:
        patches = _parse(text)
    except Exception as e:  # noqa: BLE001 - any oddity means FULL
        return _ParseFailed(str(e))
    if not patches:
        return _ParseFailed("no file patches")
    return patches
