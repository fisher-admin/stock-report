# spike4 影子重放报告 (确定性输出, 无墙钟)

- 重放交易日: 91 (20260224~20260710)
- 台账推荐数: 1560
- 影子参数: {'hold_days': 5, 'consecutive_loss': {'lookback_days': 10, 'loss_line': -0.05, 'trade_limit': 90, 'stop_duration_days': 3}, 'drawdown_brake': {'lookback_days': 20, 'max_drawdown': 0.11, 'trade_limit': 10}, 'low_edge': {'lookback_days': 10, 'required_excess': -2.0, 'trade_limit': 100, 'stop_duration_days': 3}, 'post_failure_cooldown': {'cooldown_days': 2}, 'market_stress_thresholds': [0.03, 0.05, 0.08]}
- 落锁次数: 49; 被挡推荐: 660
- 事件计数: {'INSUFFICIENT_DATA': 72, 'PROTECTION_TRIGGER': 49}

## MARKET_STRESS 多档阈值分布 (未拍板, 并列供决策)

- 日度 exit_blocked 占比: n=90, max=0.1000, p95=0.0400, p50=0.0000, 非零天数=19

| 阈值 | 触发天数 | 触发日 |
|---|---|---|
| 3% | 8 | 20260303,20260313,20260520,20260608,20260624,20260625,20260626,20260629 |
| 5% | 5 | 20260303,20260608,20260624,20260625,20260626 |
| 8% | 3 | 20260303,20260624,20260625 |

## 落锁明细 (逐条溯源)

| day | scope | key | reason_code | end | created_by |
|---|---|---|---|---|---|
| 20260312 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260317/next_open | protection:ConsecutiveLossGuard |
| 20260313 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260318/next_open | protection:ConsecutiveLossGuard |
| 20260316 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260319/next_open | protection:ConsecutiveLossGuard |
| 20260323 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260326/next_open | protection:ConsecutiveLossGuard |
| 20260330 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260402/next_open | protection:ConsecutiveLossGuard |
| 20260331 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260403/next_open | protection:ConsecutiveLossGuard |
| 20260401 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260407/next_open | protection:ConsecutiveLossGuard |
| 20260402 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260413/next_open | protection:ConsecutiveLossGuard |
| 20260403 | strategy | prebreakout_v41 | CONSECUTIVE_LOSS | 20260414/next_open | protection:ConsecutiveLossGuard |
| 20260403 | global | * | DRAWDOWN_BRAKE | 20260407/after_review | protection:StrategyDrawdownBrake |
| 20260403 | strategy | prebreakout_v41 | LOW_EDGE | 20260414/next_open | protection:LowEdgePause |
| 20260407 | global | * | DRAWDOWN_BRAKE | 20260413/after_review | protection:StrategyDrawdownBrake |
| 20260407 | strategy | prebreakout_v41 | LOW_EDGE | 20260415/next_open | protection:LowEdgePause |
| 20260413 | global | * | DRAWDOWN_BRAKE | 20260414/after_review | protection:StrategyDrawdownBrake |
| 20260413 | strategy | prebreakout_v41 | LOW_EDGE | 20260416/next_open | protection:LowEdgePause |
| 20260414 | global | * | DRAWDOWN_BRAKE | 20260415/after_review | protection:StrategyDrawdownBrake |
| 20260414 | strategy | prebreakout_v41 | LOW_EDGE | 20260417/next_open | protection:LowEdgePause |
| 20260415 | global | * | DRAWDOWN_BRAKE | 20260416/after_review | protection:StrategyDrawdownBrake |
| 20260415 | strategy | prebreakout_v41 | LOW_EDGE | 20260420/next_open | protection:LowEdgePause |
| 20260416 | strategy | prebreakout_v41 | LOW_EDGE | 20260421/next_open | protection:LowEdgePause |
| 20260417 | strategy | prebreakout_v41 | LOW_EDGE | 20260422/next_open | protection:LowEdgePause |
| 20260420 | strategy | prebreakout_v41 | LOW_EDGE | 20260423/next_open | protection:LowEdgePause |
| 20260421 | strategy | prebreakout_v41 | LOW_EDGE | 20260424/next_open | protection:LowEdgePause |
| 20260422 | strategy | prebreakout_v41 | LOW_EDGE | 20260427/next_open | protection:LowEdgePause |
| 20260508 | strategy | prebreakout_v41 | LOW_EDGE | 20260513/next_open | protection:LowEdgePause |
| 20260511 | strategy | prebreakout_v41 | LOW_EDGE | 20260514/next_open | protection:LowEdgePause |
| 20260512 | strategy | prebreakout_v41 | LOW_EDGE | 20260515/next_open | protection:LowEdgePause |
| 20260513 | strategy | prebreakout_v41 | LOW_EDGE | 20260518/next_open | protection:LowEdgePause |
| 20260514 | strategy | prebreakout_v41 | LOW_EDGE | 20260519/next_open | protection:LowEdgePause |
| 20260515 | strategy | prebreakout_v41 | LOW_EDGE | 20260520/next_open | protection:LowEdgePause |
| 20260518 | strategy | prebreakout_v41 | LOW_EDGE | 20260521/next_open | protection:LowEdgePause |
| 20260519 | strategy | prebreakout_v41 | LOW_EDGE | 20260522/next_open | protection:LowEdgePause |
| 20260520 | strategy | prebreakout_v41 | LOW_EDGE | 20260525/next_open | protection:LowEdgePause |
| 20260521 | strategy | prebreakout_v41 | LOW_EDGE | 20260526/next_open | protection:LowEdgePause |
| 20260522 | strategy | prebreakout_v41 | LOW_EDGE | 20260527/next_open | protection:LowEdgePause |
| 20260525 | strategy | prebreakout_v41 | LOW_EDGE | 20260528/next_open | protection:LowEdgePause |
| 20260625 | strategy | prebreakout_v41 | LOW_EDGE | 20260630/next_open | protection:LowEdgePause |
| 20260626 | global | * | DRAWDOWN_BRAKE | 20260629/after_review | protection:StrategyDrawdownBrake |
| 20260626 | strategy | prebreakout_v41 | LOW_EDGE | 20260701/next_open | protection:LowEdgePause |
| 20260629 | global | * | DRAWDOWN_BRAKE | 20260630/after_review | protection:StrategyDrawdownBrake |
| 20260629 | strategy | prebreakout_v41 | LOW_EDGE | 20260702/next_open | protection:LowEdgePause |
| 20260630 | global | * | DRAWDOWN_BRAKE | 20260701/after_review | protection:StrategyDrawdownBrake |
| 20260630 | strategy | prebreakout_v41 | LOW_EDGE | 20260703/next_open | protection:LowEdgePause |
| 20260701 | global | * | DRAWDOWN_BRAKE | 20260702/after_review | protection:StrategyDrawdownBrake |
| 20260701 | strategy | prebreakout_v41 | LOW_EDGE | 20260706/next_open | protection:LowEdgePause |
| 20260702 | global | * | DRAWDOWN_BRAKE | 20260703/after_review | protection:StrategyDrawdownBrake |
| 20260702 | strategy | prebreakout_v41 | LOW_EDGE | 20260707/next_open | protection:LowEdgePause |
| 20260709 | strategy | prebreakout_v41 | POST_FAILURE_COOLDOWN | 20260713/next_open | protection:PostFailureCooldown |
| 20260710 | global | * | POST_FAILURE_COOLDOWN | 20260714/next_open | protection:PostFailureCooldown |

## 被挡推荐三列报告 (锁生效日 / 被挡推荐 / 后验超额) 见 blocked_recommendations.csv

