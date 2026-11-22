"""Map-driven selection (Stage B3): which tests can a CHANGED or DATA file affect?

Rules (docs/GUIDE_B_Selector.md, Stage B3):
 a) A changed line -> all lines of its smallest enclosing statement -> map "lines".
 b) Import-time effects (line in import_lines, class body, def header/decorator/default):
    class_users of every enclosing class; at module level, the names the statement binds ->
    every place in sqlglot/** and tests/** using those names (name index) -> their tests,
    recursively for uses that are themselves import-time. Cannot resolve -> FULL.
    A def body that runs at import time -> FULL (its effect can be anywhere).
 c) Inside a def, nothing found -> DEAD (nobody runs it). FUNC_LEVEL=1: every line in the
    enclosing def is looked up as well, for every changed line in a def. FUNC_LEVEL=2 (default,
    hybrid): only when the changed statement itself found nothing, or the file's scope signature
    changed (a name turned local/global/free, a def turned generator).
 d) tests/*.py: inside a test function -> that test (+ map lines); module level or helper ->
    all tests in the file + tests covering the helper.
 e) DATA files: files_read; nothing -> fixture name fallback (blast/fixtures.py); else FULL.
 f) Always add failed_at_baseline, and UNMAPPED tests living in a file we selected from.
 s) setUpClass stopgap: class-level setup runs inside the FIRST test of its class (tests.json
    order = collection order) and the map credits it to that test alone. So when a files_read
    set contains the first test of a class, every test of that class is added; for map lines,
    only when the line's test set holds that first test and no other test of the class.
 x) SYNTAX_LAZY=1: a .py file whose new text does not parse (BROKEN) fails every import of it. Its
    closure = itself + every file importing it at import time (blast/imports.py, recursively).
    Any closure module loaded by `import sqlglot` -> FULL. Else: tests of closure test files, tests
    the map credits with any line of a closure module, class_users of its classes, tests that ran
    a def-level import of it, a dynamic-import def (importlib), or a use of its module basename /
    class names as an attribute (`pkg.X` on a lazy package: sqlglot.optimizer, sqlglot.dialects),
    + rule g tests. A use outside a def (outside the closure) -> FULL.
 b') ATTR_LEVEL=1 (map v4): a class-body edit is diffed (blast/attrs.py, old vs new text) into the
    attributes it changes and, for dict literals, the keys. Each attribute -> attr_users, each dict
    entry -> key_users[key] | key_users["*"], over the class and every subclass (by base name,
    index/imports.json "classes"). Read at import (attr_import / key_import), ambiguous, or no
    such key in the map -> class_users (rule b) as before.
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

import ast
import gzip
import json
import os
from pathlib import Path

from blast import attrs, infection, prover, rescue, safe
from blast.fixtures import TestIndex
from blast.imports import ImportGraph
from blast.locate import Loc, Locator, module_of, names_bound
from blast.mapdb import MapDB

# Built at docker build time by blast/build_index.py (eval_sample.py builds it into .cache/).
INDEX_DIR = Path(os.environ.get("INDEX_DIR", Path(__file__).resolve().parent.parent / "index"))
# 0: changed statement only; 1: every line of the enclosing def; 2 (hybrid): the statement when
# the map credits any test with it, else the def. 2 falls back to 1 for a file whose scope
# signature changed (a name became local/global/free, a def became a generator...): such an
# edit acts on lines other than its own.
FUNC_LEVEL = int(os.environ.get("FUNC_LEVEL", "2") or "2")
# Rule s (setUpClass stopgap) on/off. SETUP_STOPGAP=1 on, 0 off.
SETUP_STOPGAP = os.environ.get("SETUP_STOPGAP", "1") != "0"
# IMPORT_FUNC=1: a changed def body that runs at import time -> follow the def's name (its call
# sites, rule b name chain; a call site inside another def run at import recurses) instead of FULL.
IMPORT_FUNC = os.environ.get("IMPORT_FUNC", "1") == "1"
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "4"))
MAX_REF_FILES = 15  # a name used more widely than this -> FULL (cheaper than parsing it all)
MAX_REF_LINES = 300
# WIDE_NAMES=1: a name used more widely than the caps above is followed anyway; the commit goes FULL
# only when the tests it selects take more than WIDE_FRACTION of all test time.
WIDE_NAMES = os.environ.get("WIDE_NAMES", "0") == "1"
WIDE_FRACTION = 0.6
# UNUSED_FILES=1: a file the suite never runs contributes nothing: never imported (static graph,
# no lazy loader) and the map saw none of its lines; or a test file none of whose tests is in
# tests.json and that nothing imports (its own unlabelled tests are all it can break).
UNUSED_FILES = os.environ.get("UNUSED_FILES", "1") == "1"
# NOCU_NAMES=1: a class body change whose class has no class_users entry (never instantiated, nor
# a subclass) follows the class name through rule b's name chain instead of FULL.
NOCU_NAMES = os.environ.get("NOCU_NAMES", "0") == "1"
# ATTR_LEVEL=1 (map v4): a class-body edit selects the tests that read the attribute(s) it changes
# (attr_users) or, for dict entries, that looked up the changed keys or iterated the dict
# (key_users[key] | key_users["*"]), for the class and every subclass key in the map (rule b').
# Anything read at import time (attr_import / key_import), ambiguous, or unknown to the map ->
# class_users as before.
ATTR_LEVEL = os.environ.get("ATTR_LEVEL", "1") == "1"
# INFECTION=1: a file whose only change is one operator of one Compare / BoolOp (blast/infection.py)
# keeps, of the tests ITS rules selected that evaluated that node, only those where the new
# operator would have changed the outcome at least once (maps/infection_map.json.gz). Tests that
# never evaluated it, setUpClass extras (rule s) and every other rule's tests stay.
INFECTION = os.environ.get("INFECTION", "1") == "1"
# MODULE_NAMES=1: a module-level name bound in file D is looked up only where it can mean D's
# binding: in D itself, as an attribute (x.NAME), or in a file that imports NAME (or *) from D.
# Re-exports chain through rule b (the importing line binds NAME in the importer).
MODULE_NAMES = os.environ.get("MODULE_NAMES", "1") == "1"
# RESCUE_IMPORT=1 (probe-E class 6): an import-time change the name rules cannot follow (a class
# with no class_users, a module-level statement binding no plain names, a dunder def run at import)
# selects every test that can import the module (rule x's closure) instead of FULL, when the module
# is not loaded by `import sqlglot` and the changed code writes only to state the module owns
# (blast/rescue.py). Otherwise the original FULL.
RESCUE_IMPORT = os.environ.get("RESCUE_IMPORT", "0") == "1"
# RESCUE_NOTHING=1 (probe-E class 7, "lookup found nothing"): a sqlglot file whose every change is in
# def bodies no test runs contributes nothing; a file whose import-time lookups found nothing, or
# that the map has no lines for, -> the importer closure (as RESCUE_IMPORT); a .py outside sqlglot/
# and tests/ that no sqlglot/tests file names -> nothing; a tests/ data file no test reads, names,
# or could list -> nothing. Otherwise the original FULL.
RESCUE_NOTHING = os.environ.get("RESCUE_NOTHING", "0") == "1"

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
        func_level: int = FUNC_LEVEL,
        index_dir: Path = INDEX_DIR,
        fixture_index: TestIndex | None = None,
        setup_stopgap: bool | None = None,
    ):
        if setup_stopgap is None:
            setup_stopgap = SETUP_STOPGAP
        self.src = src
        self.db = db
        self.func_level = int(func_level)
        self.name_index_path = index_dir / "name_index.json.gz"
        self._names: dict | None = None
        self.fixtures = fixture_index or TestIndex(src, index_dir / "test_strings.json.gz")
        self.number_by_id = {t["id"]: t["number"] for t in tests}
        self.file_of = {t["number"]: t["file"] for t in tests}
        self.tests_by_file: dict[str, frozenset[int]] = {}
        for t in tests:
            self.tests_by_file.setdefault(t["file"], set()).add(t["number"])
        self.tests_by_file = {k: frozenset(v) for k, v in self.tests_by_file.items()}
        # rule s: test groups (file::Class, or the file for module-level tests), in tests.json order
        groups: dict[str, list[int]] = {}
        for t in tests:
            head, _, _name = t["id"].rpartition("::")
            groups.setdefault(head if "::" in head else t["file"], []).append(t["number"])
        self.group_of_first: dict[int, frozenset[int]] = {
            members[0]: frozenset(members) for members in groups.values() if len(members) > 1
        } if setup_stopgap else {}
        self._setup_extra: dict[tuple[str, int], frozenset[int]] = {}
        self._loc: dict[str, Locator] = {}
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.total_ms = sum(self.ms.values())
        self._attr_lines: dict[str, dict[str, set[int]]] = {}
        self._from_imports: dict[str, dict[str, set[str]]] = {}
        self.imports_path = index_dir / "imports.json"
        self.subclasses_path = index_dir / "subclasses.json"
        self._infection: infection.InfectionMap | None = None
        self._subclasses: dict[str, list[str]] | None = None
        self._imports: ImportGraph | None = None
        self.import_sqlglot = _import_sqlglot(index_dir / "import_sqlglot.json")
        self.subprocess_import_tests = frozenset(
            self.number_by_id[i] for i in SUBPROCESS_IMPORT_TESTS if i in self.number_by_id
        )

    # -- rule s ------------------------------------------------------------------------------
    def _extra_for_set(self, kind: str, idx: int) -> frozenset[int]:
        """Tests of classes whose first test is in set `idx` and, for "lines", alone in it."""
        key = (kind, idx)
        extra = self._setup_extra.get(key)
        if extra is None:
            s = self.db.tests_of_set(idx)
            out: set[int] = set()
            for n in s:
                group = self.group_of_first.get(n)
                if group is not None and (kind == "files" or len(s & group) == 1):
                    out |= group
            extra = self._setup_extra[key] = frozenset(out - s)
        return extra

    def line_tests(self, path: str, lines) -> tuple[frozenset[int] | None, frozenset[int]]:
        """(tests the map credits with any of `lines` or None, rule-s extra tests)."""
        ids = self.db.line_set_ids(path, lines)
        if not ids:
            return None, frozenset()
        base: set[int] = set()
        extra: set[int] = set()
        for i in ids:
            base |= self.db.tests_of_set(i)
            extra |= self._extra_for_set("lines", i)
        return frozenset(base), frozenset(extra - base)

    def file_read_tests(self, path: str) -> tuple[frozenset[int] | None, frozenset[int]]:
        idx = self.db.file_read_set_id(path)
        if idx is None:
            return None, frozenset()
        return self.db.tests_of_set(idx), self._extra_for_set("files", idx)

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

    def _scan(self, path: str) -> None:
        tree = self.locator(path).tree
        attrs: dict[str, set[int]] = {}
        froms: dict[str, set[str]] = {}  # imported module -> names (as bound here; "*" = star)
        is_pkg = path.endswith("__init__.py")
        mod = module_of(path)
        here = mod if is_pkg else mod.rpartition(".")[0]
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                attrs.setdefault(n.attr, set()).add(n.end_lineno)
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base = here.split(".")
                    if n.level > 1:
                        base = base[: len(base) - (n.level - 1)]
                    src_mod = ".".join(base + ([n.module] if n.module else []))
                else:
                    src_mod = n.module or ""
                for a in n.names:
                    if a.asname and a.asname != a.name:
                        froms.setdefault(src_mod, set()).add(f"{a.name} as {a.asname}")
                    else:
                        froms.setdefault(src_mod, set()).add(a.name)
        self._attr_lines[path] = attrs
        self._from_imports[path] = froms

    def ref_means(self, path: str, line: int, name: str, origin: str) -> bool:
        """MODULE_NAMES: can the use of `name` at path:line be origin's module-level binding?"""
        if path == origin:
            return True
        if path not in self._attr_lines:
            self._scan(path)
        if line in self._attr_lines[path].get(name, ()):
            return True
        names = self._from_imports[path].get(module_of(origin), set())
        if "*" in names or name in names:
            return True
        if any(n.startswith(f"{name} as ") for n in names):
            raise Full(f"{path} imports {name} from {origin} under another name")
        return False

    def unused(self, path: str) -> bool:
        if not UNUSED_FILES:
            return False
        g = self.imports()
        if not path.endswith(".py") or not path.startswith(("sqlglot/", "tests/")):
            return False
        name = path.rsplit("/", 1)[-1]
        if path.startswith("tests/") and name.startswith("test_") and not self.tests_by_file.get(path):
            return not (g.top_importers.get(path) or g.lazy_sites.get(path))
        return g.never_imported(path) and not self.db.has_file(path) and not self.loaded_by_import_sqlglot(path)

    def infection(self) -> infection.InfectionMap:
        if self._infection is None:
            self._infection = infection.InfectionMap(self.number_by_id)
        return self._infection

    def runtime_subclasses(self, key: str) -> set[str] | None:
        """Transitive subclasses of `key` from index/subclasses.json (every sqlglot module loaded at
        build time). None when the index is missing (caller falls back)."""
        if self._subclasses is None:
            try:
                self._subclasses = json.loads(self.subclasses_path.read_text("utf-8"))["subclasses"]
            except (OSError, ValueError, KeyError):
                return None
        out, todo = set(), [key]
        while todo:
            for s in self._subclasses.get(todo.pop(), ()):
                if s not in out:
                    out.add(s)
                    todo.append(s)
        return out

    def imports(self) -> ImportGraph:
        if self._imports is None:
            try:
                self._imports = ImportGraph(self.imports_path)
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise Full(f"no import graph ({type(e).__name__})") from e
        return self._imports

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


