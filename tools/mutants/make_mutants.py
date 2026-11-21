"""Generate mutant patches against sqlglot at the pinned commit (DEV-ONLY).

Output is for measuring our selector. It is NEVER shipped in the submission
image and NEVER read by the selector (that would be a banned patch->tests table).

Runs inside the mapgen image (Python 3.13, pristine sqlglot in /opt/sqlglot):
  python make_mutants.py --n 300 --seed 1 --out /mo/pending --map /maps/coverage_map.json.gz

Writes <out>/<id>.patch (git-style, 3 lines of context, like sample/commits)
and <out>/<id>.json ({"kind", "detail", "files"}).

Kinds (sample-like):   code_token, comment, fixture, multi, blank_line,
                       syntax_error, root_file
Priority kinds (>= 30% of the set, asked by Person B):
  dialect_attr       class attribute in a dialect/parser/generator/tokenizer
                     (WEEK_OFFSET, NUMERIC_ESCAPES, X = True, ...)
  table_entry        one entry in FUNCTIONS / TRANSFORMS / SHOW_PARSERS /
                     KEYWORDS / ... (deleted, key changed, value swapped)
  docstring          docstring-only edit
  import_only_fn     edit inside a function that only runs at import time
                     (helpers called in class bodies, metaclass code)
  tests_py           edit to tests/**/*.py
  shared_fixture     fixture file read by more than one test
  comment_plus_real  comment-only edit in one file + a real change in another
"""
import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import random
import re
import tokenize
import difflib

SRC = "/opt/sqlglot"

WEIGHTS = {
    # sample-like (50%)
    "code_token": 0.20, "comment": 0.08, "fixture": 0.07, "multi": 0.07,
    "blank_line": 0.03, "syntax_error": 0.03, "root_file": 0.02,
    # priority (50%)
    "dialect_attr": 0.09, "table_entry": 0.10, "docstring": 0.05,
    "import_only_fn": 0.07, "tests_py": 0.07, "shared_fixture": 0.05,
    "comment_plus_real": 0.07,
}
PRIORITY = {"dialect_attr", "table_entry", "docstring", "import_only_fn",
            "tests_py", "shared_fixture", "comment_plus_real"}
SAMPLE_KIND = {
    "code_token": "code", "syntax_error": "code", "dialect_attr": "code",
    "table_entry": "code", "import_only_fn": "code", "tests_py": "code",
    "comment": "comment", "blank_line": "comment", "docstring": "comment",
    "fixture": "fixture", "shared_fixture": "fixture",
    "multi": "multi", "comment_plus_real": "multi", "root_file": "other",
}
WORDS = "note todo keep this in sync see above cheap fast path handles edge case why".split()
KEYWORD_SWAPS = {"AND": "OR", "OR": "AND", "ASC": "DESC", "DESC": "ASC", "LEFT": "RIGHT",
                 "RIGHT": "LEFT", "INNER": "LEFT", "MIN": "MAX", "MAX": "MIN", "SUM": "COUNT",
                 "COUNT": "SUM", "UNION": "INTERSECT", "TRUE": "FALSE", "FALSE": "TRUE",
                 "NULL": "1", "DISTINCT": "ALL", "WHERE": "HAVING"}


class Skip(Exception):
    pass


# ---------------------------------------------------------------- files
def read(path):
    with open(os.path.join(SRC, path), encoding="utf-8") as f:
        return f.read()


def walk(prefix, pred):
    out = []
    for root, dirs, files in os.walk(os.path.join(SRC, prefix)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            p = os.path.relpath(os.path.join(root, f), SRC)
            if pred(p):
                out.append(p)
    return out


SQLGLOT_PY = walk("sqlglot", lambda p: p.endswith(".py"))
TESTS_PY = walk("tests", lambda p: p.endswith(".py") and not p.startswith("tests/fixtures/"))
FIXTURES = walk("tests/fixtures", lambda p: not p.endswith((".py", ".pyc")))
DIALECTISH = [p for p in SQLGLOT_PY if re.match(
    r"sqlglot/(dialects|parsers|generators)/|sqlglot/(tokens|parser|generator)\.py$", p)]
ROOT_FILES = [f for f in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "MANIFEST.in", "LICENSE")
              if os.path.exists(os.path.join(SRC, f))]


