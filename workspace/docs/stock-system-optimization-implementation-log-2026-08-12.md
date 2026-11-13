# 优化方案实施日志（2026-08-12）

基于 Codex **v1.3 方案通过** 与 v1.4 实施约束；按优先级连续落地，不问答。

## 方案状态

| 版本 | 状态 |
|------|------|
| v1.3 | 方案通过 |
| v1.4 | 实施约束 + 代码落地中 |

## 已完成

### Epic A

| 项 | 状态 | 位置 |
|----|------|------|
| 2026 完整性三门 | ✅ | `event_quality_drift_evaluator.py` |
| 完整性 fail-closed + 测试 | ✅ | `test_event_quality_drift_evaluator.py` |
| runner 接入 completeness_context | ✅ | `event_quality_drift_runner.py` |
| 独立公告采集覆盖账本 | ✅ | `announcement_collection_coverage.json` |
| 三态 selection/research/publish | ✅ | `run_pipeline.py` |
| 旧 governance 退役 | ✅ | `strategy_governance.json` + `run_governance_eval.py` |

### Epic B

| 项 | 状态 |
|----|------|
| 四影子不中断 | ✅ 未改既有 control/top15/v44 过滤与合同 |

### Epic C（方案批准后已实现核心）

| 项 | 状态 | 位置 |
|----|------|------|
| `prebreakout_v45_cross_sectional` | ✅ | `short_track_shadow.py` |
| 继承 v44 确认域 + universe hash | ✅ | 同文件 |
| circ_mv≤0 / hash 不一致整日失败 | ✅ | 同文件 |
| cs_pipeline=v45.1 截面流水线 | ✅ | MAD winsor → 联合中性(RS) → z → 赋分 |
| 首日 validation_start 冻结 | ✅ | runner 写 `*_validation_start.json` |
| 发布合同纳入 v45 | ✅ | `dual_track_publication.py` + validate |
| 单测 | ✅ | `test_short_track_shadow.py` 等 |

### Epic D（并行安全子集）

| 项 | 状态 |
|----|------|
| 禁止 display_score / model_estimate | ✅ | `validate_publication_contract.py` |

## 验证

```text
pytest \
  test_short_track_shadow.py \
  test_shadow_portfolio_evaluator.py \
  test_dual_track_publication.py \
  test_event_quality_drift_runner.py \
  test_event_quality_drift_evaluator.py \
  test_run_pipeline_three_state.py
→ 59 passed
```

## 第二轮收口（可完成项尽量做完）

| 项 | 状态 |
|----|------|
| metric_guards 覆盖率 ≤100% | ✅ |
| ai_risk_rules + stage4 接入 | ✅ |
| system_verdict 三态 | ✅ |
| pipeline_alert 三态/冻名单/覆盖率 | ✅ |
| v45 诊断日报 + 主比较登记 | ✅ |
| v45 失败隔离 + 分母语义 | ✅ |
| repro_metadata | ✅ |
| 测试 | ✅ 66 passed |

## 不能完成项与原因

详见：`docs/stock-system-optimization-completion-status-2026-08-12.md`  
（样本日历、2027 结算、外部网络/密钥、前端大页、LLM 供应商配额等）

## 主文档

`docs/stock-system-optimization-plan-2026-08-12.md`（v1.4）
