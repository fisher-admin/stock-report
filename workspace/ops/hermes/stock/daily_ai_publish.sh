#!/bin/bash
set -uo pipefail
export TZ=Asia/Shanghai

export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
export STOCK_SYSTEM_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
export STOCK_SYSTEM_VENV="${STOCK_SYSTEM_VENV_FORCE:-$STOCK_SYSTEM_ROOT/venv}"
export STOCK_SYSTEM_PYTHON="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_VENV/bin/python}"
export STOCK_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
export STOCK_PYTHON="$STOCK_SYSTEM_PYTHON"
# 20261010: 主管线提前到 17:30 且数据晚到时可等到约 18:47；本任务 18:00 启动，等待发布回执最多 90 分钟。
export OPENCLAW_RECEIPT_TIMEOUT_SECONDS="${OPENCLAW_RECEIPT_TIMEOUT_SECONDS:-5400}"
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
GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job daily_ai_publish 2>&1)
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

# 2026-10-26: the 19:30 pipeline already announced a market-gate halt; no
# broadcast and no second "failure" alert.
if "$PY" -c 'import json,sys,datetime
d=json.load(open(sys.argv[1]))
today=datetime.datetime.now().strftime("%Y%m%d")
sys.exit(0 if d.get("failure_category")=="market_halt" and str(d.get("target_trade_date") or d.get("trade_date") or "")==today else 1)' \
    "$WS/stock_data/03-working/health/orchestrator_run.json" 2>/dev/null; then
  exit 0
fi

OUT=$("$PY" "$WS/scripts/run_ai_summary_and_publish.py" 2>&1)
RET=$?

# 同步候选股到 heatmap 高亮文件（best-effort，失败不阻断发布）
"$PY" "$WS/skills/stock-system-orchestrator/scripts/sync_heatmap_candidates.py" >/dev/null 2>&1 || true

if [ $RET -eq 0 ]; then
  announce_recovery daily_ai_publish
  echo "$OUT"
else
  SHAPED=$( {
    echo "🚨 【A股晚间播报执行失败】 (退出码: $RET)"
    echo "$OUT" | tail -n 5
  } | shape_failure daily_ai_publish "$RET" )
  printf "%s\n" "$SHAPED"
  printf "%s\n" "$SHAPED" | head -n 1 >&2
fi
exit $RET