# ---------------------------------------------------------------- python helpers
def docstring_lines(tree):
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def token_candidates(text, line_filter=None, allow_docstrings=False):
    """One-token mutations: (line, col_start, col_end, replacement, label)."""
    tree = ast.parse(text)
    doc = set() if allow_docstrings else docstring_lines(tree)
    out = []
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    for i, t in enumerate(toks):
        (ln, c0), (ln2, c1) = t.start, t.end
        if ln != ln2 or ln in doc or (line_filter and ln not in line_filter):
            continue
        s = t.string
        if t.type == tokenize.NAME:
            rep = {"and": "or", "or": "and", "True": "False", "False": "True", "is": "=="}.get(s)
            if rep:
                out.append((ln, c0, c1, rep, f"{s}->{rep}"))
            if s == "not" and i + 1 < len(toks) and toks[i + 1].start[0] == ln:
                out.append((ln, c0, toks[i + 1].start[1], "", "deleted not"))
        elif t.type == tokenize.OP:
            rep = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}.get(s)
            if rep:
                out.append((ln, c0, c1, rep, f"{s}->{rep}"))
        elif t.type == tokenize.NUMBER and re.fullmatch(r"\d+", s):
            n = int(s)
            rep = str(n + 1 if n == 0 or random.random() < 0.5 else n - 1)
            out.append((ln, c0, c1, rep, f"{s}->{rep}"))
        elif t.type == tokenize.STRING and re.fullmatch(r"[rbuRBU]?(['\"])[^'\"\\]{1,40}\1", s):
            q = s[-1]
            body = s[s.index(q) + 1:-1]
            rep = s[:s.index(q) + 1] + (body.swapcase() if body.lower() != body.upper() else body + "_x") + q
            if rep != s:
                out.append((ln, c0, c1, rep, f"string {s[:20]}"))
    return out


def apply_token(text, cand):
    ln, c0, c1, rep, _ = cand
    lines = text.splitlines(keepends=True)
    line = lines[ln - 1]
    lines[ln - 1] = line[:c0] + rep + line[c1:]
    return "".join(lines)


def parses(text):
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def stmt_starts(tree):
    """(lineno, col) of every statement, for inserting comments/blank lines."""
    return sorted({(n.lineno, n.col_offset) for n in ast.walk(tree) if isinstance(n, ast.stmt)})


def insert_line(text, lineno, new_line):
    lines = text.splitlines(keepends=True)
    lines.insert(lineno - 1, new_line)
    return "".join(lines)


# ---------------------------------------------------------------- mutators
# Each returns ({path: new_text}, detail) or raises Skip.

def m_code_token(path=None, line_filter=None, files=SQLGLOT_PY):
    path = path or random.choice(files)
    text = read(path)
    cands = token_candidates(text, line_filter)
    random.shuffle(cands)
    for c in cands[:20]:
        new = apply_token(text, c)
        if new != text and parses(new):
            return {path: new}, f"{path}:{c[0]} {c[4]}"
    raise Skip


def m_tests_py():
    return m_code_token(files=TESTS_PY)


def m_comment(path=None):
    path = path or random.choice(SQLGLOT_PY + TESTS_PY[:20])
    text = read(path)
    words = " ".join(random.sample(WORDS, 3))
    where = random.random()
    if where < 0.2:
        new, ln = insert_line(text, 1, f"# {words}\n"), 1
    else:
        ln, col = random.choice(stmt_starts(ast.parse(text)))
        new = insert_line(text, ln, " " * col + f"# {words}\n")
    return {path: new}, f"{path}:{ln} comment"


