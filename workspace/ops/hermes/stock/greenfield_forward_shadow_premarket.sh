#!/bin/bash
# Pre-market self-heal for the comp_rank_ew6 forward shadow (forward_shadow_v1).
# The 23:30 job is the only attempt per signal date; a late/throttled upstream used to turn into a permanent
# gap (10-20..11-04 lost four rebalance days). forward_shadow_v1 accepts a record any time before 09:30 of the
# entry session, so this reruns greenfield_forward_shadow.sh for the previous session when that date is neither
# in books.jsonl nor in gaps.jsonl. Silent when there is nothing to do. Never touches the SPEC.
# Hermes jobs: weekdays 07:20 and 08:35 (idempotent; the second run is silent once the first one recorded D).
# Source of truth: ops/hermes/stock/; deploy with ops/hermes/deploy.py.
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
export STOCK_SYSTEM_WORKSPACE="$WS" OPENCLAW_WORKSPACE="$WS" OPENCLAW_WORKSPACE_DIR="$WS"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$WS" || exit 2

GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job greenfield_forward_shadow_premarket 2>&1)
GATE_RC=$?
[ "$GATE_RC" -eq 3 ] && exit 0
if [ "$GATE_RC" -ne 0 ]; then
  echo "⚠️ 【绿地前瞻影子·盘前补记】交易日历检查失败 (rc=$GATE_RC)" >&2
  exit "$GATE_RC"
fi

# Previous official session and whether it is already accounted for (recorded or gap-registered).
read -r D STATE < <("$PY" - <<'PYEOF'
import json
from datetime import datetime
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo("Asia/Shanghai"))
today = now.strftime("%Y%m%d")
cal = json.load(open("stock_data/03-working/health/trading_calendar.json")).get("open_dates", [])
prev = [d for d in cal if d < today]
if not prev:
    print("- none"); raise SystemExit
d = prev[-1]
late = now.hour * 60 + now.minute >= 9 * 60 + 20
base = "factor_factory/data/forward_shadow/"
def dates(name):
    try:
        return {json.loads(l).get("signal_date") for l in open(base + name) if l.strip()}
    except OSError:
        return set()
state = "recorded" if d in dates("books.jsonl") else "gap" if d in dates("gaps.jsonl") else "missing"
print(d, "late" if state == "missing" and late else state)
PYEOF
)
if [ "${STATE:-}" = "late" ]; then
  echo "🚨 【绿地前瞻影子·盘前补记】$D 仍未入账且已过 09:20，来不及自动补跑；09:30 前人工处理，否则该日永久缺口"
  echo "🚨 【绿地前瞻影子·盘前补记】$D 未入账且已过 09:20" >&2
  exit 1
fi
[ "${STATE:-}" = "missing" ] || exit 0

echo "🩹 【绿地前瞻影子·盘前补记】$D 昨晚未入账，09:30 入场前自动补跑一次（含输入自愈）"
export FS_PREFLIGHT_HEAL_TIMEOUT="${FS_PREFLIGHT_HEAL_TIMEOUT:-1500}"
OUT=$(FS_PREFLIGHT_CALLER=premarket timeout 2700 bash "$HERE/greenfield_forward_shadow.sh" "$D" 2>/dev/null)
RC=$?
printf "%s\n" "$OUT" | head -n 30
if [ "$RC" -eq 0 ] && grep -q "\"signal_date\": \"$D\"" factor_factory/data/forward_shadow/books.jsonl; then
  echo "🔁 【绿地前瞻影子·盘前补记】$D 已在入场前正式入账"
  exit 0
fi
echo "🚨 【绿地前瞻影子·盘前补记】$D 补记失败 (退出码 $RC)：09:30 前需人工处理，否则该日永久缺口"
echo "🚨 【绿地前瞻影子·盘前补记】$D 补记失败 (退出码 $RC)" >&2
exit 1
