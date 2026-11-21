"""HEAVY_REPLAY: drop a heavy TestExecutor test when it provably cannot fail on this commit.

The three tests (tests/test_executor.py::TestExecutor):
  test_execute_tpcds / test_execute_tpch  for every (meta, sql) of the TPC fixture: parse_one(sql)
      in the main process, pickled to a pool worker; there, if meta["execute"]: find_tables, then
      sqlglot.executor.execute = ensure_tables(data) + ensure_schema + optimize + Plan +
      PythonExecutor(...).execute(plan) over the CSV data; the main process compares the rows
      with duckdb running transpile(sql, write="duckdb"). setUpClass loads the CSVs into
      sqlglot Tables and duckdb views. Time: the PythonExecutor run over the data.
  test_optimized_tpch  for every TPC-H (sql, optimized): duckdb runs transpile(sql) and
      transpile(optimized) and compares frames. Time: duckdb. sqlglot code: the two transpiles.

Split. CHEAP (no data): parse_one, the pickle round trip, find_tables, ensure_schema, optimize,
expression.sql(pretty=True) (execute() evaluates it for a debug log), Plan, transpile to duckdb.
EXPENSIVE: setUpClass, ensure_tables, PythonExecutor.execute, reading the result Table, and the
lines of executor.execute() itself.

Build time (pinned sqlglot, `python heavy.py build <src> <out.json>`):
  - E_LINES: the sqlglot lines the expensive phase runs, per test, traced with sys.monitoring:
    setUpClass once, then per query in a fresh fork (cheap untraced, expensive traced, like a
    pool worker's first task) and all queries in one fork in order (later tasks: warm caches).
  - DIGESTS: the cheap phase's outputs, one canonical dump per query, plus a dump of the
    module-level and class-level state of every module with E_LINES (side effects of the cheap
    phase that the expensive phase could read). Run twice in fresh interpreters; a test whose
    dumps differ between the two runs is marked unstable and never replayed.

Replay = two fresh interpreters, like the real test: the test process's part (parse_one, pickle,
transpile), then a pool worker's part on those pickles (unpickle, find_tables, ensure_schema,
optimize, sql, Plan); the module state is dumped in the worker only (the test process's state
never reaches a spawned worker). opt_tpch's expensive phase runs no sqlglot code: no state.

Runtime (selector, HEAVY_REPLAY=static: the guard and static drops only; =1: also replays), when a
heavy test is selected:
  guard  every changed file is NOOP, a sqlglot/**.py whose changed lines (old text and '+' lines
         of the new text) all lie in def bodies whose OUTERMOST def has no E_LINES (the expensive
         phase never calls it, so with identical inputs it runs identical code), or a TPC .sql
         fixture (its queries are the cheap phase's input). Anything else (tests/*.py, data
         files TestExecutor reads, module/class-level code, unparsable files) -> keep.
  replay the cheap phase on a /tmp overlay of the patched tree (fresh interpreter, same hash
         seed as the build); drop the test iff every digest equals the pinned one. Any error,
         timeout or difference -> keep.
Residual assumptions: sqlglot code is deterministic given its inputs and PYTHONHASHSEED; state the
cheap phase leaves behind is visible in module/class attributes (not in C-level caches or in
transient values overwritten before the expensive phase reads them).
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":  # run as a script: blast/ is sys.path[0] and blast/select.py would
    sys.path[0] = str(Path(__file__).resolve().parent.parent)  # shadow the stdlib `select`

import ast
import json
import os

# enum, hashlib, pickle, re, subprocess, types: imported inside the functions that need them, so
# the selector's static checks (HEAVY_REPLAY=static) do not pay for them at startup.

TESTS = {
    "tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "opt_tpch": "tests/test_executor.py::TestExecutor::test_optimized_tpch",
}
FIXTURE = {"tpcds": "optimizer/tpc-ds/tpc-ds.sql", "tpch": "optimizer/tpc-h/tpc-h.sql",
           "opt_tpch": "optimizer/tpc-h/tpc-h.sql"}
SQL_FIXTURES = {"tests/fixtures/" + f for f in FIXTURE.values()}
DATA_DIRS = ("tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")
HASHSEED = "0"
REPLAY_TIMEOUT = 60


# -- canonical dump ----------------------------------------------------------------------------
def _named(x) -> bool:
    import types
    return isinstance(x, (type, types.FunctionType, types.BuiltinFunctionType))


def _name_of(x) -> str:
    return f"{getattr(x, '__module__', '')}.{getattr(x, '__qualname__', '')}"


def _identity_hashed(k) -> bool:
    """Keys whose hash is their address (classes, functions, plain objects): dict order built
    from them, e.g. by iterating a set, differs from run to run."""
    import types
    if isinstance(k, (type, types.FunctionType)):
        return True
    return type(k).__hash__ is object.__hash__


_SLOTS: dict[type, tuple[str, ...]] = {}


def _slots(t: type) -> tuple[str, ...]:
    sl = _SLOTS.get(t)
    if sl is None:
        names: list[str] = []
        for cls in t.__mro__:
            for s in getattr(cls, "__slots__", ()) or ():
                if isinstance(s, str) and s not in ("__dict__", "__weakref__") and s not in names:
                    names.append(s)
        sl = _SLOTS[t] = tuple(names)
    return sl


def canon(root, _flat_sets: bool = False) -> str:
    """Deterministic text for an object graph: scalars by repr, enums/classes/functions by name,
    containers in iteration order, objects by type + __dict__/__slots__, shared objects and
    cycles as back references. Sets are ordered by each element's shallow key."""
    import enum, re, types
    out: list[str] = []
    memo: dict[int, int] = {}
    keep: list = []

    def shallow(o) -> str:
        t = type(o)
        if o is None or t in (bool, int, float, str, bytes, complex):
            return repr(o)
        if isinstance(o, enum.Enum):
            return f"E{t.__qualname__}.{o.name}"
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict):
            items = sorted((k, repr(v)) for k, v in d.items()
                           if type(v) in (bool, int, float, str, bytes, type(None)))
            return f"{t.__module__}.{t.__qualname__}{items}"
        return f"{t.__module__}.{t.__qualname__}"

    deep_cache: dict[int, str] = {}

    def deep(o) -> str:
        """Tie-break for set elements with equal shallow keys: the element's dump with nested sets
        reduced to their sorted shallow keys (context-free, terminates). Elements still equal under
        it keep iteration order; if that order shows, the build's double run marks it unstable."""
        k = id(o)
        if k not in deep_cache:
            deep_cache[k] = canon(o, True)
        return deep_cache[k]

    def w(o) -> None:
        t = type(o)
        if o is None or t in (bool, int, float, str, bytes, complex):
            out.append(repr(o))
            return
        if isinstance(o, enum.Enum):
            out.append(f"E:{t.__module__}.{t.__qualname__}.{o.name}")
            return
        if isinstance(o, type):
            out.append(f"T:{o.__module__}.{o.__qualname__}")
            return
        if isinstance(o, re.Pattern):
            out.append(f"R:{o.pattern!r}:{o.flags}")
            return
        if isinstance(o, (types.FunctionType, types.BuiltinFunctionType, types.MethodType,
                          types.MethodWrapperType, types.WrapperDescriptorType, types.ModuleType)) \
                or t.__name__ in ("method_descriptor", "getset_descriptor", "member_descriptor",
                                  "classmethod_descriptor", "_lru_cache_wrapper", "cython_function_or_method"):
            name = getattr(o, "__qualname__", None) or getattr(o, "__name__", "?")
            out.append(f"F:{getattr(o, '__module__', None)}.{name}")
            return
        i = id(o)
        if i in memo:
            out.append(f"@{memo[i]}")
            return
        memo[i] = len(memo)
        keep.append(o)
        if isinstance(o, (list, tuple)):
            out.append(f"{t.__qualname__}[")
            # a sequence of classes/functions is often tuple(some_set): order follows addresses
            seq = sorted(o, key=_name_of) if o and all(_named(x) for x in o) else o
            for x in seq:
                w(x)
                out.append(",")
            out.append("]")
        elif isinstance(o, dict):
            out.append(f"{t.__qualname__}{{")
            items = o.items()
            if any(_identity_hashed(k) for k in o):  # insertion order follows addresses
                items = sorted(items, key=lambda kv: deep(kv[0]))
            for k, v in items:
                w(k)
                out.append(":")
                w(v)
                out.append(",")
            out.append("}")
        elif isinstance(o, (set, frozenset)):
            out.append(f"{t.__qualname__}{{")
            if _flat_sets:
                out.append(f"{t.__qualname__}{sorted(shallow(x) for x in o)}")
                return
            groups: dict[str, list] = {}
            for x in o:
                groups.setdefault(shallow(x), []).append(x)
            ordered = []
            for k in sorted(groups):
                g = groups[k]
                ordered.extend(g if len(g) == 1 else sorted(g, key=deep))
            for x in ordered:
                w(x)
                out.append(",")
            out.append("}")
        elif isinstance(o, (property, staticmethod, classmethod)):
            out.append(f"{t.__qualname__}(")
            w(getattr(o, "fget", None) or getattr(o, "__func__", None))
            out.append(")")
        else:
            out.append(f"O:{t.__module__}.{t.__qualname__}(")
            d = getattr(o, "__dict__", None)
            if isinstance(d, dict):
                for k, v in d.items():
                    out.append(f"{k}=")
                    w(v)
                    out.append(",")
            for sl in _slots(t):
                if hasattr(o, sl):
                    out.append(f"{sl}=")
                    w(getattr(o, sl))
                    out.append(",")
            out.append(")")

    w(root)
    return "".join(out)


