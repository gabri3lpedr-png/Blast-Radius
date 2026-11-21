"""RUN_SMALL: run a tiny selection for real and output only the tests that failed.

When the selection's recorded seconds are <= RUN_SMALL_T and the modelled CPU of running it
(STARTUP + RATIO x recorded seconds) is below its SCORING.md cost, every selected test is run in
ONE fresh interpreter on a /tmp overlay of the pinned tree with the patch applied (patched files
real, everything else symlinked, so the build's .pyc are reused). The answer is exactly the
selected tests that failed or errored ([] if none). Nothing unrun is ever dropped.

sqlglot's suite is plain unittest (no conftest, no pytest config, no pytest imports), so the child
uses unittest's loader and runner directly: same import names as pytest's prepend mode
(tests.dialects.test_x), same setUpClass/subTest semantics, a fraction of pytest's startup.

Any runner problem keeps the original selection: a patch that cannot be applied exactly (or a
rename, delete or binary patch), a crash, a timeout of the whole run, a non-JSON result, a test
that was skipped, not run or reported twice, a class/module fixture error, or a result for an id
that was not asked for.
"""

from __future__ import annotations

import json
import os
import sys

MARK = "RUNSMALL-RESULT "


# -- child: python runsmall.py <overlay>, test ids as JSON on stdin --------------------------------
def child(ov: str) -> None:
    ids = json.loads(sys.stdin.read())
    sys.path.insert(0, ov)
    os.chdir(ov)
    import io
    import unittest

    want = {}
    for i in ids:
        path, cls, meth = i.split("::")
        want[f"{path[:-3].replace('/', '.')}.{cls}.{meth}"] = i
    state: dict[str, str] = {}
    problems: list[str] = []

    class Result(unittest.TestResult):
        def _id(self, test):
            tid = test.id() if isinstance(test, unittest.TestCase) else str(test)
            if tid not in want:
                problems.append(f"unexpected {tid}")
                return None
            return want[tid]

        def startTest(self, test):
            super().startTest(test)
            t = self._id(test)
            if t is not None:
                if t in state:
                    problems.append(f"twice {t}")
                state[t] = "ok"

        def _fail(self, test):
            if isinstance(test, unittest.TestCase):
                t = self._id(test)
                if t is not None:
                    state[t] = "failed"
            else:  # _ErrorHolder: setUpClass/setUpModule/tearDown*Class/Module error
                problems.append(f"fixture error {test}")

        def addError(self, test, err):
            super().addError(test, err)
            self._fail(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._fail(test)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self._fail(test)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._fail(test)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            problems.append(f"skipped {test}")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in want:
        suite.addTest(loader.loadTestsFromName(name))  # an import error raises: runner problem
    if loader.errors:
        problems.extend(loader.errors)
    real_out = sys.stdout
    sys.stdout = io.StringIO()  # tests that print must not reach the parent's result channel
    try:
        suite.run(Result())
    finally:
        sys.stdout = real_out
    missing = sorted(set(ids) - set(state))
    if missing:
        problems.append(f"not run {missing[:3]}")
    real_out.write(MARK + json.dumps({"failed": sorted(t for t, s in state.items() if s == "failed"),
                                      "problems": problems[:5]}) + "\n")
    real_out.flush()


# -- parent ---------------------------------------------------------------------------------------
class Runner:
    def __init__(self, src, tmp, tests: list[dict], threshold: float, startup: float, ratio: float,
                 budget_s: float = 60.0):
        self.src, self.tmp = src, tmp
        self.id_of = {t["number"]: t["id"] for t in tests}
        self.num_of = {t["id"]: t["number"] for t in tests}
        self.secs = {t["number"]: t["seconds"] for t in tests}
        self.threshold, self.startup, self.ratio = threshold, startup, ratio
        self.budget = budget_s  # wall seconds for all runs in the batch
        self.runs = 0

    def maybe_run(self, selected: list[int], patches, sel_cost_ms: int) -> tuple[list[int], str]:
        if not selected:
            return selected, ""
        rec = sum(self.secs[n] for n in selected)
        model = self.startup + self.ratio * rec
        if rec > self.threshold or model * 1000 >= sel_cost_ms:
            return selected, ""
        if self.budget <= 0:
            return selected, "r:budget spent"
        import resource
        import time

        r0, t0 = resource.getrusage(resource.RUSAGE_CHILDREN), time.time()
        try:
            failed, why = self._run(selected, patches, timeout=max(10.0, 5 * model))
        except Exception as e:  # noqa: BLE001 - any runner problem keeps the selection
            failed, why = None, f"{type(e).__name__}: {e}"
        r1, wall = resource.getrusage(resource.RUSAGE_CHILDREN), time.time() - t0
        self.budget -= wall
        self.runs += 1
        cpu = r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime
        m = f"[rec={rec:.3f} cpu={cpu:.3f} wall={wall:.3f}]"
        if failed is None:
            return selected, f"r:keep {m} ({why})"[:300]
        return sorted(failed), f"r:ran {len(selected)} -> {len(failed)} failed {m}"

    def _overlay(self, patches) -> str:
        from blast.apply import apply
        from blast.heavy import overlay

        patched = {}
        for fp in patches:
            if fp.is_new or fp.is_deleted or fp.old_path != fp.new_path or fp.is_binary or fp.mode_change:
                raise ValueError("new/deleted/renamed/binary file")
            orig = (self.src / fp.path).read_bytes().decode("utf-8")
            patched[fp.path], _ = apply(fp, orig)  # ApplyError -> keep
        for p in patched:
            if not (self.src / p).is_file():
                raise ValueError(f"not a file {p}")
        ov = self.tmp / "runsmall_ov"
        overlay(self.src, ov, patched)
        return str(ov)

    def _run(self, selected, patches, timeout):
        import subprocess

        ov = self._overlay(patches)
        ids = [self.id_of[n] for n in selected]
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="1", PYTHONDONTWRITEBYTECODE="1",
                   HOME=os.environ.get("HOME", "/tmp"))
        try:
            p = subprocess.run([sys.executable, "-s", __file__, ov], input=json.dumps(ids), cwd=ov,
                               env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        lines = p.stdout.splitlines()
        if p.returncode != 0 or len(lines) != 1 or not lines[0].startswith(MARK):
            return None, f"rc {p.returncode} {p.stderr[-120:]!r}"
        r = json.loads(lines[0][len(MARK):])
        if r["problems"]:
            return None, "; ".join(r["problems"])
        failed = r["failed"]
        if not set(failed) <= set(ids):
            return None, "unknown failed id"
        return [self.num_of[i] for i in failed], ""


if __name__ == "__main__":
    sys.path.pop(0)  # blast/ would shadow the stdlib `select`
    child(sys.argv[1])
