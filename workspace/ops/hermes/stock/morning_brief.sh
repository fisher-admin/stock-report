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
GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job morning_brief 2>&1)
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

OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/publish_market_overview.py" 2>&1)
RET=$?

if [ $RET -eq 0 ]; then
  announce_recovery morning_brief
  "$PY" -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '$WS/skills/stock-system-orchestrator/scripts')
import stock_message_formatter as fmt
brief_file = Path('$WS/stock-report/data/recommendation_analytics/market_morning_brief_latest.json')
verdict_file = Path('$WS/stock-report/data/latest/system_verdict.json')
brief = json.loads(brief_file.read_text(encoding='utf-8')) if brief_file.is_file() else {}
verdict = json.loads(verdict_file.read_text(encoding='utf-8')) if verdict_file.is_file() else {}
print(fmt.format_morning_brief(brief, verdict))
"
  # 2026-10-23: a lagging selection no longer aborts the brief; say so plainly.
  LAG=$(printf "%s" "$OUT" | grep -o '"strategy_lagging_trade_date": "[0-9]\{8\}"' | grep -o '[0-9]\{8\}' | head -1)
  if [ -n "$LAG" ]; then
    echo "ℹ️ 选股结果仍停在 ${LAG}（前一晚主管线未完成），本次晨判未更新网页。"
  fi
else
  SHAPED=$( {
    echo "🚨 【A股晨判执行失败】 (退出码: $RET)"
    echo "$OUT" | tail -n 5
  } | shape_failure morning_brief "$RET" )
  printf "%s\n" "$SHAPED"
  printf "%s\n" "$SHAPED" | head -n 1 >&2
fi
exit $RET