def m_blank_line():
    path = random.choice(SQLGLOT_PY)
    text = read(path)
    ln, _ = random.choice(stmt_starts(ast.parse(text)))
    return {path: insert_line(text, ln, "\n")}, f"{path}:{ln} blank line"


def m_docstring():
    path = random.choice(SQLGLOT_PY)
    text = read(path)
    doc = sorted(docstring_lines(ast.parse(text)))
    if not doc:
        raise Skip
    lines = text.splitlines(keepends=True)
    for ln in random.sample(doc, min(10, len(doc))):
        line = lines[ln - 1]
        words = list(re.finditer(r"\b[a-z]{4,}\b", line))
        if words:
            w = random.choice(words)
            lines[ln - 1] = line[:w.start()] + w.group()[::-1] + line[w.end():]
            new = "".join(lines)
            if parses(new):
                return {path: new}, f"{path}:{ln} docstring word"
    raise Skip


def m_syntax_error():
    path = random.choice(SQLGLOT_PY)
    text = read(path)
    lines = text.splitlines(keepends=True)
    for ln in random.sample(range(1, len(lines) + 1), min(50, len(lines))):
        line = lines[ln - 1]
        if line.rstrip().endswith(":") and not line.lstrip().startswith("#"):
            lines[ln - 1] = line.rstrip()[:-1] + "\n"
            new = "".join(lines)
            if not parses(new):
                return {path: new}, f"{path}:{ln} removed ':'"
    raise Skip


def m_root_file():
    if not ROOT_FILES:
        raise Skip
    path = random.choice(ROOT_FILES)
    text = read(path)
    lines = text.splitlines(keepends=True)
    ln = random.randint(1, max(1, len(lines)))
    return {path: insert_line(text, ln, " ".join(random.sample(WORDS, 4)) + "\n")}, f"{path}:{ln} text"


def m_fixture(files=None):
    path = random.choice(files or FIXTURES)
    text = read(path)
    lines = text.splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("--", "#"))]
    random.shuffle(idx)
    for i in idx[:30]:
        line = lines[i]
        toks = list(re.finditer(r"[A-Za-z_]+|\d+", line))
        if not toks:
            continue
        t = random.choice(toks)
        s = t.group()
        if s.isdigit():
            rep = str(int(s) + 1)
        elif s.upper() in KEYWORD_SWAPS and s.isupper():
            rep = KEYWORD_SWAPS[s]
        else:
            rep = s + "1"
        lines[i] = line[:t.start()] + rep + line[t.end():]
        return {path: "".join(lines)}, f"{path}:{i + 1} {s}->{rep}"
    raise Skip


def class_level_assigns(tree):
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for st in cls.body:
                tgt = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    tgt = st.targets[0].id
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    tgt = st.target.id
                if tgt and tgt == tgt.upper() and any(c.isalpha() for c in tgt):
                    yield cls, tgt, st


def m_dialect_attr():
    for _ in range(20):
        path = random.choice(DIALECTISH)
        text = read(path)
        tree = ast.parse(text)
        cands = []
        for cls, name, st in class_level_assigns(tree):
            v = st.value
            if st.lineno == st.end_lineno and isinstance(v, (ast.Constant, ast.UnaryOp)):
                cands.append((cls, name, st))
        if not cands:
            continue
        cls, name, st = random.choice(cands)
        tc = token_candidates(text, {st.lineno})
        tc = [c for c in tc if c[1] >= st.value.col_offset - (1 if isinstance(st.value, ast.UnaryOp) else 0)]
        if isinstance(st.value, ast.Constant) and st.value.value is None:
            line = text.splitlines(keepends=True)[st.lineno - 1]
            new = text.replace(line, line.replace("None", "1", 1), 1)
            if parses(new):
                return {path: new}, f"{path}:{st.lineno} {cls.name}.{name} None->1"
        random.shuffle(tc)
        for c in tc:
            new = apply_token(text, c)
            if new != text and parses(new):
                return {path: new}, f"{path}:{st.lineno} {cls.name}.{name} {c[4]}"
    raise Skip


