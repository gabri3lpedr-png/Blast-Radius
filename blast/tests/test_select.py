"""Rules a-f of blast/select.py against a tiny hand-written source tree and coverage map.

Run: python3 -m unittest discover -s blast/tests -t .     (or: make unit)
"""

from __future__ import annotations

import difflib
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from contextlib import contextmanager

from blast import build_index, diff, imports, prover, safe
from blast import select as sel
from blast.fixtures import TestIndex
from blast.mapdb import MapDB

MOD = textwrap.dedent('''\
    LIMIT = 10


    def helper(x):
        y = x + 1
        return y * LIMIT


    def unused():
        return 0


    class Thing:
        KIND = "a"

        def run(self, z=3):
            return z
    ''')
# line:  1 LIMIT | 4 def helper | 5 y = | 6 return y*LIMIT | 9 def unused | 10 return 0
#       13 class Thing | 14 KIND | 16 def run | 17 return z

USER = textwrap.dedent('''\
    from sqlglot.mod import LIMIT


    def use():
        return LIMIT
    ''')

TEST_MOD = textwrap.dedent('''\
    import unittest
    from sqlglot.mod import helper


    class TestMod(unittest.TestCase):
        def setUp(self):
            self.v = 1

        def test_a(self):
            self.assertEqual(helper(1), 20)

        def test_b(self):
            self.assertTrue(True)

        def test_c(self):
            self.assertTrue(True)
    ''')
# line: 2 import helper | 7 self.v | 10 test_a body | 13 test_b body | 16 test_c body

# Imported by sqlglot/__init__.py, so `import sqlglot` loads it (rule g); mod.py is not.
EAGER = textwrap.dedent('''\
    FLAG = 1


    def use_flag():
        return FLAG
    ''')
# line: 1 FLAG | 4 def use_flag | 5 return FLAG

TEST_FIX = textwrap.dedent('''\
    import unittest
    from tests.helpers import load_sql_fixture_pairs


    class TestFix(unittest.TestCase):
        def test_fix(self):
            for _ in load_sql_fixture_pairs("optimizer/foo.sql"):
                pass

        def test_other(self):
            pass
    ''')

# ATTR_LEVEL fixture: class attributes and dict entries
KINDS = textwrap.dedent('''\
    class Base:
        NAME = "base"
        TABLE = {
            "a": 1,
            "b": 2,
        }


    class Sub(Base):
        OTHER = 1


    class Dep:
        T = {"x": 1}
        U = {**T}
    ''')
# line: 1 Base | 2 NAME | 3-6 TABLE | 9 Sub | 10 OTHER | 13 Dep | 14 T | 15 U

IDS = [
    "tests/test_mod.py::TestMod::test_a",     # 0
    "tests/test_mod.py::TestMod::test_b",     # 1
    "tests/test_fix.py::TestFix::test_fix",   # 2
    "tests/test_fix.py::TestFix::test_other", # 3
    "tests/test_mod.py::TestMod::test_c",     # 4  UNMAPPED (not in the map)
    "tests/test_base.py::TestB::test_flaky",  # 5  failed_at_baseline
    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",  # 6 UNMAPPED, rule g
]
A, B, FIX, OTHER, C, FLAKY, LAZY = range(7)

# The fake map. Map test index != tests.json number on purpose (order differs).
MAP_TESTS = [IDS[FLAKY], IDS[A], IDS[B], IDS[FIX], IDS[OTHER], "tests/gone.py::T::test_gone"]
a, b, fix, other, flaky = 1, 2, 3, 4, 0  # map indexes
FAKE_MAP = {
    "version": 1,
    "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
    "tests": MAP_TESTS,
    "sets": [[a], [a, b], [b], [other], [fix], [a, b, 5]],
    "lines": {
        "sqlglot/mod.py": {"5": 0, "6": 1, "17": 2},
        "sqlglot/user.py": {"5": 2},
        "sqlglot/eager.py": {"5": 0},
        "tests/test_mod.py": {"7": 1, "10": 0, "13": 2},
    },
    "import_lines": {"sqlglot/mod.py": [1, 4, 9, 13, 14, 16], "sqlglot/user.py": [1, 4], "sqlglot/eager.py": [1, 4]},
    "files_read": {"tests/fixtures/optimizer/bar.sql": 2},
    "class_users": {"sqlglot.mod:Thing": 3},
    "failed_at_baseline": [IDS[FLAKY]],
    "meta": {},
}


