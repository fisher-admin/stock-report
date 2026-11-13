#!/bin/bash
# Hermes job (daily 01:30): parallel full-refetch check of the incremental
# short-track history for the first five trading days after 2026-10-30.
# No exchange-calendar gate: Saturday 01:30 checks Friday's run. The script is
# idempotent per target date and silent when results agree.
# Source of truth: ops/hermes/stock/short_track_incremental_verify.sh; deploy with ops/hermes/deploy.py.
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
export STOCK_SYSTEM_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
export STOCK_SYSTEM_PYTHON="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
export STOCK_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
export OPENCLAW_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
WS="$STOCK_SYSTEM_WORKSPACE"
PY="$STOCK_SYSTEM_PYTHON"
cd "$WS" || exit 2
OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/short_track_incremental_verify.py" 2>&1)
RET=$?
if [ $RET -ne 0 ] && [ -z "$OUT" ]; then
  OUT="⚠️ 【短线增量核对】核对程序异常 (退出码: $RET)"
fi
if [ -n "$OUT" ]; then
  printf "%s\n" "$OUT" | tail -n 8
  if [ $RET -ne 0 ]; then
    printf "%s\n" "$OUT" | grep -v '^$' | head -n 1 >&2
  fi
fi
exit $RET
