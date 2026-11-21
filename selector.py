"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr.

Stage B3. Each file of a commit is classified by the Prover (blast/prover.py):
all NOOP -> []; any FULL -> every test; otherwise CHANGED and DATA files are narrowed with
the coverage map (blast/select.py, rules a-f), and anything it cannot explain -> FULL.
The selection is packed onto the cheapest worker count. Any exception on a commit falls back
to the FULL selection.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast import diff, mapdb, prover, safe  # noqa: E402
from blast import select as sel  # noqa: E402
from blast.log import log  # noqa: E402
from blast.pack import duration_ms, pack  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", "/data"))
SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))


def select(commit: str, patch_text: str, all_tests: list[int], ctx_factory) -> tuple[list[int], str]:
    """Return (selected test numbers, reason)."""
    patches = diff.parse(patch_text)
    if not patches:
        return all_tests, f"FULL parse failed: {getattr(patches, 'reason', '')}"
    verdicts = [prover.classify(fp, SRC) for fp in patches]
    trail = "; ".join(str(v) for v in verdicts)

    if any(v.kind == prover.FULL for v in verdicts):
        return all_tests, f"FULL {trail}"
    if all(v.kind == prover.NOOP for v in verdicts):
        return [], f"NOOP {trail}"
    try:
        selected, rules = sel.select(ctx_factory(), verdicts)
    except sel.Full as e:
        return all_tests, f"FULL {e}; {trail}"
    if len(selected) >= len(all_tests):
        return all_tests, f"FULL (selected everything) {rules}; {trail}"
    return sorted(selected), f"MAP {len(selected)} tests [{rules}]; {trail}"


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    number_by_id = {t["id"]: t["number"] for t in tests}
    ctx = None

    def ctx_factory() -> sel.Ctx:
        nonlocal ctx
        if ctx is None:
            db = mapdb.load(number_by_id)
            log(f"map {db.name}: {'EMPTY' if db.empty else 'loaded'}, {len(db.unmapped)} unmapped, "
                f"FUNC_LEVEL={sel.FUNC_LEVEL}, SETUP_STOPGAP={int(sel.SETUP_STOPGAP)}, "
                f"SYNTAX_LAZY={int(prover.SYNTAX_LAZY)}, IMPORT_FUNC={int(sel.IMPORT_FUNC)}, "
                f"MODULE_NAMES={int(sel.MODULE_NAMES)}, MAX_DEPTH={sel.MAX_DEPTH}, "
                f"UNUSED_FILES={int(sel.UNUSED_FILES)}, WIDE_NAMES={int(sel.WIDE_NAMES)}, "
                f"NOCU_NAMES={int(sel.NOCU_NAMES)}, "
                f"SAFE_GROUPS={safe.describe()}")
            ctx = sel.Ctx(SRC, db, tests)
        return ctx
    all_tests = sorted(ms)
    full = pack([(n, ms[n]) for n in all_tests])

    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        try:
            text = patch.read_bytes().decode("utf-8", "replace")
            selected, reason = select(commit, text, all_tests, ctx_factory)
            workers = full if selected is all_tests else pack([(n, ms[n]) for n in selected])
        except Exception as e:  # noqa: BLE001 - never crash the run
            workers, reason = full, f"FULL exception {type(e).__name__}: {e}"
        log(f"{commit} {reason}")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))

    sys.stdout.write("".join(line + "\n" for line in out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
