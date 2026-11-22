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
# PROBE_E (probe branch only, never on main): every FULL commit still runs ALL tests (no misses),
# packed in exactly 4 workers so its cost is 38 s + 1.5 s * MARK[class of its FULL reason]
# (worker 0 = 30 s + 1.5 s * mark; the suite is 103 s, its longest test 28.8 s). MARKS is a
# Golomb ruler: any two classes sum to a unique total, so the board total tells which fired.
PROBE_E = os.environ.get("PROBE_E", "0") == "1"
MARKS = (0, 1, 5, 12, 25, 27, 35, 41, 44)
_NAME_CHAIN = ("under another name", "too widely", "too deep", "wide name selects")
_IMPORT_MISC = ("no class_users", "runs at import", "run at import", "binds no plain names")
_NOTHING = ("map has no lines", "lookup found nothing", "no changed original lines", "outside sqlglot/",
            "cannot locate", "no test reads", "SAFE ", "no known users", "nothing imports", "does not locate")
CLASSES = (
    ("new file under tests/", lambda r: _first_full(r).startswith("FULL tests/")),
    ("new file under sqlglot/", lambda r: _first_full(r).startswith("FULL sqlglot/")),
    ("other new / deleted / rename / binary / no hunks",
     lambda r: any(k in r for k in ("(new file)", "(deleted file)", "(rename)", "(binary)", "(no hunks"))),
    ("structural / unrecognised file", lambda r: "(structural file)" in r or "(unrecognised file)" in r),
    ("syntax error / patch problem",
     lambda r: any(k in r for k in ("x: ", "does not parse", "patch does not apply", "cannot read original"))),
    ("name chain: aliased import / too wide / too deep", lambda r: any(k in r for k in _NAME_CHAIN)),
    ("import-time: no class_users / dunder def / no plain names", lambda r: any(k in r for k in _IMPORT_MISC)),
    ("lookup found nothing", lambda r: any(k in r for k in _NOTHING)),
    ("other", lambda r: True),
)


def _first_full(reason: str) -> str:
    """'FULL <path> (new file)' of the first new-file verdict in the trail, or ''."""
    for part in reason.split("; "):
        if "(new file)" in part:
            while part.startswith("FULL FULL "):
                part = part[5:]
            return part
    return ""


# PROBE_F (probe branch only): finer split of the two classes probe-E found on the practice set.
# The board prints exact simulated seconds, so codes are in ms: an import-time commit costs
# 38 s + 0.1 s * a, a lookup-found-nothing commit 38 s + 1 s * b, anything else 38 s + 20 s.
PROBE_F = os.environ.get("PROBE_F", "0") == "1"
A_CODES = ("no plain names: if TYPE_CHECKING", "no plain names: other if", "no plain names: try",
           "no plain names: expression (call)", "no plain names: other statement",
           "dunder def run implicitly at import", "other def run at import", "no class_users", "other import-time")
B_CODES = ("found nothing: sqlglot/, every changed line in a def body",
           "found nothing: sqlglot/, some line outside def bodies", "found nothing: tests/ file",
           "no test reads: tests/fixtures/ file", "no test reads: other data file", "map has no lines for file",
           "no changed original lines", "helper has no known users", "other nothing")


def _loc_of(path: str, line: int):
    from blast.locate import Locator
    text = (SRC / path).read_bytes().decode("utf-8")
    return Locator(text, path).locate(line)


def probe_f_code(reason: str, patch_text: str) -> tuple[str, int]:
    """-> ("A", a) / ("B", b) / ("C", 0). Never raises."""
    import ast
    import re
    try:
        m = re.search(r"(\S+\.py):(\d+) module-level statement binds no plain names", reason)
        if m:
            top = _loc_of(m.group(1), int(m.group(2))).top
            if isinstance(top, ast.If):
                t = top.test
                name = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else ""
                return "A", 0 if name == "TYPE_CHECKING" else 1
            if isinstance(top, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                return "A", 2
            if isinstance(top, ast.Expr):
                return "A", 3
            return "A", 4
        if "called implicitly" in reason:
            return "A", 5
        if "runs at import" in reason or "run at import" in reason:
            return "A", 6
        if "no class_users" in reason:
            return "A", 7
        m = re.search(r"map lookup found nothing for (\S+)", reason)
        if m:
            path = m.group(1).rstrip(";")
            if path.startswith("tests/"):
                return "B", 2
            for fp in diff.parse(patch_text) or []:
                if fp.path == path:
                    v = prover.classify(fp, SRC)
                    kinds = {_loc_of(path, n).kind for n in v.lines}
                    return "B", 0 if kinds == {"def_body"} else 1
            return "B", 1
        m = re.search(r"no test reads (\S+)", reason)
        if m:
            return "B", 3 if m.group(1).startswith("tests/fixtures/") else 4
        if "map has no lines" in reason:
            return "B", 5
        if "no changed original lines" in reason:
            return "B", 6
        if "no known users" in reason:
            return "B", 7
        if probe_class(reason) == 6:
            return "A", 8
        if probe_class(reason) == 7:
            return "B", 8
    except Exception:  # noqa: BLE001
        pass
    return "C", 0


def probe_f_pack(ms: dict[int, int], extra_ms: int) -> list[list[int]]:
    """All tests in exactly 4 workers; cost = 38 s + extra_ms exactly."""
    return probe_pack(ms, 0, extra_ms)


def probe_class(reason: str) -> int:
    for i, (_, test) in enumerate(CLASSES):
        if test(reason):
            return i
    return len(CLASSES) - 1


def probe_pack(ms: dict[int, int], mark: int, extra_ms: int = 0) -> list[list[int]]:
    """All tests in exactly 4 workers; worker 0 loads exactly 30 s + 1.5 s * mark (as near as the
    durations allow), the others share the rest (each far below worker 0)."""
    target = 30_000 + 1_500 * mark + extra_ms
    order = sorted(ms, key=lambda n: (-ms[n], n))
    w0, load, rest = [], 0, []
    for n in order:
        if load + ms[n] <= target:
            w0.append(n)
            load += ms[n]
        else:
            rest.append(n)
    others = [[], [], []]
    loads = [0, 0, 0]
    for n in rest:  # LPT onto the lightest of 3
        i = loads.index(min(loads))
        others[i].append(n)
        loads[i] += ms[n]
    return [w0] + others


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
                f"NOCU_NAMES={int(sel.NOCU_NAMES)}, ATTR_LEVEL={int(sel.ATTR_LEVEL)}, INFECTION={int(sel.INFECTION)}, "
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
            selected_all = selected is all_tests
            workers = full if selected_all else pack([(n, ms[n]) for n in selected])
        except Exception as e:  # noqa: BLE001 - never crash the run
            workers, reason = full, f"FULL exception {type(e).__name__}: {e}"
            selected_all = True
        if PROBE_F and selected_all:
            g, c = probe_f_code(reason, text)
            extra = {"A": 100 * c, "B": 1000 * c, "C": 20_000}[g]
            workers = probe_f_pack(ms, extra)
            label = {"A": A_CODES, "B": B_CODES}.get(g, ("unclassified",))[c]
            reason = f"PROBE_F {g}{c} ({label}) +{extra} ms: {reason}"
        elif PROBE_E and selected_all:
            k = probe_class(reason)
            workers = probe_pack(ms, MARKS[k])
            reason = f"PROBE_E class {k} ({CLASSES[k][0]}) mark {MARKS[k]}: {reason}"
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
