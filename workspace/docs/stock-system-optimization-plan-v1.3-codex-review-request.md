# 请 Codex 最终收口复审：选股系统优化方案 v1.3

## 背景

你对 **v1.2** 的结论是 **有条件通过**：八项中六项已关闭，仍余两处实质漏洞：

1. 中期策略可能在 2026 未完整结束时提前过门  
2. v45 可能因 `circ_mv` 单票剔除而与 v44 股票池不同  

现已升级为 **v1.3**，请判断这两处是否已关闭。

主文档：

```text
/Users/fisher/.openclaw/workspace/docs/stock-system-optimization-plan-2026-08-12.md
```

相关现网代码（只读核对）：

```text
/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/event_quality_drift_evaluator.py
/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/test_event_quality_drift_evaluator.py
/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/short_track_shadow.py
```

## 请按此结构回复

### 1. 总评

`通过` / `有条件通过` / `退回`，一句话原因。

### 2. 对照 v1.2 剩余两项

| # | 项目 | 结论（已关闭/部分关闭/未关闭） | 说明 |
|---|------|--------------------------------|------|
| 1 | 2026 完整性门槛（信号窗完整 + 结算完整 + 结算前不得过门） |  |  |
| 2 | v45/v44 同域（继承 v44 域、hash、circ_mv≤0 整日失败、不改 v44） |  |  |

并请快速确认 v1.2 已关闭的六项 **未被 v1.3 回退**。

### 3. 必须修改清单（若仍有）

区分 **改文档** vs **Epic A/C 改代码**。

### 4. 可选改进

### 5. 是否批准开发

- Epic A（含 2026 完整性代码修复）  
- Epic B 继续  
- Epic C / D  

## 约束

- 只做方案审议，不要直接改生产代码  
- 用中文、结构清晰  
