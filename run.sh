#!/usr/bin/env bash
# Outcome-first agent loop controller.
# Usage: ./run.sh <SIM_URL> [--run-dir <dir>] [--tick <minutes>]
set -euo pipefail

SIM_URL="${1:?usage: ./run.sh <SIM_URL> [--run-dir <dir>] [--tick <minutes>]}"
RUN_DIR="${2:-runs/controller}"
TICK="${3:-60}"

echo "Starting controller -> $SIM_URL  run-dir=$RUN_DIR  tick=${TICK}m" >&2
exec python -m controller --url "$SIM_URL" --run-dir "$RUN_DIR" --tick "$TICK"