def _digest(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def module_state(modules: list[str]) -> str:
    """Module-level and class-level state of `modules` (each class defined there: its __dict__)."""
    parts = []
    for name in sorted(modules):
        m = sys.modules.get(name)
        if m is None:
            parts.append(f"{name}:absent")
            continue
        items = []
        for k, v in list(vars(m).items()):
            if k.startswith("__") and k.endswith("__"):
                continue
            if isinstance(v, type) and v.__module__ == name:
                items.append((k, {kk: vv for kk, vv in vars(v).items()
                                  if kk not in ("__dict__", "__weakref__", "__doc__")}))
            else:
                items.append((k, v))
        parts.append(f"{name}:{canon(items)}")
    return "\n".join(parts)


# -- the cheap phase -----------------------------------------------------------------------------
ERRORS: dict[str, int] = {}


def cheap_main(kind: str) -> tuple[list[str], list[bytes]]:
    """The test process's part, per query: (canonical record, the pickle the worker receives)."""
    import pickle
    from sqlglot import parse_one, transpile
    from tests.helpers import load_sql_fixture_pairs

    recs, blobs = [], []
    for meta, sql, optimized in load_sql_fixture_pairs(FIXTURE[kind]):
        # the raw text is never consumed: only what is computed from it (and meta["execute"])
        rec: list = [meta]
        blob = b""
        try:
            if kind == "opt_tpch":
                rec.append(transpile(sql, write="duckdb")[0])
                rec.append(transpile(optimized, write="duckdb")[0])
            else:
                blob = pickle.dumps(parse_one(sql))
                if meta.get("execute"):
                    rec.append(transpile(sql, write="duckdb")[0])  # cached_execute: duckdb's query
        except Exception as e:  # noqa: BLE001 - an error is an output too
            rec.append(("error", type(e).__qualname__, str(e)))
            ERRORS[kind] = ERRORS.get(kind, 0) + 1
        recs.append(canon(rec))
        blobs.append(blob)
    return recs, blobs


def cheap_worker(kind: str, blobs: list[bytes]) -> list[str]:
    """A pool worker's part before the expensive phase, per query (fresh process, like spawn)."""
    import pickle
    if kind == "opt_tpch":
        return ["" for _ in blobs]
    from sqlglot import find_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    schema_dict = TPCDS_SCHEMA if kind == "tpcds" else TPCH_SCHEMA
    metas = [m for m, _, _ in load_sql_fixture_pairs(FIXTURE[kind])]
    out = []
    for meta, blob in zip(metas, blobs):
        rec: list = []
        try:
            received = pickle.loads(blob) if blob else None
            if received is not None and meta.get("execute"):
                rec.append([t.name for t in find_tables(received)])
                schema = ensure_schema(schema_dict, dialect=None)
                rec.append(schema.supported_table_args)
                expression = optimize(received, schema, leave_tables_isolated=True, dialect=None)
                rec.append(expression.sql(pretty=True))
                rec.append(Plan(expression))
        except Exception as e:  # noqa: BLE001
            rec.append(("error", type(e).__qualname__, str(e)))
            ERRORS[kind] = ERRORS.get(kind, 0) + 1
        out.append(canon(rec))
    return out


def cheap(kind: str) -> list[str]:
    """Both parts in one process (build-time coverage of the cheap phase)."""
    recs, blobs = cheap_main(kind)
    return [m + w for m, w in zip(recs, cheap_worker(kind, blobs))]


def _big_stack(fn):
    """Run fn() in a thread with a 1 GiB stack (deep expression trees, recursive dumps)."""
    import threading

    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box["e"] = e

    threading.stack_size(1 << 30)
    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


def replay_main(kinds: list[str], blob_path: str) -> dict:
    import pickle
    sys.setrecursionlimit(1_000_000)

    def run():
        res, blobs = {}, {}
        for kind in kinds:
            recs, blobs[kind] = cheap_main(kind)
            res[kind] = {"main": [_digest(r) for r in recs], "errors": ERRORS.get(kind, 0)}
        with open(blob_path, "wb") as f:
            pickle.dump(blobs, f)
        return res

    return _big_stack(run)


def replay_worker(kinds: list[str], blob_path: str, modules: dict[str, list[str]]) -> dict:
    import pickle
    sys.setrecursionlimit(1_000_000)

    def run():
        with open(blob_path, "rb") as f:
            blobs = pickle.load(f)
        res = {}
        for kind in kinds:
            res[kind] = {"worker": [_digest(r) for r in cheap_worker(kind, blobs[kind])],
                         "errors": ERRORS.get(kind, 0)}
        for kind in kinds:  # what the worker-side cheap steps left for the expensive phase
            res[kind]["state"] = "" if kind == "opt_tpch" else _digest(module_state(modules.get(kind, [])))
        return res

    return _big_stack(run)


# -- build time ------------------------------------------------------------------------------
def _trace(fn, root: str, hits: set) -> None:
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "heavy")

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(root):
            hits.add((f[len(root):], line))
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)
    try:
        fn()
    finally:
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.LINE, None)
        mon.free_tool_id(tool)


