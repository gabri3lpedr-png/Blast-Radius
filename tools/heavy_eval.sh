#!/bin/sh
# DEV-ONLY: run the image under the scorer's flags on a data dir with HEAVY_REPLAY=$2, score it.
# Usage: sh tools/heavy_eval.sh <data dir> <0|static|1> [image]    (prints score.py + the selector cpu line)
set -eu
D=$(cd "$1" && pwd); H=$2; IMG=${3:-blast-radius-entry}
OUT=$(mktemp); ERR=$(mktemp)
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m \
  --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp --env HEAVY_REPLAY="$H" --env ATTR_LEVEL="${ATTR_LEVEL:-1}" \
  --volume "$D/commits:/data/commits:ro" --volume "$D/tests.json:/data/tests.json:ro" \
  "$IMG" > "$OUT" 2> "$ERR"
python3 score.py "$D" "$D/answers.json" "$OUT" | head -2
grep "^cpu " "$ERR"
grep -o "h:[a-z_]* [A-Za-z]*[^;]*" "$ERR" | sed 's/ (.*//' | sort | uniq -c
cp "$OUT" "${TMPDIR:-/tmp}/heavy_eval_out.jsonl"; cp "$ERR" "${TMPDIR:-/tmp}/heavy_eval_err.txt"
rm -f "$OUT" "$ERR"
