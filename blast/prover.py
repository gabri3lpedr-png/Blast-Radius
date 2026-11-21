"""The Prover: classify each FilePatch of a commit, then combine into a commit verdict.

Per file:
  NOOP     .py file whose new AST equals the original AST (comments/whitespace only), or
           differs only in docstrings (no labelled test reads __doc__: tests/test_docs.py's
           doctests are not in tests.json); a documentation file outside sqlglot/ and tests/
           (README.md, posts/*.md, LICENSE, images; no test opens them); a plain 644<->755
           mode change with no content change.
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
_DOC_SUFFIXES = (".md", ".rst", ".png", ".gif", ".jpg", ".jpeg", ".svg")
_DOC_NAMES = {"LICENSE", "LICENSE.md", "LICENSE.txt", "AUTHORS", "NOTICE"}
_PLAIN_MODES = {"100644", "100755"}
_STRUCTURAL_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "Makefile",
                     "conftest.py", "requirements.txt", "tox.ini", "pytest.ini"}


@dataclass
class FileVerdict:
    kind: str
    path: str
    detail: str = ""
    lines: list[int] = field(default_factory=list)  # CHANGED only: original line numbers
    fp: FilePatch | None = field(default=None, repr=False)
    new_text: str = field(default="", repr=False)  # CHANGED only

    def __str__(self) -> str:
        return f"{self.kind} {self.path}" + (f" ({self.detail})" if self.detail else "")


def _structural(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(_STRUCTURAL_PREFIXES)
        or name in _STRUCTURAL_NAMES
        or name.startswith("setup.")
    )


def _doc(path: str | None) -> bool:
    """A documentation file no test can read: outside sqlglot/ and tests/, doc-like name."""
    if not path or path.startswith(("sqlglot/", "tests/", "sqlglotrs/", "sqlglotc/", ".")) or _structural(path):
        return False
    name = path.rsplit("/", 1)[-1]
    return name in _DOC_NAMES or name.lower().endswith(_DOC_SUFFIXES)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Drop the docstring of the module and of every def/class (in place)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]
    return tree


def _parse(text: str, path: str) -> ast.AST:
    # Parse bytes, not str, so a coding cookie is honoured exactly as on import.
    return ast.parse(text.encode("utf-8"), filename=path)


def classify(fp: FilePatch, src: Path = SRC) -> FileVerdict:
    v = _classify(fp, src)
    v.fp = fp
    return v


def _classify(fp: FilePatch, src: Path) -> FileVerdict:
    path = fp.path or "?"
    if _doc(fp.old_path if fp.old_path is not None else fp.new_path) and (
        fp.new_path is None or _doc(fp.new_path)
    ):
        return FileVerdict(NOOP, path, "documentation file")
    if fp.is_new:
        return FileVerdict(FULL, path, "new file")
    if fp.is_deleted:
        return FileVerdict(FULL, path, "deleted file")
    if fp.is_rename:
        return FileVerdict(FULL, path, "rename")
    if fp.is_binary:
        return FileVerdict(FULL, path, "binary")
    if not fp.hunks and fp.mode_change is not None and set(fp.mode_change) <= _PLAIN_MODES:
        return FileVerdict(NOOP, path, f"mode {fp.mode_change[0]} -> {fp.mode_change[1]} only")
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
    if ast.dump(_strip_docstrings(old_tree), include_attributes=False) == ast.dump(
        _strip_docstrings(new_tree), include_attributes=False
    ):
        return FileVerdict(NOOP, path, "docstrings only")
    return FileVerdict(CHANGED, path, f"lines {_ranges(changed)}", changed, fp, new_text)


def _ranges(nums: list[int]) -> str:
    parts: list[list[int]] = []
    for n in nums:
        if parts and parts[-1][1] == n - 1:
            parts[-1][1] = n
        else:
            parts.append([n, n])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in parts) or "-"
