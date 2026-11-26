#!/bin/bash
# 今日页（#/today）发布：v4.3 退役后（2026-11-20，零付费架构 A2）改为 EW6 前向影子持仓。
# 工作日 23:45（23:30 前瞻影子记账后）与 08:45（07:20/08:35 盘前补记后）各跑一次；走唯一推送路径
# publish_stock_report_v2.sh 的退役分支，按 EW6 信号日幂等（同一信号日只推一次）。
# 源在 ops/hermes/stock/，由 ops/hermes/deploy.py deploy 部署。发布成功一句中文；幂等跳过/未退役静默；失败一句中文。
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
export STOCK_SYSTEM_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
export STOCK_SYSTEM_PYTHON="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
export OPENCLAW_WORKSPACE_DIR="$STOCK_SYSTEM_WORKSPACE" OPENCLAW_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
WS="$STOCK_SYSTEM_WORKSPACE"
PY="$STOCK_SYSTEM_PYTHON"
cd "$WS" || exit 2
S="$WS/skills/stock-system-orchestrator/scripts"

"$PY" "$S/v43_retirement.py" status 2>/dev/null | grep -q '"retired": true' || exit 0
proc_running() {
  local rc=0
  pgrep -f "$1" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) return 2 ;;
  esac
}

# Never overlap the ledger writers this page reads.
WRITER_BUSY=1
for attempt in $(seq 1 30); do
  BUSY=0
  for pat in "factor_factory/scripts/forward_shadow\.py" "forward_shadow_v2(_cutoff)?\.(sh|py)"; do
    PRC=0
    proc_running "$pat" || PRC=$?
    if [ "$PRC" -eq 2 ]; then
      MSG="⚠️ 【今日页发布】进程探测失败（pgrep 异常），终止本次发布"
      echo "$MSG"
      echo "$MSG" >&2
      exit 1
    elif [ "$PRC" -eq 0 ]; then
      BUSY=1
      break
    fi
  done
  if [ "$BUSY" -eq 0 ]; then
    WRITER_BUSY=0
    break
  fi
  if [ "$attempt" -lt 30 ]; then
    sleep 20
  fi
done
if [ "$WRITER_BUSY" -ne 0 ]; then
  MSG="⚠️ 【今日页发布】等待前向影子记账超时，终止本次发布"
  echo "$MSG"
  echo "$MSG" >&2
  exit 1
fi

V2_LOCK="$WS/factor_factory/data/forward_shadow/v2/.v2.lock"
mkdir -p "$(dirname "$V2_LOCK")" || exit 2
exec 9>>"$V2_LOCK" || exit 2
if ! flock -w 600 9; then
  MSG="⚠️ 【今日页发布】等待 v2 账本锁超时，终止本次发布"
  echo "$MSG"
  echo "$MSG" >&2
  exit 1
fi
OUT=$(timeout 1800 bash "$S/publish_stock_report_v2.sh" 2>&1)
RC=$?
flock -u 9 2>/dev/null || true
exec 9>&-
if [ "$RC" -ne 0 ]; then
  MSG="⚠️ 【今日页发布】EW6 持仓发布失败（退出码 $RC）：$(printf '%s\n' "$OUT" | grep -E 'FATAL|ABORT|ERROR' | tail -n 1 | sed 's/^\[[0-9:]*\] //' | cut -c1-120)"
  echo "$MSG"
  echo "$MSG" >&2
  exit 1
fi
if printf '%s\n' "$OUT" | grep -q "OK: 已推送"; then
  D=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("signal_date",""))' "$WS/stock-report/data/latest/ew6_today_state.json" 2>/dev/null)
  echo "🌐 【今日页】已发布 EW6 前向影子持仓（信号日 ${D:-?}，前向验证纸面记录）"
fi
exit 0
