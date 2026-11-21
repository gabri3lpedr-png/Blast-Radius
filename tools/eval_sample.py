#!/usr/bin/env python3
"""Run our selector on the host against ./sample and score it, with a per-commit table.

Usage: python3 tools/eval_sample.py [data_dir]   (default: $DATA_DIR or ./sample)
Prints per commit: kind, #selected, cost, MISS?, and the reason the selector logged.
Then runs score.py on the output for the official total.
"""

from __future__ import annotations

import json
import resource
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from blast.pack import cost, duration_ms  # noqa: E402
from blast.srctree import TARBALL_URL, extract  # noqa: E402

CACHE = ROOT / ".cache"


def sqlglot_src() -> Path:
    """The pinned sqlglot source, downloaded and extracted once into .cache/ (git-ignored)."""
    if "SQLGLOT_SRC" in os.environ:
        return Path(os.environ["SQLGLOT_SRC"])
    dest = CACHE / "sqlglot"
    if not (dest / "sqlglot" / "__init__.py").exists():
        import urllib.request

        CACHE.mkdir(exist_ok=True)
        tgz = CACHE / "sqlglot.tgz"
        if not tgz.exists():
            print(f"downloading {TARBALL_URL}", file=sys.stderr)
            urllib.request.urlretrieve(TARBALL_URL, tgz)
        extract(tgz, dest)
    return dest


def index_dir(src: Path) -> Path:
    """The selector's indexes, built once into .cache/index (the image builds them at docker
    build). Delete .cache/index after changing blast/build_index.py."""
    if "INDEX_DIR" in os.environ:
        return Path(os.environ["INDEX_DIR"])
    out = CACHE / "index"
    if not (out / "test_strings.json.gz").exists():
        subprocess.run([sys.executable, str(ROOT / "blast" / "build_index.py"), str(src), str(out)], check=True)
    return out


def main() -> int:
    data = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATA_DIR", ROOT / "sample"))
    data = data.resolve()
    answers_path = data / "answers.json"
    out_path = ROOT / "output.jsonl"

    src = sqlglot_src()
    env = dict(os.environ, DATA_DIR=str(data), SQLGLOT_SRC=str(src), INDEX_DIR=str(index_dir(src)))
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu0 = ru0.ru_utime + ru0.ru_stime
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "selector.py")], env=env, capture_output=True, text=True
    )
    wall = time.time() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = ru.ru_utime + ru.ru_stime - cpu0
    out_path.write_text(proc.stdout, "utf-8")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print(f"selector exited with {proc.returncode}")
        return 1

    reasons: dict[str, str] = {}
    for line in proc.stderr.splitlines():
        if line.startswith("map "):
            print(line)
        commit, _, rest = line.partition(" ")
        if commit and rest:
            reasons.setdefault(commit, rest)

    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    answers = json.loads(answers_path.read_text("utf-8"))["commits"]

    rows = []
    total = misses = 0
    for line in proc.stdout.splitlines():
        rec = json.loads(line)
        commit, workers = rec["commit"], rec["workers"]
        selected = {t for w in workers for t in w}
        c = cost(workers, ms)
        ans = answers.get(commit, {})
        missing = set(ans.get("failing", [])) - selected
        total += c
        if missing:
            misses += 1
            total += 300_000
        rows.append((commit, ans.get("kind", "?"), len(selected), len(workers), c, missing,
                     len(ans.get("failing", [])), reasons.get(commit, "")))

    print(f"{'commit':6} {'kind':8} {'#sel':>5} {'k':>2} {'cost s':>8} {'#fail':>5} miss  reason")
    for commit, kind, nsel, k, c, missing, nfail, reason in sorted(rows):
        miss = f"MISS({len(missing)})" if missing else ""
        print(f"{commit:6} {kind:8} {nsel:5d} {k:2d} {c / 1000:8.3f} {nfail:5d} {miss:5} {reason}")
    print(f"\nours: total {total / 1000:.3f} s, {misses} misses, selector wall {wall:.2f} s, CPU {cpu:.2f} s\n")

    sys.stdout.flush()
    r = subprocess.run(
        [sys.executable, str(ROOT / "score.py"), str(data), str(answers_path), str(out_path)]
    )
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
