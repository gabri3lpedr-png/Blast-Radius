"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr.

Stage B1: every commit runs every test, packed onto the cheapest worker count.
Any exception on a commit falls back to the FULL selection.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast import diff  # noqa: E402
from blast.log import log  # noqa: E402
from blast.pack import duration_ms, pack  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", "/data"))


def select(commit: str, patch_text: str, all_tests: list[int]) -> tuple[list[int], str]:
    """Return (selected test numbers, reason)."""
    patches = diff.parse(patch_text)
    if not patches:
        return all_tests, f"FULL parse failed: {getattr(patches, 'reason', '')}"
    files = ",".join(p.path or "?" for p in patches)
    return all_tests, f"FULL stage-B1 runs everything ({files})"


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    all_tests = sorted(ms)
    full = pack([(n, ms[n]) for n in all_tests])

    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        try:
            text = patch.read_bytes().decode("utf-8", "replace")
            selected, reason = select(commit, text, all_tests)
            workers = full if selected is all_tests else pack([(n, ms[n]) for n in selected])
        except Exception as e:  # noqa: BLE001 - never crash the run
            workers, reason = full, f"FULL exception {type(e).__name__}: {e}"
        log(f"{commit} {reason}")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))

    sys.stdout.write("".join(line + "\n" for line in out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
