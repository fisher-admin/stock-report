#!/usr/bin/env bash
set -euo pipefail

echo "== OpenClaw Model Health Check =="
date "+%Y-%m-%d %H:%M:%S %Z"

echo "\n[1] Current status"
STATUS="$(openclaw models status 2>/dev/null || true)"
echo "$STATUS"

# Parse rough percentages from status output if present
FIVE_H="$(echo "$STATUS" | sed -n 's/.*5h \([0-9]\+%\) left.*/\1/p' | head -1)"
DAY_H="$(echo "$STATUS" | sed -n 's/.*Day \([0-9]\+%\) left.*/\1/p' | head -1)"

five_num=100
[[ -n "$FIVE_H" ]] && five_num="${FIVE_H%%%}"
day_num=100
[[ -n "$DAY_H" ]] && day_num="${DAY_H%%%}"

echo "\n[2] Recommendation"
if (( five_num < 20 || day_num < 15 )); then
  echo "RED: 建议切换到 MiniMax 主模型"
  echo "cmd: openclaw models set minimax/MiniMax-M2.5"
elif (( five_num <= 40 || day_num <= 30 )); then
  echo "YELLOW: 预警区，长任务建议用 MiniMax，复杂推理保留 Codex"
else
  echo "GREEN: 正常，继续使用 Codex"
fi

echo "\n[3] Quick commands"
echo "- 切到 Codex:   openclaw models set openai-codex/gpt-5.3-codex"
echo "- 切到 MiniMax: openclaw models set minimax/MiniMax-M2.5"
echo "- 再次检查:     openclaw models status"
