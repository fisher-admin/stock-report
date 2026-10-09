#!/bin/bash
# ============================================================================
# 唯一允许推送 GitHub Pages 的 v2 发布路径（2026-06 收口）。
# 流程：v2 发布层生成统一合同 JSON → 合同硬校验(--strict) → 校验通过才推送。
# 合同校验失败 = 绝不推送、非零退出、记录原因，阻止旧/坏合同覆盖线上。
# 旧路径（stock-report-repo 的 generate_github_pages.py + 裸 push）已被本脚本取代。
# ============================================================================
set -uo pipefail

# Caller-supplied batch identity (stage6 exports RUN_ID/TRADE_DATE).  Captured
# before this script reuses those names for the run_manifest values below.
EXPECTED_RUN_ID="${RUN_ID:-}"
EXPECTED_TRADE_DATE="${TRADE_DATE:-}"

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

# GitHub's HTTP/2 connection stalled on 2026-09-09.
# Scope the compatibility/stall policy to publisher network calls only.
network_git() {
  GIT_TERMINAL_PROMPT=0 command git -c http.version=HTTP/1.1 -c http.lowSpeedLimit=1 -c http.lowSpeedTime=20 "$@"
}

LOCK_FILE="$WS/stock_data/03-working/health/stock_publisher.lock"

# 0) 获取原子发布锁（绑定当前 shell 进程 PID $$ 与 owner token，防并发多发布者抢跑与非法删锁）
LOCK_OUT=$("$PY" "$SCRIPTS/publish_lock.py" --acquire --lock-file "$LOCK_FILE" --pid $$ 2>>"$LOG") || {
  log "FATAL: 发布锁获取失败，已有发布进程在运行"
  exit 4
}
LOCK_TOKEN=$(printf "%s\n" "$LOCK_OUT" | sed -n 's/^TOKEN=//p' | head -1)

cleanup() {
  "$PY" "$SCRIPTS/publish_lock.py" --release --lock-file "$LOCK_FILE" --token "$LOCK_TOKEN" --pid $$ >>"$LOG" 2>&1 || true
  if [ -n "${PAGES_PREFLIGHT_DIR:-}" ] && [ -d "$PAGES_PREFLIGHT_DIR" ]; then
    rm -rf -- "$PAGES_PREFLIGHT_DIR"
  fi
}
trap cleanup EXIT


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

# 2b) recommendation_history.csv 等公开摘要必须从刷新后的统一复盘重建。
SUMMARY_SCRIPT="$PUB_REPO/generate_view_summaries.py"
if [ ! -f "$SUMMARY_SCRIPT" ]; then
  log "FATAL: 公开摘要生成器不存在 $SUMMARY_SCRIPT"; exit 2
fi
log "step2b: 重建公开战绩摘要（generate_view_summaries.py）"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SUMMARY_SCRIPT" >>"$LOG" 2>&1 || { log "FATAL: 公开战绩摘要生成失败"; exit 2; }
if [ ! -f "$PUB_REPO/data/latest/review_track_latest.json" ]; then
  log "FATAL: 公开战绩摘要未生成 data/latest/review_track_latest.json"; exit 2
fi

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

