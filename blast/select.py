"""Map-driven selection (Stage B3): which tests can a CHANGED or DATA file affect?

Rules (docs/GUIDE_B_Selector.md, Stage B3):
 a) A changed line -> all lines of its smallest enclosing statement -> map "lines".
 b) Import-time effects (line in import_lines, class body, def header/decorator/default):
    class_users of every enclosing class; at module level, the names the statement binds ->
    every place in sqlglot/** and tests/** using those names (name index) -> their tests,
    recursively for uses that are themselves import-time. Cannot resolve -> FULL.
    A def body that runs at import time -> FULL (its effect can be anywhere).
 c) Inside a def, nothing found -> DEAD (nobody runs it). FUNC_LEVEL=1 (default): every line in
    the enclosing def is looked up as well, for every changed line in a def.
 d) tests/*.py: inside a test function -> that test (+ map lines); module level or helper ->
    all tests in the file + tests covering the helper.
 e) DATA files: files_read; nothing -> fixture name fallback (blast/fixtures.py); else FULL.
 f) Always add failed_at_baseline, and UNMAPPED tests living in a file we selected from.
 g) Tests that run `import sqlglot` in a SUBPROCESS (the map cannot see them, e.g. test_lazy_load):
    added when a commit changes import-time code (module level, class body, def header) of a
    module that `import sqlglot` loads: IMPORT_SQLGLOT (hand list) union index/import_sqlglot.json
    (recorded at docker build time). Tests are looked up by id in tests.json.
A CHANGED file for which every lookup finds nothing -> FULL (never guess empty on code).

SAFE_GROUPS (blast/safe.py) turns rule families back into FULL: B = class_users + name chain,
C = map lines/def for runtime code, D = fixtures, E = DEAD lines / names that find nothing.

'+' lines are also located in the NEW text: one that lands outside a def body (new module
statement, class attribute, def header) is an import-time effect and goes through rule b.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from blast import prover, safe
from blast.fixtures import TestIndex
from blast.locate import Loc, Locator, module_of, names_bound
from blast.mapdb import MapDB

# Built at docker build time by blast/build_index.py (eval_sample.py builds it into .cache/).
INDEX_DIR = Path(os.environ.get("INDEX_DIR", Path(__file__).resolve().parent.parent / "index"))
FUNC_LEVEL = os.environ.get("FUNC_LEVEL", "1") != "0"
MAX_DEPTH = 3
MAX_REF_FILES = 15  # a name used more widely than this -> FULL (cheaper than parsing it all)
MAX_REF_LINES = 300

# Rule g. Tests that run `import sqlglot` in a subprocess (ids, never numbers).
SUBPROCESS_IMPORT_TESTS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)
# Modules `import sqlglot` loads (Person A's list); a trailing "/" is a whole package.
IMPORT_SQLGLOT = frozenset({
    "sqlglot/__init__.py", "sqlglot/_typing.py", "sqlglot/dialects/__init__.py",
    "sqlglot/dialects/dialect.py", "sqlglot/expressions/", "sqlglot/parser.py",
    "sqlglot/generator.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py", "sqlglot/schema.py",
    "sqlglot/optimizer/__init__.py", "sqlglot/optimizer/scope.py", "sqlglot/helper.py",
    "sqlglot/time.py", "sqlglot/trie.py", "sqlglot/jsonpath.py", "sqlglot/diff.py",
    "sqlglot/errors.py", "sqlglot/parsers/__init__.py", "sqlglot/parsers/base.py",
    "sqlglot/typing/__init__.py",
})


class Full(Exception):
    """This commit cannot be narrowed: run everything."""


class Ctx:
    def __init__(
        self,
        src: Path,
        db: MapDB,
        tests: list[dict],
        func_level: bool = FUNC_LEVEL,
        index_dir: Path = INDEX_DIR,
        fixture_index: TestIndex | None = None,
    ):
        self.src = src
        self.db = db
        self.func_level = func_level
        self.name_index_path = index_dir / "name_index.json.gz"
        self._names: dict | None = None
        self.fixtures = fixture_index or TestIndex(src, index_dir / "test_strings.json.gz")
        self.number_by_id = {t["id"]: t["number"] for t in tests}
        self.file_of = {t["number"]: t["file"] for t in tests}
        self.tests_by_file: dict[str, frozenset[int]] = {}
        for t in tests:
            self.tests_by_file.setdefault(t["file"], set()).add(t["number"])
        self.tests_by_file = {k: frozenset(v) for k, v in self.tests_by_file.items()}
        self._loc: dict[str, Locator] = {}
        self.import_sqlglot = _import_sqlglot(index_dir / "import_sqlglot.json")
        self.subprocess_import_tests = frozenset(
            self.number_by_id[i] for i in SUBPROCESS_IMPORT_TESTS if i in self.number_by_id
        )

    def loaded_by_import_sqlglot(self, path: str) -> bool:
        return path in self.import_sqlglot or any(
            p.endswith("/") and path.startswith(p) for p in self.import_sqlglot
        )

    def locator(self, path: str) -> Locator:
        loc = self._loc.get(path)
        if loc is None:
            try:
                text = (self.src / path).read_bytes().decode("utf-8")
                loc = Locator(text, path)
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as e:
                raise Full(f"cannot locate in {path}: {type(e).__name__}") from e
            self._loc[path] = loc
        return loc

    def name_refs(self, name: str) -> dict[str, list[int]]:
        if self._names is None:
            try:
                with gzip.open(self.name_index_path, "rt", encoding="utf-8") as f:
                    self._names = json.load(f)
            except OSError as e:
                raise Full(f"no name index ({type(e).__name__})") from e
        return self._names.get(name) or {}


def _import_sqlglot(path: Path) -> frozenset[str]:
    """IMPORT_SQLGLOT union what the build recorded. Missing/broken record -> Full on use."""
    try:
        recorded = json.loads(path.read_text("utf-8"))
        if not isinstance(recorded, list) or "sqlglot/__init__.py" not in recorded:
            raise ValueError("bad record")
    except (OSError, ValueError) as e:
        return frozenset({"<unknown>", *IMPORT_SQLGLOT, f"<error {type(e).__name__}>"})
    return IMPORT_SQLGLOT | frozenset(recorded)


def _added_new_lines(fp) -> list[int]:
    out = []
    for h in fp.hunks:
        n = h.new_start
        for tag, _ in h.lines:
            if tag == "+":
                out.append(n)
            if tag != "-":
                n += 1
    return out


def _def_lines(func) -> range:
    return range(func.lineno, func.end_lineno + 1)


class _File:
    """Selection for one file; `notes` is the rule trail for the log."""

    def __init__(self, ctx: Ctx, path: str):
        self.ctx, self.path = ctx, path
        self.out: set[int] = set()
        self.notes: list[str] = []
        self._seen_names: set[str] = set()
        self.import_time = False  # rule g: this file's own import-time code changed

    def note(self, s: str) -> None:
        if len(self.notes) < 12:
            self.notes.append(s)

    def add(self, tests, tag: str) -> bool:
        if tests:
            self.out |= tests
            self.note(f"{tag}+{len(tests)}")
            return True
        return False

    # -- rule b: import-time effect of a location ----------------------------------------
    def import_effect(self, path: str, loc: Loc, depth: int) -> None:
        ctx = self.ctx
        if path.startswith("tests/"):
            in_file = ctx.tests_by_file.get(path)
            if in_file:
                self.add(in_file, f"d:{path.rsplit('/', 1)[-1]}:all")
                return
            # a helper module (tests/helpers.py): follow the names it binds
        elif loc.classes:
            if safe.on("B"):
                raise Full(f"SAFE B: {path}:{loc.line} class body {'.'.join(loc.classes)}")
            for key in loc.class_keys(module_of(path)):
                t = ctx.db.class_user_tests(key)
                if t is None:
                    raise Full(f"no class_users for {key}")
                self.add(t, f"b:class {key.split(':')[1]}")
            return
        if loc.top is None:
            return  # blank/comment line at module level: neighbours were looked up in a)
        names = names_bound(loc.top)
        if names is None:
            raise Full(f"{path}:{loc.line} module-level statement binds no plain names")
        if safe.on("B"):
            raise Full(f"SAFE B: {path}:{loc.line} module-level names {','.join(names)}")
        for name in names:
            self.name_effect(name, depth)

    def name_effect(self, name: str, depth: int) -> None:
        if name in self._seen_names:
            return
        self._seen_names.add(name)
        if depth >= MAX_DEPTH:
            raise Full(f"name chain too deep at {name}")
        ctx = self.ctx
        refs = ctx.name_refs(name)
        if len(refs) > MAX_REF_FILES or sum(len(v) for v in refs.values()) > MAX_REF_LINES:
            raise Full(f"name {name} used too widely ({len(refs)} files)")
        before = len(self.out)
        for p, lines in sorted(refs.items()):
            lp = ctx.locator(p)
            imp = ctx.db.import_lines(p)
            for line in lines:
                loc = lp.locate(line)
                ran = ctx.db.line_tests(p, loc.stmt_lines)
                if ran:
                    self.out |= ran
                if loc.kind == "def_body":
                    if any(n in imp for n in loc.stmt_lines):
                        raise Full(f"{p}:{line} uses {name} in a def run at import")
                else:
                    self.import_effect(p, loc, depth + 1)
        if len(self.out) == before and safe.on("E"):
            raise Full(f"SAFE E: name {name} adds no tests")
        self.note(f"b:name {name}+{len(self.out) - before}")

    # -- rules a-c for one changed ORIGINAL line ------------------------------------------
    def changed_line(self, loc: Loc) -> None:
        ctx, path = self.ctx, self.path
        imp = ctx.db.import_lines(path)
        if loc.kind == "def_body" and safe.on("C"):
            raise Full(f"SAFE C: {path}:{loc.line} runtime line")
        hit = self.add(ctx.db.line_tests(path, loc.stmt_lines), f"a:L{loc.line}")
        if loc.kind == "def_body":
            if any(n in imp for n in loc.stmt_lines):
                raise Full(f"{path}:{loc.line} def body runs at import")
            if ctx.func_level:
                hit |= self.add(ctx.db.line_tests(path, _def_lines(loc.func)), f"c:def {loc.func.name}")
            if not hit:
                if safe.on("E"):
                    raise Full(f"SAFE E: {path}:{loc.line} dead line")
                self.note(f"DEAD L{loc.line}")
            return
        self.import_time = True  # module level, class body or def header
        if loc.kind == "def_header":
            self.add(ctx.db.line_tests(path, _def_lines(loc.func)), f"b:def {loc.func.name}")
        self.import_effect(path, loc, 0)

    # -- rule d: a tests/*.py line -------------------------------------------------------
    def test_line(self, loc: Loc) -> None:
        ctx, path = self.ctx, self.path
        if loc.kind != "def_body":
            self.add(ctx.db.line_tests(path, loc.stmt_lines), f"a:L{loc.line}")
            self.import_effect(path, loc, 0)
            return
        top = loc.top
        test_id, unit = None, top
        if top.name.startswith("test") and not hasattr(top, "bases"):
            test_id = f"{path}::{top.name}"
        elif hasattr(top, "bases"):  # ClassDef: the method holding the line
            unit = next((n for n in top.body if hasattr(n, "args") and n.lineno <= loc.line <= n.end_lineno), top)
            if unit is not top and unit.name.startswith("test"):
                test_id = f"{path}::{top.name}::{unit.name}"
        if test_id is not None and test_id in ctx.number_by_id:
            self.add(frozenset([ctx.number_by_id[test_id]]), f"d:{unit.name}")
            self.add(ctx.db.line_tests(path, loc.stmt_lines), f"a:L{loc.line}")
            return
        # helper (setUp, a helper method or function): every test of the file + its users
        self.add(ctx.tests_by_file.get(path), "d:file")
        self.add(ctx.db.line_tests(path, _def_lines(unit)), f"d:helper {getattr(unit, 'name', '?')}")
        if not ctx.tests_by_file.get(path) and not self.out:
            raise Full(f"helper {path}:{loc.line} has no known users")

    def run(self, v: prover.FileVerdict) -> set[int]:
        ctx, path = self.ctx, self.path
        is_test = path.startswith("tests/")
        if not is_test:
            if not path.startswith("sqlglot/"):
                raise Full(f"{path} is outside sqlglot/ and tests/")
            if not ctx.db.has_file(path):
                raise Full(f"map has no lines for {path}")
        if not v.lines:
            raise Full(f"{path}: no changed original lines")
        orig = ctx.locator(path)
        for line in v.lines:
            loc = orig.locate(line)
            if is_test:
                self.test_line(loc)
            else:
                self.changed_line(loc)
        added = _added_new_lines(v.fp) if v.fp is not None else []
        if added:
            try:
                new = Locator(v.new_text, path)
            except (SyntaxError, ValueError) as e:
                raise Full(f"new {path} does not locate: {e}") from e
            for line in added:
                loc = new.locate(line)
                if loc.kind == "def_body":
                    continue  # runtime code: covered by the original neighbour lines
                self.import_time = True
                if is_test:
                    self.test_line(loc)
                else:
                    self.import_effect(path, loc, 0)
        if not self.out:
            raise Full(f"map lookup found nothing for {path}")
        return self.out


def select(ctx: Ctx, verdicts: list[prover.FileVerdict]) -> tuple[set[int], str]:
    """Union over CHANGED and DATA files, plus rule f. Raises Full."""
    out: set[int] = set()
    trail: list[str] = []
    for v in verdicts:
        if v.kind == prover.CHANGED:
            f = _File(ctx, v.path)
            out |= f.run(v)
            if f.import_time and v.path.startswith("sqlglot/"):
                if "<unknown>" in ctx.import_sqlglot:
                    raise Full("no record of the modules `import sqlglot` loads")
                if ctx.loaded_by_import_sqlglot(v.path) and ctx.subprocess_import_tests - out:
                    out |= ctx.subprocess_import_tests
                    f.note(f"g:import sqlglot+{len(ctx.subprocess_import_tests)}")
            trail.append(f"{v.path}: {' '.join(f.notes)}")
        elif v.kind == prover.DATA:
            if safe.on("D"):
                raise Full(f"SAFE D: {v.path}")
            t, rule = ctx.db.file_read_tests(v.path), "e:files_read"
            if not t and v.path.startswith("tests/fixtures/"):
                t = frozenset(
                    ctx.number_by_id[i] for i in ctx.fixtures.tests_mentioning(v.path) if i in ctx.number_by_id
                )
                rule = "e:fixture-name"
            if not t:
                raise Full(f"no test reads {v.path}")
            out |= t
            trail.append(f"{v.path}: {rule}+{len(t)}")
        elif v.kind != prover.NOOP:
            raise Full(f"{v}")
    if out:
        extra = set(ctx.db.failed_at_baseline)
        files = {ctx.file_of[n] for n in out if n in ctx.file_of}
        if not ctx.db.empty:  # with the stub every test is "unmapped": skip
            extra |= {n for n in ctx.db.unmapped if ctx.file_of.get(n) in files}
        extra -= out
        if extra:
            out |= extra
            trail.append(f"f:+{len(extra)}")
    return out, "; ".join(trail)
