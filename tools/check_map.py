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

MAP = sys.argv[1] if len(sys.argv) > 1 else "maps/coverage_map.json.gz"
SAMPLE = sys.argv[2] if len(sys.argv) > 2 else "sample"
SRC = sys.argv[3] if len(sys.argv) > 3 else ".cache/sqlglot"

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> {old_path: set(original line numbers touched)}; None path = new file."""
    files, path, old = {}, None, 0
    for line in text.splitlines():
        if line.startswith("--- "):
            p = line[4:].split("\t")[0]
            path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if line.startswith("+++ "):
            if path is None:
                newp = line[4:].split("\t")[0]
                path = "NEW:" + (newp[2:] if newp.startswith("b/") else newp)
            files.setdefault(path, set())
            continue
        m = HUNK.match(line)
        if m:
            old = int(m.group(1))
            continue
        if path is None or not line or line.startswith(("diff ", "index ", "\\")):
            continue
        if line[0] == "-":
            files[path].add(old)
            old += 1
        elif line[0] == "+":
            files[path].update({max(old - 1, 1), old})
        elif line[0] == " ":
            old += 1
    return files


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
            print("         missed: " + ", ".join(
                f"{t} {tj['tests'][t]['id']}" for t in sorted(missed)))
    print(f"\ncommits: {len(rows)}   commits with a MISS: {tot_miss}   "
          f"fallbacks (run all): {tot_fb}   total selected seconds: {tot_sec:.1f}")
    return 1 if tot_miss else 0


if __name__ == "__main__":
    sys.exit(main())
