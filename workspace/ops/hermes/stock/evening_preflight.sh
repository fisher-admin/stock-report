#!/bin/bash
# Hermes job (weekdays 16:30): run the 19:30 chain's preflight hours early.
# Source of truth: ops/hermes/stock/evening_preflight.sh; deploy with ops/hermes/deploy.py.
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
  echo "⚠️ 【晚间链预检】主 Python 环境不可用：$PY（19:30 主管线必然失败）" >&2
  exit 2
fi
cd "$WS" || exit 2
GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job evening_preflight 2>&1)
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

# Phase 4A parallel comparison (2026-10-30): the same preflight in the container
# image first, so the host run below rewrites the shared health files last.
# Observation only (health/container_shadow/); never changes this job's result.
SHADOW="$WS/ops/container/container_shadow.py"
REPORT=stock_data/03-working/health/evening_chain_preflight.json
"$PY" "$SHADOW" run --job evening_preflight --capture "$REPORT" -- \
  /Users/fisher/.openclaw/venv/bin/python scripts/evening_chain_preflight.py >/dev/null 2>&1 || true

ALERT_POLICY="$WS/skills/stock-system-orchestrator/scripts/stock_alert_policy.py"
OUT=$("$PY" "$WS/scripts/evening_chain_preflight.py" 2>&1)
RET=$?
printf "%s" "$OUT" | "$PY" "$SHADOW" host --job evening_preflight --rc "$RET" --capture "$REPORT" >/dev/null 2>&1 || true
if [ $RET -eq 0 ]; then
  "$PY" "$ALERT_POLICY" success --job evening_preflight 2>/dev/null || true
  exit 0
fi
[ -n "$OUT" ] || OUT="⚠️ 【晚间链预检】预检程序异常 (退出码: $RET)"
SHAPED=$(printf "%s\n" "$OUT" | "$PY" "$ALERT_POLICY" failure --job evening_preflight --rc "$RET" 2>/dev/null || printf "%s\n" "$OUT")
printf "%s\n" "$SHAPED"
printf "%s\n" "$SHAPED" | head -n 1 >&2
exit $RET
