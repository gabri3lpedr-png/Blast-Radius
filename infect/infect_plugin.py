"""pytest plugin building the INFECTION map (dev-only; run by infect/make_infection_map.sh in the
blast-radius-mapgen image). Mirrors mapgen/blast_plugin.py's crediting: one context per test node
id, setup@/teardown@ phases credited to the whole class, process pools run inline (args and results
pickled), every sqlglot module imported up front in the "" context (import/collection).

Writes $INFECT_OUT/infection_raw.json: nodes, tests, contexts, failures.
"""
import concurrent.futures
import concurrent.futures.process
import json
import os
import pickle
import pkgutil
import sys
import time

import _infect_rt as rt
import instrument

ROOT = os.environ.get("INFECT_ROOT", "/opt/sqlglot")
OUT = os.environ.get("INFECT_OUT", "/out")
instrument.install(ROOT)
_t0 = time.time()

_contexts = {"": rt.cur}
_vcontexts = {"": rt.curv}  # CONST: per context {nid: set of value keys | MANY}
_seen_tests = []
_failed = {}


class _InlineExecutor(concurrent.futures.Executor):
    """Synchronous stand-in for ProcessPoolExecutor (as mapgen/blast_plugin.py)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(tuple(initargs))))

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


concurrent.futures.ProcessPoolExecutor = _InlineExecutor
concurrent.futures.process.ProcessPoolExecutor = _InlineExecutor

import sqlglot  # noqa: E402

_import_errors = []
for _m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if _m.name.endswith("__main__"):
        continue
    try:
        __import__(_m.name)
    except Exception as e:  # noqa: BLE001
        _import_errors.append(f"{_m.name}: {type(e).__name__}: {e}")


def _switch(key):
    rt.cur = _contexts.setdefault(key, {})
    rt.curv = _vcontexts.setdefault(key, {})


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _seen_tests.append(item.nodeid)
    _switch(item.nodeid)
    try:
        yield
    finally:
        _switch("")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _switch("setup@" + item.nodeid)
    try:
        yield
    finally:
        _switch(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _switch("teardown@" + item.nodeid)
    try:
        yield
    finally:
        _switch(item.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if report.failed and report.nodeid not in _failed:
        _failed[report.nodeid] = f"{report.when}"


def pytest_unconfigure(config):
    raw = {
        "nodes": rt.NODES,
        "tests": _seen_tests,
        "contexts": {k: {str(n): m for n, m in v.items()} for k, v in _contexts.items()},
        "values": {k: {str(n): rt.export_values(vs) for n, vs in v.items()} for k, v in _vcontexts.items() if v},
        "lit": {str(n): side for n, side in rt.LIT.items() if side},
        "failed": _failed,
        "meta": {"run_seconds": round(time.time() - _t0, 1), "import_errors": _import_errors,
                 "python": sys.version.split()[0]},
    }
    with open(os.path.join(OUT, "infection_raw.json"), "w") as f:
        json.dump(raw, f)
