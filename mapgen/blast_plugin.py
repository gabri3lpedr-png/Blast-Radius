"""pytest plugin that records, per test, what each sqlglot test touched.

Loaded with `-p blast_plugin` (mapgen/ on PYTHONPATH), inside the mapgen image.
Writes to $BLAST_OUT (default /out):
  .coverage          line coverage, one dynamic context per test node id
                     ("" = lines run outside any test: import / collection)
  blast_extra.json   files_read, class_users, failed tests, meta

Coverage starts when this module is imported, before conftest and collection,
so import-time lines land in the empty context.
"""
import builtins
import io
import json
import os
import pkgutil
import sys
import time

import coverage

ROOT = "/opt/sqlglot"
OUT = os.environ.get("BLAST_OUT", "/out")

_cov = coverage.Coverage(
    data_file=os.path.join(OUT, ".coverage"),
    source=[f"{ROOT}/sqlglot", f"{ROOT}/tests"],
    branch=False,
)
_cov.start()
_t0 = time.time()

# ---------------------------------------------------------------- state
_current = None            # node id of the running test, or None
_files = {}                # node id -> set of repo-relative non-.py paths
_types = {}                # node id -> set of classes instantiated
_failed = {}               # node id -> first failure description
_seen_tests = []


# ---------------------------------------------------------------- files_read
_real_open = io.open


def _rel(path):
    try:
        p = os.path.realpath(os.fspath(path))
    except Exception:
        return None
    if p.startswith(ROOT + "/") and not p.endswith((".py", ".pyc")):
        return p[len(ROOT) + 1:]
    return None


def _tracking_open(file, *args, **kwargs):
    if _current is not None and not isinstance(file, int):
        rel = _rel(file)
        if rel is not None:
            _files[_current].add(rel)
    return _real_open(file, *args, **kwargs)


builtins.open = _tracking_open
io.open = _tracking_open


# ---------------------------------------------------------------- process pools
# tests/test_optimizer.py and tests/test_executor.py run work in a
# ProcessPoolExecutor (spawn). Coverage in this process cannot see those
# workers, so their lines would be credited to no test. Run pool jobs inline
# instead, in this process, under the current test's context. Patched before
# collection, so `from concurrent.futures import ProcessPoolExecutor` in the
# test modules picks this up.
import concurrent.futures  # noqa: E402
import concurrent.futures.process  # noqa: E402
import pickle  # noqa: E402


