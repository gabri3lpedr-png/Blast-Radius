#!/usr/bin/env python3
"""Run selector.py on tools/weird_patches/*.patch and check its stdout.

Usage: python3 tools/weird_patches/run.py

Builds a temp DATA_DIR (these patches + sample/tests.json), runs the selector on the host the
way tools/eval_sample.py does, then checks: exit 0, stdout is only valid JSON lines, one per
patch, every test number in tests.json, and each case's outcome (FULL / NOOP / MAP) matches
expected.json. Prints what each case selected and why (the selector's stderr log).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from eval_sample import index_dir, sqlglot_src  # noqa: E402


def run() -> tuple[int, str, str, dict[str, set[int]], list[str]]:
    """(returncode, stdout, stderr, selected per case, problems with stdout)."""
    src = sqlglot_src()
    tests = json.loads((ROOT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    numbers = {t["number"] for t in tests}
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / "commits").mkdir()
        shutil.copy(ROOT / "sample" / "tests.json", data / "tests.json")
        for p in HERE.glob("*.patch"):
            shutil.copy(p, data / "commits" / p.name)
        env = dict(os.environ, DATA_DIR=str(data), SQLGLOT_SRC=str(src), INDEX_DIR=str(index_dir(src)))
        proc = subprocess.run([sys.executable, str(ROOT / "selector.py")], env=env, capture_output=True)
    out, err = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    problems: list[str] = []
    selected: dict[str, set[int]] = {}
    if not out.endswith("\n"):
        problems.append("stdout does not end with a newline")
    for i, line in enumerate(out.splitlines(), 1):
        try:
            rec = json.loads(line)
            commit, workers = rec["commit"], rec["workers"]
            assert set(rec) == {"commit", "workers"}, f"keys {sorted(rec)}"
            assert isinstance(workers, list) and all(isinstance(w, list) for w in workers)
            sel = [n for w in workers for n in w]
            assert all(isinstance(n, int) and n in numbers for n in sel), "unknown test number"
            assert len(sel) == len(set(sel)), "test in two workers"
            assert commit not in selected, "commit twice"
            selected[commit] = set(sel)
        except Exception as e:  # noqa: BLE001
            problems.append(f"stdout line {i}: {type(e).__name__}: {e}: {line[:120]!r}")
    expected = set(p.stem for p in HERE.glob("*.patch"))
    if set(selected) != expected:
        problems.append(f"commits answered {sorted(selected)} != patches {sorted(expected)}")
    return proc.returncode, out, err, selected, problems


def outcome(reason: str) -> str:
    return reason.split(" ", 1)[0]  # FULL / NOOP / MAP


def main() -> int:
    code, _, err, selected, problems = run()
    expected = json.loads((HERE / "expected.json").read_text("utf-8"))
    total = len(json.loads((ROOT / "sample" / "tests.json").read_text("utf-8"))["tests"])
    reasons = {}
    for line in err.splitlines():
        commit, _, rest = line.partition(" ")
        if commit in selected:
            reasons.setdefault(commit, rest)
    bad = list(problems)
    if code:
        bad.append(f"selector exited {code}")
    print(f"{'case':22} {'want':5} {'got':5} {'#sel':>5}  reason")
    for c in sorted(selected):
        got, want = outcome(reasons.get(c, "?")), expected.get(c, "?")
        if got == "FULL" and len(selected[c]) != total:
            bad.append(f"{c}: FULL but only {len(selected[c])} tests")
        flag = "" if got == want else "  <-- UNEXPECTED"
        if flag:
            bad.append(f"{c}: want {want}, got {got}")
        print(f"{c:22} {want:5} {got:5} {len(selected[c]):5d}  {reasons.get(c, '')[:200]}{flag}")
    print()
    print("\n".join(bad) if bad else f"ok: {len(selected)} cases, stdout is {len(selected)} valid JSON lines")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
