# mapgen: the labelling environment (Person A)

Rebuilds the environment the organisers labelled commits in, so we can run
sqlglot's real tests to build maps. Nothing here ships in the submitted image.

- `Dockerfile.mapgen`: the pinned `python:3.13-slim` digest, sqlglot at
  `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` in `/opt/sqlglot` (downloaded as a
  tarball, no git or apt), `requirements.lock`, plus `coverage` for map building.
  `SKIP_INTEGRATION=0`, `PYTHONPATH=/opt/sqlglot`, `COVERAGE_CORE=sysmon`.
- `run_baseline.sh`: runs inside the container. Full test suite, one process
  (no xdist), JUnit XML to `/out/baseline.xml`, wall-clock to `/out/baseline_time.txt`.
- `build_and_baseline.sh`: builds the image for linux/amd64 and runs the
  baseline with `./mapgen_out` mounted at `/out`.

Check that our test ids match the organisers':

```sh
sh mapgen/build_and_baseline.sh
python3 tools/compare_ids.py mapgen_out/baseline.xml sample/tests.json
```

## Stage A2: the coverage map

```sh
sh mapgen/make_map.sh                       # full suite -> maps/coverage_map.json.gz
sh mapgen/make_map.sh tests/test_optimizer.py   # quick slice -> mapgen_out/slice_map.json.gz
python3 mapgen/sanity_check.py              # the guide's three checks
```

- `blast_plugin.py` (pytest plugin): coverage with one dynamic context per test
  node id, `files_read` (non-.py files opened during a test), `class_users`
  (wraps `__init__` of `Expr`, `Dialect`, `Tokenizer`, `Parser`, `Generator`,
  `Schema`, ... and every subclass with its own `__init__`; Tokenizer / Parser /
  Generator also credit their `dialect`), and failures. It imports every sqlglot
  module before tests run, so lazily loaded dialects count as import-time lines
  instead of being credited to the first test that loaded them.
- `export_map.py`: turns the raw data into the contract format, fails if > 12 MB.
- **`make_map.sh` sets `COVERAGE_CORE=ctrace`, overriding the image's `sysmon`.**
  coverage 7.17 warns "Dynamic contexts aren't supported with core=sysmon": with
  sysmon, each line is credited only to the first test that ran it, which would
  make the map wrong. ctrace is slower but correct.
- `sanity_check.py` needs `.cache/sqlglot` (a copy of `/opt/sqlglot` from the image).

`mapgen_out/` and `.cache/` are git-ignored.
