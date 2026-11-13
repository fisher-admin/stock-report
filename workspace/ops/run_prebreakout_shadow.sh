#!/bin/bash
# 启动前夕 · 日淘(可选) + 因子二次加工厂（不改生产因子）
# 19:30 主管线已含 prebreakoutDailyPan + factory；本脚本供 20:45 收盘闭环补跑。
# 失败非致命。
#
# 交易日贯通（P0）：
#   TARGET_TRADE_DATE / OPENCLAW_TARGET_TRADE_DATE / --trade-date YYYYMMDD
#   日淘与工厂必须同一日期；禁止只传工厂导致日淘刷今日、工厂刷历史。
set -uo pipefail

WS="$HOME/.openclaw/workspace"
PY="$HOME/.openclaw/venv/bin/python"
[ -x "$PY" ] || PY="python3"
DAILY="$WS/skills/stock-system-orchestrator/scripts/prebreakout_factory_search_daily.py"
FACTORY="$WS/skills/stock-system-orchestrator/scripts/prebreakout_factory.py"
SETTLE="$WS/skills/stock-system-orchestrator/scripts/prebreakout_shadow_research.py"
LOCK="$WS/stock_data/03-working/health/prebreakout_factory.lock"

# 解析 --trade-date / 环境变量
TD="${OPENCLAW_TARGET_TRADE_DATE:-${TARGET_TRADE_DATE:-}}"
FORWARD_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --trade-date)
      TD="${2:-}"
      shift 2 || true
      ;;
    --trade-date=*)
      TD="${1#--trade-date=}"
      shift
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

DATE_ARGS=()
if [ -n "${TD}" ]; then
  export OPENCLAW_TARGET_TRADE_DATE="$TD"
  export TARGET_TRADE_DATE="$TD"
  DATE_ARGS=(--trade-date "$TD")
  echo "=== target trade_date=$TD ==="
fi

# 单实例：与工厂脚本 flock 互补
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "⚠️ prebreakout lock busy — skip"
    exit 0
  fi
fi

# 20:45 补跑：默认不再做完整日淘（19:30 已做），仅工厂刷新。
# 设 PREBREAKOUT_RERUN_DAILY_PAN=1 可强制再淘（与工厂同一 trade_date）。
# bash 3.2 + set -u: empty "${arr[@]}" is unbound; expand only when non-empty.
if [ "${PREBREAKOUT_RERUN_DAILY_PAN:-0}" = "1" ]; then
  echo "=== prebreakout daily pan (forced) $(date '+%F %T') ==="
  if [ ${#DATE_ARGS[@]} -gt 0 ]; then
    "$PY" "$DAILY" "${DATE_ARGS[@]}" || echo "⚠️ daily pan rc=$?"
  else
    "$PY" "$DAILY" || echo "⚠️ daily pan rc=$?"
  fi
fi

echo "=== prebreakout factory $(date '+%F %T') ==="
if [ ${#DATE_ARGS[@]} -gt 0 ] && [ ${#FORWARD_ARGS[@]} -gt 0 ]; then
  "$PY" "$FACTORY" "${DATE_ARGS[@]}" "${FORWARD_ARGS[@]}"
elif [ ${#DATE_ARGS[@]} -gt 0 ]; then
  "$PY" "$FACTORY" "${DATE_ARGS[@]}"
elif [ ${#FORWARD_ARGS[@]} -gt 0 ]; then
  "$PY" "$FACTORY" "${FORWARD_ARGS[@]}"
else
  "$PY" "$FACTORY"
fi
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "⚠️ prebreakout factory rc=$RC"
fi
if [ ${#DATE_ARGS[@]} -gt 0 ]; then
  "$PY" "$SETTLE" --settle-only "${DATE_ARGS[@]}" >/dev/null 2>&1 || true
else
  "$PY" "$SETTLE" --settle-only >/dev/null 2>&1 || true
fi
echo "=== prebreakout factory done ($(date '+%F %T')) rc=$RC ==="
echo "产物: health/prebreakout_factory_latest.json + data/latest/prebreakout_shadow_watch.json"
exit $RC
