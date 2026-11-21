#!/usr/bin/env python3
"""Grade the coverage map against the sample commits (dev-only, never shipped).

A deliberately naive selector: union of every test the map links to the changed
lines / classes / files. It grades the MAP, not our real selector.

Usage: python3 tools/check_map.py [maps/coverage_map.json.gz] [sample] [.cache/sqlglot]
.cache/sqlglot is a copy of /opt/sqlglot from the mapgen image (for ast lookups).

Per commit: lines removed or changed -> their original line numbers; pure
insertions -> the original lines just before and after. A line found in "lines"
gives its tests. A line that is import-only or never run, inside a class, uses
class_users["module:Class.Inner"]. Non-.py files use files_read. Anything the
map cannot place (module-level import-only code, a new file, an unread non-.py
file) is marked FALLBACK: our selector would run every test there.
"""
import ast
import gzip
import json
import os
import re
import sys
from functools import lru_cache

V4 = "--v4" in sys.argv     # also grade map v4 keys (needs sqlglot importable: run in the image)
ARGS = [a for a in sys.argv[1:] if a != "--v4"]
MAP = ARGS[0] if len(ARGS) > 0 else "maps/coverage_map.json.gz"
SAMPLE = ARGS[1] if len(ARGS) > 1 else "sample"
SRC = ARGS[2] if len(ARGS) > 2 else ".cache/sqlglot"

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _tag(text):
    """Yield (kind, line): kind is "---", "+++", "@@" or "body".

    Inside a hunk, lines are counted against its old/new lengths, so a removed
    line whose text starts with "-- " (a SQL comment in a fixture) is body,
    not a "--- " file header.
    """
    left_old = left_new = 0
    for line in text.splitlines():
        if left_old > 0 or left_new > 0:
            c = line[:1]
            if c == "-":
                left_old -= 1
            elif c == "+":
                left_new -= 1
            elif c in (" ", ""):
                left_old -= 1
                left_new -= 1
            elif c == "\\":
                pass
            else:
                left_old = left_new = 0  # malformed: fall through to headers
            if c in "-+ \\" or c == "":
                yield "body", line
                continue
        m = HUNK.match(line)
        if m:
            left_old = 1 if m.group(2) is None else int(m.group(2))
            left_new = 1 if m.group(4) is None else int(m.group(4))
            yield "@@", line
        elif line.startswith("--- "):
            yield "---", line
        elif line.startswith("+++ "):
            yield "+++", line
        else:
            yield "other", line


def parse_patch(text):
    """-> {old_path: set(original line numbers touched)}; None path = new file."""
    files, path, old = {}, None, 0
    for kind, line in _tag(text):
        if kind == "---":
            p = line[4:].split("\t")[0]
            path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if kind == "+++":
            if path is None:
                newp = line[4:].split("\t")[0]
                path = "NEW:" + (newp[2:] if newp.startswith("b/") else newp)
            files.setdefault(path, set())
            continue
        m = HUNK.match(line) if kind == "@@" else None
        if m:
            old = int(m.group(1))
            continue
        if path is None or kind != "body" or not line or line.startswith("\\"):
            continue
        if line[0] == "-":
            files[path].add(old)
            old += 1
        elif line[0] == "+":
            files[path].update({max(old - 1, 1), old})
        elif line[0] == " ":
            old += 1
    return files


def patched_sources(text):
    """-> {path: new source} for every existing .py file the patch edits."""
    out, path, hunks = {}, None, {}
    cur = None
    for kind, line in _tag(text):
        if kind == "---":
            p = line[4:].split("\t")[0]
            path = p[2:] if p.startswith("a/") else None if p == "/dev/null" else p
            continue
        if kind == "+++":
            continue
        m = HUNK.match(line) if kind == "@@" else None
        if m and path:
            cur = [int(m.group(1)), int(m.group(2) or 1), []]
            hunks.setdefault(path, []).append(cur)
            continue
        if cur is not None and kind == "body" and line[:1] in (" ", "+"):
            cur[2].append(line[1:])
    for path, hs in hunks.items():
        if not path.endswith(".py"):
            continue
        try:
            old = open(os.path.join(SRC, path)).read().split("\n")
        except OSError:
            continue
        for start, count, new in sorted(hs, reverse=True):
            i = start - 1 if count else start
            old[i:i + count] = new
        out[path] = "\n".join(old)
    return out


