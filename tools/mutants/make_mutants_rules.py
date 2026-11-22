#!/usr/bin/env python3
"""Mutants aimed at the narrowing rules of branch victor/full (DEV-ONLY).

Runs inside the mapgen image (Python 3.13, pristine sqlglot at /opt/sqlglot):
  python /mutants/make_mutants_rules.py --n 120 --seed 21 --map /repo/maps/coverage_map.json.gz --out /mr/pending
Same output format as make_mutants.py, so label.py labels it. Never shipped, never committed
(the output lives under mutants_new/, git-ignored).

Kinds (counts for --n 120; scaled otherwise):
  dead_token    one-token edit inside a def whose body the map credits to no test (INERT_EMPTY)
  dead_raise    `raise` as the first statement of such a def
  near_dead     edit on an uncovered statement of a def that IS partly covered
  tc_body       add or change an import inside `if TYPE_CHECKING:` (INERT_EMPTY)
  tc_header     `if TYPE_CHECKING:` -> `if True:` (the block now runs: NOT inert)
  try_edit      edit inside a module-level try/except block (COMPOUND_NAMES)
  alias_def     edit the definition of a name some file imports under an alias (ALIAS_IMPORTS)
  main_py       edit sqlglot/__main__.py (MAIN_UNUSED)
  inert_combo   an inert edit + a real edit in another file (+ sometimes a fixture)
  random        any kind of make_mutants_new.py
"""
import argparse
import ast
import gzip
import hashlib
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
import make_mutants as mm  # noqa: E402
import make_mutants_new as mn  # noqa: E402
from make_mutants import Skip, read, parses  # noqa: E402

COUNTS = {"dead_token": 14, "dead_raise": 6, "near_dead": 16, "tc_body": 8, "tc_header": 4,
          "try_edit": 8, "alias_def": 12, "main_py": 6, "inert_combo": 18, "random": 28}
SAMPLE_KIND = {k: "code" for k in COUNTS}
SAMPLE_KIND["inert_combo"] = "multi"

COVERED: dict[str, set[int]] = {}   # path -> lines some test ran (map "lines")
IMPORTED: dict[str, set[int]] = {}  # path -> import_lines


def load_map(path):
    m = json.load(gzip.open(path, "rt"))
    for p, d in m["lines"].items():
        COVERED[p] = {int(k) for k in d}
    for p, ls in m["import_lines"].items():
        IMPORTED[p] = set(ls)


def done(path, old, new, detail):
    if new == old or not parses(new):
        raise Skip
    return {path: new}, detail


def _defs(path):
    text = read(path)
    tree = ast.parse(text)
    return text, [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and (len(n.body) > 1 or not mn.is_docstring(n.body[0]))]


def _body_lines(fn):
    first = fn.body[1] if mn.is_docstring(fn.body[0]) and len(fn.body) > 1 else fn.body[0]
    return set(range(first.lineno, fn.end_lineno + 1)), first


def _dead_defs(path):
    text, defs = _defs(path)
    seen = COVERED.get(path, set()) | IMPORTED.get(path, set())
    return text, [f for f in defs if not (_body_lines(f)[0] & seen)]


def _code_files():
    return [p for p in mm.SQLGLOT_PY if p in COVERED and p != "sqlglot/__main__.py"]


def k_dead_token():
    path = random.choice(_code_files())
    text, dead = _dead_defs(path)
    if not dead:
        raise Skip
    fn = random.choice(dead)
    cands = mm.token_candidates(text, _body_lines(fn)[0])
    if not cands:
        raise Skip
    c = random.choice(cands)
    return done(path, text, mm.apply_token(text, c), f"{path}:{c[0]} dead def {fn.name}: {c[4]}")


def k_dead_raise():
    path = random.choice(_code_files())
    text, dead = _dead_defs(path)
    if not dead:
        raise Skip
    fn = random.choice(dead)
    first = _body_lines(fn)[1]
    line = text.splitlines()[first.lineno - 1]
    ind = line[: len(line) - len(line.lstrip())]
    new = mm.insert_line(text, first.lineno, f'{ind}raise RuntimeError("dead def probe")\n')
    return done(path, text, new, f"{path}:{first.lineno} dead def {fn.name}: raise")