def scope_signature(text: str, path: str) -> list:
    """Per function scope (walk order): how each name binds + generator/coroutine-ness."""
    import symtable

    out: list = []

    def walk(t, trail):
        if t.get_type() != "module":
            syms = tuple(sorted(
                (s.get_name(), s.is_parameter(), s.is_local(), s.is_global(), s.is_declared_global(),
                 s.is_nonlocal(), s.is_free())
                for s in t.get_symbols()
            ))
            out.append((trail, t.get_type(), syms))
        for c in t.get_children():
            walk(c, trail + (c.get_name(),))

    walk(symtable.symtable(text, path, "exec"), ())
    tree = ast.parse(text.encode("utf-8"), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack, gen = list(ast.iter_child_nodes(node)), False
            while stack:
                n = stack.pop()
                if isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)):
                    gen = True
                    break
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                    stack.extend(ast.iter_child_nodes(n))
            out.append(("gen", gen))
    return out


def _unread_data(ctx: Ctx, path: str) -> bool:
    """A tests/ data file no test read in the map, whose name no sqlglot/tests file contains, while
    no test file lists a directory: no test can open it."""
    if not path.startswith("tests/") or rescue.lists_directories(ctx.src):
        return False
    name = path.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    return not rescue.mentions(ctx.src, [name, stem])


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
        self._seen_names: set = set()
        self.wide = False  # WIDE_NAMES: a name past the caps was followed
        self.import_time = False  # rule g: this file's own import-time code changed
        self.verdict: prover.FileVerdict | None = None
        self._scopes_same: bool | None = None
        self._attr_cache: dict[tuple[str, ...], frozenset[int] | None] = {}
        self.s_extra: set[int] = set()  # rule s (setUpClass) additions: INFECTION keeps them
        self._new: Locator | None = None  # RESCUE_*: locator of the new text

    # -- rule b': ATTR_LEVEL ------------------------------------------------------------------
    def attr_level(self, classes: list[str]) -> frozenset[int] | None:
        """Tests for a class-body edit of the innermost class `classes`; None = use class_users."""
        key = tuple(classes)
        if key not in self._attr_cache:
            try:
                self._attr_cache[key] = self._attr_level(classes)
            except (SyntaxError, ValueError, RecursionError):
                self._attr_cache[key] = None
        t = self._attr_cache[key]
        self.note(f"A:{'.'.join(classes)} " + ("fallback" if t is None else f"+{len(t)}"))
        return t

    def _attr_level(self, classes: list[str]) -> frozenset[int] | None:
        ctx, db, v = self.ctx, self.ctx.db, self.verdict
        if not db.has_attr_maps or v is None or v.kind != prover.CHANGED or not v.new_text:
            return None
        old_tree = ctx.locator(self.path).tree
        new_tree = Locator(v.new_text, self.path).tree
        oc, nc = attrs.class_node(old_tree, classes), attrs.class_node(new_tree, classes)
        if oc is None or nc is None:
            return None
        diff = attrs.class_diff(oc, nc, attrs.aliases(old_tree), attrs.aliases(new_tree))
        if diff is None:
            return None
        cls_key = f"{module_of(self.path)}:{'.'.join(classes)}"
        subs = ctx.imports().subclasses(cls_key)  # by base name, static (over-includes)
        loaded = ctx.runtime_subclasses(cls_key)  # __subclasses__ with every module imported
        if subs is None or loaded is None:
            return None
        owners = [cls_key, *sorted(set(subs) | loaded)]
        out: set[int] = set()
        for name in sorted(diff.whole):
            bases = [f"{c}.{name}" for c in owners]
            if any(b in db.attr_import for b in bases) or any(
                    k.startswith(tuple(f"{b}[" for b in bases)) for k in db.key_import):
                return None
            found = [db.attr_users[b] for b in bases if b in db.attr_users]
            if not found:
                return None
            for i in found:
                out |= db.tests_of_set(i)
        for name, spellings in sorted(diff.keys.items()):
            bases = [f"{c}.{name}" for c in owners]
            # a dict read at import (attr_import) is fine: what matters is whether its keys were
            # used then, K[*] or K[changed key] in key_import (Person A, map v4 note 2:
            # _Dialect.__new__ reads every generator's TRANSFORMS at import to pop JSONPath keys)
            look = [f"{b}[{k}]" for b in bases for k in sorted(spellings | {"*"})]
            if any(k in db.key_import for k in look):
                return None
            found = [db.key_users[k] for k in look if k in db.key_users]
            if not found:
                return None
            for i in found:
                out |= db.tests_of_set(i)
        return frozenset(out) or None

    def scopes_same(self) -> bool:
        """FUNC_LEVEL=2 guard: old and new text have the same scope signature."""
        if self._scopes_same is None:
            try:
                old = (self.ctx.src / self.path).read_bytes().decode("utf-8")
                self._scopes_same = self.verdict is not None and scope_signature(old, self.path) == \
                    scope_signature(self.verdict.new_text, self.path)
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                self._scopes_same = False
            if not self._scopes_same:
                self.note("c:scope changed")
        return self._scopes_same

    def note(self, s: str) -> None:
        if len(self.notes) < 12:
            self.notes.append(s)

    def add(self, tests, tag: str) -> bool:
        if tests:
            self.out |= tests
            self.note(f"{tag}+{len(tests)}")
            return True
        return False

    def map_lines(self, path: str, lines, tag: str | None) -> bool:
        """Rule a/c lookup (+ rule s extra); True when the map credits any test with `lines`."""
        base, extra = self.ctx.line_tests(path, lines)
        if base is None:
            return False
        self.s_extra |= extra
        if tag is None:
            self.out |= base | extra
        else:
            self.add(base, tag)
            self.add(extra, f"s:{tag}")
        return bool(base)

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
            if ATTR_LEVEL and path == self.path:
                t = self.attr_level(loc.classes)
                if t is not None:
                    self.add(t, f"A:{'.'.join(loc.classes)}")
                    return
            for key in loc.class_keys(module_of(path)):
                t = ctx.db.class_user_tests(key)
                if t is None and NOCU_NAMES and loc.top is not None and names_bound(loc.top):
                    self.note(f"b:no class_users {key.split(':')[1]}")
                    for name in names_bound(loc.top):
                        self.name_effect(name, depth, path)
                    continue
                if t is None:
                    if RESCUE_IMPORT:
                        self.rescue_closure(path, [loc.top], f"no class_users for {key}")
                        continue
                    raise Full(f"no class_users for {key}")
                self.add(t, f"b:class {key.split(':')[1]}")
            return
        if loc.top is None:
            return  # blank/comment line at module level: neighbours were looked up in a)
        names = names_bound(loc.top)
        if names is None:
            if RESCUE_IMPORT:
                self.rescue_closure(path, [loc.top], f"{path}:{loc.line} module-level statement binds no plain names")
                return
            raise Full(f"{path}:{loc.line} module-level statement binds no plain names")
        if safe.on("B"):
            raise Full(f"SAFE B: {path}:{loc.line} module-level names {','.join(names)}")
        for name in names:
            self.name_effect(name, depth, path)

    def import_func(self, path: str, func, depth: int) -> None:
        """IMPORT_FUNC: def `func` of `path` runs at import time and its body changed."""
        name = getattr(func, "name", None)
        if not name or (name.startswith("__") and name.endswith("__")):
            if RESCUE_IMPORT and name:
                self.rescue_closure(path, [func], f"{path}: def {name} runs at import, called implicitly")
                return
            raise Full(f"{path}: def {name} runs at import, called implicitly")
        self.note(f"i:def {name}")
        self.name_effect(name, depth, path)

    def name_effect(self, name: str, depth: int, origin: str | None = None) -> None:
        key = (origin, name) if MODULE_NAMES and origin else name
        if key in self._seen_names:
            return
        self._seen_names.add(key)
        if depth >= MAX_DEPTH:
            raise Full(f"name chain too deep at {name}")
        ctx = self.ctx
        refs = ctx.name_refs(name)
        if MODULE_NAMES and origin:
            refs = {p: kept for p, lines in refs.items()
                    if (kept := [n for n in lines if ctx.ref_means(p, n, name, origin)])}
        if len(refs) > MAX_REF_FILES or sum(len(v) for v in refs.values()) > MAX_REF_LINES:
            if not WIDE_NAMES:
                raise Full(f"name {name} used too widely ({len(refs)} files)")
            self.wide = True
        before = len(self.out)
        found = False  # a lookup credited some test (maybe one already selected: re-exports)
        for p, lines in sorted(refs.items()):
            if ctx.unused(p):
                continue
            lp = ctx.locator(p)
            imp = ctx.db.import_lines(p)
            for line in lines:
                loc = lp.locate(line)
                found |= self.map_lines(p, loc.stmt_lines, None)
                if loc.kind == "def_body":
                    if any(n in imp for n in loc.stmt_lines):
                        if not IMPORT_FUNC:
                            raise Full(f"{p}:{line} uses {name} in a def run at import")
                        self.import_func(p, loc.func, depth + 1)
                else:
                    self.import_effect(p, loc, depth + 1)
        if len(self.out) == before and not found and safe.on("E"):
            raise Full(f"SAFE E: name {name} adds no tests")
        self.note(f"b:name {name}+{len(self.out) - before}")

    # -- rules a-c for one changed ORIGINAL line ------------------------------------------
    def changed_line(self, loc: Loc) -> None:
        ctx, path = self.ctx, self.path
        imp = ctx.db.import_lines(path)
        if loc.kind == "def_body" and safe.on("C"):
            raise Full(f"SAFE C: {path}:{loc.line} runtime line")
        hit = self.map_lines(path, loc.stmt_lines, f"a:L{loc.line}")
        if loc.kind == "def_body":
            at_import = any(n in imp for n in loc.stmt_lines)
            if at_import:
                if not IMPORT_FUNC:
                    raise Full(f"{path}:{loc.line} def body runs at import")
                self.import_time = True
                self.import_func(path, loc.func, 0)
                hit = True
            if ctx.func_level == 1 or (ctx.func_level == 2 and (not hit or not self.scopes_same())):
                hit |= self.map_lines(path, _def_lines(loc.func), f"c:def {loc.func.name}")
            if not hit:
                if safe.on("E"):
                    raise Full(f"SAFE E: {path}:{loc.line} dead line")
                self.note(f"DEAD L{loc.line}")
            return
        self.import_time = True  # module level, class body or def header
        if loc.kind == "def_header":
            self.map_lines(path, _def_lines(loc.func), f"b:def {loc.func.name}")
        self.import_effect(path, loc, 0)

    # -- rule x: a file that no longer parses -----------------------------------------------
    def broken(self) -> set[int]:
        self.closure_tests(self.path)
        if not self.out and not self.path.startswith("tests/"):
            raise Full(f"x: nothing imports {self.path}")
        return self.out

    # -- RESCUE_IMPORT / RESCUE_NOTHING ---------------------------------------------------------
    def rescue_closure(self, path: str, nodes, why: str, unmapped_ok: bool = False) -> None:
        """Every test that can import `path` (rule x's closure), when that is provably a superset;
        else Full(why), the original reason."""
        trees = [(nodes, self.ctx.locator(path).tree)]
        if path == self.path:  # the changed file itself: guard its old AND new changed statements
            trees += self._changed_tops()
        for ns, tree in trees:
            if not rescue.writes_only_owned(ns, tree):
                raise Full(why)
        before = set(self.out)
        try:
            self.closure_tests(path, unmapped_ok)
        except Full:
            self.out = before
            raise Full(why) from None
        self.note(f"r:closure {path.rsplit('/', 1)[-1]}+{len(self.out - before)}")

    def _changed_tops(self) -> list[tuple[list, ast.Module]]:
        """Module-level statements holding a changed line: (old ones, old tree), (new ones, new tree)."""
        v = self.verdict
        if v is None or v.kind != prover.CHANGED:
            return []
        old = self.ctx.locator(self.path)
        if self._new is None:
            self._new = Locator(v.new_text, self.path)
        new = self._new
        olds = [old.locate(n).top for n in v.lines]
        news = [new.locate(n).top for n in (_added_new_lines(v.fp) if v.fp is not None else [])]
        return [(olds, old.tree), (news, new.tree)]

    def closure_tests(self, path: str, unmapped_ok: bool = False) -> None:
        """Rule x: add every test that can import `path`; Full if `import sqlglot` loads it.
        unmapped_ok: `path` itself may have no map lines (RESCUE_NOTHING, map has no lines)."""
        ctx = self.ctx
        if "<unknown>" in ctx.import_sqlglot:
            raise Full("no record of the modules `import sqlglot` loads")
        g = ctx.imports()
        closure = g.closure(path)
        for x in sorted(closure):
            if x.startswith("sqlglot/") and ctx.loaded_by_import_sqlglot(x):
                raise Full(f"x: {path} breaks {x}, loaded by `import sqlglot`")
        self.note(f"x:closure {len(closure)}")
        names: set[str] = set()
        for x in sorted(closure):
            if x.startswith("tests/"):
                self.add(ctx.tests_by_file.get(x), f"x:file {x.rsplit('/', 1)[-1]}")
                continue
            if not ctx.db.has_file(x):
                if unmapped_ok and x == path:
                    continue
                raise Full(f"x: map has no lines for {x}")
            self.map_lines(x, ctx.db.file_lines(x), f"x:lines {x.rsplit('/', 1)[-1]}")
            mod = module_of(x)
            for key in ctx.db.class_keys_of_module(mod):
                self.add(ctx.db.class_user_tests(key), f"x:class {key.split(':')[1]}")
            names.update(g.lazy_names.get(x, ()))  # lazy attribute access: sqlglot.dialects.X
            for imp, line in g.lazy_sites.get(x, ()):
                if imp in closure:
                    continue
                self.ref_site(imp, line, f"x:lazy {imp.rsplit('/', 1)[-1]}:{line}")
        for name in sorted(names):
            for p, lines in sorted(g.attr_refs.get(name, {}).items()):
                if p in closure:
                    continue
                for line in lines:
                    self.ref_site(p, line, None)
            self.note(f"x:attr {name}")
        for p, a, b in g.dynamic:
            self.map_lines(p, range(a, b + 1), f"x:dynamic {p.rsplit('/', 1)[-1]}:{a}")
        if self.ctx.subprocess_import_tests:
            self.add(ctx.subprocess_import_tests, "x:lazy_load")

    def ref_site(self, p: str, line: int, tag: str | None) -> None:
        """A use of a broken module outside its closure: runtime -> who ran it; else FULL."""
        ctx = self.ctx
        loc = ctx.locator(p).locate(line)
        if loc.kind != "def_body":
            if p.startswith("tests/") and ctx.tests_by_file.get(p):
                self.add(ctx.tests_by_file[p], f"x:file {p.rsplit('/', 1)[-1]}")
                return
            raise Full(f"x: {p}:{line} uses the broken module at import time")
        if any(n in ctx.db.import_lines(p) for n in loc.stmt_lines):
            raise Full(f"x: {p}:{line} uses the broken module in a def run at import")
        self.map_lines(p, loc.stmt_lines, tag)

    # -- rule d: a tests/*.py line -------------------------------------------------------
    def test_line(self, loc: Loc) -> None:
        ctx, path = self.ctx, self.path
        if loc.kind != "def_body":
            self.map_lines(path, loc.stmt_lines, f"a:L{loc.line}")
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
            self.map_lines(path, loc.stmt_lines, f"a:L{loc.line}")
            return
        # helper (setUp, a helper method or function): every test of the file + its users
        self.add(ctx.tests_by_file.get(path), "d:file")
        self.map_lines(path, _def_lines(unit), f"d:helper {getattr(unit, 'name', '?')}")
        if not ctx.tests_by_file.get(path) and not self.out:
            raise Full(f"helper {path}:{loc.line} has no known users")

    def infect(self, v: prover.FileVerdict) -> None:
        """INFECTION: narrow this file's own selection when its change is one operator."""
        ctx, path = self.ctx, self.path
        if v.fp is None:
            return
        tags = [tag for h in v.fp.hunks for tag, _ in h.lines if tag in "+-"]
        if tags.count("-") != 1 or tags.count("+") != 1 or v.trees is None or len(v.lines) != 1:
            return  # an operator swap is one line out, one line in
        added = _added_new_lines(v.fp)
        if added != v.lines:
            return  # the new line must sit where the old one was
        change = infection.single_op_change_at(v.trees[0], v.trees[1], v.lines[0])
        if change is None:
            return
        imap = ctx.infection()
        if not imap.ok:
            self.note("i:no infection map")
            return
        line, col, kind, new_op = change
        executed, differs = imap.lookup(path, line, col, kind, new_op)
        if executed is None:
            self.note(f"i:L{line} keep ({differs})")
            return
        drop = (self.out & executed) - differs - self.s_extra
        self.out -= drop
        self.note(f"i:L{line} {kind}->{new_op} -{len(drop)}")

    def run(self, v: prover.FileVerdict) -> set[int]:
        ctx, path = self.ctx, self.path
        self.verdict = v
        if ctx.unused(path):
            self.note("u:unused file")
            return self.out
        is_test = path.startswith("tests/")
        if not is_test:
            if not path.startswith("sqlglot/"):
                if RESCUE_NOTHING and self.unreferenced_outside(path):
                    self.note("r:outside, never named")
                    return self.out
                raise Full(f"{path} is outside sqlglot/ and tests/")
            if not ctx.db.has_file(path):
                if RESCUE_NOTHING:
                    self.rescue_closure(path, [], f"map has no lines for {path}", unmapped_ok=True)
                    return self.out
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
            why = f"map lookup found nothing for {path}"
            if RESCUE_NOTHING:
                if not is_test and not self.import_time:
                    self.note("r:dead defs only")  # no test runs any changed def: nothing to run
                    return self.out
                self.rescue_closure(path, [], why)
                if self.out:
                    return self.out
            raise Full(why)
        return self.out

    def unreferenced_outside(self, path: str) -> bool:
        """A .py outside sqlglot/ and tests/ (benchmarks/, pdoc/...) that no sqlglot or test file
        names: neither its top directory nor its file name appears anywhere in their text."""
        if "/" not in path:
            return False  # a top-level script (setup.py is structural anyway)
        top, name = path.split("/", 1)[0], path.rsplit("/", 1)[-1]
        return not rescue.mentions(self.ctx.src, [top, name])


