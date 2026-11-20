# Blast Radius — Person B: "The Selector & Prover"

> **How to use this file:** open Claude Code in the team repo, and paste the
> **"Paste into Claude Code"** block of the stage you're on. Do the stages in order.
> Your partner (Person A) is working at the same time from `GUIDE_A_MapMaker.md`.
> You two only meet at the **Contract** below — keep to it exactly and nothing clashes.

---

## 0. The 60-second picture (read once)

We're competing in the RWX "Blast Radius" track. For each code change (patch) to
sqlglot, our program prints which tests to run and how to split them over ≤ 8 workers.

**Score per commit = slowest worker's test seconds + 2 s per worker + 300 s if we skip any
test that fails** (+ our CPU time). Lowest total wins. Only 1st place pays.

**Facts measured from the starter kit (sample set, 30 commits):**
- 1,620 tests, **103 s** total. `tests/test_parser.py::TestParser::test_max_nodes` alone takes **28.8 s**;
  `test_executor.py::TestExecutor::test_execute_tpcds` 21.5 s; `test_generator.py::TestGenerator::test_generator_memory` 6.7 s.
- So "run everything" is cheapest on **4 workers (36.8 s/commit)**. 8 workers would be 44.8 s.
  **The best worker count depends on the selection — we compute it every time.**
- **12 of 30 commits break nothing** → `[]` costs 0. Three are comment-only edits.
- Kinds: `code` 16, `fixture` 8 (edits to `tests/fixtures/optimizer/*.sql`), `comment` 3, `multi` 3.
- Every fixture commit broke exactly 1 test — the test that reads that fixture file.
- Perfect oracle ≈ **94 s** on the sample. Run-everything ≈ 1,104 s.

**Our plan — you own everything that ships:**
1. **Prover:** prove a change can't matter (comments/whitespace only → identical AST) → `[]`.
2. **Map-driven selection:** look up changed lines in Person A's map → only the tests that touch them.
3. **Packer:** split the chosen tests over the cheapest number of workers.
4. **Safety net:** anything we can't explain → run everything on the best worker count. A miss (300 s) costs ~8 full runs.

---

## 1. Rules you must not break (from README/SCORING)

- Output: **exactly one JSON line per commit on stdout**, `{"commit": "<id>", "workers": [[...], ...]}`.
  Logs go to **stderr only**. Anything else on stdout, or > 2 MB, rejects the run. No test twice, ≤ 8 non-empty workers.
- Runtime: no network, 2 cores, 4 GB, **3 min for the whole batch** (4.5 min in the final, ~1.5× commits),
  read-only disk, `/tmp` 64 MB, user `nobody`, `HOME=/tmp`. Files in the image must be readable by `nobody`.
- Build: network, 4 cores, 8 GB, 10 min. Download only from PyPI / npm / Docker Hub / GitHub releases at a
  fixed tag / **sqlglot's repo at the pinned commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`**. No apt.
- **No lookup table from a patch or a kind of edit to the tests it breaks.** Maps from the pinned commit are fine.
  Our mutant practice set (from Person A) is for **measuring only** — never in the image, never read by the selector.
- "A change that only adds a comment is not promised to be harmless" — that's why the Prover compares
  ASTs instead of trusting "the diff only adds a `#` line".
- Repo ≤ 20 MB; keep the repo public and **never delete or rewrite pushed commits** (the final rebuilds the chosen commit).
- Ties in the final (within 1 s) go to the **earliest submission** → submit every real improvement as soon as it works.

---

## 2. THE CONTRACT (shared with Person A — do not change without telling them)

Person A delivers **`maps/coverage_map.json.gz`** (gzip JSON):

