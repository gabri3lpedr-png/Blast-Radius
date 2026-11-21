#!/bin/sh
# Stage A4 (DEV-ONLY): make mutants, then label them. From the repo root:
#   sh tools/mutants/run.sh 20 1      # trial: 20 mutants, seed 1
#   sh tools/mutants/run.sh 300 1     # full set (hours; resumable, partial results usable)
# Output: mutants_out/ (git-ignored, never shipped). Grade the selector with:
#   make sample SAMPLE=$(pwd)/mutants_out
set -eu
cd "$(dirname "$0")/../.."
N=${1:-20}; SEED=${2:-1}
mkdir -p mutants_out/pending
docker run --rm --platform linux/amd64 \
  --volume "$(pwd)/maps:/maps:ro" \
  --volume "$(pwd)/tools/mutants:/mutants:ro" \
  --volume "$(pwd)/mutants_out:/mo" \
  blast-radius-mapgen python /mutants/make_mutants.py --n "$N" --seed "$SEED" --out /mo/pending
python3 tools/mutants/label.py --jobs 4
