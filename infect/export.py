"""infection_raw.json -> maps/infection_map.json.gz (dev-only).

{
  "version": 1, "sqlglot_commit": ...,
  "tests": [test ids], "sets": [[test indexes]],
  "nodes": { "path:lineno:col:kind": {"op": "Lt", "end": [end_lineno, end_col],
                                      "exec": set, "alt": {"LtE": set, ...},
                                      "import_exec": bool, "import_alt": ["LtE", ...]} },
  "failed": {...}, "meta": {...}
}
kind: "cmp" (single-operator Compare, alternatives = the other 9 operators) or "bool" (and/or in a
boolean context, alternative = the other one). exec = tests that evaluated the node; alt[X] =
tests in which operator X would have given a different outcome at least once (setup@/teardown@
phases credited to every test of the class, as mapgen does). Nodes are listed even if never run.
"""
import gzip
import json
import sys

sys.path.insert(0, "/infect")
import _infect_rt as rt  # noqa: E402

BOOL_NAMES = ["And", "Or"]


def main(raw_path, out_path):
    raw = json.load(open(raw_path))
    tests = raw["tests"]
    tindex = {t: i for i, t in enumerate(tests)}

    def scope(key):
        if "@" not in key.split("::")[0]:
            return [tindex[key]] if key in tindex else []
        nid = key.split("@", 1)[1]
        prefix = nid.rsplit("::", 1)[0] + "::"
        return [tindex[t] for t in tests if t.startswith(prefix)] or ([tindex[nid]] if nid in tindex else [])

    n_nodes = len(raw["nodes"])
    exec_sets = [set() for _ in range(n_nodes)]
    alt_sets = [dict() for _ in range(n_nodes)]
    import_exec = [False] * n_nodes
    import_alt = [0] * n_nodes
    for key, ctx in raw["contexts"].items():
        if key == "":
            for n, m in ctx.items():
                import_exec[int(n)] = True
                import_alt[int(n)] |= m
            continue
        owners = scope(key)
        for n, m in ctx.items():
            n = int(n)
            exec_sets[n].update(owners)
            bit = 0
            while m:
                if m & 1:
                    alt_sets[n].setdefault(bit, set()).update(owners)
                m >>= 1
                bit += 1
    # CONST: per node, per test, the distinct values of the non-literal side (or "many")
    tvals = [dict() for _ in range(n_nodes)]  # n -> {test index: set of reprs | MANY}
    import_vals = {}
    for key, ctx in raw.get("values", {}).items():
        for n, vs in ctx.items():
            n = int(n)
            if key == "":
                old = import_vals.get(n, set())
                import_vals[n] = rt.MANY if (vs == rt.MANY or old == rt.MANY) else old | set(vs)
                continue
            for o in scope(key):
                old = tvals[n].get(o, set())
                if vs == rt.MANY or old == rt.MANY:
                    tvals[n][o] = rt.MANY
                else:
                    new = old | set(vs)
                    tvals[n][o] = rt.MANY if len(new) > rt.VCAP else new
    lit = {int(n): side for n, side in raw.get("lit", {}).items()}
    set_index = {}
    sets = []

    def sid(s):
        k = tuple(sorted(s))
        if k not in set_index:
            set_index[k] = len(sets)
            sets.append(list(k))
        return set_index[k]

    nodes = {}
    for n, (path, line, col, eline, ecol, kind, op) in enumerate(raw["nodes"]):
        names = rt.NAMES if kind == "cmp" else BOOL_NAMES
        if kind == "cmp":
            alts = {names[b]: sid(s) for b, s in sorted(alt_sets[n].items())}
            imp = [names[b] for b in range(len(names)) if import_alt[n] >> b & 1]
        else:  # one alternative, stored at bit 0
            other = names[1 - op]
            alts = {other: sid(alt_sets[n][0])} if 0 in alt_sets[n] else {}
            imp = [other] if import_alt[n] & 1 else []
        e = nodes[f"{path}:{line}:{col}:{kind}"] = {"op": names[op], "end": [eline, ecol], "exec": sid(exec_sets[n]),
                                                    "alt": alts, "import_exec": import_exec[n], "import_alt": imp}
        if kind == "cmp" and lit.get(n):
            groups = {}
            for t, vs in tvals[n].items():
                k = rt.MANY if vs == rt.MANY else tuple(sorted(vs))
                groups.setdefault(k, []).append(t)
            e["cv"] = {"side": lit[n],
                       "vals": [[sid(ts), (k if k == rt.MANY else list(k))] for k, ts in groups.items()]}
            if n in import_vals:
                iv = import_vals[n]
                e["cv"]["imp"] = iv if iv == rt.MANY else sorted(iv)
    out = {"version": 1, "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "tests": tests,
           "sets": sets, "nodes": nodes, "failed": raw["failed"], "meta": raw["meta"]}
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(out, f, separators=(",", ":"))
    import os
    print(f"wrote {out_path}: {len(nodes)} nodes, {sum(1 for x in exec_sets if x)} executed by a test, "
          f"{len(sets)} sets, {len(tests)} tests, {len(raw['failed'])} failed, "
          f"{os.path.getsize(out_path) / 1e6:.2f} MB", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
