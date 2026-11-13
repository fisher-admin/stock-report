#!/bin/bash
# Premarket observation-only event strategy.
# Schedule daily (including weekends) before 09:00 Asia/Shanghai. The runner
# processes yesterday's announcement date and refuses to backdate after entry.
set -euo pipefail

WS="${OPENCLAW_WORKSPACE_DIR:-$HOME/.openclaw/workspace}"
PY="${STOCK_SYSTEM_PYTHON:-$HOME/.openclaw/venv/bin/python}"
[ -x "$PY" ] || PY="python3"

exec "$PY" "$WS/skills/stock-system-orchestrator/scripts/event_quality_drift_runner.py"
