"""Build the selector's indexes at docker BUILD time from the pinned sqlglot source.

<out>/name_index.json.gz    name -> {path: [lines]} over sqlglot/** and tests/**: every place
                            a name is used or bound (ast.Name ids, attribute names x.NAME,
                            import aliases). Rule b) uses it to find who sees a module-level
                            name when its definition changes.
<out>/test_strings.json.gz  [[test id, string literals]] for the fixture-name fallback.
<out>/import_sqlglot.json   sorted source paths of the sqlglot modules a bare `import sqlglot`
                            loads (a fresh, isolated interpreter; sys.modules afterwards).
                            Tests that run `import sqlglot` in a subprocess use it.

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
import subprocess  # noqa: E402 - after the fix above: subprocess imports `select`
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


_IMPORT_PROBE = """
import json, os, sys
src = os.path.realpath(sys.argv[1])
sys.path.insert(0, src)
import sqlglot  # noqa: F401
out = set()
for name, mod in list(sys.modules.items()):
    f = getattr(mod, "__file__", None)
    if f and (name == "sqlglot" or name.startswith("sqlglot.")):
        rel = os.path.relpath(os.path.realpath(f), src).replace(os.sep, "/")
        if not rel.startswith(".."):
            out.add(rel)
print(json.dumps(sorted(out)))
"""


def import_sqlglot_modules(src: Path) -> list[str]:
    """Run `import sqlglot` in a fresh isolated interpreter; the source files it loaded.
    Raises if the import fails: a build that cannot answer this must not ship."""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _IMPORT_PROBE, str(src)],
        capture_output=True, text=True, check=True, cwd=str(src),
    )
    mods = json.loads(proc.stdout.strip().splitlines()[-1])
    if "sqlglot/__init__.py" not in mods:
        raise RuntimeError(f"import sqlglot probe found no sqlglot/__init__.py: {mods}")
    return mods


def _write(path: Path, data) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {path} ({path.stat().st_size} bytes)", file=sys.stderr)


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "name_index.json.gz", build(src))
    _write(out / "test_strings.json.gz", TestIndex(src)._build())
    mods = import_sqlglot_modules(src)
    (out / "import_sqlglot.json").write_text(json.dumps(mods), "utf-8")
    print(f"wrote {out / 'import_sqlglot.json'} ({len(mods)} modules)", file=sys.stderr)


if __name__ == "__main__":
    main()
