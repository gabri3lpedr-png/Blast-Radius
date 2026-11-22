"""Build the selector's indexes at docker BUILD time from the pinned sqlglot source.

<out>/name_index.json.gz    name -> {path: [lines]} over sqlglot/** and tests/**: every place
                            a name is used or bound (ast.Name ids, attribute names x.NAME,
                            import aliases). Rule b) uses it to find who sees a module-level
                            name when its definition changes.
<out>/test_strings.json.gz  [[test id, string literals]] for the fixture-name fallback.
<out>/imports.json          static import graph (blast/imports.py): who imports whom, at
                            import time or inside a def; defs that import dynamically.
<out>/subclasses.json       "module:qualname" -> direct subclasses ("module:qualname"), from
                            __subclasses__() after importing EVERY module under sqlglot/ (a fresh,
                            isolated interpreter; __main__ skipped): a class only lists the
                            subclasses that are loaded. ATTR_LEVEL (rule b') reads it.
<out>/import_used.json      "module:qualname" of every sqlglot class USED while every sqlglot
                            module imports (same fresh interpreter walk as subclasses.json, under
                            sys.setprofile): the type of an INSTANCE passed as the first argument
                            (self) of a sqlglot Python function (classmethods do not count), outside
                            class-creation hooks (__init_subclass__, a metaclass __new__/__init__).
                            E.g. generators/duckdb.py parses SQL in its class body, which builds
                            Select nodes. ATTR_IMPORT_FULL reads it.
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
from blast import imports  # noqa: E402


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


_SUBCLASS_PROBE = """
import importlib, json, os, pkgutil, sys
src = os.path.realpath(sys.argv[1])
sys.path.insert(0, src)
import sqlglot
failed = []
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if info.name.split(".")[-1] == "__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception as e:  # an optional module that cannot load here cannot load in a test either
        failed.append(f"{info.name}: {type(e).__name__}")
def key(c):
    return f"{c.__module__}:{c.__qualname__}"
out, seen, todo = {}, set(), [object]
while todo:
    c = todo.pop()
    try:
        subs = type.__subclasses__(c) if isinstance(c, type) else []
    except TypeError:
        subs = []
    for s in subs:
        if s.__module__.startswith("sqlglot") and c.__module__.startswith("sqlglot"):
            out.setdefault(key(c), set()).add(key(s))
        if s not in seen:
            seen.add(s)
            todo.append(s)
print(json.dumps({"subclasses": {k: sorted(v) for k, v in sorted(out.items())}, "failed": failed}))
"""


_USED_PROBE = """
import importlib, json, os, pkgutil, sys
src = os.path.realpath(sys.argv[1])
sys.path.insert(0, src)
pre = src + os.sep + "sqlglot" + os.sep
HOOKS = {"__init_subclass__", "__new__", "__init__", "__set_name__", "__class_getitem__"}
used, hook_frames = set(), set()
def key(c):
    return f"{c.__module__}:{c.__qualname__}"
def prof(frame, event, arg):
    if event == "return":
        hook_frames.discard(id(frame))
        return
    if event != "call":
        return
    co = frame.f_code
    if not co.co_filename.startswith(pre):
        return
    first = frame.f_locals.get(co.co_varnames[0]) if co.co_argcount else None
    if co.co_name in HOOKS and isinstance(first, type) or co.co_name == "__init_subclass__":
        hook_frames.add(id(frame))
        return
    if hook_frames:
        return
    if isinstance(first, type):
        return  # a classmethod (Func.sql_names() for every function at import): not an instance
    c = type(first)
    if getattr(c, "__module__", "").startswith("sqlglot"):
        used.add(key(c))
sys.setprofile(prof)
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if info.name.split(".")[-1] == "__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
sys.setprofile(None)
print(json.dumps({"used": sorted(used)}))
"""


def import_used(src: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _USED_PROBE, str(src)],
        capture_output=True, text=True, check=True, cwd=str(src),
    )
    rec = json.loads(proc.stdout.strip().splitlines()[-1])
    if "sqlglot.expressions.query:Select" not in rec["used"]:
        raise RuntimeError("import_used probe did not see Select built at import")
    return rec


def subclass_index(src: Path, require_generator: bool = True) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _SUBCLASS_PROBE, str(src)],
        capture_output=True, text=True, check=True, cwd=str(src),
    )
    rec = json.loads(proc.stdout.strip().splitlines()[-1])
    if require_generator and not rec["subclasses"].get("sqlglot.generator:Generator"):
        raise RuntimeError("subclass probe found no Generator subclasses")
    return rec


def _write(path: Path, data) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {path} ({path.stat().st_size} bytes)", file=sys.stderr)


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "name_index.json.gz", build(src))
    _write(out / "test_strings.json.gz", TestIndex(src)._build())
    (out / "imports.json").write_text(json.dumps(imports.build(src), separators=(",", ":")), "utf-8")
    print(f"wrote {out / 'imports.json'}", file=sys.stderr)
    sub = subclass_index(src)
    (out / "subclasses.json").write_text(json.dumps(sub, separators=(",", ":")), "utf-8")
    print(f"wrote {out / 'subclasses.json'} ({len(sub['subclasses'])} classes with subclasses, "
          f"{len(sub['failed'])} modules failed to import: {sub['failed'][:5]})", file=sys.stderr)
    used = import_used(src)
    (out / "import_used.json").write_text(json.dumps(used, separators=(",", ":")), "utf-8")
    print(f"wrote {out / 'import_used.json'} ({len(used['used'])} classes)", file=sys.stderr)
    mods = import_sqlglot_modules(src)
    (out / "import_sqlglot.json").write_text(json.dumps(mods), "utf-8")
    print(f"wrote {out / 'import_sqlglot.json'} ({len(mods)} modules)", file=sys.stderr)


if __name__ == "__main__":
    main()
