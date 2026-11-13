#!/bin/zsh
# 验收6 跨 pandas 版本重跑: 在独立 venv(pandas 2.3.x)下重跑 ties 金值锁定测试。
# 用法: zsh scripts/run_ties_cross_version.sh [venv目录]
set -e
SPIKE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${1:-/tmp/spike5_pandas2_venv}"
MAIN_PY="/Users/fisher/.openclaw/venv/bin/python"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$MAIN_PY" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -q "pandas==2.3.3" >/dev/null
fi
echo "--- 主 venv ---"
cd "$SPIKE_DIR/tests" && "$MAIN_PY" test_a6_ties.py 2>&1 | tail -4
echo "--- 异版本 venv ---"
cd "$SPIKE_DIR/tests" && "$VENV_DIR/bin/python" test_a6_ties.py 2>&1 | tail -4
