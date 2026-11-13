#!/bin/bash
# Greenfield Phase 4 forward shadow (comp_rank_ew6, h=5). Research only: reads production caches,
# writes factor_factory/data/forward_shadow/ and outputs/forward_shadow/*.md, commits only those paths.
# Hermes job 7f838fa43045 (23:30 weekdays) runs ~/.hermes/scripts/stock/greenfield_forward_shadow.sh,
# deployed from ops/hermes/stock/ by ops/hermes/deploy.py. Stdout is delivered to the stock Discord channel: the v4.3 cross-audit
# summary on success, one short line on gaps/failures.
# Usage: greenfield_forward_shadow.sh [YYYYMMDD]   (default today; a manual rerun must finish before 09:30
# of the next session, otherwise forward_shadow.py logs a gap)
set -uo pipefail
export TZ=Asia/Shanghai
export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
PY="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_ROOT/venv/bin/python}"
cd "$WS" || exit 2

mtime_display() {
  if stat --version >/dev/null 2>&1; then
    stat -c '%y' "$1" 2>/dev/null || true
  else
    stat -f '%Sm' -t '%m-%d %H:%M' "$1" 2>/dev/null || true
  fi
}

GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job greenfield_forward_shadow 2>&1)
GATE_RC=$?
printf "%s\n" "$GATE_OUT"
[ "$GATE_RC" -eq 3 ] && exit 0
if [ "$GATE_RC" -ne 0 ]; then
  GATE_FIRST=$(printf "%s\n" "$GATE_OUT" | grep -v '^$' | head -n 1)
  echo "${GATE_FIRST:-⚠️ 【绿地前瞻影子】交易日历检查失败 (rc=$GATE_RC)}" >&2
  exit "$GATE_RC"
fi

D="${1:-$(date +%Y%m%d)}"
# Never overlap the 22:15 research job: it writes today's daily_basic and PIT universe.
for _ in $(seq 1 100); do
  pgrep -f run_post_publish_research.py >/dev/null || break
  sleep 30
done
if pgrep -f run_post_publish_research.py >/dev/null; then
  echo "⚠️ 【绿地前瞻影子】22:15 研究任务 50 分钟后仍在运行，今晚未记录 $D（次日开盘前可手动补跑）"
  echo "⚠️ 【绿地前瞻影子】22:15 研究任务仍在运行，今晚未记录 $D" >&2
  exit 75
fi

# Unit/magnitude guard on the production caches (amount=千元, circ_mv=万元): warn-only, never blocks the record.
UNIT_WARN=""
"$PY" "$WS/factor_factory/scripts/unit_guard.py" >/dev/null 2>&1 || UNIT_WARN="⚠️ 【单位校验】生产缓存量级校验未通过（成交额/市值/换手率关系异常），请检查 factor_factory/scripts/unit_guard.py 输出"

OUT=$("$PY" "$WS/factor_factory/scripts/forward_shadow.py" --date "$D" 2>&1)
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "⚠️ 【绿地前瞻影子】运行失败 $D (退出码 $RC)"
  echo "$OUT" | grep -v -i warning | tail -n 4
  echo "⚠️ 【绿地前瞻影子】运行失败 $D (退出码 $RC)" >&2
  exit "$RC"
fi
STATUS=$(printf '%s\n' "$OUT" | tail -n 1 | "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("status",""))' 2>/dev/null)
case "$STATUS" in
  recorded|already_recorded) ;;
  *) echo "⚠️ 【绿地前瞻影子】$D 状态: ${STATUS:-unknown}"; exit 0 ;;
esac

DIGEST=$("$PY" "$WS/factor_factory/scripts/forward_shadow_digest.py" --date "$D" 2>&1)
DRC=$?

# DOUBLE-PASS tier monitor: settle matured T+1/T+5 periods from the tier ledger (research only, non-blocking).
TIER=$("$PY" "$WS/factor_factory/scripts/tier_monitor.py" settle 2>&1 | grep -v -i warning | tail -n 1)