def _execute_lines(src: Path) -> list[int]:
    """Every line of sqlglot.executor.execute (its body sits between the cheap and expensive
    steps; any edit there -> keep)."""
    tree = ast.parse((src / "sqlglot/executor/__init__.py").read_text("utf-8"))
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "execute":
            return list(range(n.lineno, n.end_lineno + 1))
    raise RuntimeError("sqlglot.executor.execute not found")


def build_lines(src: Path, jobs: int = 4) -> dict[str, dict[str, list[int]]]:
    import pickle
    import multiprocessing as mp

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.setrecursionlimit(20000)
    from sqlglot import find_tables, parse_one
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests import test_executor as te
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    root = str(src.resolve()) + "/"
    setup_hits: set = set()
    _trace(te.TestExecutor.setUpClass, root, setup_hits)
    cls = te.TestExecutor

    def one(kind: str, q: int, hits: set) -> None:
        tables_all = cls.tpcds_tables if kind == "tpcds" else cls.tpch_tables
        schema_dict = TPCDS_SCHEMA if kind == "tpcds" else TPCH_SCHEMA
        meta, sql, _ = list(load_sql_fixture_pairs(FIXTURE[kind]))[q]
        if not meta.get("execute"):
            return
        received = pickle.loads(pickle.dumps(parse_one(sql)))
        tables = {t.name: tables_all[t.name] for t in find_tables(received)}

        def expensive_tables():
            nonlocal tables_
            tables_ = ensure_tables(tables, dialect=None)

        tables_ = None
        _trace(expensive_tables, root, hits)
        schema = ensure_schema(schema_dict, dialect=None)
        expression = optimize(received, schema, leave_tables_isolated=True, dialect=None)
        expression.sql(pretty=True)
        plan = Plan(expression)

        def expensive_run():
            result = PythonExecutor(tables=tables_).execute(plan)
            result.rows, result.columns  # noqa: B018 - the main process reads them

        _trace(expensive_run, root, hits)

    global _ONE
    _ONE = one
    worker_hits = _worker_start_lines(src, root, cls, load_sql_fixture_pairs, parse_one)
    ctx = mp.get_context("fork")
    out: dict[str, set] = {}
    for kind in ("tpcds", "tpch"):
        n = len(list(load_sql_fixture_pairs(FIXTURE[kind])))
        tasks = [(kind, [q]) for q in range(n)] + [(kind, list(range(n)))]  # fresh + warm order
        hits = set(setup_hits) | worker_hits
        with ctx.Pool(jobs, maxtasksperchild=1) as pool:
            for h in pool.imap_unordered(_worker, tasks):
                hits.update(map(tuple, h))
        out[kind] = hits
    out["opt_tpch"] = set(setup_hits)
    exec_lines = _execute_lines(src)
    res = {}
    for kind, hits in out.items():
        by_file: dict[str, set] = {}
        for f, line in hits:
            by_file.setdefault(f, set()).add(line)
        by_file.setdefault("sqlglot/executor/__init__.py", set()).update(exec_lines)
        res[kind] = {f: sorted(v) for f, v in sorted(by_file.items())}
    return res


