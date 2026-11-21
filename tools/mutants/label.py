#!/usr/bin/env python3
"""Label mutant patches by running sqlglot's REAL test suite (DEV-ONLY).

Usage (repo root):  python3 tools/mutants/label.py [--jobs 3] [--limit N] [--dir mutants_new]

For each mutants_out/pending/<id>.patch not labelled yet: copy pristine sqlglot
(.cache/sqlglot) to mutants_out/work, `git apply` the patch, run the full suite
in the mapgen image (pytest-xdist, SKIP_INTEGRATION=0), and record failing
tests. Results are written after EVERY patch, so a partial run is usable:

  mutants_out/answers.json      same format as sample/answers.json (+ detail)
  mutants_out/commits/<id>.patch  only labelled patches
  mutants_out/tests.json        copy of sample/tests.json
  mutants_out/label_log.jsonl   one line per patch (seconds, #failing, status)

so `make sample SAMPLE=$(pwd)/mutants_out` grades the selector on it.
Resumable: re-run it and it skips patches already in answers.json.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MO = os.path.join(REPO, "mutants_out")
PENDING, COMMITS, RAW, WORK = (os.path.join(MO, d) for d in ("pending", "commits", "raw", "work"))
PRISTINE = os.path.join(REPO, ".cache", "sqlglot")
IMAGE = "blast-radius-mapgen"
CONTAINER = "blast-label"
RUN_TIMEOUT = 45 * 60  # whole suite, seconds
TEST_TIMEOUT = 300     # per test (pytest-timeout), --test-timeout


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


EXECUTOR = "tests/test_executor.py"  # 112 of 251 s, and runs its own 8-process pool
CONFIRM_MAX = 80                     # re-check at most this many failing tests serially


def pytest_run(tag, args, timeout):
    """One docker run of pytest on WORK. -> (failed, ok) or None on timeout/no result."""
    for old in glob.glob(os.path.join(RAW, f"{tag}.*.json")):
        os.remove(old)
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    cmd = ["docker", "run", "--rm", "--name", CONTAINER, "--platform", "linux/amd64",
           "--volume", f"{WORK}:/opt/sqlglot",
           "--volume", f"{os.path.join(REPO, 'tools', 'mutants')}:/mutants:ro",
           "--volume", f"{RAW}:/out",
           "--env", "PYTHONPATH=/opt/sqlglot:/mutants", "--env", f"LABEL_OUT=/out/{tag}",
           IMAGE, "python", "-m", "pytest", *args, "-p", "label_plugin", "-p", "no:cacheprovider",
           "-q", "--timeout", str(TEST_TIMEOUT)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", CONTAINER], capture_output=True)
        return None
    # One file per pytest process (main / xdist controller and every worker):
    # subtest failures are only seen by the worker that ran the test.
    files = glob.glob(os.path.join(RAW, f"{tag}.*.json"))
    if not any(f.endswith(".main.json") for f in files):
        return None
    failed, ok = set(), set()
    for f in files:
        r = json.load(open(f))
        failed |= set(r["failed"])
        ok |= set(r["ok"])
    return failed, ok


def run_suite(mid, jobs, all_ids):
    """Full suite on WORK. -> (failing ids, status, n_flaky).

    Phase 1: everything but the executor tests, xdist with --dist loadscope (one
    worker per test class, so TestOptimizer's process pool exists once).
    Phase 2: the executor tests alone (they parallelise internally).
    Phase 3: every test that failed or never reported is re-run serially; only
    the ones that fail again count. Parallel runs on a 4 GB Docker VM can fail a
    heavy test spuriously (seen on the clean tree), and a wrong label is worse
    than a slow one. Skipped when more than CONFIRM_MAX fail (syntax errors etc.).
    """
    t_end = time.time() + RUN_TIMEOUT
    r1 = pytest_run(f"{mid}.p1", ["tests", "--ignore", EXECUTOR, "-n", str(jobs),
                                  "--dist", "loadscope"], max(60, t_end - time.time()))
    r2 = pytest_run(f"{mid}.p2", [EXECUTOR], max(60, t_end - time.time()))
    if r1 is None or r2 is None:
        return None, "timeout_or_no_result", 0
    failed, ok = r1[0] | r2[0], r1[1] | r2[1]
    suspects = {i for i in all_ids if i in failed or i not in ok}
    if not suspects or len(suspects) > CONFIRM_MAX:
        return suspects, "ok", 0
    r3 = pytest_run(f"{mid}.p3", sorted(suspects), max(60, t_end - time.time()))
    if r3 is None:
        return suspects, "ok_unconfirmed", 0
    confirmed = {i for i in suspects if i in r3[0] or i not in r3[1]}
    return confirmed, "ok", len(suspects) - len(confirmed)


def prepare_work(patch_path):
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    shutil.copytree(PRISTINE, WORK, symlinks=True)
    if patch_path is None:
        return True
    # WORK lives inside our own git repo. Inside a repo's subdirectory, `git apply`
    # SILENTLY SKIPS paths outside that subdirectory, so stop repo discovery at
    # mutants_out/ and treat WORK as a plain folder.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=MO)
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", os.path.abspath(patch_path)],
                       cwd=WORK, capture_output=True, text=True, env=env)
    if r.returncode:
        log(f"  git apply failed: {r.stderr.strip()[:200]}")
        return False
    # Prove it applied: every file the patch touches must now differ from pristine.
    for line in open(patch_path):
        if line.startswith("+++ b/"):
            p = line[6:].rstrip("\n")
            old = os.path.join(PRISTINE, p)
            new = os.path.join(WORK, p)
            if os.path.exists(old) and open(old, "rb").read() == open(new, "rb").read():
                log(f"  patch did not change {p}")
                return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="label at most N patches this run")
    ap.add_argument("--dir", default="mutants_out",
                    help="set folder (repo-relative): mutants_out, mutants_new, ...")
    ap.add_argument("--test-timeout", type=int, default=300, help="pytest-timeout per test, seconds")
    args = ap.parse_args()
    global MO, PENDING, COMMITS, RAW, WORK, TEST_TIMEOUT
    MO = os.path.join(REPO, args.dir)
    PENDING, COMMITS, RAW, WORK = (os.path.join(MO, d) for d in ("pending", "commits", "raw", "work"))
    TEST_TIMEOUT = args.test_timeout
    for d in (PENDING, COMMITS, RAW):
        os.makedirs(d, exist_ok=True)

    tj = json.load(open(os.path.join(REPO, "sample", "tests.json")))
    shutil.copy(os.path.join(REPO, "sample", "tests.json"), os.path.join(MO, "tests.json"))
    number = {t["id"]: t["number"] for t in tj["tests"]}

    def label(mid):
        failing, status, flaky = run_suite(mid, args.jobs, number)
        nums = sorted(number[i] for i in failing) if failing is not None else None
        return nums, status, flaky

    ans_path = os.path.join(MO, "answers.json")
    answers = json.load(open(ans_path)) if os.path.exists(ans_path) else {"commits": {}}
    write_json(ans_path, answers)
    log_path = os.path.join(MO, "label_log.jsonl")

    # Baseline once: the clean tree must label 0 failing, or the environment is off.
    base_path = os.path.join(MO, "baseline_label.json")
    if not os.path.exists(base_path):
        log("baseline: labelling the clean tree (must be 0 failing)")
        prepare_work(None)
        t0 = time.time()
        fails, status, flaky = label("baseline")
        write_json(base_path, {"status": status, "failing": fails, "flaky": flaky,
                               "seconds": round(time.time() - t0)})
        log(f"baseline: {status}, failing={fails}, flaky={flaky}, {time.time() - t0:.0f} s")
        if status != "ok" or fails:
            os.remove(base_path)
            sys.exit("Baseline is not clean: fix the environment before labelling mutants.")
    base_secs = json.load(open(base_path))["seconds"]

    todo = sorted(p[:-6] for p in os.listdir(PENDING) if p.endswith(".patch")
                  and p[:-6] not in answers["commits"])
    skipped = set()
    if os.path.exists(log_path):
        for line in open(log_path):
            r = json.loads(line)
            if r["status"] in ("apply_failed", "timeout_or_no_result"):
                skipped.add(r["id"])
    todo = [m for m in todo if m not in skipped]
    if args.limit:
        todo = todo[: args.limit]
    log(f"{len(answers['commits'])} labelled, {len(todo)} to go (~{base_secs} s each)")

    times = []
    for k, mid in enumerate(todo, 1):
        patch = os.path.join(PENDING, f"{mid}.patch")
        meta_path = os.path.join(PENDING, f"{mid}.json")
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {"kind": "code"}
        t0 = time.time()
        flaky = 0
        if not prepare_work(patch):
            status, fails = "apply_failed", None
        else:
            fails, status, flaky = label(mid)
        secs = time.time() - t0
        times.append(secs)
        if status.startswith("ok"):
            shutil.copy(patch, os.path.join(COMMITS, f"{mid}.patch"))
            answers["commits"][mid] = {"failing": fails, "kind": meta["kind"],
                                       "detail": meta.get("detail"), "where": meta.get("where"),
                                       "confirmed": status == "ok"}
            write_json(ans_path, answers)
        with open(log_path, "a") as f:
            f.write(json.dumps({"id": mid, "status": status, "seconds": round(secs),
                                "n_failing": None if fails is None else len(fails),
                                "flaky_dropped": flaky, "detail": meta.get("detail")}) + "\n")
        avg = sum(times[-10:]) / len(times[-10:])
        eta = avg * (len(todo) - k)
        log(f"[{k}/{len(todo)}] {mid} {meta.get('detail', ''):17} {status:12} "
            f"failing={'-' if fails is None else len(fails):<4} {secs:5.0f} s   "
            f"ETA {eta / 3600:.1f} h ({time.strftime('%H:%M', time.localtime(time.time() + eta))})")
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    log(f"done: {len(answers['commits'])} labelled in mutants_out/answers.json")


if __name__ == "__main__":
    main()
