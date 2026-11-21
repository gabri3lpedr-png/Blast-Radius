"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr.

Stage B2: the Prover. Each file of a commit is classified (blast/prover.py):
all NOOP -> []; NOOP + DATA -> the tests whose source names the fixture; anything
else (FULL or CHANGED) -> every test, packed onto the cheapest worker count.
Any exception on a commit falls back to the FULL selection.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast import diff, prover  # noqa: E402
from blast.fixtures import TestIndex  # noqa: E402
from blast.log import log  # noqa: E402
from blast.pack import duration_ms, pack  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", "/data"))
SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))

_index: TestIndex | None = None


def select(
    commit: str, patch_text: str, all_tests: list[int], number_by_id: dict[str, int]
) -> tuple[list[int], str]:
    """Return (selected test numbers, reason)."""
    global _index
    patches = diff.parse(patch_text)
    if not patches:
        return all_tests, f"FULL parse failed: {getattr(patches, 'reason', '')}"
    verdicts = [prover.classify(fp, SRC) for fp in patches]
    trail = "; ".join(str(v) for v in verdicts)

    if any(v.kind in (prover.FULL, prover.CHANGED) for v in verdicts):
        return all_tests, f"FULL {trail}"

    selected: set[int] = set()
    notes = []
    for v in verdicts:
        if v.kind != prover.DATA:
            continue
        if not v.path.startswith("tests/fixtures/"):
            return all_tests, f"FULL no rule for data file; {trail}"
        if _index is None:
            _index = TestIndex(SRC)
        nums = {number_by_id[t] for t in _index.tests_mentioning(v.path) if t in number_by_id}
        if not nums:
            return all_tests, f"FULL no test mentions fixture {v.path}; {trail}"
        selected |= nums
        notes.append(f"{v.path} -> {len(nums)} tests by name")

    if not selected:
        return [], f"NOOP {trail}"
    rest = "; ".join(str(v) for v in verdicts if v.kind != prover.DATA)
    return sorted(selected), f"DATA {'; '.join(notes)}" + (f"; {rest}" if rest else "")


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    number_by_id = {t["id"]: t["number"] for t in tests}
    all_tests = sorted(ms)
    full = pack([(n, ms[n]) for n in all_tests])

    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        try:
            text = patch.read_bytes().decode("utf-8", "replace")
            selected, reason = select(commit, text, all_tests, number_by_id)
            workers = full if selected is all_tests else pack([(n, ms[n]) for n in selected])
        except Exception as e:  # noqa: BLE001 - never crash the run
            workers, reason = full, f"FULL exception {type(e).__name__}: {e}"
        log(f"{commit} {reason}")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))

    sys.stdout.write("".join(line + "\n" for line in out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