def patch_for(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
    ))


class RulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.src = root / "src"
        files = {
            "sqlglot/__init__.py": "from sqlglot import eager\n",
            "sqlglot/eager.py": EAGER,
            "sqlglot/mod.py": MOD,
            "sqlglot/user.py": USER,
            "sqlglot/kinds.py": KINDS,
            "tests/__init__.py": "",
            "tests/test_mod.py": TEST_MOD,
            "tests/test_fix.py": TEST_FIX,
        }
        for p, text in files.items():
            (cls.src / p).parent.mkdir(parents=True, exist_ok=True)
            (cls.src / p).write_text(text, "utf-8")
        cls.index = root / "index"
        cls.index.mkdir()
        build_index._write(cls.index / "name_index.json.gz", build_index.build(cls.src))
        build_index._write(cls.index / "test_strings.json.gz", TestIndex(cls.src)._build())
        (cls.index / "imports.json").write_text(json.dumps(imports.build(cls.src)), "utf-8")
        cls.import_mods = build_index.import_sqlglot_modules(cls.src)
        (cls.index / "import_sqlglot.json").write_text(json.dumps(cls.import_mods), "utf-8")
        files_of = {IDS[i]: IDS[i].split("::")[0] for i in range(len(IDS))}
        cls.tests = [{"number": i, "id": tid, "file": files_of[tid], "seconds": 1.0} for i, tid in enumerate(IDS)]
        cls.number_by_id = {t["id"]: t["number"] for t in cls.tests}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def ctx(self, func_level=True, raw=FAKE_MAP) -> sel.Ctx:
        # rule s (setUpClass stopgap) has its own tests below (SetupStopgapTest)
        return sel.Ctx(self.src, MapDB(raw, self.number_by_id), self.tests, func_level, self.index,
                       setup_stopgap=False)

    def run_sel(self, edits: dict[str, tuple[str, str]], func_level=True, raw=FAKE_MAP):
        """edits: path -> (old line, new line) replaced once, or a full new text for data files."""
        text = ""
        for path, (old, new) in edits.items():
            if path.endswith(".py"):
                orig = (self.src / path).read_text("utf-8")
                assert orig.count(old) == 1, old
                text += patch_for(path, orig, orig.replace(old, new))
            else:
                text += patch_for(path, old, new)
        verdicts = [prover.classify(fp, self.src) for fp in diff.parse(text)]
        return sel.select(self.ctx(func_level, raw), verdicts)

    def assertSelects(self, edits, expected, func_level=True, raw=FAKE_MAP):
        got, trail = self.run_sel(edits, func_level, raw)
        self.assertEqual(sorted(got), sorted(expected), trail)
        return trail

    # -- mapdb ------------------------------------------------------------------------
    def test_mapdb_ids_to_numbers(self):
        db = MapDB(FAKE_MAP, self.number_by_id)
        self.assertEqual(db.tests_of_set(5), {A, B})  # map index 5 is unknown to tests.json
        self.assertEqual(db.unmapped, {C, LAZY})
        self.assertEqual(db.failed_at_baseline, {FLAKY})

    # -- a) statement lines -> map "lines" (+ f: baseline, unmapped in a selected file) ----
    def test_a_statement_lines(self):
        trail = self.assertSelects(
            {"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 2\n")}, {A, C, FLAKY}, func_level=False
        )
        self.assertIn("a:L5", trail)
        self.assertIn("f:+2", trail)

    # -- c) FUNC_LEVEL ------------------------------------------------------------------
    def test_c_func_level_widens_to_the_def(self):
        self.assertSelects({"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 2\n")}, {A, B, C, FLAKY})

    def test_c_hybrid_line_level_when_the_statement_ran(self):
        self.assertSelects({"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 2\n")}, {A, C, FLAKY}, func_level=2)

    def test_c_hybrid_widens_when_the_scope_changes(self):
        # LIMIT becomes a local of helper: line 6 now raises UnboundLocalError
        trail = self.assertSelects(
            {"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 1\n    LIMIT = 2\n")}, {A, B, C, FLAKY}, func_level=2
        )
        self.assertIn("c:scope changed", trail)

    def test_x_broken_module_selects_its_importers(self):
        old = prover.SYNTAX_LAZY
        prover.SYNTAX_LAZY = True
        try:
            got, trail = self.run_sel({"sqlglot/mod.py": ("    y = x + 1\n", "    y = x +\n")})
        finally:
            prover.SYNTAX_LAZY = old
        self.assertIn("x:closure", trail)
        self.assertTrue({A, B, C} <= got, trail)  # tests/test_mod.py imports sqlglot.mod

    def test_x_broken_module_loaded_by_import_sqlglot_is_full(self):
        old = prover.SYNTAX_LAZY
        prover.SYNTAX_LAZY = True
        try:
            with self.assertRaises(sel.Full):
                self.run_sel({"sqlglot/eager.py": ("FLAG = 1\n", "FLAG = \n")})
        finally:
            prover.SYNTAX_LAZY = old

    def test_c_dead_line_alone_is_full(self):
        for fl in (False, True):
            with self.assertRaises(sel.Full):
                self.run_sel({"sqlglot/mod.py": ("    return 0\n", "    return 1\n")}, func_level=fl)

    def test_c_dead_line_contributes_nothing(self):
        orig = (self.src / "sqlglot/mod.py").read_text("utf-8")
        new = orig.replace("    y = x + 1\n", "    y = x + 2\n").replace("    return 0\n", "    return 1\n")
        verdicts = [prover.classify(fp, self.src) for fp in diff.parse(patch_for("sqlglot/mod.py", orig, new))]
        got, trail = sel.select(self.ctx(False), verdicts)
        self.assertEqual(got, {A, C, FLAKY})
        self.assertIn("DEAD L10", trail)

    # -- b) import time -----------------------------------------------------------------
    def test_b_class_body_uses_class_users(self):
        trail = self.assertSelects({"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')}, {OTHER, FLAKY})
        self.assertIn("b:class Thing", trail)

    def test_b_def_default_uses_class_users_and_the_def(self):
        self.assertSelects(
            {"sqlglot/mod.py": ("    def run(self, z=3):\n", "    def run(self, z=4):\n")}, {B, OTHER, C, FLAKY}
        )

    def test_b_module_level_name_follows_its_users(self):
        # LIMIT is used by helper() (tests a, b) and by user.use() (test b)
        trail = self.assertSelects({"sqlglot/mod.py": ("LIMIT = 10\n", "LIMIT = 11\n")}, {A, B, C, FLAKY})
        self.assertIn("b:name LIMIT", trail)

    def test_b_new_module_statement_without_names_is_full(self):
        with self.assertRaises(sel.Full):
            self.run_sel({"sqlglot/mod.py": ("LIMIT = 10\n", "LIMIT = 10\nprint('import-time')\n")})

    def test_b_import_only_file_is_mapped(self):
        # a module whose code only runs at import has import_lines but no "lines" (tsql.py)
        raw = dict(FAKE_MAP, lines={k: v for k, v in FAKE_MAP["lines"].items() if k != "sqlglot/mod.py"})
        trail = self.assertSelects(
            {"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')}, {OTHER, FLAKY}, raw=raw
        )
        self.assertIn("b:class Thing", trail)

    def test_b_class_without_class_users_is_full(self):
        raw = dict(FAKE_MAP, class_users={})
        with self.assertRaises(sel.Full):
            self.run_sel({"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')}, raw=raw)

    # -- g) `import sqlglot` in a subprocess ----------------------------------------------------
    def test_g_probe_records_what_import_sqlglot_loads(self):
        self.assertEqual(self.import_mods, ["sqlglot/__init__.py", "sqlglot/eager.py"])

    def test_g_import_time_change_in_loaded_module_adds_lazy_load(self):
        trail = self.assertSelects({"sqlglot/eager.py": ("FLAG = 1\n", "FLAG = 2\n")}, {A, C, FLAKY, LAZY})
        self.assertIn("g:import sqlglot+1", trail)

    def test_g_new_module_statement_in_loaded_module_adds_lazy_load(self):
        self.assertSelects(
            {"sqlglot/eager.py": ("FLAG = 1\n", "FLAG = 1\nOTHER_FLAG = FLAG\n")}, {A, C, FLAKY, LAZY}
        )

    def test_g_def_body_change_does_not(self):
        self.assertSelects({"sqlglot/eager.py": ("    return FLAG\n", "    return FLAG + 1\n")}, {A, C, FLAKY})

    def test_g_module_not_loaded_by_import_sqlglot_does_not(self):
        self.assertSelects({"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')}, {OTHER, FLAKY})

    def test_g_hand_list_is_used_even_if_the_probe_missed_a_module(self):
        ctx = self.ctx()
        self.assertFalse(ctx.loaded_by_import_sqlglot("sqlglot/mod.py"))
        for path in ("sqlglot/parser.py", "sqlglot/expressions/core.py", "sqlglot/eager.py",
                     "sqlglot/optimizer/scope.py", "sqlglot/_typing.py"):
            self.assertTrue(ctx.loaded_by_import_sqlglot(path), path)
        self.assertEqual(ctx.subprocess_import_tests, {LAZY})

    def test_g_missing_probe_record_is_full_for_import_time_changes(self):
        rec = self.index / "import_sqlglot.json"
        saved = rec.read_text("utf-8")
        rec.unlink()
        try:
            with self.assertRaises(sel.Full):
                self.run_sel({"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')})
            self.assertSelects({"sqlglot/eager.py": ("    return FLAG\n", "    return FLAG + 1\n")},
                               {A, C, FLAKY})  # runtime-only changes do not need it
        finally:
            rec.write_text(saved, "utf-8")

    # -- SAFE_GROUPS -------------------------------------------------------------------------
    @contextmanager
    def safe_groups(self, value: str):
        saved = safe.GROUPS
        safe.GROUPS = safe.parse(value)
        try:
            yield
        finally:
            safe.GROUPS = saved

    def assertFullOnlyWith(self, group: str, edits, others: str = "ABCDE"):
        """FULL with `group` listed; unchanged with every other group listed."""
        base, _ = self.run_sel(edits)
        with self.safe_groups(",".join(g for g in others if g != group)):
            self.assertEqual(self.run_sel(edits)[0], base)
        with self.safe_groups(group.lower() + " , "):
            with self.assertRaises(sel.Full) as cm:
                self.run_sel(edits)
        self.assertIn(f"SAFE {group}", str(cm.exception))

    def test_safe_parse(self):
        self.assertEqual(safe.parse(" b, d ,,"), {"B", "D"})
        self.assertEqual(safe.parse(""), frozenset())
        self.assertEqual(safe.parse(None), frozenset())

    def test_safe_b_class_users(self):
        self.assertFullOnlyWith("B", {"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')})

    def test_safe_b_name_chain(self):
        self.assertFullOnlyWith("B", {"sqlglot/mod.py": ("LIMIT = 10\n", "LIMIT = 11\n")})

    def test_safe_c_runtime_line(self):
        self.assertFullOnlyWith("C", {"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 2\n")})

    def test_safe_d_fixture(self):
        self.assertFullOnlyWith("D", {"tests/fixtures/optimizer/bar.sql": ("SELECT 1;\n", "SELECT 2;\n")})

    def test_safe_e_dead_line(self):
        # a live line (helper) and a dead one (unused) in one file: DEAD contributes nothing.
        # C would also turn this FULL (runtime lines), so the others are checked without C.
        orig = (self.src / "sqlglot/mod.py").read_text("utf-8")
        new = orig.replace("    y = x + 1\n", "    y = x + 2\n").replace("    return 0\n", "    return 1\n")
        text = patch_for("sqlglot/mod.py", orig, new)

        def run():
            return sel.select(self.ctx(), [prover.classify(fp, self.src) for fp in diff.parse(text)])

        base, trail = run()
        self.assertIn("DEAD L10", trail)
        with self.safe_groups("A,B,D"):
            self.assertEqual(run()[0], base)
        with self.safe_groups("E"):
            with self.assertRaises(sel.Full) as cm:
                run()
        self.assertIn("SAFE E", str(cm.exception))

    def test_safe_test_function_edit_is_never_widened(self):
        edits = {"tests/test_mod.py": ("        self.assertTrue(True)\n\n    def test_c",
                                       "        self.assertTrue(False)\n\n    def test_c")}
        base, _ = self.run_sel(edits)
        with self.safe_groups("A,B,C,D,E"):
            self.assertEqual(self.run_sel(edits)[0], base)

    # -- d) tests/*.py --------------------------------------------------------------------
    def test_d_inside_a_test_selects_that_test(self):
        trail = self.assertSelects(
            {"tests/test_mod.py": ("        self.assertTrue(True)\n\n    def test_c",
                                   "        self.assertTrue(False)\n\n    def test_c")},
            {B, C, FLAKY},
        )
        self.assertIn("d:test_b", trail)

    def test_d_helper_selects_the_whole_file(self):
        self.assertSelects({"tests/test_mod.py": ("        self.v = 1\n", "        self.v = 2\n")}, {A, B, C, FLAKY})

    def test_d_module_level_selects_the_whole_file(self):
        self.assertSelects(
            {"tests/test_mod.py": ("from sqlglot.mod import helper\n", "from sqlglot.mod import helper as helper\n")},
            {A, B, C, FLAKY},
        )

    # -- e) data files ------------------------------------------------------------------
    def test_e_files_read(self):
        trail = self.assertSelects({"tests/fixtures/optimizer/bar.sql": ("SELECT 1;\n", "SELECT 2;\n")}, {B, C, FLAKY})
        self.assertIn("e:files_read", trail)

    def test_e_fixture_name_fallback(self):
        trail = self.assertSelects({"tests/fixtures/optimizer/foo.sql": ("SELECT 1;\n", "SELECT 2;\n")}, {FIX, FLAKY})
        self.assertIn("e:fixture-name", trail)

    def test_e_unknown_data_is_full(self):
        with self.assertRaises(sel.Full):
            self.run_sel({"tests/fixtures/optimizer/baz.sql": ("SELECT 1;\n", "SELECT 2;\n")})

    # -- the stub: every CHANGED file -> FULL -------------------------------------------
    def test_stub_map_is_full_for_code(self):
        stub = {k: ([] if isinstance(v, list) else {} if isinstance(v, dict) else v) for k, v in FAKE_MAP.items()}
        with self.assertRaises(sel.Full):
            self.run_sel({"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 2\n")}, raw=stub)


if __name__ == "__main__":
    unittest.main()


# -- rule s: setUpClass stopgap ----------------------------------------------------------------
EX = "tests/test_executor.py::TestExecutor::"
OPT = "tests/test_optimizer.py::TestOptimizer::"
S_IDS = [  # tests.json order = collection order
    EX + "test_agg_order",      # 0  first of TestExecutor: setUpClass runs inside it
    EX + "test_execute_tpcds",  # 1
    EX + "test_execute_tpch",   # 2
    EX + "test_scalar",         # 3
    OPT + "test_simplify",      # 4  first of TestOptimizer
    OPT + "test_tpch",          # 5
    "tests/test_mod.py::TestMod::test_a",  # 6 first of TestMod
    "tests/test_mod.py::TestMod::test_b",  # 7
]
AGG, TPCDS, TPCH_EX, SCALAR, SIMPLIFY, TPCH_OPT, MA, MB = range(8)
S_MAP = {
    "version": 1, "tests": S_IDS,
    "sets": [[AGG, TPCH_OPT], [AGG], [AGG, SCALAR], [MA], [SIMPLIFY, TPCH_OPT], [TPCH_OPT]],
    "lines": {"sqlglot/mod.py": {"5": 1, "6": 2, "17": 3}, "sqlglot/user.py": {"5": 4}},
    "import_lines": {"sqlglot/mod.py": [1, 4, 9, 13, 14, 16], "sqlglot/user.py": [1, 4]},
    "files_read": {"tests/fixtures/optimizer/tpc-h/tpc-h.sql": 0,
                   "tests/fixtures/optimizer/simplify.sql": 4,
                   "tests/fixtures/optimizer/tpc-h/other.sql": 5},
    "class_users": {}, "failed_at_baseline": [], "meta": {},
}
EXECUTOR = {AGG, TPCDS, TPCH_EX, SCALAR}


class SetupStopgapTest(RulesTest):
    """Rule s against an executor-shaped fake map (same source tree as RulesTest)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tests = [{"number": i, "id": tid, "file": tid.split("::")[0], "seconds": 1.0}
                     for i, tid in enumerate(S_IDS)]
        cls.number_by_id = {t["id"]: t["number"] for t in cls.tests}

    def ctx(self, func_level=True, raw=S_MAP) -> sel.Ctx:
        return sel.Ctx(self.src, MapDB(raw, self.number_by_id), self.tests, func_level, self.index)

    def assertSelects(self, edits, expected, func_level=True, raw=S_MAP):
        return super().assertSelects(edits, expected, func_level, raw)

    def run_sel(self, edits, func_level=True, raw=S_MAP):
        return super().run_sel(edits, func_level, raw)

    def test_s_tpch_fixture_reaches_test_execute_tpch(self):
        # tpc-h.sql is read in TestExecutor.setUpClass: the map credits test_agg_order only
        got, trail = self.run_sel({"tests/fixtures/optimizer/tpc-h/tpc-h.sql": ("SELECT 1;\n", "SELECT 2;\n")})
        self.assertIn(TPCH_EX, got)
        self.assertEqual(got, EXECUTOR | {TPCH_OPT}, trail)
        self.assertIn("s:+3", trail)

    def test_s_files_read_expands_even_when_class_mates_also_read(self):
        # simplify.sql: first of TestOptimizer + test_tpch -> still the whole class (files: always)
        got, _ = self.run_sel({"tests/fixtures/optimizer/simplify.sql": ("a\n", "b\n")})
        self.assertEqual(got, {SIMPLIFY, TPCH_OPT})

    def test_s_files_read_without_a_first_test_is_unchanged(self):
        got, trail = self.run_sel({"tests/fixtures/optimizer/tpc-h/other.sql": ("a\n", "b\n")})
        self.assertEqual(got, {TPCH_OPT})
        self.assertNotIn("s:", trail)

    def test_s_line_credited_to_first_test_alone_expands(self):
        got, trail = self.run_sel({"sqlglot/mod.py": ("    y = x + 1\n", "    y = x + 2\n")}, func_level=False)
        self.assertEqual(got, EXECUTOR, trail)
        self.assertIn("s:a:L5", trail)

    def test_s_line_shared_with_a_class_mate_does_not_expand(self):
        # line 6 ran in test_agg_order AND test_scalar: not setUpClass-only evidence
        got, trail = self.run_sel(
            {"sqlglot/mod.py": ("    return y * LIMIT\n", "    return y * LIMIT * 2\n")}, func_level=False
        )
        self.assertEqual(got, {AGG, SCALAR}, trail)

    def test_s_env_flag_off_disables_the_rule(self):
        saved = sel.SETUP_STOPGAP
        try:
            sel.SETUP_STOPGAP = False
            self.assertEqual(self.ctx().group_of_first, {})
            got, trail = self.run_sel({"tests/fixtures/optimizer/tpc-h/tpc-h.sql": ("SELECT 1;\n", "SELECT 2;\n")})
            self.assertEqual(got, {AGG, TPCH_OPT})
            self.assertNotIn("s:", trail)
        finally:
            sel.SETUP_STOPGAP = saved

    def test_s_single_test_class_is_not_a_group(self):
        ctx = self.ctx()
        self.assertEqual(set(ctx.group_of_first), {AGG, SIMPLIFY, MA})
        self.assertEqual(ctx.group_of_first[AGG], frozenset(EXECUTOR))

    # RulesTest's own cases assume the stopgap off; do not rerun them here
    for _name in [n for n in dir(RulesTest) if n.startswith("test_")]:
        locals()[_name] = None
    del _name


_K = "sqlglot.kinds:"
ATTR_MAP = {
    **FAKE_MAP,
    "sets": FAKE_MAP["sets"] + [[a, b, fix, other]],  # 6: class_users of every kinds.py class
    "import_lines": {**FAKE_MAP["import_lines"], "sqlglot/kinds.py": [1, 2, 3, 4, 5, 6, 9, 10, 13, 14, 15]},
    "class_users": {**FAKE_MAP["class_users"], _K + "Base": 6, _K + "Sub": 6, _K + "Dep": 6},
    "attr_users": {_K + "Base.NAME": 0, _K + "Sub.NAME": 2, _K + "Dep.U": 3},
    "key_users": {_K + "Base.TABLE['a']": 0, _K + "Sub.TABLE['a']": 4, _K + "Base.TABLE[*]": 2,
                  _K + "Base.TABLE['b']": 3, _K + "Dep.T['x']": 0},
    "attr_import": [],
    "key_import": [],
}
CLASS_USERS = {A, B, FIX, OTHER, FLAKY, C}  # set 6 + rule f (baseline FLAKY, unmapped C in test_mod.py)


class AttrLevelTest(RulesTest):
    """ATTR_LEVEL (rule b') against a hand-made map v4."""

    def setUp(self):
        self._saved = sel.ATTR_LEVEL
        sel.ATTR_LEVEL = True

    def tearDown(self):
        sel.ATTR_LEVEL = self._saved

    def sel_kinds(self, old, new, raw=ATTR_MAP):
        return self.run_sel({"sqlglot/kinds.py": (old, new)}, raw=raw)

    def test_dict_entry_selects_key_users_of_class_and_subclass_plus_star(self):
        got, trail = self.sel_kinds('        "a": 1,\n', '        "a": 9,\n')
        self.assertEqual(got, {A, FIX, B, FLAKY, C}, trail)
        self.assertIn("A:Base", trail)

    def test_new_key_selects_star(self):
        got, trail = self.sel_kinds('        "b": 2,\n', '        "b": 2,\n        "z": 5,\n')
        self.assertEqual(got, {B, FLAKY, C}, trail)

    def test_scalar_attr_selects_attr_users_of_class_and_subclass(self):
        got, trail = self.sel_kinds('    NAME = "base"\n', '    NAME = "other"\n')
        self.assertEqual(got, {A, B, FLAKY, C}, trail)

    def test_dependent_attr_changes_as_a_whole(self):
        got, trail = self.sel_kinds('    T = {"x": 1}\n', '    T = {"x": 2}\n')
        self.assertEqual(got, {A, OTHER, FLAKY, C}, trail)

    def test_key_read_at_import_falls_back(self):
        raw = {**ATTR_MAP, "key_import": [_K + "Base.TABLE['a']"]}
        got, trail = self.sel_kinds('        "a": 1,\n', '        "a": 9,\n', raw)
        self.assertEqual(got, CLASS_USERS, trail)
        self.assertIn("fallback", trail)

    def test_attr_read_at_import_in_a_subclass_falls_back(self):
        raw = {**ATTR_MAP, "attr_import": [_K + "Sub.TABLE"]}
        got, trail = self.sel_kinds('        "a": 1,\n', '        "a": 9,\n', raw)
        self.assertEqual(got, CLASS_USERS, trail)

    def test_ambiguous_key_falls_back(self):
        got, trail = self.sel_kinds('        "b": 2,\n', '        key_var: 2,\n')
        self.assertEqual(got, CLASS_USERS, trail)

    def test_attr_unknown_to_the_map_falls_back(self):
        got, trail = self.sel_kinds("    OTHER = 1\n", "    OTHER = 2\n")
        self.assertEqual(got, CLASS_USERS, trail)

    def test_non_attribute_statement_falls_back(self):
        got, trail = self.sel_kinds("    U = {**T}\n", "    U = {**T}\n    print(U)\n")
        self.assertEqual(got, CLASS_USERS, trail)

    def test_old_map_without_attr_keys_is_unchanged(self):
        raw = {k: v for k, v in ATTR_MAP.items()
               if k not in ("attr_users", "key_users", "attr_import", "key_import")}
        got, trail = self.sel_kinds('        "a": 1,\n', '        "a": 9,\n', raw)
        self.assertEqual(got, CLASS_USERS, trail)

    def test_flag_off_is_unchanged(self):
        sel.ATTR_LEVEL = False
        got, trail = self.sel_kinds('        "a": 1,\n', '        "a": 9,\n')
        self.assertEqual(got, CLASS_USERS, trail)

    def test_key_spellings(self):
        import ast as _ast

        from blast import attrs

        def sp(src, al=None):
            return attrs.key_spellings(_ast.parse(src, mode="eval").body, al or {})

        self.assertIn("Alias", sp("exp.Alias"))
        self.assertIn("DataType.Type.INT", sp("exp.DataType.Type.INT"))
        self.assertIn("TokenType.SELECT", sp("TT.SELECT", {"TT": "TokenType"}))
        self.assertEqual(sp("'x'"), {"'x'"})
        self.assertIsNone(sp("exp.SOME_CONST"))
        self.assertIsNone(sp("name"))
        self.assertIsNone(sp("f(1)"))

    # RulesTest's own cases run in RulesTest; do not rerun them here
    for _name in [n for n in dir(RulesTest) if n.startswith("test_")]:
        locals()[_name] = None
    del _name
