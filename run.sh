#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> starts the controller and runs until the scenario finishes. No Docker needed.
# Needs Python 3.10+ with requirements.txt installed (only httpx is used at runtime; SQLite is stdlib).
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
# One state file per scenario, so a restart resumes and a different scenario never reuses old state.
SCEN="$(curl -s "$SIM_URL/sim/state" | "$PY" -c 'import sys, json; print(json.load(sys.stdin)["scenario"])')"
mkdir -p state
echo "controller: scenario=$SCEN sim=$SIM_URL state=state/$SCEN.db" >&2
exec "$PY" controller/main.py --url "$SIM_URL" --db "state/$SCEN.db"
