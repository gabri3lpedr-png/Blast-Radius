"""Apply a FilePatch's hunks to the original file text, in memory.

Every context (' ') and removed ('-') line must match the original exactly, otherwise
ApplyError is raised and the selector falls back to a FULL run.
"""

from __future__ import annotations

from blast.diff import FilePatch


class ApplyError(Exception):
    pass


def _split(text: str) -> tuple[list[str], bool]:
    """Lines without their '\\n' (a trailing '\\r' is kept), and whether the text ended in '\\n'."""
    if text == "":
        return [], False
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
        return lines, True
    return lines, False


def apply(fp: FilePatch, original: str) -> tuple[str, list[int]]:
    """Return (new text, changed ORIGINAL line numbers, 1-based and sorted).

    Changed lines: every removed line; for an insertion with no removed line next to it,
    the original line just before and just after the insertion point (where they exist).
    """
    old, trailing_nl = _split(original)
    crlf = bool(old) and all(line.endswith("\r") for line in old)
    out: list[str] = []
    changed: set[int] = set()
    pos = 0  # index into old of the next line not yet copied
    last_hunk_end = -1

    for h in fp.hunks:
        # For a pure insertion (old_len == 0), old_start is the line AFTER which text goes.
        start = h.old_start if h.old_len == 0 else h.old_start - 1
        if start < pos or start > len(old):
            raise ApplyError(f"hunk @@ -{h.old_start},{h.old_len} out of order or range")
        out.extend(old[pos:start])
        pos = start
        pending_insert = False  # '+' lines seen since the last ' ' line, no '-' adjacent
        run_has_minus = False
        for tag, body in h.lines:
            if tag in (" ", "-"):
                if pos >= len(old) or old[pos].rstrip("\r") != body:
                    got = old[pos] if pos < len(old) else "<EOF>"
                    raise ApplyError(f"line {pos + 1} mismatch: want {body!r} got {got!r}")
            if tag == " ":
                if pending_insert and not run_has_minus:
                    changed.update((pos, pos + 1))  # line before (1-based pos) and after
                pending_insert = run_has_minus = False
                out.append(old[pos])
                pos += 1
            elif tag == "-":
                changed.add(pos + 1)
                run_has_minus = True
                pos += 1
            else:  # '+'
                pending_insert = True
                out.append(body + ("\r" if crlf else ""))
        if pending_insert and not run_has_minus:
            changed.update((pos, pos + 1))
        last_hunk_end = pos

    out.extend(old[pos:])

    new_trailing_nl = trailing_nl
    if fp.hunks and last_hunk_end == len(old):
        last = fp.hunks[-1]
        if last.new_no_newline_at_eof:
            new_trailing_nl = False
        elif last.old_no_newline_at_eof:
            new_trailing_nl = True
    eol = "\n"
    new_text = eol.join(out) + (eol if new_trailing_nl and out else "")
    return new_text, sorted(n for n in changed if 1 <= n <= len(old))
