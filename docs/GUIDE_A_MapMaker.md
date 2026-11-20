# Blast Radius — Person A: "The Map Maker"

> **How to use this file:** open Claude Code in the team repo, and paste the
> **"Paste into Claude Code"** block of the stage you're on. Do the stages in order.
> Your partner (Person B) is working at the same time from `GUIDE_B_Selector.md`.
> You two only meet at the **Contract** below — keep to it exactly and nothing clashes.

---

## 0. The 60-second picture (read once)

We're competing in the RWX "Blast Radius" track. For each code change (patch) to
sqlglot, our program prints which tests to run and how to split them over ≤ 8 workers.

**Score per commit = slowest worker's test seconds + 2 s per worker + 300 s if we skip any
test that fails** (+ our CPU time). Lowest total wins. Only 1st place pays.

**Facts measured from the starter kit (sample set, 30 commits):**
- 1,620 tests, **103 s** total. One test, `tests/test_parser.py::TestParser::test_max_nodes`,
  takes **28.8 s** alone; `test_executor.py::TestExecutor::test_execute_tpcds` takes 21.5 s.
- Because of that 28.8 s test, "run everything" is cheapest on **4 workers (36.8 s/commit)**, not 8.
- **12 of 30 commits break nothing** → the right answer is `[]` (cost 0).
- Commit kinds: `code` 16, `fixture` 8 (edits to `tests/fixtures/optimizer/*.sql`), `comment` 3, `multi` 3.
- Every fixture commit broke exactly 1 test — the test that reads that fixture file.
- A perfect oracle would score **~94 s** on the sample. Running everything scores ~1,104 s. The gap is the prize.

**Our plan (two layers in one program):**
1. **Prover (Person B):** prove a change can't matter (e.g. comments only) → `[]`.
2. **Map Maker (YOU):** for everything else, a map of "which tests execute this line / read this
   file / use this class", built once from the pinned sqlglot commit and committed to the repo.

**Your job:** everything that *runs sqlglot's real tests*: the coverage map, and later our private
"mutant army" practice set. Person B owns everything that ships in the final selector.

---

## 1. Rules you must not break (from README/SCORING)

- Pinned sqlglot commit: **`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`**. Maps must be built from it.
- **Allowed:** maps built from the pinned commit (coverage, imports, fixtures).
  **Banned:** any table from a patch, or a *kind of edit*, to the tests it breaks.
  → The mutant practice set you build in Stage A5 is **for measuring only**. It must never be
  shipped in the image or read by the selector. Keep it out of git (`.gitignore`).
- Build may download only from PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and
  sqlglot's repo at the pinned commit. Nothing from an address we control.
- Repo ≤ **20 MB**. No Git LFS, no submodules. Maps are plain committed files.
- Label environment: image `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`,
  `pip install -r requirements.lock`, sqlglot checkout on `PYTHONPATH`, **`SKIP_INTEGRATION=0`**.
- pytest 9: a test can print `PASSED` next to `SUBFAILED` lines — that test **failed**.
- Scorer is linux/amd64 → always `--platform linux/amd64`.

---

## 2. THE CONTRACT (shared with Person B — do not change without telling them)

You deliver one file: **`maps/coverage_map.json.gz`** (gzip-compressed JSON, UTF-8):

```jsonc
{
  "version": 1,
  "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",

  // Every test that ran, as its pytest node id, EXACTLY the format of tests.json "id",
  // e.g. "tests/test_parser.py::TestParser::test_union". Index in this list = "test index".
  "tests": ["tests/...::...", "..."],

  // Unique sorted lists of test indexes. Everything below points into this list (dedup = small file).
  "sets": [[0, 4, 17], [5], "..."],

  // Lines executed WHILE A TEST RAN (setup/call/teardown). Repo-relative path -> line number (as string) -> set index.
  // Covers sqlglot/** AND tests/** .py files.
  "lines": { "sqlglot/parser.py": { "812": 3, "813": 3 } },

  // Lines executed OUTSIDE any test (import / collection time). Path -> sorted list of line numbers.
  // A line can appear both here and in "lines".
  "import_lines": { "sqlglot/parser.py": [1, 2, 5, 40] },

  // Non-.py files under the sqlglot checkout that were opened while a test ran. Path -> set index.
  "files_read": { "tests/fixtures/optimizer/simplify.sql": 12 },

  // Classes instantiated (directly, or as a base class in the instance's MRO) while a test ran.
  // Key "module:qualname" -> set index. Includes each Parser/Generator/Tokenizer's dialect class MRO too.
  "class_users": {
    "sqlglot.dialects.tsql:TSQL": 7,
    "sqlglot.parsers.dremio:DremioParser": 8,
    "sqlglot.expressions.ddl:TriggerProperties": 9
  },

  // Tests that failed or errored on the CLEAN pinned commit (should be empty; B treats them carefully).
  "failed_at_baseline": [],

  // Free-form stats for humans: counts, build time, coverage tool version.
  "meta": {}
}
```

