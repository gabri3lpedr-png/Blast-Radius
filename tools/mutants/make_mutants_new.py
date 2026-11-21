#!/usr/bin/env python3
"""Generalisation mutant set (DEV-ONLY): kinds the first generator never makes.

Runs inside the mapgen image (Python 3.13, pristine sqlglot at /opt/sqlglot):
  python /mutants/make_mutants_new.py --n 100 --seed 7 --out /mn/pending
Same output format as make_mutants.py (<id>.patch + <id>.json), so label.py
labels it and `make sample SAMPLE=$(pwd)/mutants_new` grades the selector.
Never shipped, never read by the selector, never committed (mutants_new/ is
git-ignored).

Kinds (counts for --n 100; scaled otherwise):
  multiline_edit   2-3 real token edits within a few lines of one function
  delete_stmt      delete one whole statement in a function body
  delete_return    delete a `return` that is not the function's last statement
  swap_args        swap the first two positional arguments of a call
  rename_local     rename a local variable everywhere in its function (harmless)
  add_method       add a new method; for generators often `<expr key>_sql`,
                   which the dispatch table picks up (a real change)
  add_dict_entry   add an entry to a class-level dict: a key copied from the
                   same-named dict of another class, or a brand-new key
  default_arg      change a default argument value
  tests_edit       tests/*.py: an assert's expected value, a deleted assert, a
                   helper or setUp/setUpClass edit
  fixture_lines    fixtures: add or remove whole lines / a whole block
  area_edit        any of the code kinds above, inside sqlglot/executor/,
                   sqlglot/optimizer/, sqlglot/typing/, sqlglot/jsonpath.py,
                   sqlglot/time.py
  combo            3-5 files, mixing the kinds above
"""
import argparse
import ast
import hashlib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import make_mutants as mm  # noqa: E402  (helpers: read, git_diff, token_candidates, ...)
from make_mutants import Skip, read, parses  # noqa: E402

COUNTS = {
    "multiline_edit": 8, "delete_stmt": 8, "delete_return": 6, "swap_args": 7,
    "rename_local": 7, "add_method": 7, "add_dict_entry": 8, "default_arg": 7,
    "tests_edit": 9, "fixture_lines": 7, "area_edit": 14, "combo": 12,
}
SAMPLE_KIND = {k: "code" for k in COUNTS}
SAMPLE_KIND.update({"fixture_lines": "fixture", "combo": "multi"})

AREA = [p for p in mm.SQLGLOT_PY if re.match(
    r"sqlglot/(executor|optimizer|typing)/|sqlglot/(jsonpath|time)\.py$", p)]
AREA_GROUPS = {
    "executor": [p for p in AREA if p.startswith("sqlglot/executor/")],
    "optimizer": [p for p in AREA if p.startswith("sqlglot/optimizer/")],
    "typing": [p for p in AREA if p.startswith("sqlglot/typing/")],
    "jsonpath": ["sqlglot/jsonpath.py"],
    "time": ["sqlglot/time.py"],
}
CODE_FILES = [p for p in mm.SQLGLOT_PY if not p.endswith("__init__.py") or "dialects" not in p]
GENERATORS = [p for p in mm.SQLGLOT_PY if re.match(r"sqlglot/(generators/|generator\.py$)", p)]


# ---------------------------------------------------------------- position helpers
def lines_of(text):
    return text.splitlines(keepends=True)


def offset(lines, ln, col_bytes):
    """ast gives UTF-8 byte columns; convert (1-based line, byte col) to a str offset."""
    before = sum(len(l) for l in lines[:ln - 1])
    return before + len(lines[ln - 1].encode()[:col_bytes].decode("utf-8", "ignore"))


def seg(text, lines, node):
    return text[offset(lines, node.lineno, node.col_offset):offset(lines, node.end_lineno, node.end_col_offset)]


def replace_spans(text, spans):
    """spans: [(start, end, new)] non-overlapping; applied right to left."""
    for s, e, new in sorted(spans, reverse=True):
        text = text[:s] + new + text[e:]
    return text


def functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def is_docstring(st):
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def pick_file(files):
    return random.choice(files)


def done(path, old, new, detail):
    if new == old or not parses(new):
        raise Skip
    return {path: new}, detail