def select(ctx: Ctx, verdicts: list[prover.FileVerdict]) -> tuple[set[int], str]:
    """Union over CHANGED and DATA files, plus rule f. Raises Full."""
    out: set[int] = set()
    trail: list[str] = []
    wide = False
    for v in verdicts:
        if v.kind == prover.CHANGED:
            f = _File(ctx, v.path)
            f.run(v)
            if INFECTION and v.path.startswith("sqlglot/") and v.path.endswith(".py"):
                f.infect(v)
            out |= f.out
            wide |= f.wide
            if f.import_time and v.path.startswith("sqlglot/"):
                if "<unknown>" in ctx.import_sqlglot:
                    raise Full("no record of the modules `import sqlglot` loads")
                if ctx.loaded_by_import_sqlglot(v.path) and ctx.subprocess_import_tests - out:
                    out |= ctx.subprocess_import_tests
                    f.note(f"g:import sqlglot+{len(ctx.subprocess_import_tests)}")
            trail.append(f"{v.path}: {' '.join(f.notes)}")
        elif v.kind == prover.BROKEN:
            f = _File(ctx, v.path)
            out |= f.broken()
            trail.append(f"{v.path}: {' '.join(f.notes)}")
        elif v.kind == prover.DATA:
            if safe.on("D"):
                raise Full(f"SAFE D: {v.path}")
            t, extra = ctx.file_read_tests(v.path)
            rule = "e:files_read"
            if t and extra:
                t = t | extra
                rule += f" s:+{len(extra)}"
            if not t and v.path.startswith("tests/fixtures/"):
                t = frozenset(
                    ctx.number_by_id[i] for i in ctx.fixtures.tests_mentioning(v.path) if i in ctx.number_by_id
                )
                rule = "e:fixture-name"
            if not t:
                if RESCUE_NOTHING and _unread_data(ctx, v.path):
                    trail.append(f"{v.path}: r:data never read or named")
                    continue
                raise Full(f"no test reads {v.path}")
            out |= t
            trail.append(f"{v.path}: {rule}+{len(t)}")
        elif v.kind != prover.NOOP:
            raise Full(f"{v}")
    if wide:
        share = sum(ctx.ms.get(n, 0) for n in out) / max(ctx.total_ms, 1)
        if share > WIDE_FRACTION:
            raise Full(f"wide name selects {share:.0%} of test time")
        trail.append(f"w:{share:.0%}")
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
