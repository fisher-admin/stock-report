# Phase2 深读: microsoft/qlib (Tier-A, G缺口: G6)

- 冻结SHA: `d5379c520f66a39953bad76234a7019a72796fd0` (引证记号 @d5379c520f)
- 许可: MIT — 可引代码片段、可改写移植。
- 议程来源: PHASE1_SYNTHESIS.md §3.2 (L96-101); PHASE1_GAPS.md G6 (L44-46)。
- 读法: 全程 raw.githubusercontent.com 按冻结SHA拉纯文本, 未克隆。
- 防重复建设硬门: 每条产出均对照 local/system_inventory.md, 只提"升级差距"; 涉及新守卫/新表的实施走 scratch worktree + 回滚剧本(红线4)。

---

## §1 PIT 双时间契约 — 一页规格(给我方 parquet 财务/事件缓存)

### 1.1 qlib 实现事实(借模型不借实现)

**存储模型**: 每股票每字段两个文件 `financial/{inst}/{field}.data|.index`。
- data 文件 = 定长记录数组, 每条 `(date, period, value, _next)`:
  - `date` uint32, 公告日(观察时间), 整数 `yyyymmdd` — qlib/config.py#L227-238@d5379c520f (`pit_record_type`)
  - `period` uint32, 报告期(事件时间; 年度=yyyy, 季度=年+季编码)
  - `value` float64
  - `_next` uint32, **同一报告期下一次修订记录的文件偏移**(链表指针), 链尾哨兵 `0xFFFFFFFF` — qlib/config.py#L233-238 (`pit_record_nan.index`)
- index 文件 = `[first_year, 每期首记录偏移...]`(季度每年4槽) — scripts/dump_pit.py#L159-168@d5379c520f 文档字符串; 增量更新时新修订 append 到 data 尾部并回写前一记录的 `_next` (dump_pit.py#L238-247 "Case I: new data => update `_next` with current index")。

