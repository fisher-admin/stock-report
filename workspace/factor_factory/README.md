# Factor Factory

当前存在两条线，但主次已经纠偏：

## 当前主线：Greenfield 多因子选股体系
- 从当前 A 股市场环境出发，独立研究、设计、实现、验证新的多因子体系。
- 核心产物放在 `outputs/greenfield_multifactor/`。
- 当前强调：市场状态感知、独立因子池、组合级 walk-forward / analog validation。
- 训练与验证默认不以 `prebreakout_v41` 为中心。

## 旧线：Phase 1 / prebreakout_v41 辅助因子工厂（仅保留参考）
- `prebreakout_v41` 仅保留为历史参考、旧系统基准、候选因子之一和对照项。
- 旧线产物继续保留在 `outputs/evolution/`、`outputs/audit_reports/`、`outputs/weight_maps/` 等目录，便于对照与复盘。
- 不再把主要算力投入到补 `prebreakout_v41` 历史记录、修其 benchmark、或围绕它做主线优化。

## Greenfield v1 目标
- 构建独立因子池：trend quality / liquid leadership / liquidity follow-through / sector rotation / downside control / chip resilience
- 输出独立组合：不依赖 legacy `selection_history`
- 完成验证：walk-forward 样本外、similar-market analog windows、当前快照选股

## 关键约束
- Legacy `prebreakout_v41`：reference only
- 组合成本：双边摩擦成本 0.2%
- 组合规则：默认 top20 等权
- 当前高位、弱广度、高量能环境下，必须显式纳入 downside control 与执行风险约束