# ---------------------------------------------------------------- code kinds
def k_multiline_edit(files=None):
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    fns = [f for f in functions(tree) if f.end_lineno - f.lineno >= 4]
    if not fns:
        raise Skip
    fn = random.choice(fns)
    body = set(range(fn.body[0].lineno, fn.end_lineno + 1))
    cands = mm.token_candidates(text, body)
    if len(cands) < 2:
        raise Skip
    first = random.choice(cands)
    near = [c for c in cands if c[0] != first[0] and abs(c[0] - first[0]) <= 4]
    if not near:
        raise Skip
    picks = [first] + random.sample(near, min(len(near), random.choice([1, 2])))
    new = text
    for c in sorted(picks, key=lambda c: (c[0], c[1]), reverse=True):
        new = mm.apply_token(new, c)
    lns = sorted({c[0] for c in picks})
    return done(path, text, new, f"{path}:{lns} {fn.name}: " + ", ".join(c[4] for c in picks))


def _stmt_lists(fn):
    """Every statement list inside a function (body, orelse, handlers...)."""
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            sts = getattr(node, field, None)
            if isinstance(sts, list) and sts and isinstance(sts[0], ast.stmt):
                yield sts
        if isinstance(node, ast.Try):
            for h in node.handlers:
                yield h.body


def _delete_stmt(text, st, sts):
    lines = lines_of(text)
    if len(sts) > 1:
        new_lines = lines[:st.lineno - 1] + lines[st.end_lineno:]
    else:  # keep the block non-empty
        indent = re.match(r"\s*", lines[st.lineno - 1]).group()
        new_lines = lines[:st.lineno - 1] + [indent + "pass\n"] + lines[st.end_lineno:]
    return "".join(new_lines)


def k_delete_stmt(files=None):
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    choices = []
    for fn in functions(tree):
        for sts in _stmt_lists(fn):
            for st in sts:
                if is_docstring(st) or isinstance(st, (ast.Return, ast.FunctionDef, ast.ClassDef)):
                    continue
                if isinstance(st, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.If,
                                   ast.For, ast.With, ast.Raise, ast.Assert)):
                    choices.append((fn, st, sts))
    if not choices:
        raise Skip
    fn, st, sts = random.choice(choices)
    new = _delete_stmt(text, st, sts)
    return done(path, text, new, f"{path}:{st.lineno}-{st.end_lineno} {fn.name}: deleted "
                f"{type(st).__name__}")


def k_delete_return(files=None):
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    choices = []
    for fn in functions(tree):
        for sts in _stmt_lists(fn):
            for st in sts:
                # a return that is not the function's final statement (an early exit)
                if isinstance(st, ast.Return) and st is not fn.body[-1]:
                    choices.append((fn, st, sts))
    if not choices:
        raise Skip
    fn, st, sts = random.choice(choices)
    new = _delete_stmt(text, st, sts)
    return done(path, text, new, f"{path}:{st.lineno} {fn.name}: deleted early return")


def k_swap_args(files=None):
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    lines = lines_of(text)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and len(n.args) >= 2
             and not any(isinstance(a, ast.Starred) for a in n.args[:2])]
    random.shuffle(calls)
    for c in calls[:20]:
        a, b = c.args[0], c.args[1]
        sa, sb = seg(text, lines, a), seg(text, lines, b)
        if sa == sb:
            continue
        sp = [(offset(lines, a.lineno, a.col_offset), offset(lines, a.end_lineno, a.end_col_offset), sb),
              (offset(lines, b.lineno, b.col_offset), offset(lines, b.end_lineno, b.end_col_offset), sa)]
        new = replace_spans(text, sp)
        if parses(new):
            return {path: new}, f"{path}:{c.lineno} swapped args {sa[:20]!r} <-> {sb[:20]!r}"
    raise Skip


def k_rename_local(files=None):
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    lines = lines_of(text)
    fns = functions(tree)
    random.shuffle(fns)
    for fn in fns[:30]:
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
        if fn.args.vararg:
            params.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            params.add(fn.args.kwarg.arg)
        declared = {n for st in ast.walk(fn) if isinstance(st, (ast.Global, ast.Nonlocal)) for n in st.names}
        stored = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        local = sorted(stored - params - declared)
        if not local:
            continue
        name = random.choice(local)
        new_name = f"{name}_r"
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        if new_name in used:
            continue
        # every Name node with that id inside this function (nested scopes too:
        # closures may read it). f-strings: Name nodes inside JoinedStr also appear.
        spans = [(offset(lines, n.lineno, n.col_offset), offset(lines, n.end_lineno, n.end_col_offset),
                  new_name) for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == name]
        # a nested def/lambda parameter with the same name would be a different variable
        if any(isinstance(a, ast.arg) and a.arg == name for a in ast.walk(fn)):
            continue
        new = replace_spans(text, spans)
        if parses(new):
            return {path: new}, f"{path}:{fn.lineno} {fn.name}: renamed local {name} ({len(spans)} uses)"
    raise Skip