_IMPORT_TRACE = """
import pickle, sys
mon = sys.monitoring
root, payload = sys.argv[1], sys.argv[2]
hits = set()
def on_line(code, line):
    f = code.co_filename
    if f.startswith(root):
        hits.add((f[len(root):], line))
    return mon.DISABLE
mon.use_tool_id(mon.COVERAGE_ID, "heavy")
mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
sys.path.insert(0, root.rstrip("/"))
import tests.test_executor  # what a spawned pool worker imports to find mp_execute
with open(payload, "rb") as f:
    for blob in pickle.load(f):  # initargs (schema, tables) and every task's expression
        pickle.loads(blob)
mon.set_events(mon.COVERAGE_ID, 0)
import json
print(json.dumps(sorted(hits)))
"""


_CHEAP_TRACE = """
import sys
mon = sys.monitoring
root, kind, app = sys.argv[1], sys.argv[2], sys.argv[3]
hits = set()
def on_line(code, line):
    f = code.co_filename
    if f.startswith(root):
        hits.add((f[len(root):], line))
    return mon.DISABLE
mon.use_tool_id(mon.COVERAGE_ID, "heavy")
mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
sys.path[0:0] = [root.rstrip("/"), app]
sys.setrecursionlimit(1000000)
from blast import heavy
heavy._big_stack(lambda: heavy.cheap(kind))
mon.set_events(mon.COVERAGE_ID, 0)
import json
print(json.dumps(sorted(hits)))
"""


