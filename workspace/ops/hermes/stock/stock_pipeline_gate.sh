#!/bin/bash
set -uo pipefail
export TZ=Asia/Shanghai

export STOCK_SYSTEM_ROOT="${STOCK_SYSTEM_ROOT_FORCE:-$HOME/.openclaw}"
export STOCK_SYSTEM_WORKSPACE="${STOCK_SYSTEM_WORKSPACE_FORCE:-$STOCK_SYSTEM_ROOT/workspace}"
export STOCK_SYSTEM_VENV="${STOCK_SYSTEM_VENV_FORCE:-$STOCK_SYSTEM_ROOT/venv}"
export STOCK_SYSTEM_PYTHON="${STOCK_SYSTEM_PYTHON_FORCE:-$STOCK_SYSTEM_VENV/bin/python}"
export STOCK_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
export STOCK_PYTHON="$STOCK_SYSTEM_PYTHON"
export OPENCLAW_WORKSPACE="$STOCK_SYSTEM_WORKSPACE"
export OPENCLAW_WORKSPACE_DIR="$STOCK_SYSTEM_WORKSPACE"

WS="$STOCK_WORKSPACE"
PY="$STOCK_PYTHON"
if [ ! -x "$PY" ]; then
  echo "Python 环境缺失，任务未执行：$PY" >&2
  exit 2
fi
cd "$WS" || exit 2

LOCK_DIR="${STOCK_PIPELINE_LOCK_DIR:-$WS/stock_data/03-working/health}/stock_pipeline_gate.lockdir"
LOCK_TOKEN="$$-$(date '+%s')-${RANDOM:-0}"
LOCK_STALE_GRACE_SECONDS="${STOCK_PIPELINE_STALE_GRACE_SECONDS:-300}"
case "$LOCK_STALE_GRACE_SECONDS" in
  ''|*[!0-9]*) LOCK_STALE_GRACE_SECONDS=300 ;;
esac

mtime_seconds() {
  if stat --version >/dev/null 2>&1; then
    stat -c '%Y' "$1" 2>/dev/null || true
  else
    stat -f '%m' "$1" 2>/dev/null || true
  fi
}

