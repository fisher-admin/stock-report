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
  echo "Python 环境缺失，任务未执行：$PY" >&2
  exit 2
fi
cd "$WS"

# Exchange-calendar gate (2026-10-03): closed day -> durable skip record, no
# business work, no routine message.  Missing/invalid calendar -> error.
GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job daily_closure 2>&1)
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

OUT=$(bash "$WS/ops/run_daily_closure.sh" 2>&1)
RET=$?

# 2026-10-03: report the day's actual business outcome (selection, final
# publication check, broadcast hand-off, Hermes delivery record), even when an
# upstream step failed.  Replaces the unconditional "完成" line.
STATUS_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/daily_business_status.py" --closure-rc "$RET" 2>&1)
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
  echo "收盘核算异常（退出码 $RET）" >&2
fi
exit $RET