def k_near_dead():
    path = random.choice(_code_files())
    text, defs = _defs(path)
    cov = COVERED.get(path, set())
    imp = IMPORTED.get(path, set())
    picks = []
    for fn in defs:
        body, _ = _body_lines(fn)
        if not (body & cov):
            continue
        inner = {n.lineno for n in ast.walk(fn) if isinstance(n, ast.stmt) and n is not fn
                 and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for ln in sorted(inner - cov - imp):
            picks.append((fn, ln))
    if not picks:
        raise Skip
    fn, ln = random.choice(picks)
    cands = mm.token_candidates(text, {ln})
    if cands and random.random() < 0.7:
        c = random.choice(cands)
        return done(path, text, mm.apply_token(text, c), f"{path}:{ln} uncovered line of {fn.name}: {c[4]}")
    line = text.splitlines()[ln - 1]
    ind = line[: len(line) - len(line.lstrip())]
    new = mm.insert_line(text, ln, f'{ind}raise RuntimeError("near dead probe")\n')
    return done(path, text, new, f"{path}:{ln} uncovered line of {fn.name}: raise")


def _tc_blocks():
    out = []
    for p in mm.SQLGLOT_PY:
        try:
            tree = ast.parse(read(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in tree.body:
            t = getattr(n, "test", None)
            if isinstance(n, ast.If) and (getattr(t, "id", None) == "TYPE_CHECKING"
                                          or getattr(t, "attr", None) == "TYPE_CHECKING"):
                out.append((p, n))
    return out


def k_tc_body():
    path, blk = random.choice(_tc_blocks())
    text = read(path)
    line = text.splitlines()[blk.body[0].lineno - 1]
    ind = line[: len(line) - len(line.lstrip())]
    r = random.random()
    if r < 0.4:
        new = mm.insert_line(text, blk.body[0].lineno, f"{ind}import blast_no_such_module  # noqa\n")
        what = "bad import added"
    elif r < 0.7:
        new = mm.insert_line(text, blk.body[-1].end_lineno + 1, f"{ind}from sqlglot import exp as _blast_exp  # noqa\n")
        what = "real import added"
    else:
        imps = [s for s in blk.body if isinstance(s, ast.ImportFrom) and s.names]
        if not imps:
            raise Skip
        s = random.choice(imps)
        a = s.names[0]
        lines = text.splitlines(keepends=True)
        ln = a.lineno - 1
        if a.name not in lines[ln]:
            raise Skip
        lines[ln] = lines[ln].replace(a.name, a.name + "Blast", 1)
        new = "".join(lines)
        what = f"renamed import {a.name}"
    return done(path, text, new, f"{path}:{blk.lineno} TYPE_CHECKING body: {what}")


def k_tc_header():
    path, blk = random.choice(_tc_blocks())
    text = read(path)
    lines = text.splitlines(keepends=True)
    ln = blk.lineno - 1
    old = lines[ln]
    if "TYPE_CHECKING" not in old:
        raise Skip
    lines[ln] = old.replace("t.TYPE_CHECKING", "True").replace("typing.TYPE_CHECKING", "True").replace(
        "TYPE_CHECKING", "True")
    return done(path, text, "".join(lines), f"{path}:{blk.lineno} if TYPE_CHECKING -> if True")


def k_try_edit():
    blocks = []
    for p in mm.SQLGLOT_PY:
        try:
            tree = ast.parse(read(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        blocks += [(p, n) for n in tree.body if isinstance(n, ast.Try)]
    path, blk = random.choice(blocks)
    text = read(path)
    span = set(range(blk.lineno, blk.end_lineno + 1))
    cands = mm.token_candidates(text, span)
    r = random.random()
    if cands and r < 0.5:
        c = random.choice(cands)
        return done(path, text, mm.apply_token(text, c), f"{path}:{c[0]} try block: {c[4]}")
    # rename an imported or assigned name inside the block, or change an except type
    lines = text.splitlines(keepends=True)
    if r < 0.75 and blk.handlers and blk.handlers[0].type is not None:
        h = blk.handlers[0]
        ln = h.lineno - 1
        typ = ast.unparse(h.type)
        if typ not in lines[ln]:
            raise Skip
        lines[ln] = lines[ln].replace(typ, "KeyError" if typ != "KeyError" else "ValueError", 1)
        return done(path, text, "".join(lines), f"{path}:{h.lineno} try block: except {typ} changed")
    imps = [s for s in ast.walk(blk) if isinstance(s, (ast.ImportFrom, ast.Import)) and s.names]
    if not imps:
        raise Skip
    s = random.choice(imps)
    a = s.names[0]
    ln = a.lineno - 1
    base = a.name.split(".")[-1]
    if base not in lines[ln]:
        raise Skip
    lines[ln] = lines[ln].replace(base, base + "_blast", 1)
    return done(path, text, "".join(lines), f"{path}:{a.lineno} try block: import {a.name} renamed")


def _alias_sites():
    out = []
    for p in mm.SQLGLOT_PY:
        try:
            tree = ast.parse(read(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 and n.module.startswith("sqlglot"):
                for a in n.names:
                    if a.asname and a.asname != a.name:
                        out.append((p, n.module, a.name))
    return out


def _module_path(mod):
    for cand in (mod.replace(".", "/") + ".py", mod.replace(".", "/") + "/__init__.py"):
        if cand in mm.SQLGLOT_PY:
            return cand
    return None


def k_alias_def():
    _, mod, name = random.choice(_alias_sites())
    path = _module_path(mod)
    if path is None:
        raise Skip
    text = read(path)
    tree = ast.parse(text)
    defs = [n for n in tree.body if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
            or (isinstance(n, (ast.Assign, ast.AnnAssign)) and name in {
                getattr(t, "id", None) for t in (n.targets if isinstance(n, ast.Assign) else [n.target])})]
    if not defs:
        raise Skip
    d = defs[0]
    cands = mm.token_candidates(text, set(range(d.lineno, d.end_lineno + 1)))
    if not cands:
        raise Skip
    c = random.choice(cands)
    return done(path, text, mm.apply_token(text, c), f"{path}:{c[0]} aliased-import target {name}: {c[4]}")


def k_main_py():
    path = "sqlglot/__main__.py"
    text = read(path)
    cands = mm.token_candidates(text)
    if not cands:
        raise Skip
    c = random.choice(cands)
    return done(path, text, mm.apply_token(text, c), f"{path}:{c[0]} {c[4]}")


def k_inert_combo():
    parts = [random.choice([k_dead_token, k_dead_raise, k_tc_body, k_main_py])(),
             random.choice([mm.m_code_token, mn.k_multiline_edit, mn.k_delete_stmt, mn.k_swap_args])()]
    if random.random() < 0.3:
        parts.append(mm.m_fixture())
    return mm.combine(parts)


def k_random():
    return random.choice(list(mn.KINDS.values()))()


KINDS = {"dead_token": k_dead_token, "dead_raise": k_dead_raise, "near_dead": k_near_dead,
         "tc_body": k_tc_body, "tc_header": k_tc_header, "try_edit": k_try_edit,
         "alias_def": k_alias_def, "main_py": k_main_py, "inert_combo": k_inert_combo,
         "random": k_random}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--map", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    random.seed(args.seed)
    load_map(args.map)
    os.makedirs(args.out, exist_ok=True)
    total = sum(COUNTS.values())
    plan = [k for k, c in COUNTS.items() for _ in range(round(c * args.n / total))]
    random.shuffle(plan)
    seen, made = set(), {}
    for kind in plan:
        for _attempt in range(80):
            try:
                files, detail = KINDS[kind]()
            except (Skip, IndexError, ValueError, SyntaxError, UnicodeDecodeError, StopIteration):
                continue
            patch = "".join(mm.git_diff(p, read(p), files[p]) for p in sorted(files) if files[p] != read(p))
            if patch and patch not in seen:
                break
        else:
            print(f"could not make a {kind} mutant, skipped")
            continue
        seen.add(patch)
        mid = "r" + hashlib.sha1(patch.encode()).hexdigest()[:5]
        with open(os.path.join(args.out, f"{mid}.patch"), "w") as f:
            f.write(patch)
        with open(os.path.join(args.out, f"{mid}.json"), "w") as f:
            json.dump({"kind": SAMPLE_KIND[kind], "detail": kind, "priority": False,
                       "where": detail, "files": sorted(files)}, f)
        made[kind] = made.get(kind, 0) + 1
    print(f"made {sum(made.values())} mutants in {args.out}: "
          + ", ".join(f"{k} {v}" for k, v in sorted(made.items())))


if __name__ == "__main__":
    main()
