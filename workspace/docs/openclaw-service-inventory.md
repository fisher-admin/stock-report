# OpenClaw 服务清单

更新时间：2026-03-24 11:15 Asia/Shanghai

## 生产关键

### `ai.openclaw.gateway`
- 角色：主 gateway，负责 Discord、webchat、agent 入口与插件加载。
- 启动方式：launchd 常驻。
- 关键端口：`18789`。
- 关键日志：`~/.openclaw/logs/gateway.log`、`~/.openclaw/logs/gateway.err.log`。
- 说明：这是当前机器上的主生产服务，任何插件配置或 gateway 行为修复都应先验证它。

### `ai.openclaw.memory-platform`
- 角色：本地 memory platform 进程，支撑 memory 侧的状态平台能力。
- 启动方式：launchd 常驻。
- 关键脚本：`~/.openclaw/memory/platform/run_memory_platform.py`。
- 关键日志：`~/.openclaw/memory/logs/memory-platform.stdout.log`、`~/.openclaw/memory/logs/memory-platform.stderr.log`。
- 说明：这是记忆控制面的基础服务，不是 gateway 本身，但属于现网重要依赖。

## 辅助服务

### `com.openclaw.minimax-proxy`
- 角色：MiniMax 代理进程，服务辅机或指定模型链路。
- 启动方式：launchd 常驻。
- 关键脚本：`~/.openclaw-monitor/scripts/minimax-proxy.py`。
- 关键端口：`18795`。
- 说明：属于模型接入辅助层，异常时会影响对应 fallback 或辅机能力，但不应误判为主 gateway 故障。

### `com.openclaw.model-check`
- 角色：定时模型检查任务。
- 启动方式：launchd 按日历触发。
- 关键脚本：`~/.openclaw/scripts/model-check.sh`。
- 说明：属于健康巡检与配额检查，不是在线请求链路的一部分。

## 操作规则

- 修改 gateway 配置或插件后，优先验证 `ai.openclaw.gateway`。
- 修改记忆平台或记忆控制面后，同时检查 `ai.openclaw.memory-platform`。
- `minimax-proxy` 和 `model-check` 的故障需要单独归类，不与主 gateway 混报。