def cheap_lines(src: Path) -> dict[str, dict[str, list[int]]]:
    """Per test: the lines the cheap phase runs (imports included), in a fresh interpreter."""
    import subprocess
    root = str(src.resolve()) + "/"
    app = str(Path(__file__).resolve().parent.parent)
    out = {}
    for kind in TESTS:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(PYTHONHASHSEED=HASHSEED, SKIP_INTEGRATION="0")
        p = subprocess.run([sys.executable, "-c", _CHEAP_TRACE, root, kind, app], cwd=str(src),
                           env=env, capture_output=True, text=True, check=True)
        by_file: dict[str, set] = {}
        for f, line in json.loads(p.stdout.strip().splitlines()[-1]):
            by_file.setdefault(f, set()).add(line)
        out[kind] = {f: sorted(v) for f, v in sorted(by_file.items())}
    return out


def _worker_start_lines(src: Path, root: str, cls, load_pairs, parse_one) -> set:
    """Lines a fresh spawned worker runs before any task: imports, unpickling initargs/tasks."""
    import pickle, subprocess
    import tempfile

    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA

    blobs = [pickle.dumps((TPCDS_SCHEMA, cls.tpcds_tables)), pickle.dumps((TPCH_SCHEMA, cls.tpch_tables))]
    for kind in ("tpcds", "tpch"):
        for meta, sql, _ in load_pairs(FIXTURE[kind]):
            blobs.append(pickle.dumps((parse_one(sql), meta)))
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        pickle.dump(blobs, f)
    try:
        env = dict(os.environ, SKIP_INTEGRATION="0")
        p = subprocess.run([sys.executable, "-c", _IMPORT_TRACE, root, f.name], cwd=str(src),
                           env=env, capture_output=True, text=True, check=True)
    finally:
        os.unlink(f.name)
    return {tuple(h) for h in json.loads(p.stdout.strip().splitlines()[-1])}


_ONE = None  # build_lines' per-query tracer, inherited by the forked pool workers


def _worker(args) -> list:
    kind, qs = args
    hits: set = set()
    for q in qs:
        _ONE(kind, q, hits)
    return sorted(hits)


def _module_names(lines: dict[str, list[int]]) -> list[str]:
    from blast.locate import module_of
    return sorted(module_of(p) for p in lines if p.startswith("sqlglot/") and p.endswith(".py"))


