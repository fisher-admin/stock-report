# 请 Codex 收口复审：选股系统优化方案 v1.2

## 背景

- 你对 **v1.1** 的结论是：**有条件通过**，并给出必须修改清单（中期门槛、v45 冻结、截面顺序、AI 估价、筹码/对照冲突、GitHub 边界、旧治理双口径、v45 验证起点等）。
- 现已升级为 **v1.2**，请判断是否关闭这些必须项。

主文档：

```text
/Users/fisher/.openclaw/workspace/docs/stock-system-optimization-plan-2026-08-12.md
```

对照：

```text
/Users/fisher/.openclaw/workspace/docs/stock-system-dual-track-implementation-2026-08-11.md
/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/event_quality_drift_evaluator.py
/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/shadow_portfolio_evaluator.py
/Users/fisher/.openclaw/workspace/skills/stock-system-orchestrator/scripts/short_track_shadow.py
/Users/fisher/.openclaw/workspace/fisher-quant/config/strategy_governance.json
```

## 审议任务（请按此结构回复）

### 1. 总评

`通过` / `有条件通过` / `退回`，一句话原因。

### 2. 对照你 v1.1 必须修改清单

对每一项标注：`已关闭` / `部分关闭` / `未关闭`：

1. 中期完整晋级门槛 + 2026 截止日期冻结  
2. v45 不可选固定实验；主比 v45 vs v44；复用 v44 输入  
3. 截面处理顺序（联合中性 + 残差再标准化）  
4. 删除 AI 估算价格  
5. 筹码降级与 control 冻结冲突  
6. GitHub / 本机 / Pages 边界  
7. 旧 governance 20 日与 run_governance_eval 双口径  
8. v45 验证起点为自身首日成功，禁止继承/回填  

### 3. 必须修改清单（若仍有）

每条给建议改法；区分 **改文档** vs **Epic A 改代码**。

### 4. 可选改进

不挡通过即可。

### 5. 是否批准开发

- 是否批准 **Epic A**  
- 是否批准 **Epic B 继续**  
- 是否仍 **禁止 Epic C/D** 直至文档关闭  

## 约束

- 只做方案审议，不要直接改生产代码  
- 用中文、结构清晰  