Rules of the contract:
- Paths are **repo-relative with forward slashes** (`sqlglot/...`, `tests/...`).
- B's selector maps node ids → test numbers using `/data/tests.json` at runtime, so **ids must match tests.json exactly**.
- Target size: **≤ 12 MB gzipped** (repo limit is 20 MB total).
- Until you deliver, B uses an empty stub with the same keys. When something can't be found in your map,
  B falls back to "run all tests". So **missing data is safe, but wrong data causes misses**. Prefer over-inclusion.

### File ownership (prevents merge conflicts)
| You (A) own | Person B owns | Nobody edits |
|---|---|---|
| `mapgen/**`, `maps/**`, `tools/check_map.py`, `tools/mutants/**`, `.gitignore` lines for `mutants_out/` | `selector.py`, `blast/**`, `Dockerfile`, `Makefile`, `tools/eval_sample.py`, `README.md` | `score.py`, `sample/**`, `SCORING.md`, `requirements.lock` |

Your branch: **`maps`**. Commit small and often. Open a PR into `main` when a stage is done.

---

## 3. Before you start (10 min)

**On Windows:** use **WSL2 (Ubuntu)** + Docker Desktop with the WSL backend. Clone the repo
*inside* WSL (`~/blast-radius`), **not** in OneDrive — OneDrive + Docker is slow and corrupts line endings.
Run once: `git config --global core.autocrlf input`.

```sh
git clone https://github.com/<team>/<repo>.git ~/blast-radius
cd ~/blast-radius
git checkout -b maps
code .            # opens VS Code; then start Claude Code in this folder
```

---

## Stage A1 — Rebuild the labelling environment (≈ 30–45 min)

**Goal:** a Docker image that runs sqlglot's tests exactly like the organisers did, and proof that
our test ids match `sample/tests.json`.

**Paste into Claude Code:**
```
Context: we're in the RWX "Blast Radius" hackathon repo (starter kit at the root: README.md,
SCORING.md, score.py, selector.py, Dockerfile, Makefile, requirements.lock, sample/). Read README.md
and SCORING.md first. I am Person A, I own mapgen/**, maps/**, tools/check_map.py, tools/mutants/**.
Do not edit selector.py, blast/**, Dockerfile, Makefile, score.py, sample/**.

Task (Stage A1): create mapgen/Dockerfile.mapgen and mapgen/README.md.
- Base image EXACTLY: python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
- Download sqlglot at the pinned commit WITHOUT apt/git, e.g.
  ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tgz
  then extract with Python tarfile (filter="data") to /opt/sqlglot.
- pip install -r requirements.lock (copy it in), plus coverage (pin a recent 7.x) for map building.
- ENV SKIP_INTEGRATION=0, PYTHONPATH=/opt/sqlglot, COVERAGE_CORE=sysmon, WORKDIR /opt/sqlglot.
- Add mapgen/run_baseline.sh that runs inside the container:
  python -m pytest tests -p no:cacheprovider -q -rfE --junitxml=/out/baseline.xml
  (mount ./mapgen_out to /out). Do NOT use xdist for this.
- Add a Makefile-free helper script mapgen/build_and_baseline.sh that builds with
  --platform linux/amd64 and runs the baseline, mounting ./mapgen_out.
- Add mapgen_out/ to .gitignore.
- Write tools/compare_ids.py: parse mapgen_out/baseline.xml, build node ids in the exact format
  of sample/tests.json "id" (e.g. tests/test_parser.py::TestParser::test_union), and report:
  how many tests.json ids were found, missing, extra, plus any failures/errors at baseline.
  Treat "skipped" key in tests.json as tests that are skipped in their environment.
Then run it and show me the report. If ids don't match, fix the id building until they do.
```