@lru_cache(maxsize=None)
def scopes(path):
    """-> list of (start, end, kind, qualname) for every class/def in the file."""
    try:
        tree = ast.parse(open(os.path.join(SRC, path)).read())
    except (OSError, SyntaxError):
        return []
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}.{child.name}" if prefix else child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                kind = "class" if isinstance(child, ast.ClassDef) else "def"
                out.append((start, child.end_lineno, kind, q))
                walk(child, q)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def enclosing(path, line):
    """Innermost (kind, qualname) containing line, plus the innermost class."""
    inner, cls = None, None
    for start, end, kind, q in scopes(path):
        if start <= line <= end:
            if inner is None or start >= inner[0]:
                inner = (start, kind, q)
            if kind == "class" and (cls is None or start >= cls[0]):
                cls = (start, q)
    return (inner[1], inner[2]) if inner else None, cls[1] if cls else None


# ---------------------------------------------------------------- map v4
def _class_stmt(tree, qual, line):
    """The statement directly in class `qual`'s body that contains `line`."""
    node = tree
    for part in qual.split("."):
        node = next((c for c in getattr(node, "body", []) if isinstance(c, ast.ClassDef)
                     and c.name == part), None)
        if node is None:
            return None
    for st in node.body:
        start = min([st.lineno] + [d.lineno for d in getattr(st, "decorator_list", [])])
        if start <= line <= st.end_lineno:
            return st
    return None


def _assigned_name(st):
    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
        return st.targets[0].id, st.value
    if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
        return st.target.id, st.value
    return None, None


def _spell(node):
    """Contract spelling of a dict-literal key, from source (None = can't)."""
    if isinstance(node, ast.Constant):
        return repr(node.value)
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return None
    parts.reverse()
    while len(parts) > 1 and parts[0][:1].islower():   # module alias: exp., parser.
        parts.pop(0)
    return ".".join(parts)


def _dict_entries(value):
    if not isinstance(value, ast.Dict):
        return None
    out = {}
    for k, v in zip(value.keys, value.values):
        if k is None:                      # **spread: not an entry of its own
            continue
        sk = _spell(k)
        if sk is None:
            return None
        out[sk] = ast.dump(v)
    return out


@lru_cache(maxsize=None)
def _subclass_keys(module, qual):
    import importlib
    import pkgutil
    if not _subclass_keys.loaded:   # __subclasses__ only lists LOADED subclasses
        import sqlglot
        for mi in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            try:
                importlib.import_module(mi.name)
            except Exception:
                pass
        _subclass_keys.loaded = True
    try:
        obj = importlib.import_module(module)
        for part in qual.split("."):
            obj = getattr(obj, part)
    except Exception:
        return None
    seen, stack = set(), [obj]
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        stack.extend(type.__subclasses__(c))
    return sorted({f"{c.__module__}:{c.__qualname__}" for c in seen
                   if c.__module__.startswith("sqlglot")})


_subclass_keys.loaded = False


def attr_level(m, path, module, qual, line, new_src, nums):
    """Tests for a class-body edit via attr_users/key_users, or None -> fall back
    to class_users. For dicts, an import-time read of the attribute alone does
    not force a fallback: only K[*] or K[changed key] in key_import does (the
    metaclass reads every generator's TRANSFORMS at import just to pop the
    JSONPath keys)."""
    try:
        old_tree = ast.parse(open(os.path.join(SRC, path)).read())
        new_tree = ast.parse(new_src) if new_src is not None else None
    except (OSError, SyntaxError):
        return None
    st = _class_stmt(old_tree, qual, line)
    name, value = _assigned_name(st) if st is not None else (None, None)
    if name is None:
        return None
    owners = _subclass_keys(module, qual)
    if not owners:
        return None
    ks = [f"{o}.{name}" for o in owners]
    au, ku = m.get("attr_users", {}), m.get("key_users", {})
    ai, ki = set(m.get("attr_import", [])), set(m.get("key_import", []))
    if not any(k in au for k in ks) and not any(k + "[*]" in ku or k + "[*]" in ki for k in ks) \
            and not any(k in ai for k in ks):
        return None                        # no key at all: never "nobody uses it"
    old_e = _dict_entries(value)
    new_e = None
    if new_tree is not None:
        nst = None
        # find the same attribute in the patched class
        node = new_tree
        for part in qual.split("."):
            node = next((c for c in getattr(node, "body", []) if isinstance(c, ast.ClassDef)
                         and c.name == part), None)
            if node is None:
                break
        if node is not None:
            for st2 in node.body:
                if _assigned_name(st2)[0] == name:
                    nst = st2
        if nst is not None:
            new_e = _dict_entries(_assigned_name(nst)[1])
    if old_e is not None and new_e is not None:
        changed = {k for k in old_e.keys() | new_e.keys() if old_e.get(k) != new_e.get(k)}
        if not changed:                     # e.g. only a comment or a ** spread moved
            changed = {"*"}
        if any(f"{k}[*]" in ki or any(f"{k}[{c}]" in ki for c in changed) for k in ks):
            return None
        out = set()
        for k in ks:
            for c in changed | {"*"}:
                if f"{k}[{c}]" in ku:
                    out |= nums(ku[f"{k}[{c}]"])
        return out
    if any(k in ai for k in ks):
        return None
    out = set()
    for k in ks:
        if k in au:
            out |= nums(au[k])
    return out


