#!/bin/bash
# 零付费数据夜间任务（A1 日历对账/确认 + A5 价格交叉核对 + A4 东财公告只追加采集），工作日 22:50。
# 不走交易日历 gate：休市工作日也要采公告、也要核对"今天确实没开市"。
# 源在 ops/hermes/stock/，由 ops/hermes/deploy.py deploy 部署。成功静默；有问题时一句中文投递到股票频道。
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
export STOCK_SYSTEM_WORKSPACE="$WS" OPENCLAW_WORKSPACE_DIR="$WS"
cd "$WS" || exit 2

OUT=$(timeout 1500 "$PY" "$WS/skills/stock-analyzer/zero_paid_daily.py" 2>/dev/null)
RC=$?
if [ "$RC" -eq 0 ]; then
  exit 0
fi
[ -n "$OUT" ] || OUT="⚠️ 【零付费数据】夜间任务异常退出（退出码 $RC），详见 health/zero_paid_daily/"
printf '%s\n' "$OUT" | head -n 1
printf '%s\n' "$OUT" | head -n 1 >&2
exit 1