def m_table_entry():
    for _ in range(30):
        path = random.choice(DIALECTISH)
        text = read(path)
        tree = ast.parse(text)
        entries = []
        for cls, name, st in class_level_assigns(tree):
            v = st.value
            if isinstance(v, ast.Dict):
                pairs = [(k, val) for k, val in zip(v.keys, v.values) if k is not None]
                for k, val in pairs:
                    if k.lineno == val.end_lineno:
                        entries.append((cls, name, "dict", k, val, pairs))
            elif isinstance(v, ast.Set):
                for e in v.elts:
                    if e.lineno == e.end_lineno:
                        entries.append((cls, name, "set", e, None, None))
        if not entries:
            continue
        cls, name, typ, k, val, pairs = random.choice(entries)
        lines = text.splitlines(keepends=True)
        line = lines[k.lineno - 1]
        op = random.choice(["delete", "key", "value"] if typ == "dict" else ["delete"])
        if op == "delete" and line.strip().endswith(",") and \
                line.strip().startswith(ast.get_source_segment(text, k).split("\n")[0][:10]):
            new_lines = lines[:k.lineno - 1] + lines[k.lineno:]
            new, detail = "".join(new_lines), "deleted entry"
        elif op == "key" and isinstance(k, ast.Constant) and isinstance(k.value, str):
            seg = ast.get_source_segment(text, k)
            new = text.replace(line, line.replace(seg, seg[:-1] + "X" + seg[-1], 1), 1)
            detail = f"key {seg}->+X"
        elif op == "value" and pairs and len(pairs) > 1:
            other = random.choice([p for p in pairs if p[1] is not val])[1]
            oseg, vseg = ast.get_source_segment(text, other), ast.get_source_segment(text, val)
            if "\n" in oseg or oseg == vseg:
                continue
            new = text.replace(line, line.replace(vseg, oseg, 1), 1)
            detail = "swapped value"
        else:
            continue
        if new != text and parses(new):
            return {path: new}, f"{path}:{k.lineno} {cls.name}.{name} {detail}"
    raise Skip


IMPORT_ONLY = {}


def m_import_only_fn():
    files = [p for p in IMPORT_ONLY if IMPORT_ONLY[p]]
    if not files:
        raise Skip
    for _ in range(20):
        path = random.choice(files)
        try:
            return m_code_token(path, IMPORT_ONLY[path])
        except Skip:
            continue
    raise Skip


def m_shared_fixture():
    return m_fixture(SHARED_FIXTURES or FIXTURES)


REAL = [m_code_token, m_dialect_attr, m_table_entry]


def combine(parts):
    files, details = {}, []
    for f, d in parts:
        if set(f) & set(files):
            raise Skip
        files.update(f)
        details.append(d)
    return files, " + ".join(details)


def m_multi():
    makers = [m_code_token, m_comment, m_fixture]
    k = random.choice([2, 2, 3])
    return combine([random.choice(makers)() for _ in range(k)])


def m_comment_plus_real():
    return combine([m_comment(random.choice(SQLGLOT_PY)), random.choice(REAL)()])


MUTATORS = {
    "code_token": m_code_token, "comment": m_comment, "fixture": m_fixture, "multi": m_multi,
    "blank_line": m_blank_line, "syntax_error": m_syntax_error, "root_file": m_root_file,
    "dialect_attr": m_dialect_attr, "table_entry": m_table_entry, "docstring": m_docstring,
    "import_only_fn": m_import_only_fn, "tests_py": m_tests_py,
    "shared_fixture": m_shared_fixture, "comment_plus_real": m_comment_plus_real,
}


# ---------------------------------------------------------------- git-style diff
def blob_hash(text):
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:7]


FUNCNAME = re.compile(r"^[A-Za-z_$]")


