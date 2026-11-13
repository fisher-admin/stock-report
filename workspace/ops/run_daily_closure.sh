#!/bin/bash
# Daily closure for the stock system (overhaul 2026-06-12) — run AFTER the
# 17:30 main pipeline + 18:00 AI bridge, suggested cron: 18:45 Asia/Shanghai.
#
# 1. Incremental T+1/T+3/T+5 verification (revives the loop dead since 3/25)
# 2. Strategy governance evaluation (promotion/demotion flags)
# 3. Active health alert push (Discord if configured)
set -uo pipefail

WS="$HOME/.openclaw/workspace"
PY="$HOME/.openclaw/venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "=== [1/3] T+1/T+3/T+5 验证回填(增量) ==="
"$PY" "$WS/fisher-quant/ops/run_verify_backfill.py" || echo "⚠️ verify backfill failed (non-fatal)"

echo "=== [2/3] 策略治理评估 ==="
"$PY" "$WS/fisher-quant/ops/run_governance_eval.py" || echo "⚠️ governance eval failed (non-fatal)"

echo "=== [3/3] 健康告警 ==="
"$PY" "$WS/fisher-quant/ops/health_alert.py" || echo "⚠️ health alert failed (non-fatal)"

echo "=== daily closure core done ($(date '+%F %T')) ==="

# caffeinate is retained for active PIT ingestion and shadow research only.
CAFFEINATE=""
command -v caffeinate >/dev/null 2>&1 && CAFFEINATE="caffeinate -i"

# WTS and S3 are archived historical controls. Their daily signal generation is intentionally disabled.

# === PEAD 业绩预告摄取 + PIT 双写(非致命) ===
# B-5决议(7/11)落地欠账: 东财 yjyg 为 latest-only 源, 每个不摄取的交易日都是永久 vintage 丢失
# (7/13-7/24 已丢约9个交易日, 2026-07-26 月度体检发现后恢复例行)。PIT 店 append-only, 重复跑安全。
echo "=== [附] PEAD业绩预告摄取(PIT双写,非致命) ==="
$CAFFEINATE "$PY" "$WS/skills/stock-system-orchestrator/scripts/ingest_pead_preannounce.py" || echo "⚠️ pead ingest failed (non-fatal)"

# Superseded prebreakout shadow research is archived. The Stage 3 short-track
# runner is now the sole forward shadow path (control / Top15 / balanced).

# === 保护三层闸 · 影子重放常态化(非致命) ===
# B-4决议: 影子常态化一周(7/11起), 7/18后拍板接线第18族。当前纯影子/只读, 对生产零扰动:
#   走 recommendations.db mode=ro + MemLockStore 纯内存零残留, 仅落 health/ 影子产物。
echo "=== [附] 保护闸影子重放(影子常态化,非致命) ==="
"$WS/ops/run_protection_shadow.sh" || echo "⚠️ protection shadow failed (non-fatal)"

# === 双轨竞技场滚动回放(非致命, 20261010 用户批准) ===
# 每个交易日收盘闭环后重放最近 30 个信号日的 v4.3/v4.4（研究回测，只读缓存，约 70s），
# 只写 research/strategy_arena/；次日 18:00 发布器 step2f 导出聚合账本。主管线仍持锁时跳过。
echo "=== [附] 双轨竞技场滚动回放(非致命) ==="
ARENA_TO=""; for t in timeout gtimeout /opt/homebrew/bin/timeout; do command -v "$t" >/dev/null 2>&1 && { ARENA_TO="$t 900"; break; }; done
(cd "$WS/skills/stock-analyzer" && $ARENA_TO "$PY" "$WS/skills/stock-system-orchestrator/scripts/strategy_arena_replay.py" --skip-if-pipeline-running 2>&1 | grep -vE "特征覆盖|📐" | tail -25) \
  || echo "⚠️ arena replay failed (non-fatal)"

echo "=== daily closure done ($(date '+%F %T')) ==="
