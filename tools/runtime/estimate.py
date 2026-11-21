#!/usr/bin/env python3
"""DEV-ONLY: what would running our own selection (tools/runtime/verify.py) do to the score?

Usage: python3 tools/runtime/estimate.py DATA_DIR OUTPUT_JSONL [VERIFY_JSONL ...] [--model A,B]

Per commit: sel = SCORING.md cost of our workers; cpu = measured CPU (verify jsonl) of running
the selection ourselves, or for unmeasured commits the model A + B * recorded seconds of the
selection; then outputting only the tests that failed costs cpu + cost(pack(failing)).
Policies:
  current     sel everywhere
  rule        run when cpu < sel (the decision uses the measured / modelled cpu)
  best        run when cpu + cost(failing) < sel (perfect hindsight; a bound)
Also: wall seconds spent running tests under each policy (the batch has 180 s, 270 s final).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from blast.pack import cost, duration_ms, pack  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    a, b = 1.0, 1.5
    if "--model" in args:
        i = args.index("--model")
        a, b = map(float, args[i + 1].split(","))
        del args[i: i + 2]
    data, out, verifies = Path(args[0]), Path(args[1]), args[2:]
    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    num = {t["id"]: t["number"] for t in tests}
    answers = json.loads((data / "answers.json").read_text())["commits"]
    measured = {}
    for v in verifies:
        for line in open(v):
            r = json.loads(line)
            measured[r["commit"]] = r
    tot = {"current": 0.0, "rule": 0.0, "best": 0.0}
    wall = {"rule": 0.0, "best": 0.0}
    exact = mismatch = 0
    rows = []
    for line in out.read_text().splitlines():
        r = json.loads(line)
        c, workers = r["commit"], r["workers"]
        sel_s = cost(workers, ms) / 1000
        selected = [n for w in workers for n in w]
        rec = sum(ms[n] for n in selected) / 1000
        m = measured.get(c)
        if m is not None:
            failing = {num[i] for i in m["failed"]}
            if failing == set(answers[c]["failing"]):
                exact += 1
            else:
                mismatch += 1
            cpu, w = m["cpu"] + m["overlay_s"], m["wall"]
        else:
            failing = set(answers[c]["failing"])  # assume exact, as measured
            cpu = w = (a + b * rec) if selected else 0.0
        fail_s = cost(pack([(n, ms[n]) for n in failing]), ms) / 1000
        run_s = cpu + fail_s
        tot["current"] += sel_s
        use_rule = bool(selected) and cpu < sel_s
        use_best = bool(selected) and run_s < sel_s
        tot["rule"] += run_s if use_rule else sel_s
        tot["best"] += run_s if use_best else sel_s
        wall["rule"] += w if use_rule else 0
        wall["best"] += w if use_best else 0
        rows.append((c, len(selected), rec, sel_s, cpu, fail_s, "M" if m else "~", use_rule, use_best))
    print(f"{'commit':7} {'#sel':>5} {'rec s':>7} {'sel s':>7} {'cpu s':>7} {'fail s':>7}  src rule best")
    for c, n, rec, s, cpu, f, src, ur, ub in sorted(rows, key=lambda x: -x[3]):
        print(f"{c:7} {n:5d} {rec:7.2f} {s:7.3f} {cpu:7.2f} {f:7.3f}   {src}   {'RUN' if ur else '-':4} {'RUN' if ub else '-'}")
    print(f"\nmeasured {len(measured)} commits: failures exact {exact}, mismatched {mismatch}; "
          f"unmeasured use cpu = {a} + {b} * recorded")
    for k, v in tot.items():
        extra = f", test wall {wall[k]:.1f} s" if k in wall else ""
        print(f"  {k:8s} total {v:9.3f} s{extra}")


if __name__ == "__main__":
    main()