cleanup_lock() {
  current_owner="$(sed -n '1p' "$LOCK_DIR/owner.txt" 2>/dev/null || true)"
  IFS='|' read -r current_pid _current_started current_token <<EOF
$current_owner
EOF
  if [ "$current_pid" = "$$" ] && [ "$current_token" = "$LOCK_TOKEN" ]; then
    rm -f "$LOCK_DIR/owner.txt" 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

write_owner() {
  owner_started="$(ps -o lstart= -p "$$" 2>/dev/null | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true)"
  owner_tmp="$(mktemp "$LOCK_DIR/.owner.XXXXXX")" || return 1
  (
    umask 077
    printf '%s|%s|%s\n' "$$" "$owner_started" "$LOCK_TOKEN" >"$owner_tmp"
  ) || return 1
  mv -f "$owner_tmp" "$LOCK_DIR/owner.txt"
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    if write_owner; then
      return 0
    fi
    rm -f "$LOCK_DIR"/.owner.* 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
    return 1
  fi

  # Never follow a planted lock-directory symlink.
  if [ -L "$LOCK_DIR" ]; then
    return 1
  fi

  # mkdir and owner metadata are separate filesystem operations. Give the
  # owner a short grace window before considering an ownerless lock stale.
  attempts=0
  while [ ! -f "$LOCK_DIR/owner.txt" ] && [ $attempts -lt 10 ]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  if [ ! -f "$LOCK_DIR/owner.txt" ]; then
    lock_mtime="$(mtime_seconds "$LOCK_DIR")"
    now_epoch="$(date '+%s')"
    if [ -z "$lock_mtime" ] || [ $((now_epoch - lock_mtime)) -lt "$LOCK_STALE_GRACE_SECONDS" ]; then
      return 1
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || return 1
    if mkdir "$LOCK_DIR" 2>/dev/null && write_owner; then
      return 0
    fi
    return 1
  fi

  owner_line="$(sed -n '1p' "$LOCK_DIR/owner.txt" 2>/dev/null || true)"
  IFS='|' read -r owner_pid owner_started _owner_token <<EOF
$owner_line
EOF
  owner_alive=false
  if [ -n "$owner_pid" ] && kill -0 "$owner_pid" 2>/dev/null; then
    if [ -n "$owner_started" ]; then
      observed_started="$(ps -o lstart= -p "$owner_pid" 2>/dev/null | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' || true)"
      if [ -z "$observed_started" ] || [ "$observed_started" = "$owner_started" ]; then
        owner_alive=true
      fi
    else
      owner_alive=true
    fi
  fi
  if [ "$owner_alive" = true ]; then
    return 1
  fi

  # Reclaim only if the owner metadata has not changed during validation.
  if [ "$(sed -n '1p' "$LOCK_DIR/owner.txt" 2>/dev/null || true)" != "$owner_line" ]; then
    return 1
  fi
  rm -f "$LOCK_DIR/owner.txt" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || return 1
  if mkdir "$LOCK_DIR" 2>/dev/null && write_owner; then
    return 0
  fi
  return 1
}

if ! acquire_lock; then
  holder="$(ps -eo pid,lstart,command | /usr/bin/grep -E 'stock_pipeline_gate\.py|run_pipeline\.py' | /usr/bin/grep -v grep | head -5 || true)"
  echo "🚨 【A股选股管线执行告警】 (退出码: 75)"
  echo "PIPELINE_ALREADY_RUNNING lock=$LOCK_DIR"
  echo "$holder"
  echo "选股管线已有实例在运行，未重复执行" >&2
  exit 75
fi

trap cleanup_lock EXIT INT TERM

# Exchange-calendar gate (2026-10-03): closed day -> durable skip record, no
# business work, no routine message.  Missing/invalid calendar -> error.
GATE_OUT=$("$PY" "$WS/skills/stock-system-orchestrator/scripts/exchange_calendar.py" gate --job stock_pipeline 2>&1)
GATE_RC=$?
printf "%s\n" "$GATE_OUT"
if [ "$GATE_RC" -eq 3 ]; then
  exit 0
fi
if [ "$GATE_RC" -ne 0 ]; then
  GATE_FIRST=$(printf "%s\n" "$GATE_OUT" | grep -v '^$' | head -n 1)
  echo "${GATE_FIRST:-交易日历检查失败，任务未执行}" >&2
  exit "$GATE_RC"
fi

# Alert shaping (2026-10-03): full text for a new failure, one line for an
# unchanged repeat, one line on recovery.  Exit codes are never changed.
ALERT_POLICY="$WS/skills/stock-system-orchestrator/scripts/stock_alert_policy.py"
shape_failure() {
  local job="$1" rc="$2" text
  text=$(cat)
  printf "%s\n" "$text" | "$PY" "$ALERT_POLICY" failure --job "$job" --rc "$rc" 2>/dev/null || printf "%s\n" "$text"
}
announce_recovery() {
  "$PY" "$ALERT_POLICY" success --job "$1" 2>/dev/null || true
}

export OPENCLAW_STAGE1_MAX_WAIT_SECONDS="${OPENCLAW_STAGE1_MAX_WAIT_SECONDS:-1800}"
# 2026-11-14: 硬超时，避免上游挂起占满调度批次（11-12/13 曾卡 80 分钟）
OUT=$(timeout "${STOCK_PIPELINE_HARD_TIMEOUT_SECONDS:-3600}" "$PY" "$WS/scripts/stock_pipeline_gate.py" "$@" 2>&1)
RET=$?

if [ $RET -eq 0 ]; then
  announce_recovery stock_pipeline
  echo "✅ 【A股主选股管线】执行完成"
elif [ $RET -eq 4 ]; then
  # 2026-10-26: market-gate halt is a business rule, not a fault.
  HALT_TEXT=$(printf "%s\n" "$OUT" | sed -n 's/^MARKET_HALT //p' | head -n 1)
  echo "🛑 【A股主选股管线】${HALT_TEXT:-市场高风险，按规则暂停今日选股与发布（非系统故障）}"
  echo "今晚不发布新名单，网页保留上一交易日；无需处理。"
  RET=0
else
  SHAPED=$( {
    echo "🚨 【A股主选股管线执行失败】 (退出码: $RET)"
    echo "$OUT" | tail -n 10
  } | shape_failure stock_pipeline "$RET" )
  printf "%s\n" "$SHAPED"
  printf "%s\n" "$SHAPED" | head -n 1 >&2
fi
exit $RET
