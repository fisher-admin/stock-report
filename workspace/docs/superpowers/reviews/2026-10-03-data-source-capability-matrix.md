# 选股数据源能力矩阵与 9 月 30 日修复核验（2026-10-03）

依据：生产代码 `skills/stock-analyzer/data_provider.py`、`backtest.py`、`fetch_akshare_cyq_fallback.py`、`fetch_market_snapshot_fallback.py`、`skills/stock-system-orchestrator/scripts/stage1_data_prepare.py`，以及 `health/data_ready_*.json`。本文件只读核验，未联网请求任何数据源。

## 1. 9 月 30 日修复是否仍在生产代码中

| 修复项 | 结论 | 位置 |
| --- | --- | --- |
| 行情后到重新检查 | 在 | `stage1_data_prepare.py` 约 332 行“A later fallback may have fetched…”逐类型重读 |
| 代理筹码可在预算内重试真实数据 | 在 | `backtest.py` 754 行：缓存为 proxy 时不复用、重新请求 |
| 免费源解释器回退 | 在 | `backtest.py` 104/127 行 `_resolve_external_data_python` / `_resolve_akshare_cyq_python` |
| 精确匹配 `daily_YYYYMMDD` / `stk_factor_YYYYMMDD` | 在 | `qfq_price_fallback.py` 89/93 行 `re.fullmatch` |
| 数值字段空白转缺失、不转 0 | 在 | `pit_market_snapshot.py` 302 行 `mask(blank)` + `to_numeric(errors="coerce")` |
| 接口上限（daily_basic 5000 行） | 在 | `data_provider.py` 354–358 行 |
| RDS 1 秒请求节奏、429 有限重试 | 在 | `data_provider.py` 170/201/203 行 |
| 本轮 PIT 输入复用 | 在 | `short_track_shadow_runner.py` 328 行 |

结论：上述修复不需要重复实现。

## 2. 能力矩阵（选股必需数据）

| 数据集 | 提供方 / 回退 | 关键字段 | 日期与覆盖校验 | 复权口径 | 来源标记 | 是否可作为合格替代 |
| --- | --- | --- | --- | --- | --- | --- |
| daily | RDS（主） | OHLC、pre_close、pct_chg、vol、amount | 请求日期必须完全一致；拒绝未请求证券；局部返回逐只补核 | 未复权（日线本义） | `rds` | 合格 |
| daily | Promax（按接口备用） | 同上 | 同上 | 未复权 | `promax` | 合格（字段齐全时） |
| daily | efinance / 新浪 / 东财实时快照 | OHLC、量额（成交量量纲启发式校正） | 必须为同一交易日，否则报错 | 未复权 | `efinance_realtime_snapshot` 等，标记 degraded | **有条件**：口径一致，但覆盖与成交量量纲为启发式；目前 degraded **不阻断**就绪 |
| stk_factor | RDS（主） | close_qfq、MACD、KDJ、RSI、BOLL、CCI | 必须同日；必须含 `close_qfq` | 前复权 | `rds` | 合格 |
| stk_factor | Promax | 同上 | 同上 | 前复权 | `promax` | **不合格**：9 月记录显示响应缺 `close_qfq`，现有字段校验会拒绝 |
| stk_factor | 按 ts_code 分批 | 同主源 | 同上 | 同主源 | `stk_factor_code_batch` | 合格（同一提供方） |
| stk_factor | 本地由日线推导 | 指标由**未复权**收盘价计算，并写入 `close_qfq` | 同日 | **口径不一致**（未复权值写成前复权字段） | `local_history_derived_technical`，标记 degraded | **不合格**，但现状只标 degraded、不阻断就绪 |
| cyq_perf | RDS / Promax | cost_15/50/85pct、weight_avg、winner_rate | 同日；分位顺序校验 | — | `rds` / `promax` | 合格 |
| cyq_perf | 按 ts_code 分批 | 同上 | 同上 | — | `cyq_perf_code_batch` | 合格 |
| cyq_perf | AkShare 东财筹码 | 只有 90% 区间上下沿、平均成本、获利比例 | 同日 | — | `fallback_source=akshare_stock_cyq_em` | **不合格**：把 5%/95% 分位值写进 `cost_15pct`/`cost_85pct`；不含 “proxy” 字样，现有 “cyq proxy forbidden” 拦不住 |
| cyq_perf | 本地回归代理 | 预测值 | — | — | 含 `proxy` | 不合格（现有守门正确拒绝） |
| trade_cal | RDS / Promax | cal_date、is_open | 未来至少 45 天 | — | `rds_trade_cal` | 合格；缺失/无效即错误 |

## 3. 新发现（需要决定）

1. **不兼容替代可能静默进入选股**：本地推导 stk_factor（未复权值写入 `close_qfq`）和 AkShare 筹码（5/95 分位写入 15/85 字段）在主备源都失败时会被缓存，Stage1 只把 `degraded` 记为真，`ok` 仍可为真，validate_run 也只记录不阻断。9 月 30 日修复让 AkShare 解释器重新可用，反而使这条路径重新可达。
2. 已准备补丁（在变更包中，未部署）：`stage1_data_prepare.py` 新增 `INCOMPATIBLE_SUBSTITUTES`，这两类来源一律不满足当日就绪，并给出明确原因；日线实时快照不受影响。5 项回归测试通过。
3. 后续需要：这两类替代文件一旦落盘，同一轮重试会继续复用缓存（只有 proxy 筹码会被重新请求）。若要在预算内恢复真实数据，`backtest.get_cached` 需对这两类来源同样重新请求——此改动涉及供应商实时行为，需在 Mac 上真实验证后再做。

## 4. 取数时机

9 月自然 19:30 运行中，凡有数据的日子均为第 1 次尝试即取齐三类数据；失败来自供应商 503/504 或凭据过期，而非“取得太早”。因此保留 19:30 启动；修正的是重试预算与阶段超时不一致（7200 秒 vs 2400 秒），见变更包中的 `run_pipeline.py`。

## 5. 就绪清单

现有 `data_ready_<日期>.json` 已原子写入且含批次 run_id、目标交易日、各类型来源与行数。本轮未新建第二份清单，以免出现两套就绪口径；研究输入预备包（前移下载 + 版本化交接）仍为待办，需在 Mac 上用真实冷启动测时后实施。