def _classes(tree):
    out = []

    def walk(node, prefix):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.ClassDef):
                q = f"{prefix}.{c.name}" if prefix else c.name
                out.append((q, c))
                walk(c, q)
            elif not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(c, prefix)
    walk(tree, "")
    return out


def _append_to_class(text, cls, block):
    lines = lines_of(text)
    last = cls.body[-1]
    indent = re.match(r"\s*", lines[cls.body[0].lineno - 1]).group()
    add = ["\n"] + [(indent + l if l.strip() else l) + "\n" for l in block.splitlines()]
    return "".join(lines[:last.end_lineno] + add + lines[last.end_lineno:])


def k_add_method(files=None):
    import sqlglot.expressions as exp_mod  # noqa: F401
    from sqlglot import exp
    if files is None and random.random() < 0.6:
        files = GENERATORS
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    classes = [(q, c) for q, c in _classes(tree) if any(isinstance(s, ast.FunctionDef) for s in c.body)]
    if not classes:
        raise Skip
    q, cls = random.choice(classes)
    own = {s.name for s in cls.body if isinstance(s, ast.FunctionDef)}
    if path in GENERATORS and q.endswith("Generator"):
        keys = sorted(k for k in exp.EXPR_CLASSES if f"{k}_sql" not in own and k.isidentifier())
        key = random.choice(keys)
        body = random.choice([
            f"def {key}_sql(self, expression: exp.Expr) -> str:\n"
            f"    return self.function_fallback_sql(expression)",
            f"def {key}_sql(self, expression: exp.Expr) -> str:\n"
            f"    return self.func(\"{key.upper()}_X\", *expression.args.values())",
        ])
        detail = f"{path}: {q}.{key}_sql added (dispatch picks it up)"
    else:
        name = f"_helper_{random.randint(100, 999)}"
        body = (f"def {name}(self, value: t.Any = None) -> t.Any:\n"
                f"    return value")
        detail = f"{path}: {q}.{name} added (unused)"
    new = _append_to_class(text, cls, body)
    if "t." in body and not re.search(r"^import typing as t", text, re.M):
        new = new.replace(": t.Any = None) -> t.Any", "=None)")
    if "exp." in body and not re.search(r"^from sqlglot import .*\bexp\b|^import sqlglot.expressions as exp",
                                        text, re.M):
        new = new.replace("expression: exp.Expr) -> str", "expression)")
    return done(path, text, new, detail)


def _class_dicts(tree):
    out = []
    for q, c in _classes(tree):
        for st in c.body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict):
                tg = st.targets[0] if isinstance(st, ast.Assign) else st.target
                if isinstance(tg, ast.Name) and st.value.keys:
                    out.append((q, tg.id, st.value))
    return out


def _module_names(tree):
    import builtins
    names = set(dir(builtins))
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n in tree.body:
            names.add(n.name)
        elif isinstance(n, ast.Assign) and n in tree.body:
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def k_add_dict_entry(files=None):
    files = files or mm.DIALECTISH
    path = pick_file(files)
    text = read(path)
    tree = ast.parse(text)
    lines = lines_of(text)
    dicts = [d for d in _class_dicts(tree) if any(k is not None for k in d[2].keys)]
    if not dicts:
        raise Skip
    q, attr, node = random.choice(dicts)
    have = {seg(text, lines, k) for k in node.keys if k is not None}
    names = _module_names(tree)
    entry = None
    if random.random() < 0.7:
        # copy an entry the same-named dict has in another file
        others = random.sample(files, min(25, len(files)))
        for op in others:
            if op == path:
                continue
            ot = read(op)
            try:
                otree = ast.parse(ot)
            except SyntaxError:
                continue
            ol = lines_of(ot)
            for oq, oattr, onode in _class_dicts(otree):
                if oattr != attr:
                    continue
                pairs = [(k, v) for k, v in zip(onode.keys, onode.values) if k is not None
                         and seg(ot, ol, k) not in have and (onode.lineno != onode.end_lineno)]
                random.shuffle(pairs)
                for k, v in pairs:
                    used = {n.id for x in (k, v) for n in ast.walk(x) if isinstance(n, ast.Name)}
                    if used <= names and v.end_lineno == v.lineno == k.lineno:
                        entry = f"{seg(ot, ol, k)}: {seg(ot, ol, v)}"
                        src = f"copied from {op}"
                        break
                if entry:
                    break
            if entry:
                break
    if entry is None:
        # a brand-new key, value copied from an existing entry of this dict
        pairs = [(k, v) for k, v in zip(node.keys, node.values) if k is not None and v.lineno == v.end_lineno]
        if not pairs:
            raise Skip
        k, v = random.choice(pairs)
        ks = seg(text, lines, k)
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            nk = repr(k.value + "_NEW")
        else:
            raise Skip
        entry = f"{nk}: {seg(text, lines, v)}"
        src = "new key"
    last = [x for x in node.values][-1]
    if node.lineno == node.end_lineno:
        raise Skip
    indent = re.match(r"\s*", lines[last.lineno - 1]).group()
    ln = last.end_lineno
    new_lines = lines[:ln] + [f"{indent}{entry},\n"] + lines[ln:]
    # the line before must end with a comma
    if not new_lines[ln - 1].rstrip().endswith(","):
        new_lines[ln - 1] = new_lines[ln - 1].rstrip("\n") + ",\n"
    new = "".join(new_lines)
    return done(path, text, new, f"{path}:{ln + 1} {q}.{attr} + {entry[:40]} ({src})")


