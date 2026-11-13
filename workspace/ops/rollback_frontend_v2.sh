#!/bin/bash
# rollback_frontend_v2.sh — 一键把 stock-report 前端从 v2 回滚到 legacy 单文件版。
#
# 用法（无需任何 git 知识）：
#   bash ~/.openclaw/workspace/ops/rollback_frontend_v2.sh
#
# 原理：9 个页面壳只引用一个入口脚本。本脚本把
#   assets/scripts/v2/app.js  →  assets/scripts/stock-workbench.js
# 的引用换回去。legacy 文件（stock-workbench.js / stock-data-hub.js）一直保留在仓库里，
# 因此回滚立即生效。重新启用 v2 时把两个路径互换执行即可。
#
# 默认同时处理发布仓和工作仓两份克隆；目录不存在则跳过。

set -u

PAGES="index.html market-overview.html decision-candidates.html recommendation-review.html research-lab.html strategy-vs-market.html industry-heatmap.html industry-compare.html market-industry-heatmap.html"

V2_SRC='./assets/scripts/v2/app.js'
LEGACY_SRC='./assets/scripts/stock-workbench.js'

rollback_repo() {
  local repo="$1"
  if [ ! -d "$repo" ]; then
    echo "[skip] 目录不存在: $repo"
    return 0
  fi
  local changed=0
  for page in $PAGES; do
    local file="$repo/$page"
    if [ ! -f "$file" ]; then
      echo "[warn] 页面缺失: $file"
      continue
    fi
    if grep -q "$V2_SRC" "$file"; then
      # macOS 与 GNU sed 兼容写法
      sed -i.rollback-bak "s|$V2_SRC|$LEGACY_SRC|g" "$file" && rm -f "$file.rollback-bak"
      echo "[ok] 已回滚: $file"
      changed=$((changed + 1))
    else
      echo "[--] 已是 legacy 引用: $file"
    fi
  done
  echo "[done] $repo 共回滚 $changed 个页面"
}

rollback_repo "$HOME/.openclaw/workspace/stock-report"
rollback_repo "$HOME/.openclaw/workspace/stock_data/03-working/stock-report-repo"

echo ""
echo "回滚完成。刷新浏览器即可看到 legacy 版本。"
echo "若要让线上 GitHub Pages 生效，等夜间流水线自动提交推送，或手动："
echo "  cd ~/.openclaw/workspace/stock-report && git add -A && git commit -m 'rollback: frontend v2 -> legacy' && git push origin main"
