#!/usr/bin/env python3
"""Gap decomposition (dev-only): where do our selections spend seconds on tests that do not fail?

Usage: python3 tools/waste.py DATA_DIR [DATA_DIR ...]   (selector env passes through)
Per data dir:
  - top 15 tests by wasted seconds: summed recorded seconds over commits that selected the test
    while it did not fail (approximate: packing makes cost non-additive);
  - top 10 commits by (our cost - oracle cost), with the rule trail, the wasted seconds per rule
    tag the selector logged (tests attributed to the first tag that added them, from a re-run of
    the selection with per-rule bookkeeping), and the most expensive wasted tests.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import gap  # noqa: E402
from eval_sample import index_dir, sqlglot_src  # noqa: E402


def rule_attribution(data: Path, commits: list[str]) -> dict[str, dict[int, str]]:
    """commit -> {test number: first rule tag that added it}, by re-running the selection with
    _File.add / map_lines wrapped. Tests from rule f (baseline/unmapped) get "f"."""
    src = sqlglot_src()
    os.environ.setdefault("SQLGLOT_SRC", str(src))
    os.environ.setdefault("INDEX_DIR", str(index_dir(src)))
    os.environ["DATA_DIR"] = str(data)
    from blast import diff, mapdb, prover
    from blast import select as sel

    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    db = mapdb.load({t["id"]: t["number"] for t in tests})
    ctx = sel.Ctx(src, db, tests)
    owner: dict[int, str] = {}
    orig_add, orig_map = sel._File.add, sel._File.map_lines

    def add(self, t, tag):
        for n in t or ():
            owner.setdefault(n, tag.split("+")[0])
        return orig_add(self, t, tag)

    def map_lines(self, path, lines, tag):
        base, extra = self.ctx.line_tests(path, lines)
        label = tag or f"b:name-ref {path.rsplit('/', 1)[-1]}"
        for n in base or ():
            owner.setdefault(n, label)
        for n in extra:
            owner.setdefault(n, "s:" + label)
        return orig_map(self, path, lines, tag)

    sel._File.add, sel._File.map_lines = add, map_lines
    out = {}
    try:
        for c in commits:
            owner = {}
            text = (data / "commits" / f"{c}.patch").read_bytes().decode("utf-8", "replace")
            fps = diff.parse(text)
            try:
                verdicts = [prover.classify(fp, src) for fp in fps]
                if any(v.kind == prover.FULL for v in verdicts):
                    raise sel.Full("prover")
                got, _ = sel.select(ctx, verdicts)
                out[c] = {n: owner.get(n, "f") for n in got}
            except sel.Full:
                out[c] = None
    finally:
        sel._File.add, sel._File.map_lines = orig_add, orig_map
    return out


def main() -> None:
    for d in sys.argv[1:]:
        data = Path(d).resolve()
        rows = gap.run(data)
        tests = {t["number"]: t for t in json.loads((data / "tests.json").read_text("utf-8"))["tests"]}
        answers = json.loads((data / "answers.json").read_text("utf-8"))["commits"]
        wasted = defaultdict(float)
        commits_of = defaultdict(int)
        for r in rows:
            failing = set(answers.get(r["commit"], {}).get("failing", []))
            for n in set(r["selected"]) - failing:
                wasted[n] += tests[n]["seconds"]
                commits_of[n] += 1
        total_waste = sum(wasted.values())
        print(f"== {d}: {len(rows)} commits, wasted test-seconds {total_waste:.1f} "
              f"(ours {sum(r['ours'] for r in rows) / 1000:.1f} s, oracle {sum(r['oracle'] for r in rows) / 1000:.1f} s)")
        print(f"  top 15 tests by wasted seconds\n  {'wasted s':>9} {'#commits':>8} {'secs':>7}  test")
        for n, w in sorted(wasted.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {w:9.1f} {commits_of[n]:8d} {tests[n]['seconds']:7.3f}  {tests[n]['id']}")
        top = sorted(rows, key=lambda r: r["oracle"] - r["ours"])[:10]
        attrib = rule_attribution(data, [r["commit"] for r in top])
        print(f"\n  top 10 commits by ours - oracle\n  {'gap s':>7} {'ours':>7} {'oracle':>7} {'#sel':>5} {'#fail':>5}  commit  path")
        for r in top:
            failing = set(answers.get(r["commit"], {}).get("failing", []))
            waste = set(r["selected"]) - failing
            print(f"  {(r['ours'] - r['oracle']) / 1000:7.3f} {r['ours'] / 1000:7.3f} {r['oracle'] / 1000:7.3f} "
                  f"{r['nsel']:5d} {r['nfail']:5d}  {r['commit']}  {r['cat']}")
            print(f"           trail: {r['reason'][:230]}")
            own = attrib.get(r["commit"])
            if own:
                by_rule = defaultdict(float)
                for n in waste:
                    by_rule[own.get(n, "f")] += tests[n]["seconds"]
                parts = ", ".join(f"{k} {v:.1f}s" for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1])[:5])
                print(f"           waste by rule: {parts}")
            else:
                print("           waste by rule: FULL (every test)")
            big = sorted(waste, key=lambda n: -tests[n]["seconds"])[:3]
            print("           biggest wasted: " + ", ".join(
                f"{tests[n]['id'].split('::', 1)[1]} {tests[n]['seconds']:.1f}s" for n in big))
        print()


if __name__ == "__main__":
    main()