def k_default_arg(files=None):
    path = pick_file(files or CODE_FILES)
    text = read(path)
    tree = ast.parse(text)
    lines = lines_of(text)
    cands = []
    for fn in functions(tree):
        for d in fn.args.defaults + [d for d in fn.args.kw_defaults if d is not None]:
            if isinstance(d, ast.Constant):
                cands.append((fn, d))
    if not cands:
        raise Skip
    fn, d = random.choice(cands)
    v = d.value
    if v is True or v is False:
        rep = repr(not v)
    elif v is None:
        rep = random.choice(["True", "False", "0", '""'])
    elif isinstance(v, int):
        rep = str(v + random.choice([-1, 1]))
    elif isinstance(v, str):
        rep = repr(v.upper() if v != v.upper() else (v + "_x"))
    else:
        raise Skip
    s, e = offset(lines, d.lineno, d.col_offset), offset(lines, d.end_lineno, d.end_col_offset)
    new = text[:s] + rep + text[e:]
    return done(path, text, new, f"{path}:{d.lineno} {fn.name}: default {text[s:e]} -> {rep}")


# ---------------------------------------------------------------- tests / fixtures
def k_tests_edit():
    path = random.choice(mm.TESTS_PY)
    text = read(path)
    tree = ast.parse(text)
    lines = lines_of(text)
    mode = random.choice(["assert_value", "assert_value", "delete_assert", "helper", "setup"])
    fns = functions(tree)
    if mode == "setup":
        targets = [f for f in fns if f.name in ("setUp", "setUpClass", "tearDown", "setUpModule")]
        mode_desc = "setUp edit"
    elif mode == "helper":
        targets = [f for f in fns if not f.name.startswith("test")]
        mode_desc = "helper edit"
    else:
        targets = [f for f in fns if f.name.startswith("test")]
        mode_desc = mode
    if not targets:
        raise Skip
    fn = random.choice(targets)
    if mode in ("setup", "helper"):
        cands = mm.token_candidates(text, set(range(fn.body[0].lineno, fn.end_lineno + 1)))
        if not cands:
            # fall back to deleting a statement in it
            sts = [s for s in fn.body if not is_docstring(s)]
            if len(sts) < 2:
                raise Skip
            st = random.choice(sts)
            return done(path, text, _delete_stmt(text, st, fn.body),
                        f"{path}:{st.lineno} {fn.name}: {mode_desc} (deleted statement)")
        c = random.choice(cands)
        return done(path, text, mm.apply_token(text, c), f"{path}:{c[0]} {fn.name}: {mode_desc} {c[4]}")
    asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
               and isinstance(n.value.func, ast.Attribute)
               and n.value.func.attr.startswith(("assert", "validate"))]
    if not asserts:
        raise Skip
    st = random.choice(asserts)
    if mode == "delete_assert":
        sts = next(s for s in _stmt_lists(fn) if st in s)
        return done(path, text, _delete_stmt(text, st, sts),
                    f"{path}:{st.lineno} {fn.name}: deleted {st.value.func.attr}")
    # change an expected value: last string/number constant argument of the call
    consts = [a for a in list(st.value.args) + [k.value for k in st.value.keywords]
              for a in ast.walk(a) if isinstance(a, ast.Constant) and isinstance(a.value, (str, int))
              and not isinstance(a.value, bool) and a.lineno == a.end_lineno]
    if not consts:
        raise Skip
    a = consts[-1] if random.random() < 0.6 else random.choice(consts)
    s, e = offset(lines, a.lineno, a.col_offset), offset(lines, a.end_lineno, a.end_col_offset)
    old = text[s:e]
    if isinstance(a.value, int):
        rep = str(a.value + 1)
    else:
        v = a.value
        rep = repr(v.swapcase() if v.lower() != v.upper() else v + " ")
    new = text[:s] + rep + text[e:]
    return done(path, text, new, f"{path}:{a.lineno} {fn.name}: {st.value.func.attr} expected "
                f"{old[:25]} -> {rep[:25]}")


