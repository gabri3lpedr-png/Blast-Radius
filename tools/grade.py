#!/usr/bin/env python3
"""DEV-ONLY: grade one selector run (stdout + stderr log) against a labelled data dir.

Usage: python3 tools/grade.py DATA_DIR OUTPUT_JSONL STDERR_LOG [--heavy]
Prints: total simulated, misses (each with kind/detail, the selector's reason, and MAP GAP vs
RULE GAP: MAP GAP = the map credits some missed test with a changed line of a changed file, i.e.
the rules threw it away; RULE GAP = nothing in the map pointed there), cost and misses per
kind/detail, and with --heavy the per-heavy-test table (tests > 2 s).
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from blast import diff  # noqa: E402
from blast.pack import cost, duration_ms, pack  # noqa: E402


def map_gap(data: Path, commit: str, missed: set[int], tests: list[dict]) -> str:
    """MAP GAP if the map credits a missed test with a line of a changed file, else RULE GAP."""
    raw = json.load(gzip.open(ROOT / "maps/coverage_map.json.gz", "rt"))
    idx = {t: i for i, t in enumerate(raw["tests"])}
    ids = {t["number"]: t["id"] for t in tests}
    want = {idx[ids[n]] for n in missed if ids[n] in idx}
    text = (data / "commits" / f"{commit}.patch").read_bytes().decode("utf-8", "replace")
    for fp in diff.parse(text) or []:
        lines = raw["lines"].get(fp.path or "", {})
        for s in lines.values():
            if want & set(raw["sets"][s]):
                return f"MAP GAP (map has a missed test on {fp.path})"
    return "RULE GAP (map never credits the missed tests with a changed file)"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    data, out, err = Path(args[0]), Path(args[1]), Path(args[2])
    tests = json.loads((data / "tests.json").read_text())["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    ans = json.loads((data / "answers.json").read_text())["commits"]
    reason = {}
    cpu = ""
    for line in err.read_text().splitlines():
        c, _, rest = line.partition(" ")
        if c == "cpu":
            cpu = line
        elif c in ans:
            reason.setdefault(c, rest)
    rows = []
    for line in out.read_text().splitlines():
        r = json.loads(line)
        c = r["commit"]
        sel = {n for w in r["workers"] for n in w}
        fail = set(ans[c]["failing"])
        rows.append((c, ans[c]["kind"], ans[c].get("detail", "-"), cost(r["workers"], ms), fail - sel, sel, fail))
    total = sum(x[3] for x in rows) + 300_000 * sum(bool(x[4]) for x in rows)
    misses = [x for x in rows if x[4]]
    print(f"total {total / 1000:.3f} s simulated, {len(misses)} misses, {cpu}")
    ids = {t["number"]: t["id"] for t in tests}
    for c, kind, det, _, missed, _, _ in misses:
        print(f"  MISS {c} {kind}/{det}: missed {len(missed)} e.g. {ids[min(missed)]}")
        print(f"       reason: {reason.get(c, '')[:300]}")
        print(f"       {map_gap(data, c, missed, tests)}")
    agg = defaultdict(lambda: [0, 0, 0])
    for c, kind, det, co, missed, _, _ in rows:
        a = agg[f"{kind}/{det}"]
        a[0] += 1
        a[1] += co
        a[2] += bool(missed)
    print(f"  {'kind/detail':28} {'n':>4} {'cost s':>9} {'misses':>6}")
    for k, (n, co, m) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        print(f"  {k:28} {n:4d} {co / 1000:9.3f} {m:6d}")
    if "--heavy" in sys.argv:
        print(f"\n  heavy tests (> 2 s): selected in / fails in / P(fail|sel) / s saved if never selected / "
              f"expected miss cost if never selected")
        for t in sorted((t for t in tests if t["seconds"] > 2), key=lambda t: -t["seconds"]):
            n = t["number"]
            sel_in = [x for x in rows if n in x[5]]
            fails = [x for x in sel_in if n in x[6]]
            saved = 0
            for c, _, _, co, _, sel, _ in sel_in:
                saved += co - cost(pack([(m, ms[m]) for m in sel - {n}]), ms)
            p = len(fails) / len(sel_in) if sel_in else 0.0
            # never selecting it: every commit it fails on becomes a miss (unless already one)
            miss_cost = sum(300 for x in fails if not x[4])
            print(f"  {t['seconds']:6.2f}s {t['id'].split('::', 1)[1]:52} sel {len(sel_in):3d} fail {len(fails):3d} "
                  f"P {p:4.2f}  saved {saved / 1000:7.1f} s  miss cost {miss_cost:6d} s  net {saved / 1000 - miss_cost:8.1f}")


if __name__ == "__main__":
    main()
