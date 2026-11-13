# 方案可完成项落地状态（2026-08-12）

对照 `stock-system-optimization-plan-2026-08-12.md`（v1.4）。  
原则：方案已确定且**当前环境可做的**全部完成；不能完成的标注原因。

## 一、本轮新完成（在既有 Epic A/C 骨架上的收口）

| 项 | 状态 | 位置 |
|----|------|------|
| 覆盖率/比率守卫（≤100%） | ✅ | `metric_guards.py`；接入告警与 o2c 合同校验 |
| AI 规则硬拦截引擎（动作上限、去估价、ai_depth） | ✅ | `ai_risk_rules.py`；`stage4_ai_publish.apply_ai_policy_fields` |
| 全观察在 observe_only 下合法 | ✅ | stage4 checks：`forced_opinion_diversity_required=false` |
| 三态写入 system_verdict | ✅ | `generate_system_verdict.py` → `pipeline_status` |
| 管线告警：三态分列、覆盖率非法、冻名单工程告警 | ✅ | `stock_pipeline_alert.py` |
| v45 诊断日报 | ✅ | `v45_diagnostic_report.py`；runner 成功日写出 |
| v45 主比较预注册书面包 | ✅ | `docs/prebreakout_v45_vs_v44_comparison_registration.md` |
| v45 失败不拖死 control/v44；失败日记入分母语义 | ✅ | `short_track_shadow_runner.py` failure.json |
| 快照 repro_metadata | ✅ | `short_track_shadow._default_contract` |
| 单元测试 | ✅ | 相关套件 **66 passed** |

## 二、此前已完成（仍有效）

- 2026 完整性三门 + runner 覆盖账本  
- 编排 `selection_ok/research_ok/publish_ok`  
- 旧 governance 弃用  
- v45 同域截面核心 + 发布合同  
- 禁止 display_score / model_estimate（合同层）

## 三、不能在本轮完成的项（原因）

| 未完成项 | 原因（阻塞类型） |
|----------|------------------|
| 短线 ≥60 成熟交易日净值与晋级包 | **时间/样本**：须自冻结日起累计交易日，无法代码加速诚实样本 |
| 中期 100 事件 + 12 个月 + 2025/2026 分段过门 | **时间/样本**；且 2026 完整性要求**年末信号 T+20 结算后（进入 2027）** 才能宣布 |
| 2026 公告覆盖账本「全年填满」 | **运营日更**：机制已具备，须每个交易日成功跑事件轨；今日无法伪造全年覆盖 |
| Epic E 晋级/退役正式决议 | **依赖满样本后的人类拍板**；未到决策点 |
| 动态权重研究 | **方案明确延后**至等权基线满 60 日后再预注册 |
| 自动交易 / 改 control / 复活 O2C·竞价 | **方案硬否决**，永不在本方案范围完成 |
| 真实 GitHub SSL 网络修复 | **依赖本机/运营商网络与凭据环境**；已做状态拆分，无法在沙箱保证 push 成功 |
| cron Discord not-delivered 全链路修复 | **依赖外部 webhook/bot 配置与线上调度状态**；未持有全部运行时密钥与排程所有权时无法保证「投递成功」 |
| stock-report 前端页面完整 UI 改版 | **仓库/发布克隆与设计资产分散**；已把三态写入 `system_verdict` JSON，页面是否渲染取决于前端读字段（本轮未改 HTML/JS 大页） |
| AI 九段深度卡片 + 分层 LLM 成本路由全量 | **依赖生产 LLM 网关配额、prompt 资产与日更成本预算**；本轮完成规则层与字段合同，未改外部模型供应商行为 |
| 中期修订链历史数据补全 | **数据工程/源站历史修订可得性**；无完整历史源则只能保持 `auxiliary_only` |
| 生产环境「v45 首日 validation_start」实盘落盘确认 | **须真实交易日跑 short_track runner**；单测已覆盖逻辑，未替你触发有副作用的生产日跑 |

## 四、建议的自然后续（非本轮强制）

1. 下一交易日收盘管线后检查：`orchestrator_run.json` 三态、`system_verdict.pipeline_status`、v45 快照/诊断是否出现  
2. 持续日更直至 short_track 样本 ≥60  
3. 2027 年初再评估 event 完整性门  

## 五、验证命令（全量，禁止子集宣称全绿）

```bash
cd ~/.openclaw/workspace/skills/stock-system-orchestrator/scripts
python3 -m pytest -q
# → 155 passed（2026-08-12 复审收口后）
```

复审问题收口见 `docs/stock-system-optimization-review-issues-2026-08-12.md` 处置记录。
