#!/bin/sh
# Build maps/infection_map.json.gz (weak-mutation map) from the PINNED sqlglot, in the
# blast-radius-mapgen image (the labelling environment). From the repo root:
#   sh infect/make_infection_map.sh                       # full suite
#   sh infect/make_infection_map.sh tests/test_parser.py  # a slice -> infect_out/slice_map.json.gz
set -eu
cd "$(dirname "$0")/.."
IMAGE=blast-radius-mapgen
mkdir -p infect_out maps
docker image inspect "$IMAGE" >/dev/null 2>&1 || \
  docker build -q --platform linux/amd64 -f mapgen/Dockerfile.mapgen -t "$IMAGE" . >/dev/null
if [ $# -gt 0 ]; then TARGETS="$*"; OUTFILE=/out/slice_map.json.gz; else TARGETS=tests; OUTFILE=/maps/infection_map.json.gz; fi
rm -f infect_out/infection_raw.json
start=$(date +%s)
docker run --rm --platform linux/amd64 \
  --volume "$(pwd)/infect:/infect:ro" \
  --volume "$(pwd)/infect_out:/out" \
  --volume "$(pwd)/maps:/maps" \
  --env PYTHONPATH=/opt/sqlglot:/infect \
  --env INFECT_OUT=/out --env SKIP_INTEGRATION=0 --env PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" sh -c "python -m pytest $TARGETS -p infect_plugin -p no:cacheprovider -p no:timeout -q -rfE; \
                  python /infect/export.py /out/infection_raw.json $OUTFILE"
echo "make_infection_map wall-clock: $(( $(date +%s) - start )) s"