**查询语义(冻结为一句话)**: *给定观察日 cur_time, 只可见 `公告日 <= cur_time` 的记录; 同一报告期取该可见集中最新一次修订。*
- 实现: `cur_time_int = yyyymmdd` 后对 `data["date"]` 做 `np.searchsorted(side="right")`, 只取切片 `[:loc]` — qlib/data/data.py#L796-802@d5379c520f;
- 期列表由可见集内 min/max period 生成, `period` 参数(PRef)优先于 start/end_index — data.py#L800-810;
- **算子层硬禁未来期**: `assert end_index <= 0`(data.py#L754) + P 算子对 `Ref($$roewa_q, -1)` 这类未来期引用直接 `raise ValueError` — qlib/data/pit.py#L32-36@d5379c520f。
- P 算子把 `<period, feature>` 逐观察日坍缩为 `<observe_time, feature>`(pit.py#L24-49); PRef 钉死单一报告期(pit.py#L63-72)。

**反面(不借的实现)**: LocalPITProvider 自认非线程安全(data.py#L746)、每次查询 `np.fromfile` 整读 .data(#L793)、加速代码以注释尸体留存(#L767-777, #L813-828)、dump_pit.py#L3-7 头部 TODO 自认"需要重新设计PIT数据库"。范本级数据模型+原型级实现, 只抄前者。

### 1.2 我方规格(升级差距, 非新建)

对照 system_inventory: 我方已有 #35 时点因子快照、#42 PEAD 公告日 PIT、panel_full; **缺的正是清单里写明的 "无 PIT as-of API; 无 revision/双时间"**。规格如下:

**字段表(parquet 财务/事件缓存统一追加)**

| 字段 | 类型 | 语义 | 必填 |
|---|---|---|---|
| `symbol` | str | 标的 | 是 |
| `field` | str | 指标名(如 net_profit_q, forecast_type) | 是 |
| `period` | int32 | 报告期/事件期, yyyymmdd(期末日)或 yyyyQq 编码, 单一口径全库统一 | 是 |
| `value` | float64 | 值(float64, 不允许 float32 落盘——qlib float32 精度反模式的反向执行) | 是 |
| `announce_date` | int32 | 公告日 yyyymmdd(**可见性的唯一依据**) | 是 |
| `revision_seq` | int16 | 同 (symbol,field,period) 内按 announce_date 升序的修订序号, 0=首发 | 是 |
| `source` | str | 数据源标记(tushare/akshare/…) | 是 |
| `ingested_at` | ts | 我方入库时间(审计用, 不参与可见性判定) | 是 |

不采用 qlib 的 `_next` 链表指针——parquet 列式场景下 `(period, announce_date, revision_seq)` 排序即等价修订链, 免维护指针。

**查询 API 形状(单一入口, 剥夺各脚本自行 join 的权力)**

```python
def pit_asof(df: DataFrame, as_of: int, *,
             period: int | None = None,     # None=取 as_of 可见的最新报告期(P语义); 指定=PRef语义
             lag: int = 0                    # lag=1 → 最新期的前一期(PRef(x,1)语义)
             ) -> DataFrame:
    """每 (symbol, field, period) 只返回 announce_date <= as_of 中 revision_seq 最大的一条。"""
```

**验收条件(全部通过才准接入闯关机/回测)**
1. 前视零记录: 任意 `as_of`, 返回集内 `announce_date > as_of` 的记录数 == 0(红线2的机械化)。
2. 毒样本负向测试: 人工注入一条 `announce_date = as_of+1` 的记录, 查询必须不返回它(PHASE1 反模式C教训: 守卫必须配毒样本)。
3. 修订一致性: 同 (symbol,field,period) 内 revision_seq 与 announce_date 排序严格一致; 同 (period, announce_date) 重复记录 hard fail。
4. 未来期禁引: `pit_asof(..., lag=-1)` 或等价"取未来期"参数直接 raise(抄 pit.py#L32-36 语义)。
5. 增量友好: 新数据只 append(带新 revision_seq), 历史行只读——与红线1"不破冻结"及账本纪律一致。

成本: 1-2天(卡 relevance 估计维持)。对新数据即刻生效, 不要求回填历史修订链。

---

## §2 parity 扩窗机制 → 我方 #52 滚动窗事故的 required_lookback 方案稿

### 2.1 qlib 机制事实: 回看需求存在于**两层**, 只有一层是自动的

**层1(表达式层, 全自动)**: 算子树自底向上推导扩窗需求, 装载器扩窗查询再截断:
- `LocalExpressionProvider.expression`: `lft_etd, rght_etd = expression.get_extended_window_size()`; `query_start = max(0, start_index - lft_etd)`; 计算后 `series.loc[start_index:end_index]` 截回 — qlib/data/data.py#L843-879@d5379c520f (扩窗 #L851-854, 截断 #L877-878)。
- 推导规则(qlib/data/ops.py@d5379c520f):
  - `Rolling(f,N)`: 回看 = f.回看 + N - 1 (#L757-763); 扩窗左界 `lft_etd = max(lft_etd+N-1, lft_etd)` (#L764-778); N==0(expanding)→ `np.inf`/警告"无法精确"(#L758-759, #L765-769); EWM(0<N<1)→ `int(log(1e-6)/log(1-N))` (#L760-761)。
  - `Ref(f,N)`: 回看 = f.回看 + N (#L811-814); **N<0(未来引用, 如 label)扩右窗** `rght_etd = max(rght_etd - N, rght_etd)` (#L816-825)——右窗需求同样是元数据, 对应我方"label 需要 T+2 后验行情"的 embargo 概念。
  - 二元/条件算子取子树 max(If: #L673-705; PairRolling: #L1435-1460)。

**层2(dataset/采样层, 手动+自认未自动化)**: 在线增量更新器需要 `hist_ref` 参数声明数据集历史依赖, `TODO: automate this hist_ref in the future`; 只有 TSDatasetH 能自动推出 `step_len - 1`, 其他情况默认 0 — qlib/workflow/online/update.py#L136-149, #L192-198@d5379c520f。取数起点 = `last_end` 前移 hist_ref (#L202-205)。

**qlib 自己的坑(我方必须升级而非照抄)**: Rolling 计算全用 `min_periods=1`(ops.py#L748, #L750, #L752)——当扩窗被 `max(0, ...)` 截到存储起点之前(data.py#L854), **不足窗口不产 NaN 不报错, 静默给出短窗值**。即: parity 保证只在"扩窗数据在盘"时成立, 数据不够时 qlib 静默漂移——这正是我方 #52 事故的形态(管线只拉 N 天, 长窗因子静默算错), qlib 只解决了"谁来算要拉多少", 没解决"拉不够怎么办"。

### 2.2 我方方案稿: 因子注册表 required_lookback 元数据 + 统一装载器 + 硬失败守卫

对照 inventory: factor_gauntlet/闯关机已存在, 升级差距 = 注册元数据 + 装载收权 + 守卫。我方因子是脚本计算而非表达式树, 无法自动推导——所以用**声明式元数据 + 双向验收**替代自动推导:

**注册表字段(factor registry 每因子追加)**

| 字段 | 类型 | 语义 |
|---|---|---|
| `required_lookback` | int | 计算当日值所需最少**交易日**根数(不含当日); expanding/EWM 类因子填保守截断值并注明 |
| `required_forward` | int | 后验依赖天数(label/复盘用, 对应 qlib rght_etd), 信号因子恒 0 |
| `lookback_basis` | str | 推导依据一句话(如 "MA60+diff1 → 60"), 供审计 |

**装载器形状(单一入口, 剥夺各脚本决定拉多少历史的权力)**

```python
def load_panel_for(factors: list[str], start: date, end: date) -> Panel:
    lb = max(registry[f].required_lookback for f in factors)
    ext_start = trading_calendar.shift(start, -lb)          # 交易日历上前移, 非自然日
    panel = read_panel(ext_start, end)
    # 守卫: 窗口不足即 hard fail(qlib 是 max(0,...)+min_periods=1 静默降级——反向执行)
    if panel.first_date > ext_start:
        raise LookbackUnderflow(f"need {ext_start}, panel starts {panel.first_date}")
    return panel  # 因子计算后由调用方截回 [start, end]
```

**验收条件**
1. 恒等回放: 同一因子在 `[T-250,T]` 与 `[T-5,T]` 两种查询窗下, 重叠日值逐元素相等(qlib parity 性质的直接移植, 也是 #52 的回归测试)。
2. 欠窗必炸: 人为把面板起点裁到 `ext_start` 之后, 装载器必须 raise 而非返回(毒样本负向测试)。
3. 元数据审计: 对每个已注册因子跑一次"lookback+1 vs lookback"扰动, 值不变则声明成立; 变了说明 `required_lookback` 低报, CI 拒绝。
4. `required_lookback` 缺失的因子禁止进入闯关机(注册即门)。

成本: 2-3天(卡估计维持; 验收3是本轮新增, +0.5天)。

---

## §3 G6 必补盲区判决: dataset/handler + processor(横截面层)

本轮逐行读了 qlib/data/dataset/{handler,processor}.py、contrib/data/{handler,loader}.py。**G6 判决: 卡的自白属实且答案找齐了——横截面处理完全在 processor 层, 且其 parity 保证是"结构性"的, 值得借形状。**

### 3.1 横截面算子在哪、长什么样

| Processor | 位置@d5379c520f | 语义 | 状态性 |
|---|---|---|---|
| `CSZScoreNorm` | processor.py#L300-323 | `groupby("datetime")` 逐日截面 zscore(可选 robust) | **无状态**(无 fit) |
| `CSRankNorm` | processor.py#L326-359 | 逐日截面 pct-rank → -0.5 → ×3.46(近似单位方差) | 无状态 |
| `CSZFillna` | processor.py#L362-371 | 逐日截面均值填 NaN | 无状态 |
| `ZScoreNorm`/`MinMaxNorm`/`RobustZScoreNorm` | #L228-259/#L196-225/#L262-297 | 时序统计标准化, 统计量在 `[fit_start_time, fit_end_time]` 上 fit | **有状态**; 注释三处强调 "fit_end_time must not include any information from the test data!!!" |
| `DropnaLabel` | #L105-111 | 按 label 删行, `is_for_infer()=False` | 推断期禁用 |
| `ProcessInf` | #L161-176 | inf → 当日截面均值(自带 FIXME "behavior is very weird") | 无状态 |
| `TimeRangeFlt` | #L383-419 | 按存续期过滤股票, 头部 WARNING "It may induce leakage!!!"(用未来存续信息选票=幸存者偏差) | — |

**关键事实1(与卡的 parity 结论拼合)**: 表达式层 `Rank($close, d)` 是**滚动时序 rank**(卡已证), 横截面 rank/zscore 只能由 processor 的 `groupby("datetime")` 完成。逐日截面操作**只消费当日横截面**, 无跨日状态 → **天然 PIT 安全、天然与查询窗口无关**——横截面层不破坏 §2 的 parity 性质, 无需扩窗参与。G6 担心的"借半套"补齐: parity 完整方案 = 表达式层扩窗(时序) + 无状态逐日截面算子(横截面), 两层正交。

**关键事实2(train/serve parity 是结构保证, 不是纪律)**: DataHandlerLP 维护三份数据 `DK_R/DK_I/DK_L`(handler.py#L52-54, #L422), 管道拓扑 `PTYPE_A: raw -[shared]- -[infer_procs]→ _infer -[learn_procs]→ _learn`(#L425-434, #L552-612)——**学习数据是推断数据的下游**, 训练与推断看到的特征变换必然一致; 加上类型门 `_run_proc_l(check_for_infer=True)` 对进入 infer/shared 管道的非推断处理器直接 `raise TypeError`(#L529-540), "训练专用操作泄漏进生产"被类型系统挡死。在线更新时 `init_type=IT_LS`(load state)只重放已 fit 的参数不重新 fit(handler.py#L628-661; update.py#L59)——统计量冻结在训练时点, 推断期零泄漏。

**关键事实3(旗舰配置用"无状态化"绕开 fit 泄漏)**: Alpha158 默认 `infer_processors=[]`(contrib/data/handler.py#L105)——因为特征在表达式层就构造成尺度无关的比值(全部除以 `$close`/`$volume`, contrib/data/loader.py Alpha158DL; Alpha360 同, #L16-23 注释明说); learn 侧只做 `DropnaLabel + CSZScoreNorm(label)`(#L37-40)——label 逐日截面标准化 = 训练目标是"当日相对排位"。有状态 fit 处理器成为可选项而非必需品。
- 隐患(新发现, 反模式增补): `check_transform_proc` 只 assert fit 时间非空(contrib/data/handler.py#L18-21), **没有任何 `fit_end_time <= train_end` 的机器校验**; 且 RollingGen 滚动生成任务时只更新 segments 与 handler end_time, `fit_start_time/fit_end_time` 从模板原样复制不随滚动前移(task/gen.py#L181-186; #L94-123 handler_mod 只改 end_time)——用户手改 fit 窗即可无声引入测试期信息。防泄漏在 qlib 是"注释大写强调", 不是守卫。

### 3.2 对我方的移植判断

我方无 ML 训练管线(AI 不宣称 alpha, 红线6), 不搬 DataHandlerLP 本体。可借的是三条**形状**:
1. **横截面规范化统一为无状态逐日算子库**(cs_zscore/cs_rank/cs_fillna 三个纯函数), 挂在因子闯关机的"日聚合"步骤旁——inventory 已有"日聚合去市场均值 placebo", 升级差距 = 把散在评估脚本里的截面操作收拢为一个模块, 声明"无跨日状态"即免扩窗审计。成本 0.5天, Wave1。
2. **is_for_infer 类型门的等价物**: 我方发布链禁用"复盘专用变换"(如用后验行情填充)——在发布合同 validate 里加一族"transform 白名单"检查(只允许登记为 production-safe 的变换出现在发布路径)。成本 0.5天, 挂在发布合同 v2 上(已有 17 族检查, +1 族)。
3. **尺度无关特征优先**的构造纪律写进闯关机文档: 比值/排位特征免 fit、免参数漂移(Alpha158 的经验), 与我方"慢变量正交源"方向一致。0成本(文档)。

---

## §4 workflow/online 滚动再训练(盲区第二半)

事实(qlib/workflow/task/gen.py + online/update.py@d5379c520f):
- `RollingGen(step, rtype)` 把单个 task 模板机械展开为滚动任务序列: 首窗对齐日历(#L228-300), 之后 train 段按 `ROLL_EX`(定起点扩窗)或 `ROLL_SD`(定长滑动)平移, valid/test 恒定长滑动(#L187-226)。
- **embargo 内建**: `trunc_segments` 按 `trunc_days` 把 train/valid 段在 test_start 前截断, 注释明说"避免未来信息泄漏"(#L126-138); `MultiHorizonGenBase(label_leak_n=2)` 默认按 label 视界留 2 天空档(#L304-306)——多日持有 label 的重叠泄漏在任务生成层机械化处理, 与我方因子评估引擎的"重叠校正"同源。
- 增量预测更新: `DSBasedUpdater` 从 recorder 加载模型+数据集, `_replace_range` 以"删除重叠区间+concat+去重 keep=last"合并新旧预测(update.py#L261-267); label 侧 `dropna` 后重算最近段(#L173-176, 后验数据未齐的行重更)。
- 短板(承接 §2 层2): hist_ref 手动、fit 窗静态(§3.1 隐患)。

**我方判决: 不实施**。inventory 无滚动训练模型, 三策略量化分数冻结; 此机制唯一潜在消费者是 Wave3 AI 消融(若消融要求"按月重放 AI 上下文窗口"再借 RollingGen 的段生成+embargo 形状, 届时 1 天)。本轮产出仅为**备用设计摘录**: 滚动任务 = (train,valid,test) 三段 + step + embargo(trunc_days) 五个数, 生成器只做日历算术——不需要框架。

---

## §5 可交易性契约(表达式元组 limit_threshold + 参与率²冲击 + T+1 核查判决)

### 5.1 qlib 事实

**涨跌停三态契约**(qlib/backtest/exchange.py@d5379c520f):
- `LT_NONE / LT_FLT / LT_TP_EXP` 三种阈值类型(#L258-271)。LT_FLT: `limit_buy = $change >= threshold`(收盘判定, 漏盘中炸板——卡反模式#2 维持); **LT_TP_EXP: `limit_threshold = (买限表达式, 卖限表达式)` 二元组, 逐股逐日算成布尔列**(#L281-286)——分板块 20cm/10cm/5cm/ST/北交所规则可全部下沉到两个表达式, 这是正确的逃生舱(本轮确认其实现: 表达式结果 `astype(bool)` 后与 suspended 做 OR)。
- 停牌契约: `suspended = $close.isna()`(#L275), 恒并入 limit_buy/limit_sell; `check_stock_suspended`: 不在票池=停牌, 窗口内全 NaN=停牌, 任一非 NaN=可能有交易机会(#L378-402)。**契约压给上游数据: 停牌日必须留 NaN 不许 ffill**。
- 执行前硬拒: `check_order = is_stock_tradable(涨跌停+停牌)`(#L417-419), deal_order 对不可交易单置 `deal_amount=0`(#L436-440)——可交易性是装载期预计算列 + 执行期硬闸, 不是策略层自觉。
- extra_quote 缺列时默认 `limit_sell=False/limit_buy=False` 仅 warning(#L249-255)——fail-open 反面细节, 我方版本缺列必须 hard fail。

**成交量/冲击成本**:
- 量限: `volume_threshold` 支持 dict(`all/buy/sell` 各自表达式列表, min 聚合)(#L78-102, #L295+); SimulatorExecutor 维护**日内累计已成交量** `dealt_order_amount`, 跨日重置(executor.py#L594-598)——同日多次触碰同一票的参与率合并计算。
- 冲击: `adj_cost_ratio = impact_cost × (本单成交额/当bar总成交额)²`(#L890-892), 建议值 0.1(#L114); **边界条件(本轮新确认)**: 当日总成交额为 0 或 NaN 时回退 `adj_cost_ratio = impact_cost` 常数并留 TODO(#L888-890)——除零路径是"按最大冲击计", 方向保守, 可照抄。
- 费用默认 open 0.0015/close 0.0025/min 5元(#L48-51), 卖侧含印花税近似但为 2023 降税前旧值——搬参数前须按我方 B1 标定值覆盖。

**T+1 核查判决(卡自认小概率遗漏, 本轮闭合)**: 逐段读 executor.py 全文结构 + position.py settle 全链:
- 结算钩子只有现金一种: `settle_type=ST_CASH` 使卖出款延迟到本 step 末 `settle_commit` 才可用(executor.py#L270-271, #L297-298; position.py#L487-493, `get_cash(include_settle)` #L442-445); 嵌套结算被 assert 禁止(position.py#L488 "settlement can't be nested!!!!")。
- Position 记录不含买入日期字段; SimulatorExecutor 订单迭代只有 TT_SERIAL(决策顺序)/TT_PARAL(按方向排序)两种(#L561-588), 无持仓龄检查。
- **判决: CONFIRMED——qlib 回测核心(exchange/executor/position)不存在任何股票层 T+1 约束, 内层 executor 亦无特殊处理**。分钟级/嵌套回测中当日买当日卖畅通。A股 T+1 必须我方自建(与 PHASE1 反模式D结论一致, 现在是全核证据而非抽样)。

### 5.2 我方 tradability 掩码模块设计稿(升级差距: 收拢 #38 后仍散在各脚本的判定)

**产物**: 全市场逐日 tradability parquet(夜间管线 stage 产出, 发布合同校验其新鲜度)。

| 字段 | 类型 | 生成规则 |
|---|---|---|
| `date, symbol` | key | |
| `board` | enum | 主板/创业/科创/北交(由代码段判定) |
| `pct_limit` | float | 分层阈值: 0.10/0.20/0.20/0.30; ST 减半(主板 0.05); 新股首日等特例置 NaN+`special_flag` |
| `limit_buy_frozen` | bool | 收盘价触及涨停(qlib 口径)——保守判"买不进" |
| `limit_sell_frozen` | bool | 收盘价触及跌停 |
| `one_word_board` | bool | 一字板: `open==high==low==close` 且触限(prebreakout 的"一字板买不进"精判, 修 qlib 收盘价判定漏盘中炸板的一半盲区; 另一半留分钟数据升级位) |
| `suspended` | bool | 停牌; **上游契约: 停牌日行情列必须 NaN, 禁 ffill**(继承 qlib NaN 约定, 加载校验) |
| `participation_cap` | float | 当日成交额 × 参与率上限(B1 标定), 供仓位可成交量裁剪 |

**接口**: `is_tradable(symbol, date, direction) -> (bool, reason)` — reason 枚举(limit_up/limit_dn/suspended/one_word/not_listed), 借 freqtrade 轨道的 reason-code 思想统一。三策略回测/复盘/前瞻账本一律经此函数, 禁止再各自 join 行情判定。
**冲击成本**: 以 `impact × (order_val/day_val)²` 形式接入 O2C/prebreakout 复盘的成本敏感性(B1 已有固定滑点, 二次项作为对照口径并列输出, **不改冻结账本历史行**, 红线1)。
**T+1 显式契约**: 复盘/回测引擎加 `sell_earliest = buy_date + 1交易日` 断言(我方均为日频, 成本≈0, 主要是把隐式假设写成守卫+单测)。
**验收**: ①与 #38 修复后的三策略现行判定回放一致(差异逐条解释); ②毒样本: 涨停日买单必须被拒且 reason=limit_up; ③缺列/缺日 hard fail(反 qlib extra_quote fail-open)。
成本: 2-3天(卡估计维持)。

---

## §6 血统/调度三小件 — 即抄清单(逐文件判决)

| 文件@d5379c520f | 抄什么 | 边界条件(本轮确认) | 不抄什么 | 成本 |
|---|---|---|---|---|
| qlib/workflow/recorder.py#L354-378 | run 启动时三连拍: `git diff`/`git status`/`git diff --cached` 各存一个文本 + `cmd-sys.argv` + `_QLIB_*` 前缀环境变量白名单(#L356-359) | 子进程失败只 log 不炸(#L376-378)——血统采集 fail-soft 合理, 照抄; 我方 subprocess 一律带 `errors=replace`(0708 事故通治) | MLflow/AsyncCaller 全套 | 0.5天: gateway runner 起跑 dump 三文本进当日 run_manifest 目录, 校验器加"diff快照存在"软检查 |
| qlib/workflow/task/manage.py#L265-311 | "领取即原子改状态、异常即回滚"语义: `find_one_and_update`(waiting→running, priority 降序)原子领取(#L278-281); `safe_fetch_task` contextmanager 在 `(Exception, KeyboardInterrupt)` 时把任务回滚原状态再 raise(#L288-311)——**KeyboardInterrupt 不是 Exception 子类, 必须显式双捕获**, 这个细节直接抄 | 空池返回 None 由调用方处理(#L283-284); 任务定义 hash 去重(卡已录) | MongoDB——SQLite 单表 `(task, status, priority, payload)` + `BEGIN IMMEDIATE` 即等价 | 1天: 夜间管线 9 stage 套状态机, 补跑=重新领取, 与 run_manifest 血统门衔接 |
| scripts/check_data_health.py#L109-190 | 四检查即抄: 缺失值(#L109-134)、大跳变 `pct_change(fill_method=None).abs()` 价格>0.5/量>3(#L136-162, **双向**, abs 已确认)、OHLCV 必需列(#L165+)、复权因子缺失(#L185+); 阈值全部构造参数化(#L21-38) | fill_method=None 防 pct_change 隐式 ffill 穿越停牌——细节照抄 | 其 CLI 骨架 | 0.5天: 对 backtest_cache parquet + panel_full 增量段跑, 接 pipeline_alert 硬告警 |

---

## §7 反模式增补(本轮新证, 供 Phase2 汇总)

1. **扩窗不足静默降级**: `min_periods=1` + `max(0, start-lft_etd)` 组合使数据不够时 parity 静默失效(ops.py#L748-752; data.py#L854)——"自动扩窗"宣传与"欠窗不报错"现实的落差, 我方 §2 守卫的反面依据。
2. **fit 窗零守卫**: fit_end_time 泄漏防护 = 大写注释 ×3(processor.py#L198-199, #L232-233, #L274-275) + assert 非空(contrib/data/handler.py#L18-21), 无 `fit_end <= train_end` 机器校验; RollingGen 滚动时 fit 窗静态不前移(gen.py#L181-186)。
3. **表达式层二次 float32**: LocalExpressionProvider 计算结果统一 `astype(np.float32)` 且 FIXME 自认精度应可配置(data.py#L868-876)——float32 反模式不止在存储层(卡已录 file_storage), 计算出口再压一次。
4. **extra_quote 缺列 fail-open**: 缺 limit 列默认"全部可交易"仅 warning(exchange.py#L249-255)。

---

## ① G条目闭合声明

| G | 状态 | 依据 |
|---|---|---|
| **G6** (qlib parity 范本的横截面半边未验证) | **闭合** | dataset/handler.py + processor.py + contrib/data/{handler,loader}.py 逐段精读: 横截面算子定位(CSZScoreNorm#L300-323 等)、无状态⇒PIT安全/窗口无关的论证、DataHandlerLP 三键+PTYPE_A+is_for_infer 类型门的 train/serve parity 结构保证、Alpha158"尺度无关特征+空 infer 管道"的无状态化路线——parity 完整方案(时序扩窗+截面无状态)两半拼齐, 可引用。附带闭合卡的两处自白: ①workflow/online/update.py+task/gen.py 已读实现(§4); ②executor 内层 T+1 遗漏疑点已全核, 判决"无股票层 T+1"升级为 CONFIRMED(§5.1)。 |

(本轨道仅分配 G6; G10 勘误确认已由 manifest_amendments 回写, 无需本轨action。)

## ② coverage_confession

- **未读**: qlib/contrib 模型 zoo/内置策略/report 归因(维持卡的未读); qlib/rl 全部; data/dataset/storage.py(HashingStockStorage)与 high_performance_ds.py; dataset/__init__.py 只读了类结构与 prepare 签名, TSDataSampler 内部未逐行(hist_ref=step_len-1 结论取自 update.py 引用处而非采样器本体); online/manager.py 与 strategy.py 只读类/方法签名级(first_train/routine 流程未逐行); data_collector 各爬虫; utils.read_period_data 函数体未读(修订链逐期读取的字节级实现, 语义从调用方+dump_pit 文档交叉确认); tests/examples/docs 全部。
- **不确定**: ①`period` 季度整数编码具体格式(yyyy+季的乘数)未从 get_period_list 源码核实, §1 字段表因此规定我方自定口径而非照抄; ②TT_PARAL 的方向排序常量(Order.BUY/SELL 数值)未核, 不影响任何结论; ③check_data_health 对分钟频数据的适用性未验(我方先用于日频)。
- **证据强度**: §1/§2/§3/§5 关键结论全部为逐行读+行号引证; §4 为关键函数逐行+流程函数签名级; §6 为逐行确认边界条件。

## ③ Wave 映射(每条产出→归属+粗成本)

| 产出 | Wave | 成本 |
|---|---|---|
| §1 PIT 双时间字段表+pit_asof API+5验收条件 | **Wave1 数据可信** | 1-2天 |
| §6 check_data_health 四检查接 pipeline_alert | Wave1 | 0.5天 |
| §6 run_manifest 补 git diff/argv 血统三连拍 | Wave1 | 0.5天 |
| §6 SQLite 任务状态机(领取-回滚语义)套夜间管线 | Wave1(运维硬化) | 1天 |
| §7-3 float32 教训→我方 parquet dtype 审计(value 列强制 float64) | Wave1 | 0.25天 |
| §2 required_lookback 注册元数据+统一装载器+欠窗硬失败(含恒等回放回归测试) | **Wave2 组合风险契约**(因子供给正确性是组合层前提; 直接了结 #52 事故债) | 2-3天 |
| §5.2 tradability 掩码 parquet+is_tradable 单入口+reason 枚举+T+1 显式守卫 | Wave2 | 2-3天 |
| §5.2 参与率²冲击成本对照口径接 B1 | Wave2 | 0.5天 |
| §3.2-1 无状态截面算子库(cs_zscore/cs_rank/cs_fillna)收拢 | Wave2(闯关机横截面层) | 0.5天 |
| §3.2-2 发布链 transform 白名单(is_for_infer 等价门) | Wave2(发布合同+1族) | 0.5天 |
| §4 RollingGen 段生成+embargo 形状(备用设计摘录, 不实施) | **Wave3 AI 消融**(仅当消融需滚动重放) | 0天(已录)/届时1天 |
| 上游迁移项目 | 无映射(qlib 非我方上游) | — |
