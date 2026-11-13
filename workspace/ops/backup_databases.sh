#!/bin/bash
# Nightly/weekly SQLite backup for the stock system (run on the Mac).
# Backs up the three live databases with sqlite3 online .backup (safe under WAL),
# keeps 14 daily copies, and runs a monthly VACUUM INTO compact copy.
#
# Suggested cron (OpenClaw or crontab):  daily 21:30  bash ~/.openclaw/workspace/ops/backup_databases.sh
set -euo pipefail

# 2026-11-11: follow the cron FORCE vars like the wrappers; the live workspace is
# /srv/hermes-stock/workspace on the VPS (the old $HOME/.openclaw path is a stub there).
WS="${STOCK_SYSTEM_WORKSPACE_FORCE:-${STOCK_SYSTEM_WORKSPACE:-$HOME/.openclaw/workspace}}"
DEST="$WS/stock_data/03-working/backups"
STAMP="$(date +%Y%m%d)"
# 20260710事故: KEEP_DAYS=14 x ~1.7G/天 ≈ 24G 超出磁盘余量, 7/8-7/9 ENOSPC 爆盘——
# 选股/AI/收盘管线连续两晚失败、Gateway崩溃(session/sqlite写不进)。改留5天(~8.5G)。
KEEP_DAYS=5
OK_COUNT=0
SKIP_COUNT=0

mkdir -p "$DEST"

# 磁盘水位护栏(20260710): 备份前检查可用空间, <8G 先按最老优先额外裁剪到5份以内;
# 仍 <4G 则跳过本次备份并大声报错(备份是冗余副本, 绝不能反过来挤死生产管线)。
# 2026-11-11: df -g is BSD/macOS only; GNU df rejects it, which silently disabled
# both disk guards on Linux. df -k works on both; $4 is Available in 1K blocks.
avail_gb() { df -k / | tail -1 | awk '{printf "%d", $4/1024/1024}'; }
if [ "$(avail_gb)" -lt 8 ]; then
  echo "⚠️ 磁盘可用 $(avail_gb)G <8G, 备份前额外裁剪最老备份" >&2
  # 20261009: 日备份均已 gzip，原 glob 只匹配 .db → 无匹配时 ls 非零, 在 pipefail 下中断整个备份。
  # 现同时匹配 .db/.db.gz（月度 compact 为 6 位日期, 不在此列）, 无匹配时安全跳过。
  shopt -s nullglob
  dailies=("$DEST"/*.????????.db "$DEST"/*.????????.db.gz)
  shopt -u nullglob
  if [ "${#dailies[@]}" -gt 15 ]; then
    ls -1t "${dailies[@]}" | tail -n +16 | while IFS= read -r old; do rm -f "$old"; done
  fi
fi
if [ "$(avail_gb)" -lt 4 ]; then
  echo "🚨 磁盘可用 $(avail_gb)G <4G, 跳过本次备份(防挤死生产管线), 请立即人工清理磁盘!" >&2
  exit 1
fi

backup_one() {
  local src="$1" name="$2"
  if [ ! -f "$src" ]; then echo "skip (missing): $src" >&2; SKIP_COUNT=$((SKIP_COUNT + 1)); return 0; fi
  local out="$DEST/${name}.${STAMP}.db"
  sqlite3 "$src" ".backup '$out'"
  gzip -f "$out"
  local gz_out="${out}.gz"
  echo "✅ $name -> $gz_out ($(du -h "$gz_out" | cut -f1))" >&2
  OK_COUNT=$((OK_COUNT + 1))
}

backup_one "$WS/daily_stock_analysis/data/stock_analysis.db" "ai_engine_stock_analysis"
backup_one "$WS/data/stock_analysis.db" "workspace_stock_analysis"
backup_one "$WS/stock_data/03-working/recommendation_warehouse/recommendations.db" "recommendations"
# 2026-11-11: cron home follows HERMES_HOME (live: /srv/hermes-stock/hermes-home).
CRON_HOME="${HERMES_HOME:-$HOME/.hermes}"
backup_one "$CRON_HOME/cron/executions.db" "hermes_cron_executions"
if [ -f "$CRON_HOME/cron/jobs.json" ]; then
  cp "$CRON_HOME/cron/jobs.json" "$DEST/hermes_cron_jobs.${STAMP}.json"
  gzip -f "$DEST/hermes_cron_jobs.${STAMP}.json"
  echo "✅ hermes_cron_jobs -> $DEST/hermes_cron_jobs.${STAMP}.json.gz" >&2
  OK_COUNT=$((OK_COUNT + 1))
else
  echo "skip (missing): $CRON_HOME/cron/jobs.json" >&2
  SKIP_COUNT=$((SKIP_COUNT + 1))
fi

# Monthly compact copy of the big warehouse (1st of month)
KEEP_COMPACT=6
if [ "$(date +%d)" = "01" ]; then
  compact_out="$DEST/recommendations.compact.$(date +%Y%m).db"
  sqlite3 "$WS/stock_data/03-working/recommendation_warehouse/recommendations.db" \
    "VACUUM INTO '$compact_out'"
  gzip -f "$compact_out"
  echo "✅ monthly VACUUM INTO done -> ${compact_out}.gz" >&2
  # Rotate: keep only the most recent KEEP_COMPACT compact archives (sorted by filename, which is YYYYMM-ordered).
  compact_count=$(find "$DEST" -maxdepth 1 -type f \( -name 'recommendations.compact.*.db.gz' -o -name 'recommendations.compact.*.db' \) | wc -l | tr -d ' ')
  compact_excess=$((compact_count - KEEP_COMPACT))
  if [ "$compact_excess" -gt 0 ]; then
    find "$DEST" -maxdepth 1 -type f \( -name 'recommendations.compact.*.db.gz' -o -name 'recommendations.compact.*.db' \) -print \
      | sort \
      | head -n "$compact_excess" \
      | while IFS= read -r old_compact; do rm -f "$old_compact"; done
  fi
  echo "🧹 pruned compact archives beyond last ${KEEP_COMPACT}" >&2
fi

# Prune old dailies (both .db and .db.gz)
find "$DEST" \( -name "*.????????.db" -o -name "*.????????.db.gz" \) -mtime +"$KEEP_DAYS" -delete
echo "🧹 pruned backups older than ${KEEP_DAYS}d; current usage: $(du -sh "$DEST" | cut -f1)" >&2
echo "数据库备份完成：成功 ${OK_COUNT} 个，跳过 ${SKIP_COUNT} 个，当前占用 $(du -sh "$DEST" | cut -f1)"
