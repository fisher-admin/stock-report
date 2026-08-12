#!/bin/bash
# ============================================================================
# stock-report pre-push 守卫（2026-06 收口）：发布合同 v2 硬校验。
# 任一克隆向 origin 推送前，校验该克隆 data/latest 的 v2 合同；不合规则拒绝推送，
# 防旧 cron/旧脚本/旧克隆用无 decision_state.gates 的旧合同覆盖线上 v2 页面。
# 安装：复制到 <clone>/.git/hooks/pre-push 并 chmod +x（见 install_pre_push_hooks）。
# ============================================================================
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
STOCK_ROOT="${STOCK_SYSTEM_ROOT:-$HOME/.openclaw}"
WORKSPACE="${STOCK_SYSTEM_WORKSPACE:-$STOCK_ROOT/workspace}"
VALIDATOR="$WORKSPACE/skills/stock-system-orchestrator/scripts/validate_publication_contract.py"

# 校验器缺失不阻断（避免误伤无关仓库的 push）。
[ -f "$VALIDATOR" ] || exit 0
# 仅对 stock-report 发布仓生效。
case "$REPO_ROOT" in
  *stock-report*) : ;;
  *) exit 0 ;;
esac
LATEST="$REPO_ROOT/data/latest"
[ -d "$LATEST" ] || exit 0

if ! PYTHONDONTWRITEBYTECODE=1 python3 "$VALIDATOR" --latest-dir "$LATEST" --strict; then
  echo "" >&2
  echo "✗ pre-push 已拒绝：发布合同 v2 校验失败（$LATEST）。" >&2
  echo "  线上 GitHub Pages 仅接受 v2 合同（decision_state.gates 等齐全）。" >&2
  echo "  请改用唯一发布路径：" >&2
  echo "    bash $WORKSPACE/skills/stock-system-orchestrator/scripts/publish_stock_report_v2.sh" >&2
  echo "  勿再用 generate_github_pages.py + 裸 git push 推送旧合同。" >&2
  exit 1
fi
exit 0
