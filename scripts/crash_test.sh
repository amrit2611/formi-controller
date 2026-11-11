#!/usr/bin/env bash
# Crash-recovery proof (C13): run the controller, SIGKILL it mid-run, restart it, and grade the result.
# Usage: scripts/crash_test.sh scenarios/public_01_dev.yaml runs/base01 [seconds_before_kill]
set -euo pipefail
cd "$(dirname "$0")/.."
SCEN="${1:?scenario yaml}"
BASE="${2:?baseline run dir}"
KILL_AFTER="${3:-2}"
NAME="$(basename "$SCEN" .yaml)"
DB="crash_${NAME}.db"
rm -f "$DB" "$DB"-*
mkdir -p reports

python3 -m sim serve --scenario "$SCEN" --run-dir "runs/crash_${NAME}" --force --port "${PORT:-8700}" > "runs/crash_${NAME}.serve.log" 2>&1 &
SIM_PID=$!
trap 'kill "$SIM_PID" 2>/dev/null || true' EXIT
until curl -s -m 1 localhost:${PORT:-8700}/health > /dev/null; do sleep 0.5; done

echo "== run 1: SIGKILL after ${KILL_AFTER}s"
timeout -s KILL "$KILL_AFTER" python3 controller/main.py --url http://127.0.0.1:${PORT:-8700} --db "$DB" || true
echo "== world when killed: $(curl -s localhost:${PORT:-8700}/sim/state)"
echo "== what the store remembered:"
sqlite3 "$DB" "SELECT 'cursor', last_chunk FROM cursor; SELECT proposal_id, status FROM proposals;"

echo "== run 2: restart"
python3 controller/main.py --url http://127.0.0.1:${PORT:-8700} --db "$DB"

kill "$SIM_PID"; wait "$SIM_PID" 2>/dev/null || true
python3 -m sim grade --run "runs/crash_${NAME}" --baseline "$BASE" --out "reports/crash_${NAME}.md"
echo "== graded: reports/crash_${NAME}.md"
