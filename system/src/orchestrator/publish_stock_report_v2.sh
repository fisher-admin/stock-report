#!/bin/bash
# ============================================================================
# 唯一允许推送 GitHub Pages 的 v2 发布路径（2026-06 收口）。
# 流程：v2 发布层生成统一合同 JSON → 合同硬校验(--strict) → 校验通过才推送。
# 合同校验失败 = 绝不推送、非零退出、记录原因，阻止旧/坏合同覆盖线上。
# 旧路径（stock-report-repo 的 generate_github_pages.py + 裸 push）已被本脚本取代。
# ============================================================================
set -uo pipefail

STOCK_ROOT="${STOCK_SYSTEM_ROOT:-$HOME/.openclaw}"
WS="${STOCK_SYSTEM_WORKSPACE:-$STOCK_ROOT/workspace}"
SCRIPTS="$WS/skills/stock-system-orchestrator/scripts"
PUB_REPO="${OPENCLAW_PUBLISHED_REPO:-$WS/stock-report}"
PY="${STOCK_SYSTEM_PYTHON:-$STOCK_ROOT/venv/bin/python}"
LOG_DIR="${OPENCLAW_LOG_DIR:-$STOCK_ROOT/logs}/publish_v2"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/publish_v2_$TS.log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

log "== v2 发布开始 =="

# 1) 生成 v2 合同 latest JSON（写入 PUBLISHED_REPO/data/latest）
log "step1: 生成 v2 合同（strategy_publication_layer.py）"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/strategy_publication_layer.py" >>"$LOG" 2>&1 || { log "FATAL: 发布层运行失败"; exit 2; }

# 1a) 推荐收益修复后必须同步重建行业派生统计，禁止旧 -100% 占位滞留在热力图。
log "step1a: 重建行业推荐统计（generate_industry_heatmap.py）"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/generate_industry_heatmap.py" >>"$LOG" 2>&1 || { log "FATAL: 行业推荐统计生成失败"; exit 2; }

# 2) 刷新其它 latest 状态（非阻断：缺失时前端有回退）
if [ -f "$SCRIPTS/generate_latest_states.py" ]; then
  log "step2: 刷新 latest 状态（generate_latest_states.py）"
  PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/generate_latest_states.py" >>"$LOG" 2>&1 || log "WARN: generate_latest_states 失败（非阻断）"
fi

# 2a) latest/review_state 刚由分析导出刷新；重建统一复盘，避免沿用上一轮旧战绩。
log "step2a: 用刷新后的复盘重建发布合同（strategy_publication_layer.py）"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/strategy_publication_layer.py" >>"$LOG" 2>&1 || { log "FATAL: 刷新后发布层运行失败"; exit 2; }

# 2b) 组合级公开摘要必须从刷新后的统一复盘重建；逐股 CSV 不公开。
SUMMARY_SCRIPT="$PUB_REPO/generate_view_summaries.py"
if [ ! -f "$SUMMARY_SCRIPT" ]; then
  log "FATAL: 公开摘要生成器不存在 $SUMMARY_SCRIPT"; exit 2
fi
log "step2b: 重建公开战绩摘要（generate_view_summaries.py）"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SUMMARY_SCRIPT" >>"$LOG" 2>&1 || { log "FATAL: 公开战绩摘要生成失败"; exit 2; }

# 2c) 发布双轨观察与真实评价合同；任何代理、伪收益或执行权限漂移都会阻断发布
log "step2c: 生成双轨观察与评价合同（dual_track_publication.py）"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/dual_track_publication.py" >>"$LOG" 2>&1 || { log "FATAL: 双轨发布合同失败"; exit 2; }

# 2d) 发布安全清理——去除本机绝对路径，敏感字段或残留私有路径会阻断发布
log "step2d: 清理公开 JSON 的本机路径并检查敏感字段"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/sanitize_public_report.py" >>"$LOG" 2>&1 || { log "FATAL: 公开报告安全检查失败"; exit 2; }

# 3) 合同硬校验——失败即退出、绝不推送
log "step3: 合同硬校验（validate_publication_contract.py --strict）"
if ! PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/validate_publication_contract.py" --strict >>"$LOG" 2>&1; then
  log "ABORT: 发布合同校验失败，拒绝推送（见日志）"
  tail -20 "$LOG"
  exit 1
fi
log "step3: 合同校验通过 ✓"

# 3b) 生成防回退监控产物 publish_guard_state.json（随发布一起推送，供系统解码页展示）
log "step3b: 生成 publish_guard_state.json"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/publish_guard.py" >>"$LOG" 2>&1 || log "WARN: publish_guard 生成失败（非阻断）"

# 3c) 只保留公开结果白名单；删除本机明细后再次审计，失败则拒绝推送。
BOUNDARY_SCRIPT="$PUB_REPO/scripts/enforce_public_boundary.py"
if [ ! -f "$BOUNDARY_SCRIPT" ]; then
  log "FATAL: 公开边界检查器不存在 $BOUNDARY_SCRIPT"; exit 2
fi
log "step3c: 清除本机明细并执行公开结果白名单审计"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$BOUNDARY_SCRIPT" --root "$PUB_REPO" --prepare >>"$LOG" 2>&1 || { log "ABORT: 公开边界审计失败，拒绝推送"; exit 1; }

# 4) 推送（从 v2 克隆，带 rebase 恢复抗多克隆竞态）
cd "$PUB_REPO" || { log "FATAL: 发布仓不存在 $PUB_REPO"; exit 2; }
git add data/ assets/ 2>>"$LOG"
if git diff --cached --quiet; then
  log "step4: 无改动，跳过提交"
else
  git commit -m "publish(v2): $(date +%Y%m%d) 合同校验通过" >>"$LOG" 2>&1 || true
fi
log "step4: fetch + rebase + push origin main"
git fetch origin >>"$LOG" 2>&1 || true
if git pull --rebase -X theirs origin main >>"$LOG" 2>&1; then
  if git push origin main >>"$LOG" 2>&1; then
    log "OK: 已推送 $(git rev-parse --short HEAD) → origin/main"
  else
    log "ERROR: push 失败（见日志）"; tail -15 "$LOG"; exit 3
  fi
else
  log "ERROR: rebase 失败，未推送（见日志）"; git rebase --abort 2>/dev/null || true; exit 3
fi
log "== v2 发布完成 =="