**Done when:** `compare_ids.py` shows ~all 1,620 ids found and ~0 baseline failures. Note the
wall-clock time of the full run (you'll need it).
**Commit:** `git add mapgen tools .gitignore && git commit -m "A1: labelling env + id check" && git push -u origin maps`

---

## Stage A2 — The coverage map, built smarter than anyone else's (≈ 1.5–2.5 h)

**Why this is our edge:** plain per-test coverage is blind to the most common kind of change in
sqlglot — edits to class-level tables like `TRANSFORMS = {...}`, `FUNCTIONS = {...}`,
`NUMERIC_ESCAPES = {...}`, `WEEK_OFFSET = -1`. Those lines run once at *import time*, so plain
coverage says either "every test" (useless) or "no test" (dangerous). We add two extra maps that fix
this: **`class_users`** (which tests actually used the TSQL dialect / the DremioParser / the
TriggerProperties expression) and **`files_read`** (which tests opened which fixture file).
Most teams will only do line coverage.

**Paste into Claude Code:**
```
Stage A2. Build the map described in the CONTRACT below and write it to maps/coverage_map.json.gz.
[PASTE SECTION "2. THE CONTRACT" FROM GUIDE_A_MapMaker.md HERE]

Implementation plan (inside the mapgen container from A1):
1. mapgen/blast_plugin.py — a pytest plugin loaded with `-p blast_plugin` (put mapgen/ on PYTHONPATH).
   - Start coverage.Coverage(source=["/opt/sqlglot/sqlglot", "/opt/sqlglot/tests"], branch=False)
     as early as possible (at plugin import / pytest_load_initial_conftests) so import-time lines are
     recorded under the empty context "".
   - Hookwrapper on pytest_runtest_protocol: cov.switch_context(item.nodeid) before, switch back to ""
     after. Make sure node ids match tests.json format (reuse the logic from tools/compare_ids.py).
   - files_read: while a test runs, wrap builtins.open and io.open (and check pathlib.Path.open /
     read_text actually go through them on Python 3.13) and record any opened path under /opt/sqlglot
     that is NOT a .py file. Store repo-relative.
   - class_users: after sqlglot is imported, wrap __init__ of the base classes: find them in this
     sqlglot version (the expression base class may be called Expr or Expression — check
     sqlglot/expressions/), Dialect, Tokenizer, Parser, Generator, plus optimizer/annotator, Schema
     and executor base classes if they exist. For speed, only add type(self) to a per-test set inside
     the wrapper; expand each type's full MRO into "module:qualname" keys at test end. For
     Parser/Generator/Tokenizer instances also add type(self.dialect).__mro__ if that attribute exists.
     Check whether Dialect instances are cached between tests (if so, the dialect would only be
     credited to the first test — that's why we also credit dialects via Parser/Generator/Tokenizer).
   - Also record failures/errors per test (treat SUBFAILED as failed) for "failed_at_baseline".
2. Run the full suite serially with the plugin: SKIP_INTEGRATION=0, -p no:cacheprovider,
   disable pytest-timeout (-p no:timeout or --timeout=0) because coverage slows tests down.
3. mapgen/export_map.py: read the coverage data (CoverageData.contexts_by_lineno per file), build
   "lines" (non-empty contexts) and "import_lines" (empty context), dedupe test sets into "sets",
   attach files_read / class_users / failed_at_baseline / meta, write gzip JSON. Print the size.
   Fail loudly if > 12 MB gzipped (if too big, first drop lines whose set is "every test" into a
   special marker... ask me before changing the contract).
4. One command to do it all: mapgen/make_map.sh (build image, run, export into maps/).
Run it end to end and show me: number of tests in map vs tests.json, file size, top 10 biggest
class_users entries, and the files_read entry for tests/fixtures/optimizer/simplify.sql.
```

**Sanity checks to ask Claude Code for (paste after it finishes):**
```
Sanity-check the map: (1) class_users for "sqlglot.dialects.tsql:TSQL" should include
tests/test_optimizer.py::TestOptimizer::test_simplify (the sample says a TSQL WEEK_OFFSET change breaks
it). (2) files_read for tests/fixtures/optimizer/pushdown_predicates.sql should include
tests/test_optimizer.py::TestOptimizer::test_pushdown_predicates. (3) lines for
sqlglot/parsers/dremio.py around the REGEXP_SPLIT entry should lead (via lines or class_users of
DremioParser) to tests/dialects/test_dremio.py::TestDremio::test_regexp_split. Report each.
```

**Done when:** map exists, < 12 MB, the three checks pass.
**Commit + push**, then **message Person B: "map v1 pushed on `maps`"** — they're waiting for it.

---

## Stage A3 — Prove the map against the sample (≈ 45 min)

**Goal:** give B hard numbers on how good the map is before they wire it in.

**Paste into Claude Code:**
```
Stage A3. Write tools/check_map.py (dev-only, never shipped). For every sample/commits/*.patch:
parse the unified diff; for each changed .py file, take the ORIGINAL line numbers of removed lines,
and for pure insertions the original lines just before and after; look them up in "lines"; if a
line is only in import_lines, use the enclosing class (find it with ast on /opt/sqlglot source, or a
local copy of sqlglot at the pinned commit) and look up class_users for "module:qualname"; for
non-.py files use files_read. Union the tests. Then compare with sample/answers.json and print a
table: commit, kind, #selected, selected seconds, #failing, MISS yes/no, and which failing tests were
missed. End with the total misses and the total "selected seconds".
This is a naive selector just to grade the MAP, not our real selector.
```

**Done when:** you have the table. Paste the table to Person B (or commit it as `maps/CHECK.md`).
Every MISS is a map gap — fix the map (Stage A2 code) before moving on, unless it's a case only the
Prover/selector logic can handle (tell B which ones).

---

## Stage A4 — Mutant army: our private 300-commit practice set (≈ 2–3 h, Saturday)

**Why:** 30 sample commits is too few to know our miss rate, and the final has "kinds of change the
practice set never had". A miss costs 300 s, so knowing our miss rate is worth more than anything.
We make our own patches that look like the sample (one-token bugs, comment-only edits, fixture edits,
multi-file combos), label them by running the REAL tests, and grade our selector on them.
**This is for measuring only — never shipped, never read by the selector, kept out of git.**

**Paste into Claude Code:**
```
Stage A4. Build tools/mutants/ (dev-only; output goes to mutants_out/, which is git-ignored and
NEVER copied into the submission Docker image).
1. tools/mutants/make_mutants.py --n 300 --seed 1: generate patches against sqlglot at the pinned
   commit, in the SAME format as sample/commits/*.patch (git-style "diff --git a/... b/...", index line,
   ---/+++ headers, 3 lines of context). Study the 30 sample patches first and imitate their mix:
   - one-token code mutations in sqlglot/**: and<->or, ==<->is, < <-> <=, True<->False, +1/-1 on ints,
     index 0<->1, swapped dict values, changed string literal, deleted "not";
   - comment-only insertions (code and module level, including at top of file);
   - fixture edits in tests/fixtures/** (one token changed in a SQL line);
   - "multi": 2–3 of the above in different files;
   - NEW kinds the sample doesn't have: docstring edits, edits in tests/*.py, a new blank line, a
     syntax error, changes to non-Python files at the repo root, an edit to a function only used at
     import time.
   Skip mutations that produce identical files.
2. tools/mutants/label.py: for each patch, copy sqlglot to a temp dir, apply it, run the full suite in
   the mapgen container (pytest-xdist -n auto is fine here, SKIP_INTEGRATION=0, treat SUBFAILED and
   import errors as failures), and write mutants_out/answers.json in the EXACT format of
   sample/answers.json, plus mutants_out/commits/*.patch and a copy of sample/tests.json, so that
   `make sample SAMPLE=$(pwd)/mutants_out` works with the kit's Makefile.
   Make it resumable (skip already-labelled patches) and print progress + ETA.
Start with --n 20 to test, then run the full 300 in the background.
```

**Done when:** `mutants_out/` has labelled answers. Tell B: "run `make sample SAMPLE=$(pwd)/mutants_out`".
Zip `mutants_out/` and send it to B directly (Drive/WhatsApp), **not via git**.

---

## Stage A5 — Close the gaps (Saturday evening → Sunday morning)

Every miss B finds on the mutant set is either a **map gap** (yours) or a **rule gap** (B's).
For yours, typical fixes:
- functions that only run at import (e.g. helpers called inside class bodies) → make sure their lines
  land in `import_lines` and B's class lookup catches them;
- tests whose id is missing from the map → find out why (collection error? parametrised id format?);
- `class_users` too small because of caching → credit the dialect from more places.

**Paste into Claude Code (each time B sends you a miss):**
```
Our selector missed this mutant: <patch file> — failing tests <ids> were not selected. Using the map
and the sqlglot source, explain exactly why the map didn't connect the changed line(s) to those
tests, then fix mapgen so it does (prefer over-inclusion), rebuild the map, and rerun
tools/check_map.py on both sample/ and mutants_out/ to show nothing got worse.
```

Push each improved map to `maps` and tell B. **Stop changing the map by Sunday 13:00 UTC** so B
can do the final submission calmly before the 16:00 UTC cutoff.

---

## Your checklist
- [ ] A1 env + ids match
- [ ] A2 map pushed (tell B)
- [ ] A3 map graded on sample, gaps fixed
- [ ] A4 mutant army labelled, zip sent to B (not in git)
- [ ] A5 gaps closed, last map by Sun 13:00 UTC
