#!/bin/bash
# ============================================================================
# 选股系统统一测试入口（唯一权威解释器：QA venv，禁止 system python / 生产 venv）
# 用法: bash ops/run_stock_tests.sh [测试路径...]
#   无参数 = 跑全量回归
# ============================================================================
set -uo pipefail

WS="/Users/fisher/.openclaw/workspace"
QA_PY="/Users/fisher/.openclaw/qa_venv/bin/python"

if [ ! -x "$QA_PY" ]; then
  echo "FATAL: QA venv missing ($QA_PY)。创建方法:" >&2
  echo "  /Users/fisher/.openclaw/venv/bin/python -m venv /Users/fisher/.openclaw/qa_venv" >&2
  echo "  /Users/fisher/.openclaw/qa_venv/bin/python -m pip install pytest" >&2
  exit 2
fi

cd "$WS" || exit 2

if [ $# -gt 0 ]; then
  TARGETS=("$@")
else
  TARGETS=(
    skills/stock-system-orchestrator/scripts/test_publish_lock.py
    skills/stock-system-orchestrator/scripts/test_stage6_deploy_and_notify.py
    skills/stock-system-orchestrator/scripts/test_generate_o2c_ai_copilot.py
    skills/stock-system-orchestrator/scripts/test_stock_model_gateway.py
    skills/stock-system-orchestrator/scripts/test_publish_idempotency_gate.py
    skills/stock-system-orchestrator/scripts/test_benchmark_policy.py
    skills/stock-system-orchestrator/scripts/test_point_in_time_store.py
    skills/stock-system-orchestrator/scripts/test_sync_public_orchestrator.py
    skills/stock-system-orchestrator/tests/test_stock_system_e2e.py
    ops/test_verify_stock_backup.py
    scripts/test_env_drift_probe.py
  )
fi

echo "== 解释器: $QA_PY ($("$QA_PY" --version)) =="
exec "$QA_PY" -m pytest "${TARGETS[@]}" -q --no-header
