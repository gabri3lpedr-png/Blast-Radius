#!/usr/bin/env python3
"""Label mutant patches by running sqlglot's REAL test suite (DEV-ONLY).

Usage (repo root):  python3 tools/mutants/label.py [--jobs 4] [--limit N]

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


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def run_suite(mid, jobs):
    """Run the full suite on WORK. -> (failed ids, ok ids, status)."""
    out = os.path.join(RAW, f"{mid}.json")
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    cmd = ["docker", "run", "--rm", "--name", CONTAINER, "--platform", "linux/amd64",
           "--volume", f"{WORK}:/opt/sqlglot",
           "--volume", f"{os.path.join(REPO, 'tools', 'mutants')}:/mutants:ro",
           "--volume", f"{RAW}:/out",
           "--env", "PYTHONPATH=/opt/sqlglot:/mutants", "--env", f"LABEL_OUT=/out/{mid}.json",
           IMAGE, "python", "-m", "pytest", "tests", "-p", "label_plugin", "-p", "no:cacheprovider",
           "-q", "-n", str(jobs), "--dist", "load", "--timeout", "300"]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", CONTAINER], capture_output=True)
        return None, None, "timeout"
    if not os.path.exists(out):
        return None, None, "no_result"
    r = json.load(open(out))
    return set(r["failed"]), set(r["ok"]), "ok"


def prepare_work(patch_path):
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    shutil.copytree(PRISTINE, WORK, symlinks=True)
    if patch_path is None:
        return True
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", patch_path], cwd=WORK,
                       capture_output=True, text=True)
    if r.returncode:
        log(f"  git apply failed: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="label at most N patches this run")
    args = ap.parse_args()
    for d in (PENDING, COMMITS, RAW):
        os.makedirs(d, exist_ok=True)

    tj = json.load(open(os.path.join(REPO, "sample", "tests.json")))
    shutil.copy(os.path.join(REPO, "sample", "tests.json"), os.path.join(MO, "tests.json"))
    number = {t["id"]: t["number"] for t in tj["tests"]}

    def failing_numbers(failed, ok):
        return sorted(n for i, n in number.items() if i in failed or i not in ok)

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
        failed, ok, status = run_suite("baseline", args.jobs)
        fails = failing_numbers(failed, ok) if status == "ok" else None
        write_json(base_path, {"status": status, "failing": fails, "seconds": round(time.time() - t0)})
        log(f"baseline: {status}, failing={fails}, {time.time() - t0:.0f} s")
        if status != "ok" or fails:
            sys.exit("Baseline is not clean: fix the environment before labelling mutants.")
    base_secs = json.load(open(base_path))["seconds"]

    todo = sorted(p[:-6] for p in os.listdir(PENDING) if p.endswith(".patch")
                  and p[:-6] not in answers["commits"])
    skipped = set()
    if os.path.exists(log_path):
        for line in open(log_path):
            r = json.loads(line)
            if r["status"] in ("apply_failed", "timeout", "no_result"):
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
        if not prepare_work(patch):
            status, fails = "apply_failed", None
        else:
            failed, ok, status = run_suite(mid, args.jobs)
            fails = failing_numbers(failed, ok) if status == "ok" else None
        secs = time.time() - t0
        times.append(secs)
        if status == "ok":
            shutil.copy(patch, os.path.join(COMMITS, f"{mid}.patch"))
            answers["commits"][mid] = {"failing": fails, "kind": meta["kind"],
                                       "detail": meta.get("detail"), "where": meta.get("where")}
            write_json(ans_path, answers)
        with open(log_path, "a") as f:
            f.write(json.dumps({"id": mid, "status": status, "seconds": round(secs),
                                "n_failing": None if fails is None else len(fails),
                                "detail": meta.get("detail")}) + "\n")
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
