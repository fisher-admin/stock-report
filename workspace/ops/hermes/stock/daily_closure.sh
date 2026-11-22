#!/bin/bash
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
  echo "FATAL: Locked stock Python environment missing or not executable: $PY" >&2
  exit 2
fi
cd "$WS"

# Exchange-calendar gate (2026-10-03): closed day -> durable skip record, no
# business work, no routine message.  Missing/invalid calendar -> error.
"$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job daily_closure
GATE_RC=$?
if [ "$GATE_RC" -eq 3 ]; then
  exit 0
fi
if [ "$GATE_RC" -ne 0 ]; then
  exit "$GATE_RC"
fi

OUT=$(bash "$WS/ops/run_daily_closure.sh" 2>&1)
RET=$?

# 2026-10-03: report the day's actual business outcome (selection, final
# publication check, broadcast hand-off, Hermes delivery record), even when an
# upstream step failed.  Replaces the unconditional "完成" line.
STATUS_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/daily_business_status.py" --previous-session --closure-rc "$RET" 2>&1)
STATUS_RC=$?
if [ "$STATUS_RC" -eq 0 ]; then
  printf "%s\n" "$STATUS_OUT"
else
  echo "⚠️ 【当日业务状态汇总失败】 (退出码: $STATUS_RC)"
  printf "%s\n" "$STATUS_OUT" | tail -n 3
fi
if [ $RET -ne 0 ]; then
  echo "⚠️ 【A股每日收盘核算异常】 (退出码: $RET)"
  echo "$OUT" | tail -n 5
fi
exit $RET
