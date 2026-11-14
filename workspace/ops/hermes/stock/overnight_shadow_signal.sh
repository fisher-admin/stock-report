#!/bin/bash
# 隔夜前瞻影子(overnight_shadow_v1)·信号: 只记账, 不下单。交易日 14:44 启动做准备, 14:45:30 起拉当日 5 分钟线,
# 14:50 前写 factor_factory/data/forward_shadow/overnight_v1/signals/YYYYMMDD.json; 超时只记"未出信号", 不补发。
# 源在 ops/hermes/stock/, 由 ops/hermes/deploy.py deploy 部署。标准输出一行中文投递到股票频道。
# 用法: overnight_shadow_signal.sh [--test]
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
export STOCK_SYSTEM_WORKSPACE="$WS" OPENCLAW_WORKSPACE_DIR="$WS"
cd "$WS" || exit 2

GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job overnight_shadow_signal 2>&1)
GATE_RC=$?
[ "$GATE_RC" -eq 3 ] && exit 0
if [ "$GATE_RC" -ne 0 ]; then
  echo "⚠️ 【隔夜影子】交易日历检查失败 (rc=$GATE_RC)" >&2
  exit "$GATE_RC"
fi

OUT=$("$PY" "$WS/factor_factory/scripts/overnight_shadow.py" signal "$@" 2>&1)
RC=$?
LAST=$(printf '%s\n' "$OUT" | grep '^{"status"' | tail -n 1)
if [ "$RC" -ne 0 ] || [ -z "$LAST" ]; then
  printf '%s\n' "$OUT" | tail -n 5
  echo "⚠️ 【隔夜影子】信号任务运行失败，今日未出信号 (退出码 $RC)" >&2
  [ "$RC" -ne 0 ] && exit "$RC"
  exit 1
fi
printf '%s\n' "$LAST" | "$PY" -c '
import json, sys
r = json.loads(sys.stdin.read()); s = r.get("status"); d = r.get("signal_date", "")
if s == "recorded":
    deg = "；降级 %d 项" % len(r["degraded"]) if r.get("degraded") else ""
    print("🌙 【隔夜影子】%s 已记录：做多 %d 只，避让标记 %d 只（最低 5%%），用时 %.0f 秒%s" % (d, len(r["long_picks"]), r["avoid_q05"], r["seconds_total"], deg))
elif s == "no_signal":
    print("⚠️ 【隔夜影子】%s 未出信号：%s" % (d, r.get("reason", "")))
elif s not in ("already_recorded", "not_a_session"):
    print("⚠️ 【隔夜影子】%s 状态 %s" % (d, s))
'
exit 0
