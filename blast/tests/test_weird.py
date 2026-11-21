"""Unusual patches: the Prover's verdicts on small cases, and tools/weird_patches/ end to end.

Run: make unit. The end-to-end test needs the pinned source in .cache/sqlglot (tools/eval_sample.py
downloads it) and is skipped without it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from blast import diff, prover
from blast.tests.test_select import patch_for

ROOT = Path(__file__).resolve().parent.parent.parent
WEIRD = ROOT / "tools" / "weird_patches"

CODE = textwrap.dedent('''\
    """Module doc."""


    def f(x):
        """Function doc."""
        return x + 1


    class K:
        """Class doc."""

        A = 1
    ''')


class ProverEdgeCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.src = Path(cls._tmp.name)
        (cls.src / "sqlglot").mkdir()
        (cls.src / "sqlglot/m.py").write_text(CODE, "utf-8")
        (cls.src / "README.md").write_text("# readme\n", "utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def verdicts(self, text: str) -> list[prover.FileVerdict]:
        patches = diff.parse(text)
        self.assertTrue(patches, getattr(patches, "reason", ""))
        return [prover.classify(fp, self.src) for fp in patches]

    def kind_of_edit(self, old: str, new: str) -> str:
        assert CODE.count(old) == 1
        [v] = self.verdicts(patch_for("sqlglot/m.py", CODE, CODE.replace(old, new)))
        return v.kind

    # -- docstrings ---------------------------------------------------------------------
    def test_docstring_edits_are_noop(self):
        for old, new in [('"""Module doc."""', '"""Other."""'), ('"""Function doc."""', '"""Other."""'),
                         ('"""Class doc."""', '"""Other\n    doc."""')]:
            self.assertEqual(self.kind_of_edit(old, new), prover.NOOP, old)

    def test_docstring_plus_code_is_changed(self):
        new = CODE.replace('"""Function doc."""', '"""Other."""').replace("x + 1", "x + 2")
        [v] = self.verdicts(patch_for("sqlglot/m.py", CODE, new))
        self.assertEqual(v.kind, prover.CHANGED)

    def test_string_that_is_not_a_docstring_is_changed(self):
        self.assertEqual(self.kind_of_edit("    A = 1\n", '    A = 1\n    "not a docstring"\n'), prover.CHANGED)

    # -- documentation files --------------------------------------------------------------
    def test_readme_edit_new_and_deleted_doc_are_noop(self):
        texts = [
            patch_for("README.md", "# readme\n", "# readme!\n"),
            "diff --git a/posts/new.md b/posts/new.md\nnew file mode 100644\n--- /dev/null\n"
            "+++ b/posts/new.md\n@@ -0,0 +1 @@\n+hi\n",
            "diff --git a/LICENSE b/LICENSE\ndeleted file mode 100644\n--- a/LICENSE\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-MIT\n",
            "diff --git a/logo.png b/logo.png\nindex 1..2 100644\nBinary files a/logo.png and b/logo.png differ\n",
        ]
        for t in texts:
            [v] = self.verdicts(t)
            self.assertEqual(v.kind, prover.NOOP, t)

    def test_doc_names_inside_the_package_or_tests_are_not_docs(self):
        for path in ("sqlglot/notes.md", "tests/fixtures/jsonpath/LICENSE", ".github/README.md"):
            [v] = self.verdicts(patch_for(path, "a\n", "b\n"))
            self.assertNotEqual(v.kind, prover.NOOP, path)

    def test_rename_from_doc_into_package_is_full(self):
        [v] = self.verdicts("diff --git a/notes.md b/sqlglot/notes.py\nsimilarity index 100%\n"
                            "rename from notes.md\nrename to sqlglot/notes.py\n")
        self.assertEqual(v.kind, prover.FULL)

    # -- mode changes ---------------------------------------------------------------------
    def test_plain_mode_change_is_noop(self):
        [v] = self.verdicts("diff --git a/sqlglot/m.py b/sqlglot/m.py\nold mode 100644\nnew mode 100755\n")
        self.assertEqual(v.kind, prover.NOOP)

    def test_symlink_mode_change_is_full(self):
        [v] = self.verdicts("diff --git a/sqlglot/m.py b/sqlglot/m.py\nold mode 100644\nnew mode 120000\n")
        self.assertEqual(v.kind, prover.FULL)

    # -- line endings -----------------------------------------------------------------------
    def test_crlf_patch_and_missing_trailing_newline(self):
        text = patch_for("sqlglot/m.py", CODE, CODE.replace("x + 1", "x + 2"))
        for t in (text.replace("\n", "\r\n"), text.rstrip("\n")):
            [v] = self.verdicts(t)
            self.assertEqual((v.kind, v.lines), (prover.CHANGED, [6]))


@unittest.skipUnless((ROOT / ".cache" / "sqlglot" / "sqlglot").is_dir(), "needs .cache/sqlglot")
class WeirdPatchesEndToEnd(unittest.TestCase):
    def test_every_case_is_valid_json_and_as_expected(self):
        sys.path.insert(0, str(WEIRD))
        import run  # tools/weird_patches/run.py

        code, _, err, selected, problems = run.run()
        self.assertEqual(code, 0, err[-2000:])
        self.assertEqual(problems, [])
        expected = json.loads((WEIRD / "expected.json").read_text("utf-8"))
        self.assertEqual(set(selected), set(expected))
        reasons = {}
        for line in err.splitlines():
            commit, _, rest = line.partition(" ")
            if commit in expected:
                reasons.setdefault(commit, rest)
        got = {c: run.outcome(reasons.get(c, "?")) for c in expected}
        self.assertEqual(got, expected)
        self.assertEqual(selected["test_function"], selected["test_function"] & selected["five_files"])
        self.assertEqual(selected["docstring_only"], set())


if __name__ == "__main__":
    unittest.main()