# Daily rolling cohorts (forward_shadow_cohort_v1, frozen 2026-10-20): record cohort D, settle matured windows.
# Research only and non-blocking for v1; a missing official cohort row is alerted below.
COH_REC=$("$PY" "$WS/factor_factory/scripts/cohort_monitor.py" record --date "$D" 2>&1 | grep -v -i warning | tail -n 1)
COH=$("$PY" "$WS/factor_factory/scripts/cohort_monitor.py" settle 2>&1 | grep -v -i warning | tail -n 1)

# Factor lifecycle board: rolling IC / decay / live-vs-backtest gap / drift alerts (research only, non-blocking).
LIFE=$("$PY" "$WS/factor_factory/scripts/lifecycle_monitor.py" 2>&1 | grep -v -i warning | tail -n 1)
# Holding-level style/industry attribution + factor redundancy (research only, non-blocking).
"$PY" "$WS/factor_factory/scripts/style_attribution.py" >/dev/null 2>&1

# Commit only the ledger + digests (scores/ and test ledgers are gitignored); other staged paths stay untouched.
LEDGER=(factor_factory/data/forward_shadow factor_factory/outputs/forward_shadow)
COMMIT_NOTE=""
for _ in 1 2 3; do
  if git add -- "${LEDGER[@]}" 2>/dev/null; then
    if git diff --cached --quiet -- "${LEDGER[@]}"; then
      COMMIT_NOTE="🔒 台账无变更，未提交"; break
    fi
    if git commit -q -m "chore(forward-shadow): $D ledger & v4.3 cross-audit" -- "${LEDGER[@]}" 2>/dev/null; then
      COMMIT_NOTE="🔒 台账已提交 $(git log -1 --format='%h %cd' --date=format-local:'%m-%d %H:%M')"; break
    fi
  fi
  sleep 20  # another git process may hold index.lock
done
if [ -z "$COMMIT_NOTE" ]; then
  COMMIT_NOTE="⚠️ 台账 git 提交失败（文件已写入，需手动提交）"
  # 2026-10-19: a 0-byte index.lock left at 10-18 21:10 by a crashed git blocked every workspace commit.
  LOCK="$WS/.git/index.lock"
  if [ -e "$LOCK" ] && ! pgrep -x git >/dev/null; then
    COMMIT_NOTE="$COMMIT_NOTE；疑似陈旧 .git/index.lock（$(mtime_display "$LOCK")，无 git 进程），确认后移除"
  fi
fi

# Completeness guard (2026-10-16): the tier ledger row is written as a side effect of the digest, and a late row is
# official=false forever. Verify the row exists and is official; otherwise say loudly that it must be rerun before 09:30.
TIER_OK=$("$PY" - "$D" <<'PYEOF' 2>/dev/null
import json, sys
d = sys.argv[1]
p = "factor_factory/data/forward_shadow/tier_ledger.jsonl"
try:
    rows = [json.loads(l) for l in open(p) if l.strip()]
except OSError:
    rows = []
print("ok" if any(r.get("signal_date") == d and r.get("official") for r in rows) else "missing")
PYEOF
)
if [ "$TIER_OK" != "ok" ]; then
  echo "🚨 【双料王牌台账缺口】$D 的 tier_ledger 官方行未写入：必须在次日 09:30 前修复并重跑 greenfield_forward_shadow.sh $D，否则该日样本永久作废"
fi

case "$COH_REC" in
  *": recorded"|*": exists") ;;
  *) echo "🚨 【滚动分组台账缺口】$D cohort 未作为官方组入账（${COH_REC:-无输出}）：必须在次日 09:30 前修复并重跑 greenfield_forward_shadow.sh $D" ;;
esac

if [ "$DRC" -ne 0 ]; then
  echo "⚠️ 【绿地前瞻影子】$D 已记录，但日报生成失败 (退出码 $DRC)"
  echo "$DIGEST" | grep -v -i warning | tail -n 3
else
  echo "$DIGEST" | grep -v -i warning
fi
[ -n "$TIER" ] && echo "📊 $TIER"
[ -n "$COH" ] && echo "🔁 $COH"
[ -n "$LIFE" ] && { case "$LIFE" in *"预警 0"*) echo "📈 $LIFE";; *) echo "⚠️ 📈 $LIFE";; esac; }
[ -n "$UNIT_WARN" ] && echo "$UNIT_WARN"
echo "$COMMIT_NOTE"
exit 0
