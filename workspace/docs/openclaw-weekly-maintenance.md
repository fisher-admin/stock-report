# OpenClaw 每周维护清单

更新时间：2026-03-24 11:15 Asia/Shanghai

## 每周例行

1. 运行 `openclaw doctor`，记录 state integrity、plugin diagnostics、channel warnings。
2. 先执行 `openclaw sessions cleanup --dry-run --fix-missing`，确认影响面后再执行 `--enforce --fix-missing`。
3. 运行 `python3 /Users/fisher/Documents/openclaw/scripts/openclaw_session_hygiene.py --store ~/.openclaw/agents/main/sessions/sessions.json`，检查 orphan transcript 数量。
4. 检查 `~/.openclaw/logs/gateway.err.log` 最近 24 小时是否有新的高频错误。
5. 检查 Discord 相关 `stale-socket` / `disconnected` 是否超出平常波动。
6. 检查 `memory/core/ACTIVE_TASKS.md` 是否混入 done 卡片；done 移入 `TASK_ARCHIVE.md`，等待项移入 `WAITING_TASKS.md`。
7. 检查 `MEMORY.md`、`MEMORY-SHARED.md` 是否与现网配置相矛盾。

## 触发式维护

- 升级 OpenClaw 或插件后，重新核对 `openclaw doctor` 的 plugin diagnostics。
- stock 系统脚本或 health 契约变更后，核对 `stock_data/03-working/health/` 是否仍能形成单一运行主线。
- 新接入记忆组件后，确认文件记忆与语义记忆的职责边界没有重新重叠。