def run_replay(src_root: str, kinds: list[str], modules: dict[str, list[str]], timeout: float) -> dict:
    """Each test on its own (a tpch worker never runs tpcds tasks): see _run_replay."""
    import time

    deadline = time.time() + timeout
    res = {}
    for k in kinds:
        res.update(_run_replay(src_root, [k], modules, max(1.0, deadline - time.time())))
    return res


def _run_replay(src_root: str, kinds: list[str], modules: dict[str, list[str]], timeout: float) -> dict:
    """The cheap phase on `src_root` (sqlglot + tests) in two fresh interpreters: the test process's
    part, then a pool worker's part on the pickles it produced."""
    import subprocess
    import tempfile
    import time

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHASHSEED")}
    here = str(Path(__file__).resolve().parent.parent)
    env.update(PYTHONPATH=f"{src_root}:{here}", PYTHONHASHSEED=HASHSEED, PYTHONDONTWRITEBYTECODE="1",
               SKIP_INTEGRATION="0")
    fd, blob_path = tempfile.mkstemp(suffix=".pkl")
    os.close(fd)
    deadline = time.time() + timeout
    try:
        outs = []
        for args in (["replay-main", json.dumps(kinds), blob_path],
                     ["replay-worker", json.dumps(kinds), blob_path, json.dumps(modules)]):
            p = subprocess.run([sys.executable, __file__, *args], cwd=src_root, env=env,
                               capture_output=True, text=True, timeout=max(1.0, deadline - time.time()))
            if p.returncode != 0:
                raise RuntimeError(f"{args[0]} exited {p.returncode}: {p.stderr[-300:]}")
            outs.append(json.loads(p.stdout.strip().splitlines()[-1]))
    finally:
        os.unlink(blob_path)
    main, worker = outs
    return {k: {"queries": [_digest(a + b) for a, b in zip(main[k]["main"], worker[k]["worker"])]
                + ([] if len(main[k]["main"]) == len(worker[k]["worker"]) else ["length mismatch"]),
                "errors": main[k]["errors"] + worker[k]["errors"], "state": worker[k]["state"]}
            for k in kinds}


def build(src: Path, out: Path) -> None:
    lines = build_lines(src)
    modules = {k: _module_names(v) for k, v in lines.items()}
    kinds = list(TESTS)
    a = run_replay(str(src), kinds, modules, 600)
    b = run_replay(str(src), kinds, modules, 600)
    rec = {"tests": TESTS, "lines": lines, "cheap_lines": cheap_lines(src), "modules": modules, "digests": a,
           "unstable": sorted(k for k in kinds if a[k] != b[k] or a[k]["errors"])}
    out.write_text(json.dumps(rec, separators=(",", ":")), "utf-8")
    print(f"wrote {out}: unstable={rec['unstable']}, "
          + ", ".join(f"{k} {sum(len(v) for v in lines[k].values())} lines" for k in kinds), file=sys.stderr)


# -- runtime: guard, overlay, replay ------------------------------------------------------------
def _outer_def(tree: ast.Module, line: int):
    """(qualname path, node) of the outermost def whose body holds `line`, or None."""
    path: list[str] = []
    body = tree.body
    while True:
        node = next((n for n in body if isinstance(n, ast.stmt)
                     and min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]) <= line
                     <= n.end_lineno), None)
        if node is None:
            return None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if line < node.body[0].lineno:
                return None  # header, decorator or default: import time
            return (*path, node.name), node
        if isinstance(node, ast.ClassDef):
            path.append(node.name)
            body = node.body
            continue
        return None  # module-level compound statement


def _find_def(tree: ast.Module, qual: tuple[str, ...]):
    body, node = tree.body, None
    for name in qual:
        node = next((n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name), None)
        if node is None:
            return None
        body = node.body
    return node


def _added_lines(fp) -> list[int]:
    out = []
    for h in fp.hunks:
        n = h.new_start
        for tag, _ in h.lines:
            if tag == "+":
                out.append(n)
            if tag != "-":
                n += 1
    return out