# 3m) 维护模式（2026-10-09）——开启时撤下全部个股级名单/个股文本，写入“系统校准升级中”状态。
#     开关：STOCK_SYSTEM_MAINTENANCE_MODE 或 $WS/config/stock_maintenance_mode.json（maintenance_mode.py enable/disable）。
#     位于合同校验（校验真实批次）之后、Pages 预检/最终检查之前；配置异常或清理后仍有个股残留即中止：宁停发不错发。
log "step3m: 维护模式（maintenance_mode.py apply）"
if ! MAINT_OUT=$(PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/maintenance_mode.py" apply --repo "$PUB_REPO" 2>>"$LOG"); then
  log "ABORT: 维护模式配置异常或个股清理未通过，拒绝提交/推送（见日志）"
  tail -5 "$LOG"
  exit 1
fi
printf "%s\n" "$MAINT_OUT" >>"$LOG"
log "step3m: $(printf "%s" "$MAINT_OUT" | cut -c1-160)"

# 3a) Pages 产物预检——发布仓当前状态必须能生成最终可部署副本。
#     这一步必须发生在 git add/commit/push 之前；否则半成品会先进入
#     main，再由 Pages CI 拒绝，造成线上旧页面与仓库失败提交长期分叉。
PAGES_BUILDER="$PUB_REPO/scripts/build_pages_artifact.py"
PAGES_PREFLIGHT_DIR="$(mktemp -d /tmp/hermes-pages-preflight.XXXXXX)"
if [ ! -f "$PAGES_BUILDER" ]; then
  log "FATAL: Pages 产物预检脚本不存在: $PAGES_BUILDER"
  exit 2
fi
log "step3a: Pages 可部署产物预检"
if ! PYTHONDONTWRITEBYTECODE=1 "$PY" "$PAGES_BUILDER" --source "$PUB_REPO" --output "$PAGES_PREFLIGHT_DIR" --published-artifact >>"$LOG" 2>&1; then
  log "ABORT: Pages 产物未达到可发布合同，拒绝提交/推送（见日志）"
  tail -30 "$LOG"
  exit 1
fi
log "step3a: Pages 产物预检通过 ✓"

MANIFEST_FILE="$PUB_REPO/data/latest/run_manifest.json"
TRADE_DATE="$("$PY" -c "import json; from pathlib import Path; print(json.loads(Path('$MANIFEST_FILE').read_text()).get('trade_date', ''))" 2>/dev/null || true)"
RUN_ID="$("$PY" -c "import json; from pathlib import Path; print(json.loads(Path('$MANIFEST_FILE').read_text()).get('run_id', ''))" 2>/dev/null || true)"
if ! [[ "$TRADE_DATE" =~ ^[0-9]{8}$ ]]; then
  log "FATAL: run_manifest trade_date 非法或缺失: ${TRADE_DATE:-<empty>}"
  exit 2
fi
if [ -z "$RUN_ID" ]; then
  log "FATAL: run_manifest run_id 缺失"
  exit 2
fi

verify_remote_alignment() {
  LOCAL_SHA="$(git -C "$PUB_REPO" rev-parse HEAD 2>/dev/null || true)"
  REMOTE_SHA="$(network_git -C "$PUB_REPO" ls-remote origin refs/heads/main 2>>"$LOG" | awk '{print $1}')"
  [ -n "$LOCAL_SHA" ] && [ -n "$REMOTE_SHA" ] && [ "$LOCAL_SHA" = "$REMOTE_SHA" ]
}

write_deployment_receipt() {
  "$PY" -c "
import sys
from pathlib import Path
sys.path.insert(0, '$SCRIPTS')
from deployment_receipt import DeploymentReceipt, now_iso
receipt = DeploymentReceipt(
    run_id='$RUN_ID',
    trade_date='$TRADE_DATE',
    local_sha='$LOCAL_SHA',
    remote_sha='$REMOTE_SHA',
    remote_confirmed=True,
    confirmed_at=now_iso(),
)
health_dir = Path('$WS/stock_data/03-working/health')
receipt.save(health_dir / 'deployment_receipt.json')
" >>"$LOG" 2>&1
}

# 3c) 生成防回退监控产物 publish_guard_state.json（随发布一起推送，供系统解码页展示）
#     2026-10-03: moved before the final gate so the gate sees every file that
#     will be committed.
log "step3c: 生成 publish_guard_state.json"
PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/publish_guard.py" >>"$LOG" 2>&1 || log "WARN: publish_guard 生成失败（非阻断）"

# 3d) 最终发布检查（2026-10-03，约束性）——校验即将提交的确切内容：批次身份、
#     结构化裁决 final_action.action、日期合同、候选/策略日期、同日 AI、开市日。
#     新批次遇 halt/畸形/过期/不一致一律拒绝；刷新不得更换推荐名单。
#     必须位于幂等闸之前：幂等“跳过并写回执”路径同样需要先通过本检查。
HEALTH_DIR="$WS/stock_data/03-working/health"
log "step3d: 最终发布检查（final_publication_gate.py validate）"
GATE_OUT=$(PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/final_publication_gate.py" validate \
  --repo "$PUB_REPO" --health-dir "$HEALTH_DIR" \
  --calendar "$HEALTH_DIR/trading_calendar.json" \
  --expected-run-id "$EXPECTED_RUN_ID" --expected-trade-date "$EXPECTED_TRADE_DATE" 2>&1)
GATE_RC=$?
printf "%s\n" "$GATE_OUT" >>"$LOG"
if [ "$GATE_RC" -ne 0 ]; then
  log "ABORT: 最终发布检查未通过，拒绝提交/推送 (RC=$GATE_RC)"
  printf "%s\n" "$GATE_OUT" | tail -5
  exit 5
fi
log "step3d: 最终发布检查通过 ✓ ($(printf "%s" "$GATE_OUT" | tail -1))"

# 3b) 发布幂等闸——同交易日+同内容指纹且已推送成功过 → 跳过重复推送（防串行双发布）
log "step3b: 发布幂等闸（publish_idempotency_gate.py --check）"
if PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/publish_idempotency_gate.py" --check --trade-date "$TRADE_DATE" >>"$LOG" 2>&1; then
  log "step3b: 幂等闸放行"
else
  RC=$?
  if [ "$RC" -eq 7 ]; then
    if ! verify_remote_alignment; then
      log "FATAL: 幂等命中但本地/远程 SHA 不一致，拒绝伪造发布回执"
      exit 3
    fi
    if ! write_deployment_receipt; then
      log "FATAL: 幂等命中后写入 deployment_receipt.json 失败"
      exit 3
    fi
    "$PY" "$SCRIPTS/final_publication_gate.py" mark-pushed --health-dir "$HEALTH_DIR" \
      --sha "$LOCAL_SHA" --outcome idempotent_skip >>"$LOG" 2>&1 || log "WARN: 发布账本记录失败"
    log "SKIP: 幂等命中——同日同内容已推送，跳过本次发布（详见 publish_fingerprint.json）"
    exit 0
  fi
  log "WARN: 幂等闸内部错误(RC=$RC)，保守放行不阻断"
fi

# 4) 推送（从 v2 克隆，带 rebase 恢复抗多克隆竞态）
# 公开页 = 合同 JSON + 前端资源 + 根目录 html 壳（标题/入口）。不提交 tests/。
cd "$PUB_REPO" || { log "FATAL: 发布仓不存在 $PUB_REPO"; exit 2; }
git add data/ assets/ *.html 2>>"$LOG"
# 4a) 提交前公开边界审计（2026-10-09）——只审将要提交的暂存内容：数据文件必须在
#     config/public-result-allowlist.txt 内、无逐股明细/本机路径/原始表格格式。不删除本机文件。
if ! BOUNDARY_OUT=$(PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/committed_public_boundary.py" --repo "$PUB_REPO" 2>>"$LOG"); then
  printf "%s\n" "$BOUNDARY_OUT" >>"$LOG"
  git reset -q 2>>"$LOG" || true
  log "ABORT: 待提交内容超出公开结果白名单，已撤销暂存，拒绝提交/推送（见日志）"
  printf "%s\n" "$BOUNDARY_OUT" | cut -c1-400
  exit 1
fi
log "step4a: 提交前公开边界审计通过 ✓ $(printf "%s" "$BOUNDARY_OUT" | cut -c1-80)"
if ! git diff --cached --quiet; then
  git commit -m "publish(v2): ${TRADE_DATE} 合同校验通过" >>"$LOG" 2>&1 || {
    log "FATAL: git commit 失败"
    exit 3
  }
else
  log "step4: 无改动，跳过提交"
fi
if ! "$PY" "$SCRIPTS/final_publication_gate.py" record-commit --repo "$PUB_REPO" --health-dir "$HEALTH_DIR" >>"$LOG" 2>&1; then
  log "FATAL: 无法记录已验证提交，拒绝推送"
  exit 3
fi

log "step4: fetch + rebase + push origin main"
network_git fetch origin >>"$LOG" 2>&1 || { log "FATAL: git fetch 失败"; exit 3; }
# 未提交的测试/文档改动不能挡住合同推送。stash 仅覆盖工作区脏文件，发布提交本身不受影响。
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
  log "step4: 工作区有未提交改动，先 stash 再 rebase"
  if git stash push -m "publisher-autostash-$TS" >>"$LOG" 2>&1; then
    STASHED=1
  else
    log "FATAL: 无法 stash 未提交改动，拒绝 rebase"
    exit 3
  fi
fi
restore_publisher_stash() {
  if [ "$STASHED" = 1 ]; then
    if git stash pop >>"$LOG" 2>&1; then
      log "step4: 已恢复发布前工作区改动"
    else
      log "WARN: stash pop 失败，改动仍在 git stash（publisher-autostash-$TS）"
    fi
    STASHED=0
  fi
}
if network_git pull --rebase -X theirs origin main >>"$LOG" 2>&1; then
  # 2026-10-03: rebase may merge a concurrent writer's changes into published
  # paths.  Push only if published content is byte-identical to what passed
  # step3d; otherwise stop and require a fresh validation run.
  if ! "$PY" "$SCRIPTS/final_publication_gate.py" verify-after-rebase --repo "$PUB_REPO" --health-dir "$HEALTH_DIR" >>"$LOG" 2>&1; then
    restore_publisher_stash
    log "ABORT: rebase 后发布内容与已验证内容不同，需重新校验，拒绝推送"
    exit 3
  fi
  if network_git push origin main >>"$LOG" 2>&1; then
    if ! verify_remote_alignment; then
      restore_publisher_stash
      log "FATAL: 远程 SHA ($REMOTE_SHA) 与本地 HEAD ($LOCAL_SHA) 不一致"
      exit 3
    fi
    log "OK: 已推送 $(git rev-parse --short HEAD) → origin/main"
    # 推送成功 → 记录内容指纹，供幂等闸下次判重
    PYTHONDONTWRITEBYTECODE=1 "$PY" "$SCRIPTS/publish_idempotency_gate.py" --mark-pushed --trade-date "$TRADE_DATE" >>"$LOG" 2>&1 || log "WARN: 指纹记录失败（非阻断）"

    write_deployment_receipt || {
      restore_publisher_stash
      log "FATAL: 写入 deployment_receipt.json 失败"
      exit 3
    }
    "$PY" "$SCRIPTS/final_publication_gate.py" mark-pushed --health-dir "$HEALTH_DIR" \
      --sha "$LOCAL_SHA" --outcome pushed >>"$LOG" 2>&1 || log "WARN: 发布账本记录失败（非阻断）"
    restore_publisher_stash
  else
    restore_publisher_stash
    log "ERROR: push 失败（见日志）"; tail -15 "$LOG"; exit 3
  fi
else
  log "ERROR: rebase 失败，未推送（见日志）"; git rebase --abort 2>/dev/null || true
  restore_publisher_stash
  exit 3
fi
log "== v2 发布完成 =="
