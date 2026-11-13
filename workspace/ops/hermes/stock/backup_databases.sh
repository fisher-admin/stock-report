#!/bin/bash
set -euo pipefail
export TZ=Asia/Shanghai

export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
export STOCK_SYSTEM_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
export STOCK_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"

WS="$STOCK_WORKSPACE"
cd "$WS"
exec bash "$WS/ops/backup_databases.sh"
