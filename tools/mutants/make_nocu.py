#!/usr/bin/env python3
"""DEV-ONLY: targeted mutants for NOCU_METHOD (blast/select.py): class-body edits in classes the
map has no class_users for (probe-F: the last practice FULL is "no class_users", a=7).

Kinds, each in a sqlglot class with no class_users entry, in a method the map credits tests with:
  top_raise   `raise RuntimeError` inserted as the first body statement (insertion: the def header
              is the neighbour line, a class-body line)                     -> breaks the callers
  top_noop    `_nocu = 0` inserted the same way                             -> breaks nothing
  default     a constant default argument changed (bool flipped, int + 1, str changed)
  deco        `@property` removed from a property
  attr        a constant class attribute changed
  new_method  a new method inserted between two methods
  imp_break   a class-level `_nocu = _nocu_undefined` (NameError at import): one in a module that
              `import sqlglot` loads (every test fails), one in a lazily loaded module
Usage (mapgen image, repo root): python tools/mutants/make_nocu.py --out mutants_nocu
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
from blast import mapdb  # noqa: E402
from blast import select as sel  # noqa: E402

QUOTA = {"top_raise": 5, "top_noop": 3, "default": 4, "deco": 2, "attr": 3, "new_method": 1}


def patch(path: str, old: str, new: str) -> str:
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\n{body}"


def insert_at(text: str, line: int, stmt: str, indent_from: int | None = None) -> str:
    lines = text.split("\n")
    ref = lines[(indent_from or line) - 1]
    lines.insert(line - 1, ref[: len(ref) - len(ref.lstrip())] + stmt)
    return "\n".join(lines)


def replace_seg(text: str, node: ast.AST, new: str) -> str | None:
    if node.lineno != node.end_lineno:
        return None
    lines = text.split("\n")
    ln = lines[node.lineno - 1]
    lines[node.lineno - 1] = ln[: node.col_offset] + new + ln[node.end_col_offset:]
    return "\n".join(lines)


def changed_const(c: ast.Constant) -> str | None:
    v = c.value
    if isinstance(v, bool):
        return repr(not v)
    if isinstance(v, int):
        return repr(v + 1)
    if isinstance(v, str) and len(v) < 40 and "\n" not in v:
        return repr(v + "_nocu")
    return None


def classes(tree):
    def walk(body, qual):
        for n in body:
            if isinstance(n, ast.ClassDef):
                q = qual + [n.name]
                yield n, q
                yield from walk(n.body, q)
    yield from walk(tree.body, [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="mutants_nocu")
    args = ap.parse_args()
    out = ROOT / args.out / "pending"
    out.mkdir(parents=True, exist_ok=True)
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    db = mapdb.load({t["id"]: t["number"] for t in tests})
    ctx = sel.Ctx(SRC, db, tests)
    cand = {k: [] for k in QUOTA}
    imp_loaded, imp_lazy = [], []
    files = sorted(os.path.relpath(f, SRC) for f in glob.glob(f"{SRC}/sqlglot/**/*.py", recursive=True))
    for f in files:
        if f.endswith("__main__.py") or not db.has_file(f):
            continue
        text = (SRC / f).read_text("utf-8")
        tree = ast.parse(text)
        ran = set(db.file_lines(f))
        mod = sel.module_of(f)
        for cls, qual in classes(tree):
            if db.class_user_tests(f"{mod}:{'.'.join(qual)}") is not None:
                continue
            if cls.decorator_list:
                continue
            methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)]
            live = [m for m in methods
                    if set(range(m.body[0].lineno, m.end_lineno + 1)) & ran]
            first_stmt_line = cls.body[0].lineno
            (imp_loaded if ctx.loaded_by_import_sqlglot(f) else imp_lazy).append(
                (f, insert_at(text, first_stmt_line, "_nocu = _nocu_undefined")))
            for m in live:
                body = m.body
                if (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                        and len(body) > 1):
                    body = body[1:]
                tgt = body[0].lineno
                if any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in ast.walk(m)):
                    pass
                else:
                    cand["top_raise"].append((f, insert_at(text, tgt, 'raise RuntimeError("nocu mutant")')))
                cand["top_noop"].append((f, insert_at(text, tgt, "_nocu = 0")))
                for d in m.args.defaults + [x for x in m.args.kw_defaults if x is not None]:
                    if isinstance(d, ast.Constant) and changed_const(d):
                        new = replace_seg(text, d, changed_const(d))
                        if new:
                            cand["default"].append((f, new))
                        break
                for dec in m.decorator_list:
                    if isinstance(dec, ast.Name) and dec.id == "property":
                        lines = text.split("\n")
                        del lines[dec.lineno - 1]
                        cand["deco"].append((f, "\n".join(lines)))
            for n in cls.body:
                if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and isinstance(n.value, ast.Constant) and changed_const(n.value)):
                    new = replace_seg(text, n.value, changed_const(n.value))
                    if new:
                        cand["attr"].append((f, new))
            if len(methods) >= 2:
                cand["new_method"].append((f, insert_at(
                    text, methods[1].lineno - len(methods[1].decorator_list),
                    "def _nocu_method(self):\n" + " " * (methods[1].col_offset + 4) + "return 1\n",
                    indent_from=methods[1].lineno)))
    made = []
    for kind, q in QUOTA.items():
        items = cand[kind]
        seen, picks = set(), []
        for f, new in items:  # spread over files
            if f not in seen:
                seen.add(f)
                picks.append((f, new))
        step = max(1, len(picks) // q)
        for f, new in picks[::step][:q]:
            made.append((kind, f, patch(f, (SRC / f).read_text("utf-8"), new)))
    for f, new in (imp_loaded[:1] + imp_lazy[:1]):
        made.append(("imp_break", f, patch(f, (SRC / f).read_text("utf-8"), new)))
    for kind, path, text in made:
        mid = "u" + hashlib.sha1(text.encode()).hexdigest()[:5]
        (out / f"{mid}.patch").write_text(text, "utf-8")
        (out / f"{mid}.json").write_text(json.dumps({"kind": "code", "detail": kind, "where": path}), "utf-8")
        print(mid, kind, path)
    print({k: len(v) for k, v in cand.items()}, len(imp_loaded), len(imp_lazy), file=sys.stderr)


if __name__ == "__main__":
    main()
