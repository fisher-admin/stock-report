# Lean 深读报告 (Tier-A, G缺口: G3)

- 深读人: Tier-A Lean 轨道研究员 (独立会话)
- 日期: 2026-07-11
- 对象: QuantConnect/Lean @ 冻结SHA `046fb456f8282c1749e42fcf7f8fa45fa4595d74` (manifest/repos.json)
- 许可: Apache-2.0 (可借鉴可改写, 引代码片段合法; 本报告仍以接口形状+机制描述为主)
- 方法: 全程 `raw.githubusercontent.com/<SHA>` / `gh api contents?ref=SHA` 纯文本拉取, 零克隆; 45+2 个源文件落盘逐行读
- 引证格式: `path#Lx-Ly@046fb456f8`
- 议程来源: PHASE1_SYNTHESIS.md §3.5 Lean 部分 + PHASE1_GAPS.md G3 + QuantConnect_Lean.card.json coverage_confession

---

## 0. 判决表 (逐文件)

| 文件 | 判决 | 一句话依据 |
|---|---|---|
| Common/Orders/Fills/EquityFillModel.cs | **LimitFill 前视: 分案裁决** — 常规流(bar边界下单)无前视; 盘中下单+粗分辨率订阅**有残余前视**(同bar部分重叠) | 详见 §1; 触碰判定只查 `bar.EndTime > order.Time` 不查 `bar.Time >= order.Time` (`#L1012-1017`) |
| Common/Orders/Fills/FillModel.cs (基类) | 同上残余前视 (`InternalLimitFill#L698` 同语义); 市价单 stale 修复语义完整且正确 | resting 市价单以 bar **开盘价** 成交如 MOO (`#L1069-1092`) |
| Brokerages/Backtesting/BacktestingBrokerage.cs | "next-bar 规则"只挡同时间戳, 不挡部分重叠 bar; fill 时二次资金校验是亮点 | `Scan()#L265-270` / `#L292-306` |
| Algorithm/Risk/* (5文件) + Algorithm.Framework/Risk/* (5模型) | **接口范本成立** (G3 闭合); 但模型状态全内存不持久化, live 重启丢锚点 | 详见 §2 |
| Algorithm/QCAlgorithm.Framework.cs | 风险叠加合并语义清晰: override-by-symbol, risk 赢 | `ProcessInsights#L200-243` |
| Engine/TransactionHandlers/* (4文件) | 状态机**宽松型**(无穷举转移表), 仅两处点状保护; 两阶段撤单 fallback 是可抄件 | 详见 §3 |
| Common/Securities/IBuyingPowerModel.cs + BuyingPowerModel.cs | 9 方法契约完整; 减仓恒足额/部分成交 pro-rate/最小订单闸三个语义可直接移植 | 详见 §4 |
| Common/Data/Auxiliary/{CorporateFactorProvider,CorporateFactorRow,FactorFile,MapFile}.cs + Common/SecurityIdentifier.cs | PIT 三件套**范本级确认**, 字段表与查询语义见 §5 | — |
| Engine/LeanEngineAlgorithmHandlers.cs / Common/Data/DataMonitor.cs / Common/Isolator.cs | 装配校验/缺口登记/双限额沙箱, 三个守卫全部实现级核实 | 详见 §6 |
| Common/Brokerages/DefaultBrokerageModel.cs | 乐观默认**全表坐实**并扩大: 零滑点之外还有无限做空+2x杠杆+IB费率 | 详见 §6.4 |

---

## 1. LimitFill 日线分辨率前视争议 — 裁决 (议程强制项)

### 1.1 证据链 (全部实现代码, 非注释)

**填单评估时机**: AlgorithmManager 主循环在证券价格用新 bar 更新之后、用户 `OnData` 之前调用 `transactions.ProcessSynchronousEvents()`，注释明言 "process fill models on the updated data before entering algorithm, applies to all non-market orders" (`Engine/AlgorithmManager.cs#L317-319@046fb456f8`)。BacktestingTransactionHandler 在其中调用 `_brokerage.Scan()` (`Engine/TransactionHandlers/BacktestingTransactionHandler.cs#L71-83@046fb456f8`)。

**同时间戳防护**: `BacktestingBrokerage.Scan()` 对非市价单有一条守卫: `if (order.Time == Algorithm.UtcTime && order.Type != OrderType.Market ...)` → 跳过本轮、等下一 bar ("all order fills are processed on the next bar (except for market orders)", `Brokerages/Backtesting/BacktestingBrokerage.cs#L265-270@046fb456f8`)。

**触碰判定数据源**: `EquityFillModel.LimitFill` 用 `GetBestEffortTradeBar(asset, order.Time)` 取"在订单时间之后收盘的 bar"; 该函数**只拒绝 `EndTime <= orderTime` 的 bar** (注释 "Do not accept trade information older than the order", `Common/Orders/Fills/EquityFillModel.cs#L1012-1017@046fb456f8`)，**不检查 bar 的开始时间是否晚于订单提交时间**。基类 `FillModel.InternalLimitFill` 同语义 (`Common/Orders/Fills/FillModel.cs#L697-698@046fb456f8`: `if (pricesEndTime <= order.Time) return fill;`)。

**填单规则** (`EquityFillModel.cs#L386-454@046fb456f8`):
- 买限价: `tradeBar.Low < LimitPrice` (严格小于) → 以 LimitPrice 成交; 若 `tradeBar.Open < LimitPrice` (有利跳空) → 以 Open 成交;
- 卖侧镜像; `FillQuantity = order.Quantity` 全量 (L417 TODO 自认应做 DepthLimited 部分成交);
- 严格不等号的理由在注释里写明: 假设自己排在该价位队尾, 触及不成交、穿透才成交 (`#L376-379`)。

**对照组 (同文件内已存在正确写法)**: `MarketOnOpenFill` 显式拒绝"在订单提交前就开始聚合"的 bar: `if (tradeBar.Time < localOrderTime) return fill;` (`EquityFillModel.cs#L531@046fb456f8`)。`StopLimitFill` 则对 bar 内次序不可知性显式保守: "Can't use minimum price, because no way to be sure minimum wasn't before the stop triggered" (`#L320, L342`)。

### 1.2 裁决

**案 A — 常规日频流 (订单在 bar 边界提交, 即上一日收盘处理完后下单): 无前视, 指控不成立。**
订单在整根 bar 开始前就已存续; bar 收盘时刻评估时全 bar OHLC 已是"已发生"信息; "Low 触及限价则成交"是对一张全程有效挂单的合法触碰检测。真正的问题是**乐观而非前视**: (a) 触价即全量成交 = 无限深度 (L417 自认); (b) 成交记账时间戳为 bar 收盘 (`fill` 的 utcTime = 当前算法时间, `#L389`), 而经济上成交发生在 bar 内某未知时刻 → 日内资金占用/权益曲线粒度失真, 但不泄露未来信息。有利跳空按开盘价成交 (`#L423-429`) 反而是把乐观往回收的正确处理。

**案 B — 盘中提交订单 + 小时/日线订阅 (如 Schedule.On 上午10点下单): 残余前视成立, 指控坐实。**
当日日线 bar 的 `EndTime`(收盘) > 订单时间, 通过 `GetBestEffortTradeBar` 检查; 于是收盘评估时用**整日** OHLC 判定触碰——包括订单提交前(上午9:30-10:00)打出的 Low/High/Open。具体可构造的错误: 10:00 提交买限价 9.90, 当日 9:35 曾打到 9.80 后回到 10.20 一路走高——真实世界该单永不成交, Lean 回测按 9.90 成交(甚至若 Open=9.85 按"有利跳空"以 9.85 成交, 而 Open 发生在订单存在之前 45 分钟)。`Scan` 的 next-bar 守卫(案 A 的保障)在此失效, 因为它比较的是时间戳相等而非 bar 区间覆盖。**同文件 MOO 有正确守卫而 LimitFill/StopMarketFill/基类 InternalLimitFill 都没有**, 属实现不一致而非设计立场。
(注: 2026-06 的 #9518 修复只治市价单 stale 价成交——`ShouldWaitForFreshDataOnStale` 粗分辨率恒等待 (`FillModel.cs#L1045-1067@046fb456f8`), resting 市价单按 bar 开盘价成交 (`#L1080-1092`); 限价/止损家族的同 bar 部分重叠问题不在修复范围。)

### 1.3 对我方的规格 (写进回测断言与闯关机)

规则 R-LIMIT-1: **限价/止损类订单的触碰判定, 只允许使用"订单存续期完整覆盖"的 bar**; 凡 bar 起点早于订单生效时刻, 该 bar 只能拆分(有分钟数据时)或顺延到下一根完整 bar, 二者都不可用时不判成交。
验收(负向测试, 红线2风格): 构造"极值发生在订单提交之前"的 bar, 断言不成交; 构造"开盘有利跳空但开盘早于订单"场景, 断言不按开盘价成交。
规则 R-LIMIT-2: 触价成交量默认设参与率上限(对齐 backtrader 滑点三件套公理与我方 B1 标定), 全量成交只能显式 opt-in。
成本: 半天 (我方 O2C/闯关机回测器只有日频, 规则退化为"信号日盘中价不可用于当日限价成交判定"——现状已满足的要写成回归测试钉死, 防未来引入分钟级撮合时破窗)。

---

## 2. G3 补账: Algorithm.Framework Risk 接口带引证读透

### 2.1 接口与接线 (manifest "风险模型接口范本" 的实体)

**接口** (`Algorithm/Risk/IRiskManagementModel.cs#L24-32@046fb456f8`):
```csharp
public interface IRiskManagementModel : INotifiedSecurityChanges
{
    IEnumerable<IPortfolioTarget> ManageRisk(QCAlgorithm algorithm, IPortfolioTarget[] targets);
}
```
输入=组合构建产出的目标仓位数组; 输出=**只含需要覆写的目标** (空集=不干预)。`IPortfolioTarget` 三字段: `Symbol / Quantity(目标持仓量) / Tag(附加说明)` (`Common/Algorithm/Framework/Portfolio/IPortfolioTarget.cs#L22-38@046fb456f8`)。基类 `RiskManagementModel` 仅提供 `OnSecuritiesChanged` 空实现 (`Algorithm/Risk/RiskManagementModel.cs#L25-45@046fb456f8`)。

**接线与合并语义** (`Algorithm/QCAlgorithm.Framework.cs@046fb456f8`): 五段流水线在 `OnFrameworkData→ProcessInsights` 中串行: `PortfolioConstruction.CreateTargets` (L179) → `RiskManagement.ManageRisk(this, targets)` (L200) → 覆写合并 `riskTargetOverrides.Concat(targets).DistinctBy(pt => pt.Symbol)` — **risk 覆写按 symbol 赢** (L227) → `Execution.Execute(this, riskAdjustedTargets)` (L243)。调用节奏=每个有数据的 slice 都跑 (L147-165), 与是否有新 insight 无关。
组合器 `CompositeRiskManagementModel` 顺序链式: 每个子模型看到的是**前序子模型已调整后的 targets**, 合并同样 newer-first `DistinctBy` (`Algorithm/Risk/CompositeRiskManagementModel.cs#L89-101@046fb456f8`); 空模型列表构造直接抛异常 (L40-43)。`NullRiskManagementModel` 返回空集 (`Algorithm/Risk/NullRiskManagementModel.cs#L17-20@046fb456f8`); `AddRiskManagement` 自动把单模型升级为 Composite (`QCAlgorithm.Framework.cs#L376-393`)。

### 2.2 五个内置模型 (全部读透)

| 模型 | 触发条件 | 动作 | 状态 | 引证 |
|---|---|---|---|---|
| MaximumDrawdownPercentPortfolio | 组合总值相对锚点回撤 > x% (isTrailing 可选跟踪高点) | 对**当前所有 targets** 发 `PortfolioTarget(symbol, 0)` 清仓 + `Insights.Cancel`; 触发后 `_initialised=false` 重置锚点等下一轮再投 | `_portfolioHigh` 内存态 | `Algorithm.Framework/Risk/MaximumDrawdownPercentPortfolio.cs#L52-90@046fb456f8` |
| MaximumDrawdownPercentPerSecurity | 单持仓 `UnrealizedProfitPercent < -x%` | 该标的 target=0 + Insights.Cancel | 无状态 | `MaximumDrawdownPercentPerSecurity.cs#L48-71@046fb456f8` |
| MaximumUnrealizedProfitPercentPerSecurity | 单持仓浮盈 > x% (止盈) | 同上 | 无状态 | `MaximumUnrealizedProfitPercentPerSecurity.cs#L48-71@046fb456f8` |
| TrailingStopRiskManagementModel | 持仓市值绝对额自峰值回撤 > x% (多空分侧, 换向重置) | 该标的 target=0 + Insights.Cancel + 删锚点 | 每 symbol `HoldingsState{Position, AbsoluteHoldingsValue}` 字典 | `TrailingStopRiskManagementModel.cs#L46-110@046fb456f8` |
| MaximumSectorExposureRiskManagementModel | 按 Morningstar `IndustryTemplateCode` 分组, 行业敞口(现值或 pending target 值取大者) > 组合总值×x% | 该行业**全部标的等比例缩量** `quantity/ratio` | `PortfolioTargetCollection` | `MaximumSectorExposureRiskManagementModel.cs#L56-113@046fb456f8`; 宇宙无基本面数据时 fail-fast 抛异常 `#L120-129` |

### 2.3 三条设计结论 (对我方 Wave2 组合风险契约)

1. **风险输出与组合输出同类型 (target overlay)**: 风险模块不直接下单、不接触订单对象, 只改"目标仓位"并由执行层统一 diff→下单。与 freqtrade 保护(入场闸)正交互补: Lean 风险=持仓侧(强制减仓/清仓), freqtrade 保护=入场侧(锁窗口)。我方合同应两者都有: `RiskOverlay(targets, portfolio_state) -> overrides` + `EntryLock(reason, until, side)`。
2. **`Insights.Cancel` 防再入耦合**: 每个清仓动作都同时取消上游 alpha 信号 (五模型全部如此), 否则组合构建下一轮立即按未过期 insight 重新买回——这是"风控清仓后被策略秒买回"问题的结构性解法。我方等价物: 风险覆写必须同步作废候选池里对应票的当日推荐(candidate_state 置 risk_vetoed), 不能只改发布名单。
3. **反面: 状态不持久化**。`_portfolioHigh`/trailing 字典全在内存, live 重启即丢失回撤锚点; 且 MaximumDrawdownPercentPortfolio 触发后重置锚点到崩后净值, 相当于自动"解锁", 无显式恢复条件。这坐实了 freqtrade 轨道"锁落库+显式恢复条件"路线的必要性——两仓对照后我方定稿: **风险状态一律落库, 恢复条件显式声明** (strategy_locks 表方案不变)。

G3 判定: manifest "风险模型接口范本"后半句**兑现**, 引证链修复完毕。

---

## 3. 盲区补账 A: TransactionHandlers 订单状态机

### 3.1 状态枚举与转移纪律

状态集 (`Common/Orders/OrderTypes.cs#L138-184@046fb456f8`): `New(0) → Submitted(1) → PartiallyFilled(2) → Filled(3)`; 旁路 `Canceled(5) / Invalid(7) / CancelPending(8) / UpdateSubmitted(9)`; `None(6)` 哨兵。
**关键发现: Lean 没有穷举转移表** (对照 NautilusTrader `OrderStatus::transition` 的穷举 match)。`order.Status` 由订单事件直接赋值, 全部保护只有一条: 已 Filled/Canceled 的单不被 Invalid 事件降级覆盖 (`Engine/TransactionHandlers/BrokerageTransactionHandler.cs#L1204-1210@046fb456f8`, 注释解释 live 场景: 对已成交单提交 update 被拒, 不能把已成交状态改成 invalid)。状态机正确性靠调用点纪律而非类型系统——**宽松型状态机**, 属可运行但不可证明的形态。

### 3.2 生命周期与可抄件

- **提交链** (`AddOrder#L317-384` → 线程池 → `HandleSubmitOrderRequest#L856-961`): 依次 = 可做空校验(见下) → 建 ticket → **快照提交时点行情** `OrderSubmissionData(bid, ask, close)` (`#L356`) → 手数取整 `RoundOffOrder` (`#L875`) → 价格最小变动价位取整 (`#L881`) → 零量作废 (`#L895-902`) → **资金校验** (`#L914`) → BrokerageModel.CanSubmitOrder (`#L920-934`) → PlaceOrder (`#L941`); 每个失败分支都 `InvalidateOrders(orders, reason)` 带因由终态。
- **做空校验环境不对称 (反模式)**: 空单超可空量, 回测=硬拒 Invalid, **live=仅 Debug 警告放行** (`#L329-341`)。"回测比实盘严"的方向反转, 意味着回测通过不保证实盘同行为。我方规则: 环境间闸门语义必须一致, 差异只允许出现在"实盘更严"方向。
- **两阶段撤单 + 失败回退 (可抄件)**: `CancelOrder` 先 `_cancelPendingOrders.Set(order.Id, 原状态)` 再置 `CancelPending` (`#L536-548`); 撤单失败 `RemoveAndFallback` **恢复保存的原状态**, 成交/终态事件 `UpdateOrRemove` 清除挂起项, 两线程竞争用 per-order 锁明确判定谁赢 (`Engine/TransactionHandlers/CancelPendingOrders.cs#L37-96@046fb456f8`)。这是"撤单不是瞬时动作而是一次可失败的请求"的最小正确实现。
- **双字典生命周期**: `_openOrders / _completeOrders(+tickets)`, 终态事件到达时原子迁移 (`#L1176-1181`); 开放单与完成单查询路径分离。
- **成交处理**: `HandleOrderEvents` 批量: 设时间戳(Canceled→CanceledTime, Fill→LastFillTime, `#L1215-1234`) → 事件回填当前限价/止损价便于消费者 (`#L1236-1268`) → `Portfolio.ProcessFills` (`#L1301`) → TradeBuilder → ticket → 最后才进用户 `OnOrderEvent` (`#L1343-1371`)——**先记账后通知**的顺序纪律。
- **回测 fill 时二次资金校验**: `BacktestingBrokerage.Scan` 在每次尝试撮合前重新 `HasSufficientBuyingPowerForOrder` (`Brokerages/Backtesting/BacktestingBrokerage.cs#L292-306@046fb456f8`), 资金不足→Invalid 并删除。资源校验发生在**提交时与执行时两处**, 挂单期间组合变化被正确感知。

### 3.3 对我方状态表的落地结论

我方"候选→发布→AI完成/失败→复盘"状态表采 NT 式**穷举转移表**为骨架(NT 轨道产出), 从 Lean 补三件: ①终态带因由 (`Invalid + reason`, 等价我方 REJECTED+reason code); ②两阶段"撤/改也是请求"模式——对我方即"撤回已发布推荐"必须走 pending→confirmed/fallback, 不允许直接改终态; ③状态快照字段 (提交时点行情快照 OrderSubmissionData 的等价物: 候选冻结时点的行情快照, 我方 S3 已有——确认现状即达标, 不新建)。成本: 并入 NT 轨道状态表设计, 增量 0.5 天。

---

## 4. 盲区补账 B: BuyingPower / SecurityMarginModel

### 4.1 接口形状 (9 方法, `Common/Securities/IBuyingPowerModel.cs#L21-98@046fb456f8`)

`GetLeverage / SetLeverage / GetMaintenanceMargin / GetInitialMarginRequirement / GetInitialMarginRequiredForOrder(含费) / HasSufficientBuyingPowerForOrder / GetMaximumOrderQuantityForTargetBuyingPower / GetMaximumOrderQuantityForDeltaBuyingPower / GetReservedBuyingPowerForPosition / GetBuyingPower` — 参数与返回全部是专用参数对象/结果对象 (如 `HasSufficientBuyingPowerForOrderResult{IsSufficient, Reason}`), 拒绝裸 bool/decimal。`SecurityMarginModel` 只是 `BuyingPowerModel` 的具名薄壳 (`Common/Securities/SecurityMarginModel.cs#L21-59@046fb456f8`)。

### 4.2 三个可移植语义 (`Common/Securities/BuyingPowerModel.cs@046fb456f8`)

1. **减仓/平仓恒足额**: 订单方向与持仓相反且量不超持仓 → 直接 Sufficient (`#L301-305`)。防"资金不足连止损都下不出去"的死锁——我方 protection 的 A股版"退出受阻"场景要继承此语义: 风控卖出永不被资金闸拦截。
2. **部分成交 pro-rate**: 校验的是**未成交余量**所需保证金 `percentUnfilled × initialMarginRequiredForOrder` (`#L313-315`), 不重复占用已成交部分。
3. **目标仓位→数量的收敛环 + 最小订单闸**: `GetMaximumOrderQuantityForTargetBuyingPower` (`#L366-449+`) 用初始保证金口径防"同目标反复下单震荡" (`#L381-383` 注释), 费用迭代收敛, 且**低于组合最小订单保证金占比直接返回 0 并给因由** (`#L395-413`)——防尘埃单。我方 target-position 落地时抄这三条断言, 不抄实现。

### 4.3 A股 T+1 的插槽定位

Lean 无 T+1 持仓锁(卡已判), 但接口拓扑给出插槽: 结算走 `SettlementModel.Scan`(AlgorithmManager 每小时对每证券扫描, `Engine/AlgorithmManager.cs#L256-266@046fb456f8`), 现金账户股票默认 `DelayedSettlementModel`(T+N 资金), 保证金账户 `ImmediateSettlementModel` (`Common/Brokerages/DefaultBrokerageModel.cs#L291-311@046fb456f8`)。**T+1 可卖余额的正确归属是 BuyingPowerModel/持仓台账层(vnpy yd/td/frozen 骨架), 而非 FillModel**——fill 层判"能否成交", 资金层判"能否下单", 结算层判"何时可用"。我方回测器三层已分离(闯关机扣费/可交易闸门), 差距仅是把 T+1 可卖余额显式建模为台账字段, 并入 vnpy 轨道条目, 不新建。

---

## 5. PIT 三件套设计稿 (对我方 = 升级差距, 非新建)

### 5.1 Lean 实现级确认 (字段表)

**A. 复权因子层** `CorporateFactorRow` (`Common/Data/Auxiliary/CorporateFactorRow.cs#L30-90@046fb456f8`):

| 字段 | 语义 |
|---|---|
| Date | 因子生效边界 = 事件**前一交易日** (文档注释明示, `CorporateFactorProvider.cs#L99-104, L136-141`) |
| PriceFactor | 分红因子 (累计, 从最新行=1 向历史累乘) |
| SplitFactor | 拆股因子 (同上) |
| PriceScaleFactor | 派生 = PriceFactor×SplitFactor (setter 自动维护) |
| ReferencePrice | 事件前一交易日 raw 收盘 (用于重建分红/拆股事件) |

查询语义 (`CorporateFactorProvider.GetPriceFactor#L42-76@046fb456f8`): 倒序日期表中找**最小的 ≥ searchDate 的行**, 取其因子; 行内已存累计积, 单行查找即得全量调整。五口径 switch: `Raw→返回0哨兵(不缩放)`, `Adjusted/ScaledRaw→PriceScaleFactor`, `TotalReturn/SplitAdjusted→SplitFactor`。精度逃生舱: INF 拆股行 → `FactorFileMinimumDate` 前移最早可交易日 (`FactorFile.cs#L44-52@046fb456f8`, 已知票 GBSN/JUNI/NEWL)。

**B. map 层** `MapFile` (`Common/Data/Auxiliary/MapFile.cs#L30-125@046fb456f8`): 行={Date, MappedSymbol}; `GetMappedSymbol(searchDate)` = 首个 `Date >= searchDate` 的行 (L106-121); `DelistingDate`=末行日期, `FirstDate`=首行 (L78-79)。改名/退市与价格数据解耦成独立数据资产。

**C. 永久 ID** `SecurityIdentifier.GenerateEquity` (`Common/SecurityIdentifier.cs#L451-461, L490-494@046fb456f8`): 经 map file 解析出首交易日+首 ticker 编入不可变 ID; `Ticker(symbol, date)` 可反查任意历史日真实 ticker (L470-482)。

**D. 装配硬校验**: `ApiDataProvider` 必须搭配 LocalZip 两 provider 否则装配期抛 ArgumentException (`Engine/LeanEngineAlgorithmHandlers.cs#L266-271@046fb456f8`)。

### 5.2 我方设计稿 (对照 system_inventory: 治"无 Raw 不可变层/缓存盘中污染/无口径 API")

```
stock_data/00-raw-immutable/
  daily/{trade_date}.parquet         # as-traded 原始价, append-only, 收盘后写一次, 永不改写
  adj_factor/{trade_date}.parquet    # tushare adj_factor 按日落盘 (累计因子, 语义≡Lean PriceScaleFactor)
  status_map.parquet                 # 见下
```

**读取端唯一口径 API** (data_adapter 增量, 不动 panel_full):
```python
load_prices(codes, start, end, *,
    mode: Literal["raw", "qfq_asof", "hfq"],   # 必填, 无默认 —— 学 DataNormalizationMode 枚举强制声明
    asof: date | None)                          # mode="qfq_asof" 时必填
# 恒等式(验收): qfq(t, asof) = close_raw(t) * adj(t) / adj(asof)
# hfq(t) = close_raw(t) * adj(t)
```
要点: tushare `adj_factor` 天然是 Lean 式累计因子, 我方**不需要因子文件生成器**, 只需按日快照防"服务端时点口径"污染 (akshare qfq 前科)。qfq 强制绑定 `asof` 使任何脚本产出可复现、可对账——这是把"面板_full 保护"从约定升级为结构保证的最小实现。

**status map 层** (Lean map file 的 A股转译——A股代码不复用, 该层承担的是**状态区间史**):
```
status_map 行: {ts_code, start_date, end_date, status ∈ {normal, ST, *ST, suspended, delisted}, name}
+ 每票聚合: {ts_code, list_date, delist_date|null}
```
来源: tushare `namechange` + `stock_basic`; S3 冻结人群仅作对照校验(不重建)。PIT 验收: 任取 as-of 日重放全市场 status, 与当日快照零差异; 负向测试=喂未来改名记录断言被拒。

**装配校验**: 管线 stage0 断言 `max(raw.daily.date) == max(adj_factor.date) == 日历最新已收盘交易日`, 不齐 fail-fast——学 FromConfiguration 组合校验, 兼治 0710 盘中裸跑污染类事故 (与现有"收盘前禁裸跑"护栏叠加, 校验的是**数据资产间一致性**而非仅时钟)。

成本: raw+factor 层+口径 API 2-3 天(大头是消费端 grep 改显式口径); status map 1 天; 装配断言 0.5 天。

---

## 6. 可插拔契约与守卫 (议程项, 实现级核实)

### 6.1 装配校验 + DataMonitor
- 十件套 handler 全部 config 字符串按类型名装配, 装配期组合合法性硬校验见 §5.1-D (`LeanEngineAlgorithmHandlers.FromConfiguration#L235-276@046fb456f8`)。
- DataMonitor 仅回测/非研究模式挂载 `DataProvider.NewDataRequest += DataMonitor.OnNewDataRequest` (`#L190-194`); 实现: 每次数据请求按成败分别追加 `succeeded/failed-data-requests.txt`, 计数器区分 universe 数据, **map/factor 文件请求显式豁免** (`Common/Data/DataMonitor.cs#L68-110@046fb456f8`)。对我方: data_adapter 的 miss 记录器 + 收盘缺口日报(卡内 0.5-1 天估计确认可行), 建议同样豁免辅助数据、区分 universe 级缺口(整市场接口挂)与单票缺口(个股停牌/摘牌), 两类走不同告警级别。

### 6.2 Isolator (stage guard 范本)
`ExecuteWithTimeLimit(timeSpan, withinCustomLimits, codeBlock, memoryCap=1024MB, sleepIntervalMillis, workerThread)` (`Common/Isolator.cs#L58-69@046fb456f8`); 监控环: `memoryCap×1024×1024`, **尖峰限额=2×memoryCap** (`#L100-101`) 区分持续超限与瞬时尖峰; 超时/超内存→取消令牌→判 fail。Engine 把整个算法 Run 包进去 (`Engine/Engine.cs#L346-366@046fb456f8`)。我方 stage runner 包装器(卡内 1 天估计)增补一条 Lean 细节: **持续限额与尖峰限额分开设**, 避免 GC 尖峰误杀。

### 6.3 FillModel stale 守卫 (修复后语义全景)
- 市价单: 粗分辨率(小时/日线)且价格陈旧→恒等待新 bar (`FillModel.ShouldWaitForFreshData#L1004-1027@046fb456f8`); 细分辨率落后超一根 bar→等待 (`ShouldWaitForFreshDataOnStale#L1045-1067`); resting 市价单(先于 bar 存在)按 **bar 开盘价** 成交, bar 内提交的按现价 (`GetMarketFillPrice#L1080-1092`)。`EquityFillModel.MarketFill` 接线 (`#L162-170`)。
- 该守卫家族**只覆盖市价单**; 限价家族残余问题见 §1。

### 6.4 DefaultBrokerageModel 乐观默认全表 (反模式清单坐实+扩充)

| 维度 | 默认值 | 引证 |
|---|---|---|
| 滑点 | NullSlippageModel (零滑点) | `Common/Brokerages/DefaultBrokerageModel.cs#L281-284@046fb456f8` |
| 股票费用 | InteractiveBrokersFeeModel (美股佣金表) | `#L252-274` |
| 股票杠杆 | 保证金账户 2x / 现金 1x | `#L172-204` |
| 可做空量 | **NullShortableProvider = 无限做空** (注释自认) | `#L355-361` |
| Fill | Equity→EquityFillModel, 多数资产→ImmediateFillModel | `#L222-245` |
| 结算 | 现金股票 DelayedSettlementModel(T+N), 保证金→Immediate | `#L291-311` |
| CanSubmit/CanUpdate/CanExecute | 恒 true (仅期货 MOO 例外) | `#L103-143` |
| BuyingPower | 现金→CashBuyingPowerModel, 保证金股票→SecurityMarginModel(2x) | `#L331-349` |

教训不变且加重: 乐观默认不止滑点/全量成交两处, 是**八处一致的隐性立场**。我方回测/复盘默认恒为保守侧(有滑点+涨跌停不可成交+参与率上限+禁做空), 乐观仅显式 opt-in——写入闯关机回归用例。

---

## G条目闭合声明

- **G3 (QuantConnect/Lean — 风险模型接口范本零引证): 闭合。** `Algorithm/Risk/` 全部 5 文件 + `Algorithm.Framework/Risk/` 全部 5 模型 + `QCAlgorithm.Framework.cs` 接线 + `IPortfolioTarget` 形状, 全部带行号引证读透 (本报告 §2); manifest why 后半句兑现, 无需降级。
- 附带闭合 (议程绑定的卡自白盲区, 对应 PHASE1_SYNTHESIS §5.2 第5条 Lean 部分): **TransactionHandlers 订单状态机** (§3, 闭合)、**SecurityMarginModel/BuyingPower** (§4, 闭合)、**LimitFill 日线前视争议** (§1, **已裁决**: 常规流无前视/盘中下单+粗分辨率有残余前视, 判据为实现代码非注释)。
- 本轨道不涉及其他 G 条目。

## coverage_confession

未读: AlgorithmManager.cs 除 L130-335 主循环片段外的部分(含 warmup/margin call 触发点); OrderTicket.cs 内部(仅 API 面+OrderSet 事件语义); Order.cs/OrderEvent.cs 全文(仅状态相关字段); CashBuyingPowerModel 具体实现(仅确认其存在与装配位置, T+N 结算资金与下单校验的交互未验); Positions/ 组合保证金体系(期权类, 与我方无关); TimeInForce.IsFillValid (GTC/Day 撤单语义); ProcessDelistings 强平路径; DelayedSettlementModel 实现; LiveTradingDataFeed/实盘容错; Python 集成层; VolumeShareSlippageModel 本轮未重读(沿用卡引证); MOO/MOC 的 tick 路径读了代码未运行。
不确定: ①§1 裁决基于静态代码阅读+调用链追踪, 未实际运行 Lean 复现案 B(构造回归算法约需 Docker 环境, 超出磁盘纪律); 置信度高(三处代码互证+对照组), 但"是否有其他上游过滤挡住盘中限价单"不能 100% 排除——若要引用到对外文档建议标"代码级证据"; ②`ProcessAsynchronousEvents` 实盘路径的 Scan 时序未追; ③Risk 模型在 warmup 期间是否被调用未验。

## Wave 映射

| # | 产出 | Wave | 粗成本 |
|---|---|---|---|
| 1 | PIT 三件套设计稿落地: raw 不可变层 + adj_factor 独立层 + 口径枚举 API(qfq 强制 asof) + status map 层 + 装配一致性断言 (§5.2) | Wave1 数据可信 | 3.5-4.5 天 |
| 2 | R-LIMIT-1/2 限价触碰规则 + 负向测试进闯关机与 B1 标定 (§1.3) | Wave1 数据可信(回测护栏) | 0.5 天 |
| 3 | 乐观默认反转清单(八处)→闯关机回归用例 (§6.4) | Wave1 | 0.5 天 |
| 4 | DataMonitor 式缺口登记: data_adapter miss 记录器+收盘缺口日报(universe/单票分级) (§6.1) | Wave1 | 0.5-1 天 |
| 5 | RiskOverlay 协议(target overlay + 覆写合并语义 + Insights.Cancel 等价物 risk_vetoed) + 风险状态落库定稿 (§2.3) | Wave2 组合风险契约 | 1-2 天 |
| 6 | 订单/推荐状态表增补: 终态带因由+两阶段撤回 fallback+执行时二次资源校验 (§3.3, 并入 NT 轨道状态表) | Wave2 组合风险契约 | 0.5 天增量 |
| 7 | BuyingPower 三断言(减仓恒足额/pro-rate/最小订单闸) + T+1 台账插槽定位(并入 vnpy 条目) (§4) | Wave2 组合风险契约 | 0.5 天 |
| 8 | Isolator 双限额(持续+尖峰) stage runner 细节 (§6.2) | Wave2 (protection) | 已含卡内 1 天估计 |
| 9 | 上游迁移警示(九年无 release)与 AI 消融: 本轨道无新增(维持卡内条目) | Wave3/上游迁移 | 0 |

红线自检: 全部条目对照 system_inventory 为升级差距(raw 层/口径 API/状态表增补), 无重复建设; 无 GPL 代码(Lean 为 Apache-2.0, 引证片段合法); 全部引证绑定冻结 SHA; 每项有回滚面(新增层/新增断言, 不改冻结资产)。