def k_fixture_lines():
    path = random.choice([p for p in mm.FIXTURES if not p.endswith((".gz", ".csv", ".parquet"))
                          and "LICENSE" not in p])
    text = read(path)
    lines = lines_of(text)
    content = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("--", "#"))]
    if len(content) < 3:
        raise Skip
    mode = random.choice(["remove_line", "remove_block", "duplicate_line", "add_line"])
    if mode == "remove_block":
        # blank-line separated block (a fixture pair / case)
        blocks, cur = [], []
        for i, l in enumerate(lines):
            if l.strip():
                cur.append(i)
            elif cur:
                blocks.append(cur)
                cur = []
        if cur:
            blocks.append(cur)
        blocks = [b for b in blocks if any(i in content for i in b)]
        if len(blocks) < 2:
            raise Skip
        b = random.choice(blocks)
        drop = set(b) | ({b[-1] + 1} if b[-1] + 1 < len(lines) and not lines[b[-1] + 1].strip() else set())
        new = "".join(l for i, l in enumerate(lines) if i not in drop)
        detail = f"{path}:{b[0] + 1}-{b[-1] + 1} removed block"
    elif mode == "remove_line":
        i = random.choice(content)
        new = "".join(lines[:i] + lines[i + 1:])
        detail = f"{path}:{i + 1} removed line"
    elif mode == "duplicate_line":
        i = random.choice(content)
        new = "".join(lines[:i + 1] + [lines[i]] + lines[i + 1:])
        detail = f"{path}:{i + 1} duplicated line"
    else:
        i = random.choice(content)
        extra = "SELECT 1;\n" if path.endswith(".sql") else lines[i]
        new = "".join(lines[:i + 1] + [extra] + lines[i + 1:])
        detail = f"{path}:{i + 2} added line"
    if new == text:
        raise Skip
    return {path: new}, detail


# ---------------------------------------------------------------- areas / combos
CODE_KINDS = [k_multiline_edit, k_delete_stmt, k_delete_return, k_swap_args, k_rename_local,
              k_default_arg]


def k_area_edit():
    group = random.choice(list(AREA_GROUPS))
    files = AREA_GROUPS[group]
    if not files:
        raise Skip
    files_d, detail = random.choice(CODE_KINDS + [mm.m_code_token])(files=files)
    return files_d, f"[{group}] {detail}"


def k_combo():
    k = random.choice([3, 3, 4, 5])
    makers = CODE_KINDS + [k_add_dict_entry, k_add_method, k_tests_edit, k_fixture_lines, k_area_edit]
    files, details = {}, []
    tries = 0
    while len(files) < k and tries < 40:
        tries += 1
        try:
            f, d = random.choice(makers)()
        except (Skip, IndexError, ValueError, SyntaxError, UnicodeDecodeError, StopIteration):
            continue
        if set(f) & set(files):
            continue
        files.update(f)
        details.append(d)
    if len(files) < 3:
        raise Skip
    return files, " + ".join(details)


KINDS = {
    "multiline_edit": k_multiline_edit, "delete_stmt": k_delete_stmt, "delete_return": k_delete_return,
    "swap_args": k_swap_args, "rename_local": k_rename_local, "add_method": k_add_method,
    "add_dict_entry": k_add_dict_entry, "default_arg": k_default_arg, "tests_edit": k_tests_edit,
    "fixture_lines": k_fixture_lines, "area_edit": k_area_edit, "combo": k_combo,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    random.seed(args.seed)
    os.makedirs(args.out, exist_ok=True)
    total = sum(COUNTS.values())
    plan = [k for k, c in COUNTS.items() for _ in range(round(c * args.n / total))]
    random.shuffle(plan)
    seen, made = set(), {}
    for kind in plan:
        for attempt in range(60):
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
        mid = "n" + hashlib.sha1(patch.encode()).hexdigest()[:5]
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