def overlay(src: Path, dst: Path, patched: dict[str, str]) -> None:
    """dst mirrors src: real directories along the patched paths, everything else a symlink."""
    import shutil

    if dst.exists():
        shutil.rmtree(dst)
    real = {"."}
    for p in patched:
        parts = p.split("/")
        for i in range(1, len(parts)):
            real.add("/".join(parts[:i]))

    def build_dir(rel: str) -> None:
        d = dst if rel == "." else dst / rel
        d.mkdir(parents=True, exist_ok=True)
        for entry in os.listdir(src if rel == "." else src / rel):
            r = entry if rel == "." else f"{rel}/{entry}"
            if r in patched:
                (dst / r).write_text(patched[r], "utf-8")
            elif r in real:
                build_dir(r)
            else:
                os.symlink(src / r, dst / r)

    build_dir(".")


class Replayer:
    """HEAVY_REPLAY at runtime. index: <index>/heavy.json from the build."""

    def __init__(self, index_path: Path, src: Path, number_by_id: dict[str, int], ms: dict[int, int],
                 budget_s: float = 90.0, replay: bool = True):
        self.src, self.ms = src, ms
        self.replay = replay  # False (HEAVY_REPLAY=static): only drops that need no replay
        self.budget = budget_s
        self.index_path = index_path
        self.numbers = {k: number_by_id[i] for k, i in TESTS.items() if i in number_by_id}
        self.idx = None  # loaded on the first commit that selects a heavy test
        self._loaded = False
        self.lines: dict = {}
        self.cheap_lines: dict = {}
        self.cpu_est = {"tpcds": 9.5, "tpch": 0.6, "opt_tpch": 0.6}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            idx = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if idx.get("tests") != TESTS:
            return  # built for other tests: unusable
        self.idx = idx
        self.lines = {k: {p: frozenset(v) for p, v in d.items()} for k, d in idx.get("lines", {}).items()}
        self.cheap_lines = {k: {p: frozenset(v) for p, v in d.items()}
                            for k, d in idx.get("cheap_lines", {}).items()}

    def _executed_query_changed(self, v) -> bool:
        """Cost only (keeping is always safe): did an `# execute: true` query's text change? Its
        plan then almost surely differs, so replaying it would be wasted CPU. Parsed with the
        pinned tests/helpers.py (a change to tests/ never gets here)."""
        import importlib.util

        from blast.apply import apply

        try:
            spec = importlib.util.spec_from_file_location("_pinned_helpers", self.src / "tests/helpers.py")
            helpers = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helpers)
            old = (self.src / v.path).read_bytes().decode("utf-8")
            new, _ = apply(v.fp, old)

            def pairs(text):
                stmts = helpers._filter_comments(text).split(";")
                return [helpers._extract_meta(stmts[i].strip()) for i in range(0, len(stmts) - 1, 2)]

            a, b = pairs(old), pairs(new)
        except Exception:  # noqa: BLE001 - cannot tell: replay
            return False
        if len(a) != len(b):
            return False
        return any(x != y and (x[1].get("execute") or y[1].get("execute")) for x, y in zip(a, b))

    def guard(self, kind: str, verdicts, ctx) -> tuple[str | None, bool]:
        """(None, replay?) if every change provably stays out of `kind`'s expensive phase, else
        (why not, False). replay? is False when no changed def runs in the cheap phase or at
        import time either and the kind's own fixture is untouched: nothing of the test runs it."""
        from blast import prover
        from blast.locate import Locator

        executor_tests = ctx.tests_by_file.get("tests/test_executor.py", frozenset())
        lines = self.lines.get(kind, {})
        replay = False
        for v in verdicts:
            if v.kind == prover.NOOP:
                continue
            if v.kind == prover.DATA:
                if v.path in SQL_FIXTURES:
                    # the cheap phase reads it: its queries are compared (its own fixture only)
                    if v.path == "tests/fixtures/" + FIXTURE[kind]:
                        if self._executed_query_changed(v):
                            return f"{v.path}: an executed query changed (replay would differ)", False
                        replay = True
                    continue
                if v.path.startswith(DATA_DIRS):
                    return f"{v.path}: TPC data", False
                readers, _ = ctx.file_read_tests(v.path)
                if readers is None or readers & executor_tests:
                    return f"{v.path}: read by TestExecutor (or unknown)", False
                continue
            if v.kind != prover.CHANGED or not v.path.startswith("sqlglot/") or not v.path.endswith(".py"):
                return f"{v.path}: {v.kind}", False
            e_lines = lines.get(v.path, frozenset())
            c_lines = self.cheap_lines.get(kind, {}).get(v.path, frozenset()) | ctx.db.import_lines(v.path)
            if kind not in self.cheap_lines:
                c_lines = None  # no record: always replay
            old = ctx.locator(v.path).tree
            new = Locator(v.new_text, v.path).tree
            defs = []
            for line in v.lines:
                d = _outer_def(old, line)
                if d is None:
                    return f"{v.path}:{line} outside a def body", False
                defs.append((d[0], d[1]))
            for line in _added_lines(v.fp) if v.fp is not None else []:
                d = _outer_def(new, line)
                if d is None:
                    return f"{v.path}: new line {line} outside a def body", False
                od = _find_def(old, d[0])
                if od is None:
                    return f"{v.path}: def {'.'.join(d[0])} is new", False
                defs.append((d[0], od))
            for qual, node in defs:
                body = set(range(node.lineno, node.end_lineno + 1))
                if e_lines & body:
                    return f"{v.path}: def {'.'.join(qual)} runs in the expensive phase", False
                if c_lines is None or c_lines & body:
                    replay = True
        return None, replay

    def maybe_drop(self, selected: list[int], verdicts, ctx, tmp: Path) -> tuple[list[int], str]:
        from blast.apply import apply
        from blast.pack import cost, pack

        sel = set(selected)
        if not any(n in sel for n in self.numbers.values()):
            return selected, ""
        self._load()
        if self.idx is None:
            return selected, "h:no index"
        present = [k for k, n in self.numbers.items() if n in sel and k not in self.idx["unstable"]]
        if not present:
            return selected, ""
        notes, kinds, candidates = [], [], []
        static_drop = set()
        for k in present:
            why, needs = self.guard(k, verdicts, ctx)
            if why is not None:
                notes.append(f"h:{k} keep ({why})")
            elif not needs:
                notes.append(f"h:{k} DROP static")
                static_drop.add(self.numbers[k])
            elif not self.replay:
                notes.append(f"h:{k} keep (needs a replay; HEAVY_REPLAY=static)")
            else:
                candidates.append(k)
        selected = [n for n in selected if n not in static_drop]
        sel -= static_drop
        base = cost(pack([(n, self.ms[n]) for n in sel]), self.ms)
        for k in candidates:  # a replay costs CPU: only when dropping the test saves more
            without = cost(pack([(n, self.ms[n]) for n in sel - {self.numbers[k]}]), self.ms)
            if base - without <= (self.cpu_est[k] + 1.0) * 1000:
                notes.append(f"h:{k} keep (gain {(base - without) / 1000:.1f}s too small)")
                continue
            kinds.append(k)
        if not kinds:
            return selected, " ".join(notes)
        est = sum(self.cpu_est[k] for k in kinds)
        if est > self.budget:
            return selected, " ".join(notes + ["h:replay budget spent"])
        try:
            patched = {}
            for v in verdicts:
                if v.kind == "CHANGED":
                    patched[v.path] = v.new_text
                elif v.kind == "DATA":
                    patched[v.path], _ = apply(v.fp, (self.src / v.path).read_bytes().decode("utf-8"))
            ov = tmp / "heavy_ov"
            overlay(self.src, ov, patched)
            import time as _t

            t0 = _t.time()
            got = run_replay(str(ov), kinds, self.idx["modules"], REPLAY_TIMEOUT)
            self.budget -= _t.time() - t0
        except Exception as e:  # noqa: BLE001 - any failure keeps the tests
            return selected, " ".join(notes + [f"h:replay failed {type(e).__name__}"])
        drop = set()
        for k in kinds:
            pinned, now = self.idx["digests"][k], got.get(k, {})
            same = now.get("queries") == pinned["queries"] and now.get("state") == pinned["state"] \
                and not now.get("errors")
            notes.append(f"h:{k} {'DROP' if same else 'keep (differs)'}")
            if same:
                drop.add(self.numbers[k])
        return [n for n in selected if n not in drop], " ".join(notes)


if __name__ == "__main__":
    if sys.argv[1] == "replay-main":
        print(json.dumps(replay_main(json.loads(sys.argv[2]), sys.argv[3])))
    elif sys.argv[1] == "replay-worker":
        print(json.dumps(replay_worker(json.loads(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4]))))
    elif sys.argv[1] == "build":
        sys.path.insert(0, str(Path(sys.argv[2]).resolve()))
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        build(Path(sys.argv[2]), Path(sys.argv[3]))
