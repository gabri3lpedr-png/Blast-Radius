"""Build the selector's indexes at docker BUILD time from the pinned sqlglot source.

<out>/name_index.json.gz    name -> {path: [lines]} over sqlglot/** and tests/**: every place
                            a name is used or bound (ast.Name ids, attribute names x.NAME,
                            import aliases). Rule b) uses it to find who sees a module-level
                            name when its definition changes.
<out>/test_strings.json.gz  [[test id, string literals]] for the fixture-name fallback.

Usage: python build_index.py <sqlglot src> <out dir>
"""

from __future__ import annotations

import ast
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

# Run as a script, sys.path[0] is blast/ itself, where blast/select.py would shadow the stdlib
# `select` module: replace it with the repo root.
sys.path[0] = str(Path(__file__).resolve().parent.parent)
from blast.fixtures import TestIndex  # noqa: E402


def build(src: Path) -> dict[str, dict[str, list[int]]]:
    index: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    files = sorted((src / "sqlglot").rglob("*.py")) + sorted((src / "tests").rglob("*.py"))
    for py in files:
        rel = py.relative_to(src).as_posix()
        try:
            tree = ast.parse(py.read_bytes(), filename=rel)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                index[node.id][rel].add(node.lineno)
            elif isinstance(node, ast.Attribute):
                index[node.attr][rel].add(node.end_lineno)
            elif isinstance(node, ast.alias):
                index[node.asname or node.name.split(".")[0]][rel].add(node.lineno)
                if node.asname is None and "." not in node.name:
                    continue
                index[node.name.rsplit(".", 1)[-1]][rel].add(node.lineno)
    return {n: {p: sorted(ls) for p, ls in files.items()} for n, files in index.items()}


def _write(path: Path, data) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {path} ({path.stat().st_size} bytes)", file=sys.stderr)


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "name_index.json.gz", build(src))
    _write(out / "test_strings.json.gz", TestIndex(src)._build())


if __name__ == "__main__":
    main()