```jsonc
{
  "version": 1,
  "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
  "tests": ["tests/test_parser.py::TestParser::test_union", "..."],   // index = test index; ids match tests.json "id"
  "sets": [[0, 4, 17], [5]],                                          // unique sorted lists of test indexes
  "lines": { "sqlglot/parser.py": { "812": 3 } },                     // lines run DURING tests -> set index (sqlglot/** and tests/**)
  "import_lines": { "sqlglot/parser.py": [1, 2, 5, 40] },             // lines run outside any test (import time)
  "files_read": { "tests/fixtures/optimizer/simplify.sql": 12 },      // non-.py files opened during tests -> set index
  "class_users": { "sqlglot.dialects.tsql:TSQL": 7 },                 // "module:qualname" (MRO-expanded) -> set index
  "failed_at_baseline": [],
  "meta": {}
}
```
- Paths are repo-relative with `/`. Convert ids → numbers with `/data/tests.json` at runtime (never hard-code numbers).
- **Until A delivers, create `maps/stub_map.json.gz`** with the same keys and empty values; the selector loads
  `coverage_map.json.gz` if it exists, else the stub. **Any lookup that finds nothing → FULL run.** That keeps
  every version safe.

### File ownership (prevents merge conflicts)
| You (B) own | Person A owns | Nobody edits |
|---|---|---|
| `selector.py`, `blast/**`, `Dockerfile`, `Makefile`, `tools/eval_sample.py`, `README.md`, `maps/stub_map.json.gz` | `mapgen/**`, `maps/coverage_map.json.gz`, `tools/check_map.py`, `tools/mutants/**` | `score.py`, `sample/**`, `SCORING.md`, `requirements.lock` |

Your branch: **`selector`**. You're also the **submitter** (you merge into `main` and submit).

---

## 3. Before you start (10 min)

**On Windows:** use **WSL2 (Ubuntu)** + Docker Desktop (WSL backend). Clone inside WSL (`~/blast-radius`),
**not** in OneDrive. Run once: `git config --global core.autocrlf input`. `make` lives in WSL (`sudo apt install make`).

```sh
git clone https://github.com/<team>/<repo>.git ~/blast-radius
cd ~/blast-radius
git checkout -b selector
make sample        # should print roughly the starter score — proves Docker works
code .
```

---

## Stage B1 — Quick win tonight: right worker count + clean structure (≈ 45 min)

**Paste into Claude Code:**
```
Context: RWX "Blast Radius" hackathon repo (starter kit at root). Read README.md and SCORING.md first.
I am Person B; I own selector.py, blast/**, Dockerfile, Makefile, tools/eval_sample.py, README.md.
Do NOT edit score.py, sample/**, SCORING.md, requirements.lock, mapgen/**, tools/mutants/**.

Stage B1:
1. Create package blast/ with:
   - blast/pack.py: pack(tests: list[(number, ms)]) -> list[list[int]]. Durations in whole ms =
     round(seconds*1000), like SCORING.md. For k in 1..8 run LPT (longest first onto the lightest
     worker), cost = max load + 2000*k; return the cheapest. Empty input -> [] (cost 0). Add a
     cost() helper that matches SCORING.md exactly.
   - blast/diff.py: parse a unified diff (git style) into FilePatch objects: old_path, new_path,
     is_new, is_deleted, is_binary, is_rename, hunks (old_start, old_len, new_start, new_len, lines
     with ' ', '-', '+', and handle "\ No newline at end of file"). Robust to odd input: if parsing
     fails, return a marker that forces a FULL run.
   - blast/log.py: log(*args) -> stderr only.
2. Rewrite selector.py: read /data/tests.json and every /data/commits/*.patch (sorted), for now select
   ALL tests for every commit, pack with blast/pack.py, print one JSON line per commit, flush at end.
   Wrap each commit in try/except: on ANY exception, log it and fall back to the FULL selection, so
   one weird patch can never crash the run.
3. Update Dockerfile: keep the exact base image digest; COPY selector.py and blast/ to /app; RUN
   chmod -R a+rX /app. Keep CMD python /app/selector.py.
4. tools/eval_sample.py: runs our selector on the host against ./sample (env var DATA_DIR overrides
   /data, add that to selector.py), then scores with score.py logic and prints, per commit: kind,
   #selected, cost, MISS?, and the reason trace the selector logged. Also print the total.
Run `make sample` and show the score. Expected about 30 x 36.8 = ~1104 with 0 misses.
```

**Then submit v0.1 tonight** (see "Merging & submitting" in chat): merge `selector` → `main`, push,
submit on the track page. Note the commit hash and score in `SUBMISSIONS.md`.

