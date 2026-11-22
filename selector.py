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

from blast import diff, heavy, mapdb, prover, safe  # noqa: E402
from blast import select as sel  # noqa: E402
from blast.log import log  # noqa: E402
from blast.pack import duration_ms, pack  # noqa: E402

DATA = Path(os.environ.get("DATA_DIR", "/data"))
# HEAVY_REPLAY (blast/heavy.py): drop TestExecutor's TPC tests when they provably cannot fail.
#   static (default)  only the drops that need no replay: no subprocess, no cheap-phase run
#   1                 also replay the cheap phase on a /tmp overlay (costs CPU per replay)
#   0                 off
HEAVY_REPLAY = os.environ.get("HEAVY_REPLAY", "static").strip().lower()
if HEAVY_REPLAY not in ("0", "static", "1"):
    HEAVY_REPLAY = "static"
TMP = Path(os.environ.get("TMPDIR", "/tmp"))
SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))


def select(commit: str, patch_text: str, all_tests: list[int], ctx_factory, replayer=None
           ) -> tuple[list[int], str]:
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
    out, note = sorted(selected), ""
    if replayer is not None:
        out, note = replayer.maybe_drop(out, verdicts, ctx_factory(), TMP)
        note = f" {note}" if note else ""
    return out, f"MAP {len(out)} tests [{rules}]{note}; {trail}"


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
                f"NOCU_NAMES={int(sel.NOCU_NAMES)}, ALIAS_IMPORTS={int(sel.ALIAS_IMPORTS)}, INERT_EMPTY={int(sel.INERT_EMPTY)}, MAIN_UNUSED={int(sel.MAIN_UNUSED)}, COMPOUND_NAMES={int(sel.COMPOUND_NAMES)}, INSERT_INERT={int(sel.INSERT_INERT)}, ATTR_IMPORT_FULL={int(sel.ATTR_IMPORT_FULL)}, ATTR_LEVEL={int(sel.ATTR_LEVEL)}, INFECTION={int(sel.INFECTION)}, "
                f"SAFE_GROUPS={safe.describe()}")
            ctx = sel.Ctx(SRC, db, tests)
        return ctx
    all_tests = sorted(ms)
    full = pack([(n, ms[n]) for n in all_tests])
    replayer = (heavy.Replayer(sel.INDEX_DIR / "heavy.json", SRC, number_by_id, ms, replay=HEAVY_REPLAY == "1")
                if HEAVY_REPLAY != "0" else None)

    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        try:
            text = patch.read_bytes().decode("utf-8", "replace")
            selected, reason = select(commit, text, all_tests, ctx_factory, replayer)
            workers = full if selected is all_tests else pack([(n, ms[n]) for n in selected])
        except Exception as e:  # noqa: BLE001 - never crash the run
            workers, reason = full, f"FULL exception {type(e).__name__}: {e}"
        log(f"{commit} {reason}")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))

    sys.stdout.write("".join(line + "\n" for line in out))
    sys.stdout.flush()
    import resource

    ru = [resource.getrusage(w) for w in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    log(f"cpu {sum(r.ru_utime + r.ru_stime for r in ru):.2f} s (children "
        f"{ru[1].ru_utime + ru[1].ru_stime:.2f} s), HEAVY_REPLAY={HEAVY_REPLAY}")


if __name__ == "__main__":
    main()
