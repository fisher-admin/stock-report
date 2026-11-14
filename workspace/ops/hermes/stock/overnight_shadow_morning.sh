#!/bin/bash
# 隔夜前瞻影子(overnight_shadow_v1)·早盘: 只记账, 不下单。交易日 10:35 运行:
#   1) settle: 对上一交易日的信号记录开盘 / 09:35 / 09:40 / 10:00 / 10:30 各时点价格, 结算做多与避让两本账(每笔 19.2bp)
#   2) prep:   备好今天 14:46 所需的历史输入(5 分钟线仓、日线、面板、事件表)
# 源在 ops/hermes/stock/, 由 ops/hermes/deploy.py deploy 部署。标准输出一行中文投递到股票频道。
# 用法: overnight_shadow_morning.sh [--test]
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
export STOCK_SYSTEM_WORKSPACE="$WS" OPENCLAW_WORKSPACE_DIR="$WS"
cd "$WS" || exit 2

GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job overnight_shadow_morning 2>&1)
GATE_RC=$?
[ "$GATE_RC" -eq 3 ] && exit 0
if [ "$GATE_RC" -ne 0 ]; then
  echo "⚠️ 【隔夜影子】交易日历检查失败 (rc=$GATE_RC)" >&2
  exit "$GATE_RC"
fi

S_OUT=$("$PY" "$WS/factor_factory/scripts/overnight_shadow.py" settle "$@" 2>&1)
S_RC=$?
P_OUT=$("$PY" "$WS/factor_factory/scripts/overnight_shadow.py" prep 2>&1)
P_RC=$?
S_LAST=$(printf '%s\n' "$S_OUT" | grep '^{"status"' | tail -n 1)
P_LAST=$(printf '%s\n' "$P_OUT" | grep '^{"status"' | tail -n 1)
MSG=$(S_LAST="$S_LAST" P_LAST="$P_LAST" S_RC="$S_RC" P_RC="$P_RC" "$PY" -c '
import json, os
def j(k):
    try: return json.loads(os.environ[k])
    except Exception: return {}
s, p = j("S_LAST"), j("P_LAST"); out = []
for r in s.get("results", []):
    if r.get("status") == "settled":
        a = r["avoid_q05"]; v = (a.get("stepaside_net_bp") or {}).get("x0940")
        out.append("🌙 【隔夜影子】%s 已结算：做多 %d 笔，账面净 %+.1fbp（开盘卖）；避让 %d 只，每次净 %s" % (
            r["signal_date"], r["long"]["n"], r["long"]["book_net_bp_open"] or 0.0, a.get("n") or 0, ("%+.0fbp" % v) if v is not None else "无"))
    elif r.get("status") == "not_ready":
        out.append("⚠️ 【隔夜影子】%s 结算数据未就绪" % r.get("signal_date"))
if os.environ["S_RC"] != "0" or not s:
    out.append("⚠️ 【隔夜影子】结算任务运行失败")
if os.environ["P_RC"] != "0" or p.get("status") != "prepared":
    out.append("⚠️ 【隔夜影子】今日 14:46 的输入未备齐：%s" % ("；".join(p.get("warnings") or []) or "准备任务失败")[:120])
print("\n".join(out))
')
[ -n "$MSG" ] && printf '%s\n' "$MSG"
if [ "$S_RC" -ne 0 ] || [ "$P_RC" -ne 0 ]; then
  printf '%s\n%s\n' "$S_OUT" "$P_OUT" | tail -n 6
  echo "⚠️ 【隔夜影子】早盘任务运行失败" >&2
  exit 1
fi
exit 0
