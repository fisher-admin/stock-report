#!/bin/bash
# Hermes 任务入口（cron 不支持脚本参数）：转调 forward_shadow_multi.sh cutoff。源在 ops/hermes/stock/。
exec bash "$(dirname "$0")/forward_shadow_multi.sh" cutoff