def git_diff(path, old, new):
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out = [f"diff --git a/{path} b/{path}\n", f"index {blob_hash(old)}..{blob_hash(new)} 100644\n",
           f"--- a/{path}\n", f"+++ b/{path}\n"]
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(3):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        ctx = next((a[k].rstrip() for k in range(i1 - 1, -1, -1) if FUNCNAME.match(a[k])), "")
        oa = f"{i1 + 1},{i2 - i1}" if i2 - i1 != 1 else f"{i1 + 1}"
        ob = f"{j1 + 1},{j2 - j1}" if j2 - j1 != 1 else f"{j1 + 1}"
        if i2 == i1:
            oa = f"{i1},0"
        if j2 == j1:
            ob = f"{j1},0"
        out.append(f"@@ -{oa} +{ob} @@{' ' + ctx if ctx else ''}\n")
        for tag, x1, x2, y1, y2 in group:
            if tag == "equal":
                out += [" " + l for l in a[x1:x2]]
            if tag in ("replace", "delete"):
                out += ["-" + l for l in a[x1:x2]]
            if tag in ("replace", "insert"):
                out += ["+" + l for l in b[y1:y2]]
    return "".join(l if l.endswith("\n") else l + "\n\\ No newline at end of file\n" for l in out)


# ---------------------------------------------------------------- main
def quotas(n):
    raw = {k: w * n for k, w in WEIGHTS.items()}
    counts = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: n - sum(counts.values())]:
        counts[k] += 1
    return counts


def load_map(path):
    global SHARED_FIXTURES
    m = json.load(gzip.open(path))
    SHARED_FIXTURES = [p for p, si in m["files_read"].items() if len(m["sets"][si]) > 1
                       and os.path.exists(os.path.join(SRC, p))]
    for p, imp in m["import_lines"].items():
        if not p.startswith("sqlglot/"):
            continue
        run = set(map(int, m["lines"].get(p, {})))
        tree = ast.parse(read(p))
        only = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = set(range(node.body[0].lineno, node.end_lineno + 1))
                hit = body & set(imp)
                if hit and not (body & run):
                    only |= hit
        IMPORT_ONLY[p] = only


SHARED_FIXTURES = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--map", default="/maps/coverage_map.json.gz")
    args = ap.parse_args()
    random.seed(args.seed)
    load_map(args.map)
    os.makedirs(args.out, exist_ok=True)

    plan = [k for k, c in quotas(args.n).items() for _ in range(c)]
    random.shuffle(plan)
    seen, made = set(), 0
    for i, kind in enumerate(plan):
        for attempt in range(40):
            try:
                files, detail = MUTATORS[kind]()
            except (Skip, IndexError, ValueError, SyntaxError, UnicodeDecodeError):
                continue
            patch = "".join(git_diff(p, read(p), files[p]) for p in sorted(files) if files[p] != read(p))
            if patch and patch not in seen:
                break
        else:
            print(f"could not make a {kind} mutant, skipped")
            continue
        seen.add(patch)
        # id from the patch itself: runs with different --n/--seed never collide,
        # and the same patch always gets the same id (label.py resumes by id).
        mid = "m" + hashlib.sha1(patch.encode()).hexdigest()[:5]
        with open(os.path.join(args.out, f"{mid}.patch"), "w") as f:
            f.write(patch)
        with open(os.path.join(args.out, f"{mid}.json"), "w") as f:
            json.dump({"kind": SAMPLE_KIND[kind], "detail": kind, "priority": kind in PRIORITY,
                       "where": detail, "files": sorted(files)}, f)
        made += 1
    pr = sum(1 for k in plan if k in PRIORITY)
    print(f"made {made} mutants in {args.out} ({pr} planned priority = {pr / max(1, len(plan)):.0%}); "
          f"{len(SHARED_FIXTURES)} shared fixtures, "
          f"{sum(1 for v in IMPORT_ONLY.values() if v)} files with import-only functions")


if __name__ == "__main__":
    main()
