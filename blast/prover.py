"""The Prover: classify each FilePatch of a commit, then combine into a commit verdict.

Per file:
  NOOP     .py file whose new AST equals the original AST (comments/whitespace only).
  CHANGED  .py file whose AST differs; carries the changed ORIGINAL line numbers.
  DATA     non-.py file under tests/ (fixtures etc.).
  FULL     anything we cannot reason about: new/deleted/renamed/binary files, structural or
           unrecognised files, patches that do not apply, new text that does not parse.

The original sources come from SQLGLOT_SRC (default /opt/sqlglot), the pinned checkout.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

from blast.apply import ApplyError, apply
from blast.diff import FilePatch

NOOP, CHANGED, DATA, FULL = "NOOP", "CHANGED", "DATA", "FULL"

SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))

_STRUCTURAL_PREFIXES = ("sqlglotrs/", "sqlglotc/", ".github/")
_STRUCTURAL_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "Makefile",
                     "conftest.py", "requirements.txt", "tox.ini", "pytest.ini"}


@dataclass
class FileVerdict:
    kind: str
    path: str
    detail: str = ""
    lines: list[int] = field(default_factory=list)  # CHANGED only: original line numbers

    def __str__(self) -> str:
        return f"{self.kind} {self.path}" + (f" ({self.detail})" if self.detail else "")


def _structural(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(_STRUCTURAL_PREFIXES)
        or name in _STRUCTURAL_NAMES
        or name.startswith("setup.")
    )


def _parse(text: str, path: str) -> ast.AST:
    # Parse bytes, not str, so a coding cookie is honoured exactly as on import.
    return ast.parse(text.encode("utf-8"), filename=path)


def classify(fp: FilePatch, src: Path = SRC) -> FileVerdict:
    path = fp.path or "?"
    if fp.is_new:
        return FileVerdict(FULL, path, "new file")
    if fp.is_deleted:
        return FileVerdict(FULL, path, "deleted file")
    if fp.is_rename:
        return FileVerdict(FULL, path, "rename")
    if fp.is_binary:
        return FileVerdict(FULL, path, "binary")
    if not fp.hunks:
        return FileVerdict(FULL, path, "no hunks (mode change?)")
    if _structural(path):
        return FileVerdict(FULL, path, "structural file")

    if not path.endswith(".py"):
        if path.startswith("tests/"):
            return FileVerdict(DATA, path)
        return FileVerdict(FULL, path, "unrecognised file")

    orig_path = src / path
    try:
        original = orig_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return FileVerdict(FULL, path, f"cannot read original: {type(e).__name__}")
    try:
        new_text, changed = apply(fp, original)
    except ApplyError as e:
        return FileVerdict(FULL, path, f"patch does not apply: {e}")
    try:
        new_tree = _parse(new_text, path)
    except (SyntaxError, ValueError) as e:
        return FileVerdict(FULL, path, f"new text does not parse: {e}")
    try:
        old_tree = _parse(original, path)
    except (SyntaxError, ValueError) as e:
        return FileVerdict(FULL, path, f"original does not parse: {e}")

    if ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
        return FileVerdict(NOOP, path, "AST equal")
    return FileVerdict(CHANGED, path, f"lines {_ranges(changed)}", changed)


def _ranges(nums: list[int]) -> str:
    parts: list[list[int]] = []
    for n in nums:
        if parts and parts[-1][1] == n - 1:
            parts[-1][1] = n
        else:
            parts.append([n, n])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in parts) or "-"
