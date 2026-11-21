#!/bin/sh
# Build maps/coverage_map.json.gz from the pinned sqlglot commit. From the repo root:
#   sh mapgen/make_map.sh                 # full suite (the real map)
#   sh mapgen/make_map.sh tests/test_optimizer.py   # quick slice, writes mapgen_out/slice_map.json.gz
set -eu
cd "$(dirname "$0")/.."
IMAGE=blast-radius-mapgen
mkdir -p mapgen_out/raw maps
docker build -q --platform linux/amd64 -f mapgen/Dockerfile.mapgen -t "$IMAGE" . >/dev/null

if [ $# -gt 0 ]; then TARGETS="$*"; OUTFILE=/out/slice_map.json.gz; else TARGETS=tests; OUTFILE=/maps/coverage_map.json.gz; fi
rm -f mapgen_out/raw/.coverage* mapgen_out/raw/blast_extra.json

start=$(date +%s)
docker run --rm --platform linux/amd64 \
  --volume "$(pwd)/mapgen:/mapgen:ro" \
  --volume "$(pwd)/mapgen_out:/out" \
  --volume "$(pwd)/maps:/maps" \
  --env PYTHONPATH=/opt/sqlglot:/mapgen \
  --env BLAST_OUT=/out/raw \
  --env COVERAGE_CORE=ctrace \
  "$IMAGE" sh -c "python -m pytest $TARGETS -p blast_plugin -p no:cacheprovider -p no:timeout -q -rfE; \
                  python /mapgen/export_map.py /out/raw $OUTFILE"
echo "make_map wall-clock: $(( $(date +%s) - start )) s"
