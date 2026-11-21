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

`mapgen_out/` is git-ignored.
