#!/bin/sh
# Build the mapgen image and run the clean baseline. Run from the repo root:
#   sh mapgen/build_and_baseline.sh
set -eu
cd "$(dirname "$0")/.."
IMAGE=blast-radius-mapgen
mkdir -p mapgen_out
docker build --platform linux/amd64 -f mapgen/Dockerfile.mapgen -t "$IMAGE" .
docker run --rm --platform linux/amd64 \
  --volume "$(pwd)/mapgen_out:/out" \
  "$IMAGE" run_baseline.sh || echo "pytest reported failures (see mapgen_out/baseline.xml)"
cat mapgen_out/baseline_time.txt
