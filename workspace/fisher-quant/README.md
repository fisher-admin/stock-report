# fisher-quant —— 渐进重构目标骨架

本目录是 A 股推荐系统**渐进重构的目标架构骨架**：旧系统（daily_stock_analysis /
scripts 等）继续跑生产，新能力一律先落在这里，验证后逐步切流。当前已落地
**阶段3 基建：诚实回测引擎 v2 + 验证闭环复活**。

## 目录结构

```
fisher-quant/
├── quant/                  # 库代码（只读访问金数据，纯函数引擎）
│   ├── datahub.py          # 只读数据访问层 + 交易日历/板块涨停阈值/银行识别
│   ├── backtest_v2.py      # 诚实回测引擎（次日开盘入场、涨停/停牌剔除、成本扣减）
│   └── verify.py           # 选股快照 T+1/T+3/T+5 验证（close-to-close 信号质量口径）
├── ops/                    # 可执行入口（CLI）
│   ├── run_verify_backfill.py   # 全量回填 selection_history 验证 → verify_history_v2/
│   └── run_backtest_replay.py   # 历史实盘选股回放（热修前因子）→ strategy_research/
└── tests/
    └── test_backtest_v2.py # 合成数据单元测试（python3 直接运行）
```

## 数据约定（强约束）

- **金数据源唯一**：`stock_data/03-working/recommendation_warehouse/recommendations.db`，
  一律 `sqlite3.connect('file:...?mode=ro', uri=True)` **只读**打开，任何代码不得写入。
- 交易日历 = `price_daily_cache` 的 `DISTINCT trade_date`；`pct_chg` 为百分数单位。
- 市场基线 = 全市场等权日均涨跌幅（按日 `AVG(pct_chg)`）。
- 路径默认相对 workspace 根目录解析（包位于 `<workspace>/fisher-quant/`），
  可用环境变量 `FISHER_QUANT_WORKSPACE` 覆盖，沙箱/宿主机两侧通用。

## 诚实执行规则（backtest_v2）

1. 选股日 T 的票，**次一交易日开盘价**买入（绝不用 T 日收盘价假装成交）。
2. 入场日开盘 `>= 前收 × (1+板块阈值) × 0.995` 判为一字/近涨停，**买不进，剔除并计数**。
   板块阈值按 ts_code 前缀：主板 9.8%，创业板(300/301/302)/科创板(688/689) 19.8%，
   北交所(8xx/43x/92x) 29.8%。
3. 入场日无行情（停牌/缺数据）→ 剔除并计数；持有期内停牌按最近收盘价携带并打标记。
4. T+N 持有期：入场日之后第 N 个交易日**收盘**卖出（N≥1，天然满足 A 股 T+1）。
5. 往返成本逐笔扣减：佣金 0.03%×2 + 印花税 0.05%(卖出) + 滑点 0.075%×2 ≈ **0.26%**
   （`total_cost_pct` 可配置）。
6. 所有指标全部由数据计算得出，**代码中不允许出现任何硬编码结果数字**。

## 两条产出线

| 产出 | 口径 | 输出位置 |
|---|---|---|
| 验证闭环（verify） | 快照日收盘→T+N 收盘，无摩擦，衡量**信号质量** | `stock_data/03-working/verify_history_v2/{date}.json` + `summary_latest.json` |
| 回测回放（backtest_v2） | 次日开盘入场+涨停剔除+成本，衡量**可实现收益** | `stock_data/03-working/strategy_research/backtest_v2_prebreakout_<lastdate>.json` |

注意：selection_history 是生产环境**热修前因子**的真实推荐快照（银行占比偏高），
回放结果如实反映系统历史上真正推荐过什么——这正是它的价值，输出已标注
「历史实盘选股回放（热修前因子）」。

## 运行

```bash
cd <workspace>/fisher-quant
python3 tests/test_backtest_v2.py        # 单元测试（全部必须通过）
python3 ops/run_verify_backfill.py       # 全量验证回填
python3 ops/run_backtest_replay.py       # 历史实盘选股回放
```

所有输出 JSON 均为 UTF-8（`ensure_ascii=False`），带 `schema_version`、
`generated_at` 与显式 `assumptions` 块。
