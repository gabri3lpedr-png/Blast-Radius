#!/usr/bin/env python3
"""Our cost vs the ORACLE cost (failing tests only, packed) per rule path / FULL reason.

Usage: python3 tools/gap.py DATA_DIR [DATA_DIR ...] [--json OUT] [--quiet]
Runs selector.py on the host (env passes through, so FUNC_LEVEL=2 etc. apply), then prints, per
data dir: total, misses, oracle total, and a table of categories sorted by gap (ours - oracle).
Dev-only: answers are read to MEASURE, never by the selector.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from blast.pack import cost, duration_ms, pack  # noqa: E402
from eval_sample import index_dir, sqlglot_src  # noqa: E402

CPU = 0.0
_TAG = re.compile(r"(?<![\w/.])(a:L|c:def|b:class|b:name|b:def|d:[\w.]+|e:files_read|e:fixture-name|g:|s:|f:\+|DEAD)")


def category(reason: str) -> str:
    if reason.startswith("NOOP"):
        return "NOOP"
    if reason.startswith("MAP"):
        rules = reason[reason.find("[") + 1: reason.rfind("]")]
        fams = set()
        for m in _TAG.finditer(rules):
            t = m.group(1)
            if t.startswith("d:"):
                t = "d:all" if t.endswith(":all") else ("d:helper" if "helper" in t or t == "d:file" else "d:test")
            fams.add(t.rstrip(":+"))
        return "MAP " + "+".join(sorted(fams))
    r = reason.split(";")[0]
    r = re.sub(r"\(new text does not parse:.*", "new text does not parse", r)
    r = re.sub(r"name \S+ used too widely \(\d+ files\)", "name used too widely", r)
    r = re.sub(r"\S+\.py:\d+", "<file:line>", r)
    r = re.sub(r"name chain too deep at \S+", "name chain too deep", r)
    r = re.sub(r"no class_users for \S+", "no class_users", r)
    r = re.sub(r"FULL FULL \S+", "FULL", r)
    return r.strip()


def run(data: Path) -> list[dict]:
    src = sqlglot_src()
    env = dict(os.environ, DATA_DIR=str(data), SQLGLOT_SRC=str(src), INDEX_DIR=str(index_dir(src)))
    import resource
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = subprocess.run([sys.executable, str(ROOT / "selector.py")], env=env, capture_output=True, text=True)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    global CPU
    CPU = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
    if proc.returncode:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"selector exited {proc.returncode}")
    reasons = {}
    for line in proc.stderr.splitlines():
        c, _, rest = line.partition(" ")
        if c and rest:
            reasons.setdefault(c, rest)
    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    answers = json.loads((data / "answers.json").read_text("utf-8"))["commits"]
    rows = []
    for line in proc.stdout.splitlines():
        rec = json.loads(line)
        c, workers = rec["commit"], rec["workers"]
        sel = {t for w in workers for t in w}
        failing = answers.get(c, {}).get("failing", [])
        oracle = cost(pack([(n, ms[n]) for n in failing]), ms)
        miss = bool(set(failing) - sel)
        rows.append({"commit": c, "kind": answers.get(c, {}).get("kind", "?"), "ours": cost(workers, ms),
                     "oracle": oracle, "miss": miss, "nsel": len(sel), "nfail": len(failing),
                     "reason": reasons.get(c, ""), "cat": category(reasons.get(c, "")),
                     "selected": sorted(sel)})
    return rows


def report(name: str, rows: list[dict], quiet: bool) -> None:
    ours = sum(r["ours"] for r in rows) + 300_000 * sum(r["miss"] for r in rows)
    orc = sum(r["oracle"] for r in rows)
    misses = [r["commit"] for r in rows if r["miss"]]
    print(f"== {name}: ours {ours / 1000:.3f} s, misses {len(misses)} {misses or ''}, "
          f"oracle {orc / 1000:.3f} s, gap {(ours - orc) / 1000:.3f} s, CPU {CPU:.2f} s")
    if quiet:
        return
    g = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        x = g[r["cat"]]
        x[0] += 1
        x[1] += r["ours"]
        x[2] += r["oracle"]
    print(f"  {'n':>4} {'ours s':>9} {'oracle s':>9} {'gap s':>9}  category")
    for cat, (n, o, q) in sorted(g.items(), key=lambda kv: kv[1][2] - kv[1][1]):
        print(f"  {n:4d} {o / 1000:9.3f} {q / 1000:9.3f} {(o - q) / 1000:9.3f}  {cat}")


def main() -> None:
    args = sys.argv[1:]
    quiet = "--quiet" in args
    out = None
    if "--json" in args:
        i = args.index("--json")
        out = args[i + 1]
        del args[i: i + 2]
    args = [a for a in args if a != "--quiet"]
    allrows = {}
    for d in args:
        rows = run(Path(d).resolve())
        allrows[d] = rows
        report(d, rows, quiet)
    if out:
        Path(out).write_text(json.dumps(allrows), "utf-8")


if __name__ == "__main__":
    main()
