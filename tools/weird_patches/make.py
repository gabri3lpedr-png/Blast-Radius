#!/usr/bin/env python3
"""Regenerate the hand-made unusual patches in tools/weird_patches/*.patch.

Usage: python3 tools/weird_patches/make.py [sqlglot_src]   (default: .cache/sqlglot)

Each case edits a scratch git copy of the pinned sqlglot source and saves `git diff`
(some are then post-processed: CRLF line endings, no trailing newline). The expected
outcome of every case is in expected.json; blast/tests/test_weird.py checks it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

EJ = "sqlglot/optimizer/eliminate_joins.py"
EJ_OLD = '                and not join.args.get("on")\n'
EJ_NEW = '                or not join.args.get("on")\n'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="surrogateescape").stdout


def edit(repo: Path, path: str, old: str, new: str) -> None:
    p = repo / path
    text = p.read_text("utf-8")
    assert text.count(old) == 1, (path, old)
    p.write_text(text.replace(old, new, 1), "utf-8")


def case_new_py(r):
    (r / "sqlglot/dialects/newdialect.py").write_text(
        "from sqlglot.dialects.dialect import Dialect\n\n\nclass NewDialect(Dialect):\n    pass\n")


def case_deleted(r):
    (r / "sqlglot/dialects/dune.py").unlink()


def case_renamed(r):
    (r / "sqlglot/serde.py").rename(r / "sqlglot/serde2.py")


def case_binary(r):
    (r / "sqlglot.png").write_bytes((r / "sqlglot.png").read_bytes() + b"\x00\x01binary")


def case_binary_fixture(r):
    p = r / "tests/fixtures/optimizer/tpc-h/region.csv.gz"
    p.write_bytes(p.read_bytes() + b"\x00\xff")


def case_fixture_tpch(r):
    # read in TestExecutor.setUpClass: the map credits test_agg_order only (rule s)
    p = r / "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
    text = p.read_text("utf-8")
    old = "        l_returnflag,\n"
    assert old in text
    p.write_text(text.replace(old, "        l_returnflag + 1,\n", 1), "utf-8")


def case_test_function(r):
    edit(r, "tests/test_transpile.py", '        self.assertEqual(transpile("0Êß")[0], "0 AS Êß")\n',
         '        self.assertEqual(transpile("0Êß")[0], "0 AS Êßx")\n')


def case_test_helper_module(r):
    edit(r, "tests/helpers.py", 'FIXTURES_DIR = os.path.join(FILE_DIR, "fixtures")\n',
         'FIXTURES_DIR = os.path.join(FILE_DIR, "fixtures2")\n')


def case_docstring_only(r):
    edit(r, EJ, '    """Determine output columns of `scope` that must have a unique combination per row"""\n',
         '    """Determine the output columns of `scope` that are unique per row"""\n')


def case_syntax_error(r):
    edit(r, EJ, EJ_OLD, '                and not join.args.get("on"\n')


def case_readme(r):
    p = r / "README.md"
    p.write_text(p.read_text("utf-8") + "\nOne more line of docs.\n", "utf-8")


def case_pyproject(r):
    p = r / "pyproject.toml"
    p.write_text(p.read_text("utf-8") + "\n# tweak\n", "utf-8")


def case_fixture_unread(r):
    p = r / "tests/fixtures/jsonpath/LICENSE"
    p.write_bytes(p.read_bytes() + b"extra line\r\n")


def case_crlf(r):
    edit(r, EJ, EJ_OLD, EJ_NEW)


def case_no_trailing_newline(r):
    edit(r, EJ, EJ_OLD, EJ_NEW)


def case_no_newline_at_eof(r):
    p = r / "sqlglot/serde.py"
    text = p.read_text("utf-8")
    assert text.endswith("    return expression\n")
    p.write_text(text[:-1], "utf-8")  # same code, "\ No newline at end of file"


def case_five_files(r):
    edit(r, EJ, EJ_OLD, EJ_NEW)
    edit(r, "sqlglot/serde.py", "            if node.type and node.type is not node:\n",
         "            if node.type or node.type is not node:\n")
    edit(r, "sqlglot/transforms.py", "from __future__ import annotations\n",
         "from __future__ import annotations\n# a comment\n")
    p = r / "tests/fixtures/optimizer/simplify.sql"
    p.write_text(p.read_text("utf-8") + "\nx;\nx;\n", "utf-8")
    edit(r, "tests/test_transpile.py", '        self.assertEqual(transpile("0Êß")[0], "0 AS Êß")\n',
         '        self.assertEqual(transpile("0Êß")[0], "0 AS Êßx")\n')


def case_mode_change(r):
    (r / EJ).chmod(0o755)


CASES = {name[5:]: fn for name, fn in globals().items() if name.startswith("case_")}


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / ".cache" / "sqlglot")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        shutil.copytree(src, repo)
        git(repo, "init", "-q")
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "add", "-A")
        git(repo, "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "base")
        for name, fn in CASES.items():
            fn(repo)
            git(repo, "add", "-A")
            out = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "-M", "--binary"],
                                 check=True, capture_output=True).stdout
            git(repo, "reset", "-q", "--hard")
            git(repo, "clean", "-qfd")
            if name == "crlf":
                out = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if name == "no_trailing_newline":
                out = out.rstrip(b"\n")
            (HERE / f"{name}.patch").write_bytes(out)
            print(f"{name}.patch  {len(out)} bytes")
    good = (HERE / "crlf.patch").read_bytes().replace(b"\r\n", b"\n")
    static = {
        "empty": b"",
        "garbage": b"this is not a diff\nat all\n",
        "bad_hunk_count": good.replace(b"@@ -68,7 +68,7 @@", b"@@ -68,9 +68,7 @@"),
    }
    for name, out in static.items():
        (HERE / f"{name}.patch").write_bytes(out)
        print(f"{name}.patch  {len(out)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
