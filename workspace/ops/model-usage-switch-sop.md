# OpenClaw 模型用量监控与切换 SOP

更新时间：2026-02-23
适用对象：大大鱼（discord #fisher）

---

## 1) 目标

在保证稳定性的前提下优先使用 `openai-codex/gpt-5.3-codex`，并在额度或故障风险升高时自动/半自动切回 `minimax/MiniMax-M2.5`。

---

## 2) 关键指标

使用以下命令查看：

```bash
openclaw /status
# 或
openclaw models status
```

关注三项：

1. `5h left`（短周期额度）
2. `Day left`（日额度）
3. 错误信号（429、gateway timeout、auth失效）

---

## 3) 阈值与动作

### 绿色（正常）
- 条件：`5h left > 40%` 且 `Day left > 30%`
- 动作：保持 `gpt-5.3-codex`

### 黄色（预警）
- 条件：`5h left 20%~40%` 或 `Day left 15%~30%`
- 动作：
  - 复杂对话继续 Codex
  - 定时任务/批处理优先 MiniMax
  - 发出提醒，不立即切主模型

### 红色（危险）
- 条件：`5h left < 20%` 或 `Day left < 15%`
- 动作：切主模型为 MiniMax，Codex 作为 fallback

### 熔断（立即切换）
满足任一即切 MiniMax：
- 连续 2 次 429
- 连续 2 次 gateway timeout
- OAuth 认证失效

---

## 4) 切换命令（可直接执行）

### 切到 Codex（主模型）

```bash
openclaw models set openai-codex/gpt-5.3-codex
```

### 切回 MiniMax（主模型）

```bash
openclaw models set minimax/MiniMax-M2.5
```

### 查看当前状态

```bash
openclaw models status
```

---

## 5) 回切防抖策略

从 MiniMax 回切到 Codex 前，需同时满足：

- `5h left > 35%`
- 最近 30 分钟无 429/timeout

避免模型频繁来回切换造成不稳定。

---

## 6) 场景建议（当前环境）

- 定时任务（早报/午报/晚报）：优先 MiniMax（稳定）
- 复杂排障/代码/策略推导：优先 gpt-5.3-codex（能力优先）
- 群聊高频短消息：额度紧张时统一 MiniMax

---

## 7) 人工巡检节奏（建议）

- 早上 08:00 前：看一次 `models status`
- 下午 14:00 前：看一次 `models status`
- 晚上 20:00 前：看一次 `models status`

如进入黄色/红色区间，按本SOP执行。

---

## 8) 回滚原则

任何切换后出现异常（连续超时/明显响应劣化）

1. 立刻切回 MiniMax
2. 记录触发原因与时间
3. 30分钟后再评估是否回切 Codex

---

## 9) 备注

当前你已启用：
- 主模型：可切换（Codex / MiniMax）
- Fallback：4个 openai-codex 模型
- OAuth：已配置

