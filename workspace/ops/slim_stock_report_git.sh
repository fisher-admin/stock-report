#!/bin/bash
# Slim the stock-report git history (249MB .git → ~10MB), run on the Mac.
# PREREQUISITES: (1) new GitHub PAT already in .secrets/git-credentials,
# (2) old PAT revoked, (3) no pipeline run in progress (run before 19:00).
#
# Strategy: keep full current worktree, rewrite history to the last 90 days,
# force-push, then re-clone the working copy. A full backup bundle is made first.
set -euo pipefail

WS="$HOME/.openclaw/workspace"
REPO="$WS/stock-report"
WORKING="$WS/stock_data/03-working/stock-report-repo"
BACKUP="$WS/trash/stock-report-git-backup-$(date +%Y%m%d)"

command -v git-filter-repo >/dev/null 2>&1 || {
  echo "需要 git-filter-repo: brew install git-filter-repo"; exit 1; }

read -r -p "⚠️ 将重写 stock-report 远端历史（保留近90天）。已轮换PAT且今晚管线未开始？(yes/no) " ok
[ "$ok" = "yes" ] || exit 1

mkdir -p "$BACKUP"
echo "==> 1/5 完整备份 bundle"
git -C "$REPO" bundle create "$BACKUP/stock-report-full.bundle" --all

echo "==> 2/5 历史裁剪（fresh clone 上操作）"
TMP="$(mktemp -d)"
git clone "$REPO" "$TMP/slim"
cd "$TMP/slim"
CUTOFF_SHA=$(git log --since="90 days ago" --reverse --format=%H | head -1)
[ -n "$CUTOFF_SHA" ] || { echo "找不到90天截断点"; exit 1; }
git replace --graft "$CUTOFF_SHA"
git filter-repo --force
git remote add origin https://github.com/fisher-admin/stock-report.git

echo "==> 3/5 强推新历史（需确认）"
read -r -p "执行 push --force 到 origin main? (yes/no) " ok2
[ "$ok2" = "yes" ] || exit 1
git -c credential.helper="store --file=$WS/.secrets/git-credentials" push --force origin main

echo "==> 4/5 替换本地两个克隆"
mv "$REPO" "$BACKUP/stock-report-old"
git clone https://github.com/fisher-admin/stock-report.git "$REPO"
git -C "$REPO" config credential.helper "store --file=$WS/.secrets/git-credentials"
mv "$WORKING" "$BACKUP/stock-report-repo-old"
git clone https://github.com/fisher-admin/stock-report.git "$WORKING"
git -C "$WORKING" config credential.helper "store --file=$WS/.secrets/git-credentials"

echo "==> 5/5 体积对比"
du -sh "$BACKUP/stock-report-old/.git" "$REPO/.git"
echo "🎉 完成。旧仓与 bundle 备份在 $BACKUP（确认无误后可移除）"
