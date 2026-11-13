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
# 20261010: 晚间链若被 ops/schedule_contingency.py 回退到 19:30，此文件恢复看门狗阈值（20:30 / 22:15）。
if [ -f "$WS/config/stock_schedule_overrides.env" ]; then set -a; . "$WS/config/stock_schedule_overrides.env"; set +a; fi

# Alert shaping (2026-10-03): full text for a new failure, one line for an
# unchanged repeat, one line on recovery.  Exit codes are never changed.
ALERT_POLICY="$WS/skills/stock-system-orchestrator/scripts/stock_alert_policy.py"
shape_failure() {
  local job="$1" rc="$2" text
  text=$(cat)
  printf "%s\n" "$text" | "$PY" "$ALERT_POLICY" failure --job "$job" --rc "$rc" 2>/dev/null || printf "%s\n" "$text"
}
announce_recovery() {
  "$PY" "$ALERT_POLICY" success --job "$1" 2>/dev/null || true
}

export STOCK_HEALTH_RUNTIME="hermes"

set +e
OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/data_freshness_watchdog.py" 2>&1)
RET=$?
set -e

HEARTBEAT_FILE="$WS/stock_data/03-working/health/watchdog_heartbeats.jsonl"
if [ $RET -eq 0 ]; then
  # Durable heartbeat; no routine green message (2026-10-03).
  printf '{"at":"%s","job":"data_freshness_watchdog","rc":0}\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" >>"$HEARTBEAT_FILE" 2>/dev/null || true
  announce_recovery data_freshness_watchdog
  exit 0
fi

printf '{"at":"%s","job":"data_freshness_watchdog","rc":%s}\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$RET" >>"$HEARTBEAT_FILE" 2>/dev/null || true
if [ -n "$OUT" ]; then
  SHAPED=$(printf "%s\n" "$OUT" | shape_failure data_freshness_watchdog "$RET")
else
  SHAPED=$(echo "⚠️ 【选股数据检查异常】检查程序执行异常 (退出码: $RET)" | shape_failure data_freshness_watchdog "$RET")
fi
printf "%s\n" "$SHAPED"
printf "%s\n" "$SHAPED" | head -n 1 >&2
exit $RET
