#!/bin/bash
# Hermes job (weekdays 21:20): one automatic retry of a failed 19:30 chain.
# Source of truth: ops/hermes/stock/evening_chain_retry.sh; deploy with ops/hermes/deploy.py.
set -uo pipefail
export TZ=Asia/Shanghai

export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
export STOCK_SYSTEM_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
export STOCK_SYSTEM_VENV="${STOCK_SYSTEM_VENV_FORCE:-$STOCK_SYSTEM_ROOT/venv}"
export STOCK_SYSTEM_PYTHON="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_VENV/bin/python}"
export STOCK_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
export STOCK_PYTHON="$STOCK_SYSTEM_PYTHON"
export OPENCLAW_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
export OPENCLAW_WORKSPACE_DIR="$STOCK_SYSTEM_WORKSPACE"

WS="$STOCK_WORKSPACE"
PY="$STOCK_PYTHON"
if [ ! -x "$PY" ]; then
  echo "⚠️ 【晚间链自动补跑】主 Python 环境不可用：$PY" >&2
  exit 2
fi
cd "$WS" || exit 2
GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job evening_chain_retry 2>&1)
GATE_RC=$?
printf "%s\n" "$GATE_OUT"
if [ "$GATE_RC" -eq 3 ]; then
  exit 0
fi
if [ "$GATE_RC" -ne 0 ]; then
  GATE_FIRST=$(printf "%s\n" "$GATE_OUT" | grep -v '^$' | head -n 1)
  echo "${GATE_FIRST:-交易日历检查失败，任务未执行}" >&2
  exit "$GATE_RC"
fi
OUT=$("$PY" "$WS/scripts/evening_chain_retry.py" 2>&1)
RET=$?
printf "%s\n" "$OUT"
if [ $RET -ne 0 ]; then
  printf "%s\n" "$OUT" | grep -v '^$' | head -n 1 >&2
fi
exit $RET
