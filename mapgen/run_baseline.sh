#!/bin/sh
# Runs inside the mapgen container. Full sqlglot test suite on the clean pinned
# commit, single process (no xdist), results as JUnit XML in /out.
set -u
cd /opt/sqlglot
start=$(date +%s)
python -m pytest tests -p no:cacheprovider -q -rfE --junitxml=/out/baseline.xml
status=$?
end=$(date +%s)
echo "baseline wall-clock: $((end - start)) s (pytest exit $status)" | tee /out/baseline_time.txt
exit $status
