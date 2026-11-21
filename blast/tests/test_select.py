"""Rules a-f of blast/select.py against a tiny hand-written source tree and coverage map.

Run: python3 -m unittest discover -s blast/tests -t .     (or: make unit)
"""

from __future__ import annotations

import difflib
import tempfile
import textwrap
import unittest
from pathlib import Path

from blast import build_index, diff, prover
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

IDS = [
    "tests/test_mod.py::TestMod::test_a",     # 0
    "tests/test_mod.py::TestMod::test_b",     # 1
    "tests/test_fix.py::TestFix::test_fix",   # 2
    "tests/test_fix.py::TestFix::test_other", # 3
    "tests/test_mod.py::TestMod::test_c",     # 4  UNMAPPED (not in the map)
    "tests/test_base.py::TestB::test_flaky",  # 5  failed_at_baseline
]
A, B, FIX, OTHER, C, FLAKY = range(6)

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
        "tests/test_mod.py": {"7": 1, "10": 0, "13": 2},
    },
    "import_lines": {"sqlglot/mod.py": [1, 4, 9, 13, 14, 16], "sqlglot/user.py": [1, 4]},
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
            "sqlglot/__init__.py": "",
            "sqlglot/mod.py": MOD,
            "sqlglot/user.py": USER,
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
        files_of = {IDS[i]: IDS[i].split("::")[0] for i in range(len(IDS))}
        cls.tests = [{"number": i, "id": tid, "file": files_of[tid], "seconds": 1.0} for i, tid in enumerate(IDS)]
        cls.number_by_id = {t["id"]: t["number"] for t in cls.tests}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def ctx(self, func_level=True, raw=FAKE_MAP) -> sel.Ctx:
        return sel.Ctx(self.src, MapDB(raw, self.number_by_id), self.tests, func_level, self.index)

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
        self.assertEqual(db.unmapped, {C})
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

    def test_b_class_without_class_users_is_full(self):
        raw = dict(FAKE_MAP, class_users={})
        with self.assertRaises(sel.Full):
            self.run_sel({"sqlglot/mod.py": ('    KIND = "a"\n', '    KIND = "b"\n')}, raw=raw)

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