---

## Stage B2 — The Prover (≈ 1.5–2 h)

**Why it's an edge:** ~40% of commits break nothing. Comment-only edits are free points, but the
organisers warn a "comment" isn't always harmless — a `#` inside a string, a broken line continuation,
an edit that's really code. Comparing **ASTs** catches every one of those for certain: if the parsed
program is identical, the behaviour is identical.

**Paste into Claude Code:**
```
Stage B2: the Prover.
1. Dockerfile: download sqlglot source at the pinned commit (no apt/git):
   ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tgz
   extract with Python tarfile (filter="data") to /opt/sqlglot, delete the tarball, chmod -R a+rX.
   selector.py gets SQLGLOT_SRC env (default /opt/sqlglot). For local runs, tools/eval_sample.py
   should download/extract the same tarball once into .cache/ (git-ignored).
2. blast/apply.py: apply a FilePatch's hunks to the original file text -> new text. Verify every
   context and '-' line matches the original exactly; if not, raise (-> FULL run). No /tmp writes
   needed, do it in memory.
3. blast/prover.py: for each .py FilePatch:
   - new text fails to parse (SyntaxError) -> verdict FULL (the module can't import; many tests fail).
   - ast.dump(original, include_attributes=False) == ast.dump(new, include_attributes=False)
     -> verdict NOOP (contributes no tests).
   - otherwise -> verdict CHANGED, plus the list of changed ORIGINAL line numbers: removed lines'
     numbers, and for pure insertions the original line just before and just after.
   Non-.py files: verdict DATA (fixtures etc.), except clearly unknown/structural files
   (pyproject.toml, setup.*, sqlglotrs/**, anything we don't recognise) -> FULL.
   New files or deleted files -> FULL for now.
   Commit verdict: all files NOOP -> workers []. Any FULL -> everything.
4. Fixture fallback until the map arrives: for a DATA file under tests/fixtures/, find test functions
   in /opt/sqlglot/tests/**/*.py whose source text contains the fixture's file name or stem (use ast
   to get each test function's source segment; build ids as "tests/...::Class::test_name" and keep
   only ids present in tests.json). If none found -> FULL. Log the rule used.
5. Log a one-line reason per commit to stderr, e.g. "9u5k NOOP sqlglot/generators/sqlite.py (AST equal)".
Run tools/eval_sample.py and make sample. Expected: the 3 comment commits -> [] with 0 misses,
fixture commits -> 1-3 tests each, total well under 800 with 0 misses.
```

**Submit v0.2** as soon as `make sample` shows 0 misses.

---

## Stage B3 — Map-driven selection, built against the stub (≈ 2–3 h, overlaps A2)

You don't wait for A's map: build against the contract with the stub, then plug A's map in.

**Paste into Claude Code:**
```
Stage B3: map-driven selection. Contract:
[PASTE SECTION "2. THE CONTRACT" FROM GUIDE_B_Selector.md HERE]
1. Create maps/stub_map.json.gz (same keys, empty). blast/mapdb.py loads maps/coverage_map.json.gz if
   present else the stub (gzip+json, once), converts test ids -> numbers via tests.json (ids missing
   from tests.json are ignored; tests.json tests missing from the map are noted in a set
   UNMAPPED). Lazy, fast: total selector CPU must stay under ~2 s for 45 commits.
2. blast/locate.py: for an ORIGINAL .py file and a line number, using ast (cache per file):
   the smallest enclosing statement (start/end lines), the enclosing def (if any), the enclosing class
   chain and its "module:qualname" (module from the path, e.g. sqlglot/parsers/dremio.py ->
   sqlglot.parsers.dremio), and whether the line is a def header/decorator/default argument.
3. blast/select.py: for each CHANGED .py file, per changed line:
   a) Use ALL lines of the smallest enclosing statement (coverage records multi-line statements on
      various lines). Tests = union of map "lines" for those lines.
   b) If any of those lines is in import_lines, or is a class-body line, or a def header/decorator/
      default: the effect happens at import time -> add class_users of the innermost enclosing class
      (and of every enclosing class). If the line is at module level (not in any class or def), find
      the names it assigns/imports, and select tests covering any line in sqlglot/** that references
      those names (build that name index at docker BUILD time with a script blast/build_index.py,
      store /app/name_index.json.gz). Can't resolve -> FULL.
   c) If a changed line is inside a def but no test executed any line of that statement AND the def
      is not executed at import: nobody runs it -> contributes nothing (log "DEAD").
      SAFETY KNOB (env FUNC_LEVEL=1, default ON for now): instead use every line of the enclosing def.
   d) Changes in tests/*.py: lines inside a test function -> that test (via "lines"); module-level
      or helper changes -> all tests in that file plus tests covering that helper.
   e) DATA files: files_read; nothing found -> the B2 grep fallback; still nothing -> FULL.
   f) Always add tests in "failed_at_baseline" and UNMAPPED tests that live in a file we selected
      anything from (cheap insurance).
   Map lookup finds nothing at all for a CHANGED file -> FULL (never guess empty on code changes).
4. Union across files; pack; print. Log the rule path per commit.
With the stub map every CHANGED commit must still fall back to FULL (0 misses). Show eval_sample.
```

