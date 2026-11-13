#!/bin/bash
# Hermes heartbeat (every 5 minutes): advance the trade-date dependency graph.
# Silent unless a step result or a root-cause alert must be delivered.
# Source of truth: ops/hermes/stock/stock_dag_tick.sh; deploy with ops/hermes/deploy.py.
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$HOME/.openclaw/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$HOME/.openclaw/venv/bin/python}"
cd "$STOCK_WORKSPACE" || exit 2
# Two heartbeats must never interleave.
LOCK="$STOCK_WORKSPACE/stock_data/03-working/health/dag/.tick.lock"
mkdir -p "$(dirname "$LOCK")"
if [[ "${STOCK_DAG_LOCK_MODE:-auto}" == "flock" || ! -x /usr/bin/lockf ]]; then
  if ! command -v flock >/dev/null 2>&1; then
    echo "FATAL: neither lockf nor flock is available" >&2
    exit 2
  fi
  exec 9>"$LOCK"
  flock -n 9 || exit 0
  "$PY" "$STOCK_WORKSPACE/scripts/stock_dag.py" tick
  RET=$?
else
  /usr/bin/lockf -s -t 0 "$LOCK" "$PY" "$STOCK_WORKSPACE/scripts/stock_dag.py" tick
  RET=$?
  [ "$RET" -eq 75 ] && exit 0  # previous heartbeat still running
fi
exit $RET
