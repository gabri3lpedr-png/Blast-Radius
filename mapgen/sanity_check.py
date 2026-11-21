"""The three Stage A2 sanity checks from docs/GUIDE_A_MapMaker.md.

Usage: python3 mapgen/sanity_check.py [maps/coverage_map.json.gz]
"""
import gzip
import json
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "maps/coverage_map.json.gz"
m = json.load(gzip.open(path))
tests, sets = m["tests"], m["sets"]


def users(set_index):
    return {tests[i] for i in sets[set_index]}


ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")


# 1. TSQL dialect users include test_simplify
t = "tests/test_optimizer.py::TestOptimizer::test_simplify"
k = "sqlglot.dialects.tsql:TSQL"
s = users(m["class_users"][k]) if k in m["class_users"] else set()
check("class_users[TSQL] has test_simplify", t in s, f"({len(s)} tests use TSQL)")

# 2. pushdown_predicates fixture readers
t = "tests/test_optimizer.py::TestOptimizer::test_pushdown_predicates"
f = "tests/fixtures/optimizer/pushdown_predicates.sql"
s = users(m["files_read"][f]) if f in m["files_read"] else set()
check("files_read[pushdown_predicates.sql] has test_pushdown_predicates", t in s, f"({len(s)} readers)")

# 3. Dremio REGEXP_SPLIT -> test_regexp_split, via lines or DremioParser class_users
t = "tests/dialects/test_dremio.py::TestDremio::test_regexp_split"
src = open(".cache/sqlglot/sqlglot/parsers/dremio.py").read().splitlines()
hits = [i + 1 for i, line in enumerate(src) if "REGEXP_SPLIT" in line]
via_lines = set()
for ln in hits:
    for d in range(-2, 3):
        si = m["lines"].get("sqlglot/parsers/dremio.py", {}).get(str(ln + d))
        if si is not None:
            via_lines |= users(si)
cls = [k for k in m["class_users"] if re.fullmatch(r"sqlglot\.parsers\.dremio:\w*Parser", k)]
via_class = set().union(*(users(m["class_users"][k]) for k in cls)) if cls else set()
check("Dremio REGEXP_SPLIT reaches test_regexp_split", t in via_lines or t in via_class,
      f"(lines {hits}: {t in via_lines}; {cls}: {t in via_class})")

# 4. Process-pool work is credited (map v2): test_tpcds reaches eliminate_joins.py
t = "tests/test_optimizer.py::TestOptimizer::test_tpcds"
ej = m["lines"].get("sqlglot/optimizer/eliminate_joins.py", {})
n = sum(t in users(si) for si in ej.values())
check("test_tpcds covers sqlglot/optimizer/eliminate_joins.py", n > 0, f"({n} lines)")

# 5. serde.py:53 (pickling expressions for pool workers) credited to ~37 tests
si = m["lines"].get("sqlglot/serde.py", {}).get("53")
n = len(sets[si]) if si is not None else 0
check("serde.py:53 credited to ~37 tests", n >= 30, f"({n} tests)")

# Bonus: a hot parser line should be credited to many tests, not one.
hot = max(m["lines"]["sqlglot/parser.py"].values(), key=lambda i: len(sets[i]))
check("hot parser.py line credited to many tests", len(sets[hot]) > 100, f"({len(sets[hot])} tests)")

print(f"meta: {json.dumps({k: m['meta'][k] for k in ('tests', 'sets', 'lines', 'class_users', 'files_read', 'run_seconds', 'coverage_core') if k in m['meta']})}")
sys.exit(0 if ok else 1)
