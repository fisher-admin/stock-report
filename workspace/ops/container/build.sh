#!/usr/bin/env bash
# Build the stock runtime image in three stacked steps under a host disk guard.
#
# 10-30 incident: repeated multi-stage builds left 14 GB of build cache, filled
# the disk to 193 MB and turned the Docker VM read-only.  This script:
#   - refuses to start a step unless host free space >= FLOOR + that step's estimate,
#   - kills the build if free space falls below FLOOR while it runs,
#   - prunes the build cache after every step (success or failure).
# Usage: ops/container/build.sh [orch|ai|qlib ...]   (default: all three, in order)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLOOR_GB="${STOCK_BUILD_FLOOR_GB:-10}"   # production chain needs ~5 GB; keep double
LOG_DIR="${STOCK_BUILD_LOG_DIR:-$HERE/../../stock_data/03-working/health/container_build}"
mkdir -p "$LOG_DIR"

# step  dockerfile        base                  estimate_gb (layer + transient build tools)
STEPS=(
  "orch Dockerfile      -                     3"
  "ai   Dockerfile.ai   stock-runtime:orch    3"
  "qlib Dockerfile.qlib stock-runtime:ai      3"
)

free_gb() {
  local path="${STOCK_BUILD_DISK_PATH:-$HERE}"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    df -g "$path" 2>/dev/null | awk 'NR==2 {print $4}'
  else
    df -Pk "$path" 2>/dev/null | awk 'NR==2 {printf "%d\n", $4 / 1024 / 1024}'
  fi
}

prune() { docker builder prune -af >/dev/null 2>&1 || true; }

run_step() {
  local name="$1" file="$2" base="$3" estimate="$4"
  local log="$LOG_DIR/$(date +%Y%m%d-%H%M%S)-$name.log"
  local free; free="$(free_gb)"
  if (( free < FLOOR_GB + estimate )); then
    echo "[$name] 跳过：可用 ${free} GB < 下限 ${FLOOR_GB} + 预计 ${estimate} GB"
    return 2
  fi
  local args=(build -f "$HERE/$file" -t "stock-runtime:$name")
  [[ "$base" != "-" ]] && args+=(--build-arg "BASE=$base")
  echo "[$name] 开始（可用 ${free} GB），日志 $log"
  docker "${args[@]}" "$HERE" >"$log" 2>&1 &
  local pid=$! low=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5
    free="$(free_gb)"
    if (( free < FLOOR_GB )); then
      low=1
      echo "[$name] 中止：可用空间降到 ${free} GB，低于下限 ${FLOOR_GB} GB"
      kill "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid"; local rc=$?
  prune
  (( low )) && rc=3
  echo "[$name] 结束 rc=$rc，清理缓存后可用 $(free_gb) GB"
  return "$rc"
}

wanted=("$@")
for line in "${STEPS[@]}"; do
  read -r name file base estimate <<<"$line"
  if (( ${#wanted[@]} )) && [[ " ${wanted[*]} " != *" $name "* ]]; then
    continue
  fi
  run_step "$name" "$file" "$base" "$estimate" || exit $?
done
docker tag stock-runtime:qlib stock-runtime:latest 2>/dev/null && echo "stock-runtime:latest -> qlib"
exit 0