def main():
    m = json.load(gzip.open(MAP))
    tj = json.load(open(os.path.join(SAMPLE, "tests.json")))
    answers = json.load(open(os.path.join(SAMPLE, "answers.json")))["commits"]
    by_id = {t["id"]: t for t in tj["tests"]}
    secs = {t["number"]: t["seconds"] for t in tj["tests"]}
    all_tests = set(secs)
    mtests, sets = m["tests"], m["sets"]

    def nums(set_index):
        return {by_id[mtests[i]]["number"] for i in sets[set_index] if mtests[i] in by_id}

    rows, tot_miss, tot_sec, tot_fb = [], 0, 0.0, 0
    for cid in sorted(answers):
        ans = answers[cid]
        failing = set(ans["failing"])
        patch = open(os.path.join(SAMPLE, "commits", f"{cid}.patch")).read()
        selected, why_fb = set(), []
        # A .py file that no longer compiles breaks every importer; no line map
        # can say who those are, so the selector should run everything.
        for path, src in patched_sources(patch).items():
            try:
                compile(src, path, "exec")
            except SyntaxError:
                why_fb.append(f"syntax error {path}")
        new_srcs = patched_sources(patch)
        for path, lines in parse_patch(patch).items():
            if path.startswith("NEW:"):
                why_fb.append(f"new file {path[4:]}")
                continue
            if not path.endswith(".py"):
                if path in m["files_read"]:
                    selected |= nums(m["files_read"][path])
                else:
                    why_fb.append(f"unread file {path}")
                continue
            file_lines = m["lines"].get(path, {})
            imp = set(m["import_lines"].get(path, []))
            module = path[:-3].replace("/", ".")
            for ln in sorted(lines):
                if str(ln) in file_lines:
                    selected |= nums(file_lines[str(ln)])
                    continue
                scope, cls = enclosing(path, ln)
                if V4 and cls is not None and (scope is None or scope[0] == "class"):
                    got = attr_level(m, path, module, cls, ln, new_srcs.get(path), nums)
                    if got is not None:
                        selected |= got
                        continue
                if cls is not None and (scope is None or scope[0] == "class" or ln in imp):
                    key = f"{module}:{cls}"
                    if key in m["class_users"]:
                        selected |= nums(m["class_users"][key])
                    # class never instantiated by any test -> no tests (fine)
                    continue
                if scope is not None and scope[0] == "def" and ln not in imp:
                    continue  # body of a function no test ever ran
                if ln in imp or scope is None:
                    why_fb.append(f"{path}:{ln} module-level")
        fallback = bool(why_fb)
        chosen = all_tests if fallback else selected
        missed = failing - chosen
        sec = sum(secs[t] for t in chosen)
        tot_miss += bool(missed)
        tot_sec += sec
        tot_fb += fallback
        rows.append((cid, ans["kind"], len(chosen), sec, len(failing), missed, fallback, why_fb))

    print(f"{'commit':6} {'kind':8} {'#sel':>5} {'sel s':>7} {'#fail':>5}  MISS  note")
    for cid, kind, n, sec, nf, missed, fb, why in rows:
        miss = "YES" if missed else "no"
        note = ("FALLBACK " + "; ".join(sorted(set(why))[:2])) if fb else ""
        print(f"{cid:6} {kind:8} {n:5d} {sec:7.1f} {nf:5d}  {miss:4}  {note}")
        if missed:
            shown = sorted(missed)[:6]
            print("         missed: " + ", ".join(
                f"{t} {tj['tests'][t]['id']}" for t in shown)
                + (f" ... (+{len(missed) - 6})" if len(missed) > 6 else ""))
    print(f"\ncommits: {len(rows)}   commits with a MISS: {tot_miss}   "
          f"fallbacks (run all): {tot_fb}   total selected seconds: {tot_sec:.1f}")
    return 1 if tot_miss else 0


if __name__ == "__main__":
    sys.exit(main())
