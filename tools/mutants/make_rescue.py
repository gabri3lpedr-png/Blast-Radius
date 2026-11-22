#!/usr/bin/env python3
"""DEV-ONLY: targeted mutants for RESCUE_IMPORT / RESCUE_NOTHING (blast/select.py).

Writes <out>/pending/<id>.patch for edits that reach the rescued FULL paths:
  dead      a `raise RuntimeError` as the first statement of a def no test runs (map: no body
            line ran, none at import), one per file over the files that have such defs
  dead2     the same def's last `return` value replaced by None (a quieter change)
  imp       a module-level `assert False` / `if __name__ == "__main__"` block in a module that
            `import sqlglot` does not load (binds no plain names -> RESCUE_IMPORT's closure)
  data      a line appended to a tests/ data file no test reads or names
  outside   a raise at the top of a .py outside sqlglot/ and tests/ that nothing names
Every patch is a git-style unified diff against the pinned tree (.cache/sqlglot).

Usage: python3 tools/mutants/make_rescue.py [--out mutants_rescue] [--per-kind N]
"""

from __future__ import annotations

import argparse
import ast
import difflib
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from eval_sample import index_dir, sqlglot_src  # noqa: E402

SRC = sqlglot_src()
os.environ.setdefault("INDEX_DIR", str(index_dir(SRC)))
from blast import mapdb, rescue  # noqa: E402
from blast import select as sel  # noqa: E402


def patch(path: str, old: str, new: str) -> str:
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\n{body}"


def insert_at(text: str, line: int, stmt: str) -> str:
    lines = text.split("\n")
    indent = lines[line - 1][: len(lines[line - 1]) - len(lines[line - 1].lstrip())]
    lines.insert(line - 1, indent + stmt)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="mutants_rescue")
    ap.add_argument("--per-kind", type=int, default=12)
    args = ap.parse_args()
    out = ROOT / args.out / "pending"
    out.mkdir(parents=True, exist_ok=True)
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    db = mapdb.load({t["id"]: t["number"] for t in tests})
    ctx = sel.Ctx(SRC, db, tests)
    made: list[tuple[str, str, str]] = []  # (kind, path, patch)

    files = sorted(os.path.relpath(f, SRC) for f in glob.glob(f"{SRC}/sqlglot/**/*.py", recursive=True))
    dead, dead2 = [], []
    for f in files:
        if ctx.unused(f) or not db.has_file(f):
            continue
        ran, imp = set(db.file_lines(f)), set(db.import_lines(f))
        text = (SRC / f).read_text("utf-8")
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or not n.body:
                continue
            body = n.body
            if isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and len(body) > 1:
                body = body[1:]  # after the docstring
            span = set(range(n.body[0].lineno, n.end_lineno + 1))
            if span & ran or span & imp or any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in ast.walk(n)):
                continue
            first = body[0]
            dead.append((f, insert_at(text, first.lineno, 'raise RuntimeError("rescue mutant")')))
            rets = [x for x in ast.walk(n) if isinstance(x, ast.Return) and x.value is not None
                    and x.lineno == x.end_lineno]
            if rets:
                r = rets[-1]
                lines = text.split("\n")
                src_line = lines[r.lineno - 1]
                seg = ast.get_source_segment(text, r)
                if seg and seg in src_line:
                    lines[r.lineno - 1] = src_line.replace(seg, "return None", 1)
                    dead2.append((f, "\n".join(lines)))
            break  # one def per file
    for kind, items in (("dead", dead), ("dead2", dead2)):
        for f, new in items[: args.per_kind]:
            made.append((kind, f, patch(f, (SRC / f).read_text("utf-8"), new)))

    lazy = [f for f in files if not ctx.loaded_by_import_sqlglot(f) and not ctx.unused(f)
            and db.has_file(f) and not f.endswith("__main__.py")]
    picks = [f for f in lazy if f.startswith(("sqlglot/generators/", "sqlglot/parsers/", "sqlglot/dialects/",
                                              "sqlglot/typing/", "sqlglot/executor/", "sqlglot/planner"))]
    step = max(1, len(picks) // max(1, args.per_kind))
    for i, f in enumerate(picks[::step][: args.per_kind]):
        text = (SRC / f).read_text("utf-8")
        tail = "\n\nassert False, 'rescue mutant'\n" if i % 2 == 0 else \
            "\n\nif __name__ == \"__main__\":\n    print(\"rescue mutant\")\n"
        made.append(("imp", f, patch(f, text, text.rstrip("\n") + tail)))

    for p in sorted(glob.glob(f"{SRC}/tests/**/*", recursive=True)):
        rel = os.path.relpath(p, SRC)
        if not os.path.isfile(p) or rel.endswith((".py", ".pyc")) or db.file_read_set_id(rel) is not None:
            continue
        name = rel.rsplit("/", 1)[-1]
        if rescue.mentions(SRC, [name, name.split(".", 1)[0]]):
            continue
        text = Path(p).read_text("utf-8", "replace")
        made.append(("data", rel, patch(rel, text, text.rstrip("\n") + "\nrescue mutant line\n")))

    for p in sorted(glob.glob(f"{SRC}/*/**/*.py", recursive=True)):
        rel = os.path.relpath(p, SRC)
        if rel.startswith(("sqlglot/", "tests/", ".")) or "/" not in rel:
            continue
        top, name = rel.split("/", 1)[0], rel.rsplit("/", 1)[-1]
        if top in ("sqlglotc", "sqlglotrs") or rescue.mentions(SRC, [top, name]):
            continue
        text = Path(p).read_text("utf-8")
        made.append(("outside", rel, patch(rel, text, 'raise SystemExit("rescue mutant")\n' + text)))

    for kind, path, text in made:
        mid = "r" + hashlib.sha1(text.encode()).hexdigest()[:5]
        (out / f"{mid}.patch").write_text(text, "utf-8")
        print(mid, kind, path)


if __name__ == "__main__":
    main()
