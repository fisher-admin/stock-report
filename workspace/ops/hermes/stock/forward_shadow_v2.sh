#!/bin/bash
# forward_shadow_v2 (Core7P-native zero-paid shadow, paper only). Research only: reads forward_shadow_v1 scores/books,
# the zero-paid financial store (seed + em_ann_store) and production price caches; writes factor_factory/data/forward_shadow/v2/
# and commits only that path. Source of truth: ops/hermes/stock/, deployed by ops/hermes/deploy.py.
# Hermes jobs (weekdays; Hermes cron cannot pass arguments, so with no argument the mode follows the clock:
# < 0930 = premarket, 0930..1159 = cutoff, >= 1200 = evening):
#   23:40  forward_shadow_v2.sh evening    -> --today-session --parity-prev (after em_ann_store 22:50 + greenfield 23:30)
#   08:40  forward_shadow_v2.sh premarket  -> --prev-session (backup; writes non-official if fundamentals are still unhealthy)
#   09:35  forward_shadow_v2_cutoff.sh / forward_shadow_v2.sh cutoff -> --prev-session (past the open = gap or non-official continuous record)
# All runs are idempotent: a signal date already in decisions.jsonl is never re-decided. Silent unless something happened
# (one Chinese line: newly booked official decision, or a problem). Weekend / holiday gate = trading calendar in python.
set -uo pipefail
export TZ=Asia/Shanghai
export COPYFILE_DISABLE=1
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$HOME/.openclaw/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$HOME/.openclaw/venv/bin/python}"
export STOCK_SYSTEM_WORKSPACE="$WS" OPENCLAW_WORKSPACE_DIR="$WS" AEGIS_WORKSPACE="$WS"
TODAY=$(date +%Y%m%d)
if [ -n "${1:-}" ]; then
  MODE="$1"
else
  HM=$((10#$(date +%H%M)))
  if [ "$HM" -lt 930 ]; then
    MODE=premarket
  elif [ "$HM" -lt 1200 ]; then
    MODE=cutoff
  else
    MODE=evening
  fi
fi
cd "$WS" || exit 2

case "$MODE" in
  evening)   ARGS=(--today-session --parity-prev --as-of "$TODAY") ;;
  premarket) ARGS=(--prev-session --as-of "$TODAY") ;;
  cutoff)    ARGS=(--prev-session --as-of "$TODAY") ;;
  *) echo "usage: forward_shadow_v2.sh [evening|premarket|cutoff]" >&2; exit 2 ;;
esac

# Never overlap the nightly zero-paid capture (em_ann_store) or the greenfield shadow: they write the inputs this job reads.
proc_running() {
  local rc=0
  pgrep -f "$1" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) return 2 ;;
  esac
}

WRITER_BUSY=1
for attempt in $(seq 1 40); do
  ALL_CLEAR=1
  for pat in "skills/stock-analyzer/zero_paid_daily.py" "factor_factory/scripts/forward_shadow.py"; do
    rc=0
    proc_running "$pat" || rc=$?
    if [ "$rc" -eq 2 ]; then
      MSG="⚠️ 【Core7P-native前向影子】$MODE 进程探测失败（pgrep 异常），终止本轮运行"
      echo "$MSG"
      echo "$MSG" >&2
      exit 2
    elif [ "$rc" -eq 0 ]; then
      ALL_CLEAR=0
    fi
  done
  if [ "$ALL_CLEAR" -eq 1 ]; then
    WRITER_BUSY=0
    break
  fi
  [ "$attempt" -lt 40 ] && sleep 30
done
if [ "$WRITER_BUSY" -ne 0 ] && [ "$MODE" != "cutoff" ]; then
  MSG="⚠️ 【Core7P-native前向影子】$MODE 等待上游任务超时（20分钟），终止本轮运行"
  echo "$MSG"
  echo "$MSG" >&2
  exit 2
fi

OUT=$("$PY" "$WS/factor_factory/scripts/forward_shadow_v2.py" "${ARGS[@]}" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "⚠️ 【Core7P-native前向影子】$MODE 运行失败 (退出码 $RC)"
  echo "$OUT" | grep -v -i warning | tail -n 4
  exit "$RC"
fi
LINE=$(printf '%s\n' "$OUT" | grep '^{' | head -n 1)
read -r D STATUS OFF PSTAT FIN < <(printf '%s' "$LINE" | "$PY" -c 'import json,sys
r=json.loads(sys.stdin.read() or "{}")
ob=r.get("official_books") or {}
off="all" if ob and all(ob.values()) else ("some" if any(ob.values()) else "none")
print(r.get("signal_date","-"), r.get("status","unknown"), off, r.get("product_status","-"), "ok" if r.get("fin_healthy", True) else "low")' 2>/dev/null)

MSG=""
case "$STATUS" in
  decided)
    if [ "$OFF" = "all" ]; then MSG="📗 【Core7P-native前向影子】$D 已正式入账（产品页 $PSTAT）"
    else MSG="⚠️ 【Core7P-native前向影子】$D 已入账但非正式（official=$OFF，财务覆盖 $FIN），详见 v2/gaps.jsonl"; fi ;;
  gap_logged|kline_gate_failed) MSG="⚠️ 【Core7P-native前向影子】$D 状态 $STATUS（已登记缺口，不回补）" ;;
  waiting_for_*)
    [ "$MODE" = "premarket" ] && [ "$((10#$(date +%H%M)))" -ge 840 ] && MSG="⏳ 【Core7P-native前向影子】$D 仍在等待（$STATUS），09:35 兜底"
    ;;
  already_decided|not_a_session_today) ;;
  *) MSG="⚠️ 【Core7P-native前向影子】$D 状态: ${STATUS:-unknown}" ;;
esac
PAR=$(printf '%s\n' "$OUT" | grep '"spearman"' | tail -n 1)
if [ -n "$PAR" ] && ! printf '%s' "$PAR" | grep -q '"ok": true'; then
  MSG="${MSG:+$MSG
}⚠️ 【Core7P-native前向影子】评分复算一致性未通过: $PAR"
fi

LEDGER=(factor_factory/data/forward_shadow/v2)
if [ -n "$(git status --porcelain -- "${LEDGER[@]}" 2>/dev/null)" ]; then
  COMMIT_OK=0
  for attempt in 1 2 3; do
    if git add -- "${LEDGER[@]}" 2>/dev/null && git commit -q -m "chore(forward-shadow-v2): ${D:-unknown} $MODE" -- "${LEDGER[@]}" 2>/dev/null; then
      COMMIT_OK=1
      break
    fi
    [ "$attempt" -lt 3 ] && sleep 20
  done
  if [ "$COMMIT_OK" -ne 1 ]; then
    [ -n "${MSG:-}" ] && echo "$MSG"
    echo "⚠️ 【Core7P-native前向影子】${D:-unknown} $MODE git 提交失败（已重试 3 次），v2 账本存在未提交改动"
    exit 3
  fi
fi
[ -n "${MSG:-}" ] && echo "$MSG"
exit 0
