"""Merge infection maps from several builds into one conservative map (dev-only).

The builder does not reproduce every operator-alternative set run to run, so the selector loads
the merge of several builds. It never drops more than any single build would:
  exec[node]       = INTERSECTION of the builds' exec sets (a test unseen by one build is kept)
  alt[node][X]     = UNION (a test differs for X if any build says so)
  import_exec      = OR, import_alt = UNION
  a node missing from any build, or whose op/end disagree, is left out (lookup -> keep everything)
CONST data ("cv") is dropped. A build with failures is refused.

Usage: python3 infect/merge_maps.py OUT.json.gz IN1.json.gz IN2.json.gz [...]
"""
import gzip
import json
import sys


def load(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        m = json.load(f)
    if m.get("failed"):
        sys.exit(f"{path}: built from a run with failures")
    tests, sets = m["tests"], m["sets"]

    def ids(i):
        return frozenset(tests[t] for t in sets[i])

    nodes = {}
    for k, e in m["nodes"].items():
        nodes[k] = {"op": e["op"], "end": tuple(e["end"]), "exec": ids(e["exec"]),
                    "alt": {a: ids(s) for a, s in e["alt"].items()},
                    "import_exec": e["import_exec"], "import_alt": set(e["import_alt"])}
    return m, nodes


def main(out, *ins):
    builds = [load(p) for p in ins]
    first = builds[0][0]
    tests = list(first["tests"])
    seen = set(tests)
    for m, _ in builds[1:]:
        for t in m["tests"]:
            if t not in seen:
                seen.add(t)
                tests.append(t)
    tindex = {t: i for i, t in enumerate(tests)}
    set_index, sets = {}, []

    def sid(s):
        k = tuple(sorted(tindex[t] for t in s))
        if k not in set_index:
            set_index[k] = len(sets)
            sets.append(list(k))
        return set_index[k]

    nodes, dropped, widened = {}, 0, 0
    keys = set(builds[0][1])
    for _, n in builds[1:]:
        keys &= set(n)
    dropped = len(set().union(*(set(n) for _, n in builds)) - keys)
    for k in sorted(keys):
        es = [n[k] for _, n in builds]
        if any((e["op"], e["end"]) != (es[0]["op"], es[0]["end"]) for e in es):
            dropped += 1
            continue
        ex = frozenset.intersection(*(e["exec"] for e in es))
        alts = {}
        for e in es:
            for a, s in e["alt"].items():
                alts[a] = alts.get(a, frozenset()) | s
        if any(e["alt"] != alts for e in es) or any(e["exec"] != ex for e in es):
            widened += 1
        nodes[k] = {"op": es[0]["op"], "end": list(es[0]["end"]), "exec": sid(ex),
                    "alt": {a: sid(s) for a, s in sorted(alts.items())},
                    "import_exec": any(e["import_exec"] for e in es),
                    "import_alt": sorted(set().union(*(e["import_alt"] for e in es)))}
    res = {"version": 1, "sqlglot_commit": first["sqlglot_commit"], "tests": tests, "sets": sets,
           "nodes": nodes, "failed": {},
           "meta": {"merged_from": len(ins), "builds": [m.get("meta", {}) for m, _ in builds]}}
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(res, f, separators=(",", ":"))
    print(f"wrote {out}: {len(nodes)} nodes ({widened} widened, {dropped} left out), {len(sets)} sets, "
          f"{len(tests)} tests", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
