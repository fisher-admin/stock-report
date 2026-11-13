# A股长多多因子集架构 v1（评审团综合稿，2026-06-28）

> 来源：4 架构师评审团（学院派风险溢价 / 实战 alpha 叠加 / A股资金面微结构 / 成本优先低换手）独立设计
> → 对抗打分 → 综合。4 套独立设计高度收敛，收敛部分即高置信结论。
> 关联记忆：strategy-efficacy-real-benchmark（价量因子已证伪）、t1-factor-no-tradeable-edge。

## 0. 前提（已确证，别再踩坑）
- 现有面板 ~140 因子几乎全是价量/技术：全市场 rank IC 封顶 ~0.02、高度共线、日频换手被 13–30bp 成本吃光。
  O2C composite 修复生存者偏差 bug 后 pre-holdout 9 配置全负、holdout@30bp −0.8%/年 → **无可部署边际**。
- 结论：不再向价量要 alpha。换底层信息源——慢变量 + 正交于价量。

## 1. 四家共识（高置信）
1. 价量降级为闸门/overlay（权重 5–12%）。
2. Alpha 主权重 → 价值/质量 + 盈余惊喜(PEAD) + 北向聪明钱 + 事件供给 + 股东户数集中。
3. 月度调仓，换手被信号速度封顶，成本摊到 ~0.5–1.4%/年。
4. 两层 z-score 打分合成（不上优化器），sleeve 内等权/ICIR 弱加权，sleeve 间静态先验 + 有界 ±30% 滚动IC 微调。
5. 三重中性化：行业 → 市值 → 风格（打分阶段）。
6. **头号风险（四家一致）**：免费 akshare 数据的时点纯净度（公告日 vs 报告期末）与可得性 = 单点失败源。

## 2. 推荐因子集构成

| Sleeve | 权重 | 换手 | 因子 | 数据源 |
|---|---|---|---|---|
| 价值锚 | 23% | 低 | EP_ttm, BP, SP_ttm, 股息率 | pit_financial_cache + stock_a_indicator_lg |
| 质量 | 21% | 低 | ROE, 毛利/总资产, 低应计, 低杠杆 | pit_financial_cache(已有ROE/负债率) + akshare |
| 盈余惊喜/修正 | 19% | 中 | 业绩预告方向幅度(PEAD), 快报超预期, 分析师上调 | stock_yjyg_em / stock_yjkb_em |
| 聪明钱流向 | 17% | 中 | 北向持股20日变动, 龙虎榜机构净买, 融资余额加速度 | stock_hsgt_hold_stock_em / stock_lhb_detail_em |
| 事件供给 | 13% | 低 | 解禁压力(负), 回购(正), 高管增减持, 股东户数集中 | stock_restricted_release / stock_repurchase_em / stock_zh_a_gdhs |
| 价量Overlay(残差) | 7% | 中 | 趋势质量, 低波领导力, 流动性领导力(对价量PC正交化) | 面板 g_trend_quality_20 / g_low_vol_leader / g_liquid_leadership |
| 硬风险闸(二值过滤,非加权) | — | — | 涨跌停/停牌/流动性下限/上市天数/崩塌剔除 | 面板 universe_flag / pct_chg / f_amihud_20 / rolling_drawdown_20 |

## 3. 合成与运行
- 合成：① sleeve 内 winsorize(1/99)→行业内 rank→z-score→对(价量PC+log市值+行业哑变量)回归取残差→等权/ICIR弱加权；
  ② sleeve 间静态先验 + 滚动126日 sleeve-IC 的 ±30% 有界微调（月更、封顶）。
- 选股：composite 行业内 Top 10–15%，流动性加权温和集中，过硬风险闸。
- 中性化：行业→市值→风格（打分阶段，非事后）。
- 调仓：月度（信号月末T-1、建仓次月首日T+1开盘VWAP分批、涨停买单顺延、停牌冻结）。单边年换手 ~150–220%。
- 成本：30bp 保守往返，拖累 ~0.5–0.7%/年。基准 = 可买指数(沪深300/中证500 ETF) 算真 IR，不用全域均值。

## 4. 落地路线（两阶段排雷）
- **Phase A（不引入新数据）**：用 pit_financial_cache(已有 ROE/负债率/EPS) + 面板低波/动量/规模代理，搭
  价值+质量+低波+动量 骨架 → full_market_factor_eval + 已有 nested/walk-forward/冻结holdout 闸门 + 安慰剂。
  **有独立净边际才往下走；没有就止损。**
- **Phase B（加正交 alpha）**：确认骨架有边际后，按公告日严格时点对齐 ingest 北向/PEAD/事件，对 Phase-A 正交化后加入，重验闸门。

## 5. 诚实边界
这是**待检验的假设架构，非已证明策略**。成立取决于 Phase A 实测闸门 + 数据时点纯净度，不取决于设计本身。
比纯价量（已证伪）在经济学根基/正交性/抗成本上明显更高，但**第一性原则不变：先把数据做干净、过闸门、过安慰剂，再谈投钱**。
