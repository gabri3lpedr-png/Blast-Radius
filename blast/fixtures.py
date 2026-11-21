"""Fixture fallback (until the coverage map arrives): which tests read a data file?

For a file under tests/fixtures/, a test is selected when its own source segment (found with
ast over SQLGLOT_SRC/tests/**/*.py) contains the fixture's file name or its stem inside a
string literal (f-string parts included) without whitespace. Fixtures are always named by
such strings, so this skips code like `optimizer.annotate_types(...)` and SQL text that only
share the stem. Only ids present in tests.json are returned; an empty result means "unknown"
-> FULL.

The index is prebuilt at docker build time (blast/build_index.py -> test_strings.json.gz);
without the prebuilt file it is built from SQLGLOT_SRC/tests on first use.
"""

from __future__ import annotations

import ast
import gzip
import json
import re
from pathlib import Path

_NO_SPACE = re.compile(r"^\S+$")


class TestIndex:
    def __init__(self, src: Path, prebuilt: Path | None = None):
        self.src = src
        self.prebuilt = prebuilt
        # (test id, the string literals in its source segment joined by "\0")
        self._funcs: list[tuple[str, str]] | None = None

    def _load(self) -> list[tuple[str, str]]:
        if self.prebuilt is not None:
            try:
                with gzip.open(self.prebuilt, "rt", encoding="utf-8") as f:
                    return [(tid, seg) for tid, seg in json.load(f)]
            except OSError:
                pass
        return self._build()

    def _build(self) -> list[tuple[str, str]]:
        funcs: list[tuple[str, str]] = []
        tests_dir = self.src / "tests"
        for py in sorted(tests_dir.rglob("*.py")):
            rel = py.relative_to(self.src).as_posix()
            try:
                text = py.read_bytes().decode("utf-8")
                tree = ast.parse(text, filename=rel)
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                continue

            def segment(node: ast.AST) -> str:
                return "\0".join(sorted({
                    n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and _NO_SPACE.match(n.value)
                }))

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("test"):
                        funcs.append((f"{rel}::{node.name}", segment(node)))
                elif isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                            funcs.append((f"{rel}::{node.name}::{sub.name}", segment(sub)))
        return funcs

    def tests_mentioning(self, path: str) -> list[str]:
        """Test ids whose source mentions the file name or stem of `path`."""
        if self._funcs is None:
            self._funcs = self._load()
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        needles = {n for n in (name, stem) if n}
        return [tid for tid, seg in self._funcs if any(n in seg for n in needles)]
