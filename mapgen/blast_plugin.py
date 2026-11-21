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
    _current = nid
    _cov.switch_context(nid)
    try:
        yield
    finally:
        _cov.switch_context("")
        _current = None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if report.failed and report.nodeid not in _failed:
        _failed[report.nodeid] = f"{report.when}: {type(report).__name__}"


def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    class_users = {}
    for nid in _seen_tests:
        for k in _expand(_types[nid]):
            class_users.setdefault(k, set()).add(nid)
    extra = {
        "tests": _seen_tests,
        "files_read": {p: sorted(n for n in _files if p in _files[n])
                       for p in sorted(set().union(*_files.values()) if _files else [])},
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
