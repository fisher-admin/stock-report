#!/bin/bash
# forward_shadow_multi_v1 (EW6 continuation + Aegis-Alpha v1, paper only). Research only: reads forward_shadow_v1
# scores/books, x_jump_ms_v1 signals + ann_store and production caches; writes factor_factory/data/forward_shadow/multi_v1/
# and commits only that path. Source of truth: ops/hermes/stock/, deployed by ops/hermes/deploy.py.
# Hermes jobs (weekdays):
#   23:50  forward_shadow_multi.sh evening    -> --today-session --parity-prev (after 23:30 greenfield + x_jump_ms_v1)
#   07:40  forward_shadow_multi.sh premarket  -> --prev-session (after A2 07:20 self-heal)
#   08:55  forward_shadow_multi.sh premarket  -> --prev-session (after A2 08:35 self-heal; last pre-open try)
#   09:10  forward_shadow_multi.sh premarket  -> --prev-session (2026-11-20: zero-paid ann wait makes 08:55 the first write; retry)
#   09:35  forward_shadow_multi.sh cutoff     -> --prev-session (past the open: gap or non-official continuous record)
# All runs are idempotent: a signal date already in decisions.jsonl is never re-decided. Silent unless something happened.
set -uo pipefail
export TZ=Asia/Shanghai
export COPYFILE_DISABLE=1
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$HOME/.openclaw/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$HOME/.openclaw/venv/bin/python}"
MODE="${1:-evening}"
cd "$WS" || exit 2

case "$MODE" in
  evening)   ARGS=(--today-session --parity-prev) ;;
  premarket) ARGS=(--prev-session) ;;
  cutoff)    ARGS=(--prev-session) ;;
  *) echo "usage: forward_shadow_multi.sh evening|premarket|cutoff" >&2; exit 2 ;;
esac

# Never overlap the greenfield shadow or x_jump_ms_v1 (they write the inputs this job reads).
for _ in $(seq 1 40); do
  pgrep -f "factor_factory/scripts/forward_shadow.py" >/dev/null || pgrep -f x_jump_ms_shadow.py >/dev/null || break
  sleep 30
done

OUT=$("$PY" "$WS/factor_factory/scripts/forward_shadow_multi.py" "${ARGS[@]}" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "⚠️ 【多账本前向影子】$MODE 运行失败 (退出码 $RC)"
  echo "$OUT" | grep -v -i warning | tail -n 4
  exit "$RC"
fi
LINE=$(printf '%s\n' "$OUT" | grep '^{' | head -n 1)
read -r D STATUS OFF < <(printf '%s' "$LINE" | "$PY" -c 'import json,sys
r=json.loads(sys.stdin.read() or "{}")
ob=r.get("official_books") or {}
# 2026-11-20 zero-paid: Core7P books need Tushare VIP announcement tables, so per contract they stay non-official
# forever ("core7p" = everything else official, which is the expected steady state, not a fault).
core = [k for k in ob if k.startswith("aegis_core7p")]
rest = [v for k, v in ob.items() if k not in core]
off = "all" if ob and all(ob.values()) else ("core7p" if rest and all(rest) else ("some" if any(ob.values()) else "none"))
print(r.get("signal_date","-"), r.get("status","unknown"), off)' 2>/dev/null)

case "$STATUS" in
  decided)
    if [ "$OFF" = "all" ]; then MSG="📗 【多账本前向影子】$D 已正式入账（全部账本）"
    elif [ "$OFF" = "core7p" ]; then MSG="📗 【多账本前向影子】$D 已正式入账（EW6 两本 + Micro5P 无 cgo 口径；Core7P 零付费下按合同非正式）"
    else MSG="⚠️ 【多账本前向影子】$D 已入账但非全部正式（official=$OFF），详见 multi_v1/gaps.jsonl"; fi ;;
  gap_logged|kline_gate_failed) MSG="⚠️ 【多账本前向影子】$D 状态 $STATUS（已登记缺口，不回补）" ;;
  waiting_for_*)
    [ "$MODE" = "premarket" ] && [ "$(date +%H%M)" -ge 0850 ] && MSG="⏳ 【多账本前向影子】$D 仍在等待（$STATUS），09:35 兜底"
    ;;
  already_decided|not_a_session_today) ;;
  *) MSG="⚠️ 【多账本前向影子】$D 状态: ${STATUS:-unknown}" ;;
esac
PAR=$(printf '%s\n' "$OUT" | grep '"spearman"' | tail -n 1)
if [ -n "$PAR" ] && ! printf '%s' "$PAR" | grep -q '"ok": true'; then
  MSG="${MSG:+$MSG
}⚠️ 【多账本前向影子】评分复算一致性未通过: $PAR"
fi

LEDGER=(factor_factory/data/forward_shadow/multi_v1)
if [ -n "$(git status --porcelain -- "${LEDGER[@]}" 2>/dev/null)" ]; then
  for _ in 1 2 3; do
    git add -- "${LEDGER[@]}" 2>/dev/null && git commit -q -m "chore(forward-shadow-multi): $D $MODE" -- "${LEDGER[@]}" 2>/dev/null && break
    sleep 20
  done
fi
[ -n "${MSG:-}" ] && echo "$MSG"
exit 0