class _InlineExecutor(concurrent.futures.Executor):
    """Synchronous stand-in for ProcessPoolExecutor, MAP-BUILDING ONLY.

    Arguments and results go through pickle like a real worker's would, so
    pickling code (sqlglot's serde.dump / serde.load via __reduce__) still runs
    and is credited to the test. map(), shutdown() and `with` come from Executor.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None,
                 initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(tuple(initargs))))

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa: BLE001 - mirror a worker's failure
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


concurrent.futures.ProcessPoolExecutor = _InlineExecutor
concurrent.futures.process.ProcessPoolExecutor = _InlineExecutor
for _name, _mod in list(sys.modules.items()):  # any test module already imported
    if _name.startswith("tests") and getattr(_mod, "ProcessPoolExecutor", None) is not None:
        _mod.ProcessPoolExecutor = _InlineExecutor


# ---------------------------------------------------------------- class_users
# Import every sqlglot module now, in the "" context, so lazily loaded dialects,
# parsers and generators count as import-time lines instead of being credited
# to whichever test happened to load them first.
import sqlglot  # noqa: E402

_import_errors = []
for _m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        __import__(_m.name)
    except Exception as e:  # optional deps etc.
        _import_errors.append(f"{_m.name}: {type(e).__name__}: {e}")


def _base_classes():
    bases = []
    for mod, name in [
        ("sqlglot.expressions.core", "Expr"),
        ("sqlglot.dialects.dialect", "Dialect"),
        ("sqlglot.tokens", "Tokenizer"),
        ("sqlglot.parser", "Parser"),
        ("sqlglot.generator", "Generator"),
        ("sqlglot.schema", "Schema"),
        ("sqlglot.optimizer.annotate_types", "TypeAnnotator"),
        ("sqlglot.executor.python", "PythonExecutor"),
        ("sqlglot.executor.table", "Table"),
        ("sqlglot.executor.env", "Env"),
    ]:
        m = sys.modules.get(mod)
        cls = getattr(m, name, None) if m else None
        if isinstance(cls, type):
            bases.append(cls)
    return bases


_BASES = _base_classes()
_patched = set()


_EXPR = _BASES[0] if _BASES and _BASES[0].__name__ == "Expr" else None


def _wrap_init(cls):
    orig = cls.__dict__["__init__"]

    if _EXPR is not None and issubclass(cls, _EXPR):
        def __init__(self, *a, **k):
            if _current is not None:
                _types[_current].add(type(self))
            return orig(self, *a, **k)
    else:
        # Tokenizer / Parser / Generator / ...: also credit the dialect they
        # carry, since Dialect instances are cached and built only once.
        def __init__(self, *a, **k):
            r = orig(self, *a, **k)
            if _current is not None:
                s = _types[_current]
                s.add(type(self))
                d = self.__dict__.get("dialect") if hasattr(self, "__dict__") else None
                if d is not None:
                    s.add(type(d))
            return r

    __init__.__wrapped__ = orig
    cls.__init__ = __init__
    _patched.add(cls)


def _patch_new_classes():
    """Wrap __init__ on every base class and every subclass that defines its
    own __init__. Re-run before each test: classes can be created at runtime."""
    stack = list(_BASES)
    seen = set()
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        if cls not in _patched and "__init__" in cls.__dict__:
            try:
                _wrap_init(cls)
            except (TypeError, AttributeError):
                pass
        stack.extend(cls.__subclasses__())


# Dialect["teradata"] / Dialect.get("teradata") hand out the CLASS without
# building an instance (e.g. build_formatted_time reads Teradata.TIME_MAPPING
# from Presto's parser), so __init__ wrappers never see it. Record lookups too.
def _wrap_dialect_lookup():
    meta = type(_BASES[1]) if len(_BASES) > 1 and _BASES[1].__name__ == "Dialect" else None
    if meta is None:
        return
    for name in ("__getitem__", "get"):
        orig = meta.__dict__.get(name)
        if orig is None:
            continue

        is_cm = isinstance(orig, classmethod)
        func = orig.__func__ if is_cm else orig

        def wrapper(cls, *a, __func=func, **k):
            r = __func(cls, *a, **k)
            if _current is not None and isinstance(r, type):
                _types[_current].add(r)
            return r

        wrapper.__wrapped__ = func
        setattr(meta, name, classmethod(wrapper) if is_cm else wrapper)


_wrap_dialect_lookup()


def _key(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def _expand(types):
    keys = set()
    for t in types:
        for c in t.__mro__:
            if c.__module__.startswith("sqlglot"):
                keys.add(_key(c))
    return keys


# ---------------------------------------------------------------- pytest hooks
import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _current
    nid = item.nodeid
    _seen_tests.append(nid)
    _files.setdefault(nid, set())
    _types.setdefault(nid, set())
    _patch_new_classes()
    _phase(nid)
    try:
        yield
    finally:
        _cov.switch_context("")
        _current = None


# setUpClass / module fixtures run inside the FIRST test's setup and
# tearDownClass inside the LAST test's teardown, yet they serve the whole class
# (TestExecutor.setUpClass reads tpc-h.sql for test_optimized_tpch). Record the
# setup/teardown phases under "setup@<nodeid>" / "teardown@<nodeid>";
# export_map.py credits them to every test of that class (or module).
def _phase(key):
    global _current
    _current = key
    _files.setdefault(key, set())
    _types.setdefault(key, set())
    _cov.switch_context(key)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _phase("setup@" + item.nodeid)
    try:
        yield
    finally:
        _phase(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _phase("teardown@" + item.nodeid)
    try:
        yield
    finally:
        _phase(item.nodeid)


def _scope_tests(key):
    """'setup@a::B::c' -> every seen test in a::B; a plain node id -> itself."""
    if "@" not in key.split("::")[0]:
        return [key]
    nid = key.split("@", 1)[1]
    scope = nid.rsplit("::", 1)[0] + "::"
    return [t for t in _seen_tests if t.startswith(scope)] or [nid]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if report.failed and report.nodeid not in _failed:
        _failed[report.nodeid] = f"{report.when}: {type(report).__name__}"


def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    class_users = {}
    files_read = {}
    for key in _types:
        owners = _scope_tests(key)
        for k in _expand(_types[key]):
            class_users.setdefault(k, set()).update(owners)
        for p in _files.get(key, ()):
            files_read.setdefault(p, set()).update(owners)
    extra = {
        "tests": _seen_tests,
        "files_read": {p: sorted(v) for p, v in sorted(files_read.items())},
        "class_users": {k: sorted(v) for k, v in sorted(class_users.items())},
        "failed": _failed,
        "meta": {
            "coverage_version": coverage.__version__,
            "coverage_core": os.environ.get("COVERAGE_CORE", ""),
            "python": sys.version.split()[0],
            "run_seconds": round(time.time() - _t0, 1),
            "patched_init_classes": len(_patched),
            "import_errors": _import_errors,
        },
    }
    with _real_open(os.path.join(OUT, "blast_extra.json"), "w") as f:
        json.dump(extra, f)
