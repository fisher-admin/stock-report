#!/bin/bash
# clean_publish_history.sh — 一次性重写 stock-report 公开仓历史（用户授权后手动运行）。
#
# 作用（不可逆，作用于公开 GitHub 仓库 fisher-admin/stock-report）：
#   1. 把当前 main 的完整工作树压成「单根提交」，抹掉全部旧历史
#      —— 同时清掉旧版含硬编码假业绩的前端快照，并把每日数据累积的 ~251MB .git 压回几 MB。
#   2. force-push 覆盖 origin/main。
#   3. 把两个本地克隆都对齐到新历史（否则下一次自动管线推送会因历史分叉而失败）。
#
# 运行前会自动备份完整旧历史为 git bundle，可随时恢复。
# 用法：  bash ~/.openclaw/workspace/ops/clean_publish_history.sh
#
# 注意：请在两次自动管线写入之间运行（T1 每工作日 15:35）。运行完两个克隆即对齐，后续管线照常。

set -euo pipefail

PUBLISHED="$HOME/.openclaw/workspace/stock-report"
CLONE2="$HOME/.openclaw/workspace/stock_data/03-working/stock-report-repo"
BRANCH="main"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/tmp/stock-report-history-backup-$STAMP.bundle"

echo "==> 1/7 预检"
cd "$PUBLISHED"
if [ -n "$(git status --porcelain)" ]; then
  echo "[中止] 工作树不干净，请先提交或清理：$PUBLISHED"
  git status --short
  exit 1
fi
if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
  echo "[中止] 当前不在 $BRANCH 分支"
  exit 1
fi
echo "    当前 main: $(git rev-parse --short HEAD)  |  .git 体积: $(du -sh .git | cut -f1)"

echo "==> 2/7 备份完整旧历史 → $BACKUP"
git bundle create "$BACKUP" --all >/dev/null
echo "    备份完成：$(du -h "$BACKUP" | cut -f1)（恢复：git clone $BACKUP 恢复目录）"

echo "==> 3/7 构建单根提交（孤儿）"
git branch -D _clean_history 2>/dev/null || true
git checkout --orphan _clean_history >/dev/null 2>&1
git add -A
git commit -q -m "A股智能选股系统 · 公开展示层（v4 客户向视觉升级）

历史于 $STAMP 重置为单根提交：移除旧版含硬编码假业绩的前端历史快照，
收敛每日数据快照累积的肥历史。完整旧历史已离线备份（git bundle）。
本仓库内容每日由量化管线自动再生。"
NEWSHA="$(git rev-parse --short HEAD)"
echo "    新根提交：${NEWSHA}（$(git ls-files | wc -l | tr -d ' ') 个文件）"

echo "==> 4/7 force-push 覆盖 origin/$BRANCH"
git push --force origin "_clean_history:$BRANCH"

echo "==> 5/7 PUBLISHED 克隆对齐新历史 + gc"
git checkout -B "$BRANCH" _clean_history >/dev/null 2>&1
git branch -D _clean_history >/dev/null 2>&1 || true
git branch --set-upstream-to="origin/$BRANCH" "$BRANCH" >/dev/null 2>&1 || true
git reflog expire --expire=now --all
git gc --prune=now --aggressive >/dev/null 2>&1
echo "    PUBLISHED .git 体积现为：$(du -sh .git | cut -f1)"

echo "==> 6/7 克隆2 对齐新历史 + gc"
if [ -d "$CLONE2/.git" ]; then
  cd "$CLONE2"
  git fetch origin "$BRANCH" >/dev/null 2>&1
  git checkout "$BRANCH" >/dev/null 2>&1 || git checkout -B "$BRANCH" "origin/$BRANCH" >/dev/null 2>&1
  git reset --hard "origin/$BRANCH"
  git reflog expire --expire=now --all
  git gc --prune=now --aggressive >/dev/null 2>&1
  echo "    克隆2 已对齐：$(git rev-parse --short HEAD)  |  .git 体积：$(du -sh .git | cut -f1)"
else
  echo "    [跳过] 克隆2 不存在：$CLONE2"
fi

echo "==> 7/7 完成"
echo "    origin/$BRANCH 已重置为单根提交 ${NEWSHA}，两个克隆均已对齐。"
echo "    旧历史备份：$BACKUP"
echo "    GitHub Pages 将在数分钟内以新内容重新部署。"