---

## Stage B4 — Plug in A's map, measure, submit v1 (≈ 1 h, when A says "map pushed")

```sh
git fetch origin && git merge origin/maps     # brings in maps/coverage_map.json.gz (no conflicts: different files)
```

**Paste into Claude Code:**
```
Stage B4: A's real map is now at maps/coverage_map.json.gz. Run tools/eval_sample.py and make sample.
For every MISS: print the patch, the changed lines, what each rule found, and the failing tests'
relationship to the changed code (look at the sqlglot source in .cache/). Classify each miss as
MAP GAP (tell me what to send Person A) or RULE GAP (fix it in blast/select.py). Fix rule gaps, rerun.
Also report: total selector CPU time (time the docker run), image size, and that Dockerfile COPYs
maps/ and the map is readable by user nobody.
```

**Submit v1.0** as soon as 0 misses on the sample. Record it in `SUBMISSIONS.md`.

---

## Stage B5 — Tune on the mutant army (Saturday night → Sunday)

When A sends `mutants_out.zip` (via chat/Drive — **never commit it**):

```sh
unzip mutants_out.zip && make sample SAMPLE=$(pwd)/mutants_out
```

**Paste into Claude Code:**
```
Stage B5: mutants_out/ is a private, locally-labelled practice set in the same format as sample/.
It is for MEASURING ONLY: the selector must never read it and the Dockerfile must never copy it.
Add mutants_out/ to .gitignore and .dockerignore.
1. Run eval on mutants_out with FUNC_LEVEL=1 and FUNC_LEVEL=0. Report total, misses, and for each miss
   the rule path and whether it's a MAP GAP or RULE GAP.
2. Expected-cost check: for each knob setting, compute total + 300*misses. Pick the setting with the
   lowest total on mutants_out AND 0 misses on sample/. If any kind of change has repeated misses,
   make that kind fall back to FULL (cheap insurance: one full run = 36.8 s, one miss = 300 s).
3. Profile the selector: total CPU for 45 commits must stay < 2 s.
```

**Final-day rules for you:**
- Submit whenever the total drops by > 1 s with 0 sample misses (earliest submission wins ties).
- Freeze features **Sun 14:00 UTC**. Last submission by **Sun 15:00 UTC** (cutoff 16:00 UTC).
- Before the last submission: `make check`, `make sample`, `make sample SAMPLE=$(pwd)/mutants_out`,
  confirm the repo is public and the commit is pushed to `main`.
- On the track page, pick the commit you trust most (lowest mutant total with 0–1 misses), not
  necessarily the one with the best practice score.

## Your checklist
- [ ] B1 packer + v0.1 submitted tonight
- [ ] B2 Prover + v0.2 submitted
- [ ] B3 selection rules working with stub (still 0 misses)
- [ ] B4 A's map plugged in, v1.0 submitted
- [ ] B5 tuned on mutants, final submission by Sun 15:00 UTC
