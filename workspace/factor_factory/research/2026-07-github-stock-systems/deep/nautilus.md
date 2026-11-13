# Tier-A 深读报告: nautechsystems/nautilus_trader

- **轨道**: NautilusTrader 深读 (G缺口: G7)
- **冻结SHA**: `72f53d4a82f31901a7c0263573f6e3efc5d36b25` (develop, pushed 2026-07-10)
- **引证格式**: `path#Lx-Ly@72f53d4a82` (全部行号在冻结SHA快照下用 curl raw + grep -n 核实)
- **许可纪律**: LGPL-3.0 — 本报告零代码复制; 全部为机制描述/接口形状/自有记号伪代码。落地实现必须自研, 禁引 nautilus 依赖。
- **议程来源**: PHASE1_SYNTHESIS.md §3.4; PHASE1_GAPS.md G7; 卡 coverage_confession。
- **防重复建设硬门**: 已对照 local/system_inventory.md — 本报告所有产出均为"升级差距", 不新建已有能力。

---

## 0. 结论速览 (三句话)

1. **对账子系统值得全额投入**: 4条规则+确定性补跑ID的完整规格已从代码级证据(`reconciliation/ids.rs` 全文精读)提炼成我方 tushare/akshare 补跑脚本设计稿, 3-5天可落地, 直接治"补跑不幂等/查询失败误判为空"两类已发生事故。
2. **订单状态机+typed deny+三态闸是我方发布链路的成熟形态**: 穷举转移表(70+合法转移对)、38枚 deny_reason 枚举(docs 由枚举自动生成=文档即合同)、TradingState 三态闸(ACTIVE/REDUCING/HALTED)——映射为"候选→发布→AI→复盘"状态表+deny_reason 落库+管线全局开关, 1-2周分两期。
3. **G7 两项盲区已补读并闭合**: accounting 实为 `crates/model/src/accounts/`(cash 账户=买锁名义额/卖锁份额, total==locked+free 恒等式+钳位构造器, 无 T+1/无结算滞后——A股直接套用不可行的账户级实证); `are_intervals_disjoint` 精确重定位到 catalog.rs#L4806; matching_engine 内部(process_bar 的 OHLC 四笔分解/缺口开盘按市价成交/queue_position 默认关)已读, 撮合语义结论从"文档级"升为"代码级"。

---

## 1. 对账子系统 (全仓最高价值) → 我方补跑脚本 4 条规则 + 确定性补跑ID规格

### 1.1 上游机制 (代码级证据)

**三报告契约与流程** — `docs/concepts/live.md#L144-L204@72f53d4a82`:
每个 venue adapter 实现同一三方法契约(order status / fills / positions 三类报告)→ 批内去重(重复报警不炸)→ 订单对账(缺失事件补齐、缺 trade 报告推断 OrderFilled、未识别 ID 生成外部订单)→ 成交对账(容差比价)→ 持仓对账(按合约精度净额比对, 差异生成 EXTERNAL+RECONCILIATION 标记的补齐订单, 价格四级回退: 计算对账价→盘口中价→现均价→市价单)。**对账失败则系统拒绝启动**(live.md#L204)。

**四条不变式** — `docs/concepts/live.md#L336-L350@72f53d4a82`:
1. 持仓量: 合约精度内一致;
2. 均价: 0.01% 容差内一致 (代码实证: `crates/execution/src/reconciliation/positions.rs#L39@72f53d4a82`, DEFAULT_TOLERANCE=0.0001);
3. PnL 完整性: 合成 fill 用计算价保未实现盈亏正确;
4. **ID 确定性: 合成 trade_id/venue_order_id 是逻辑事件的确定性函数——重启重放产生相同 ID, 自动去重而非新增**。

**运行期防误杀护栏** — `docs/concepts/live.md#L277-L314@72f53d4a82`:
- 近期事件保护窗: 最后事件落在阈值窗内的订单跳过对账(防 venue 还在处理中的竞态误报);
- 终态前靶向单查询: 批量查询"查不到"≠终态, 先发一次单订单查询兜底(捕获批量查询限制/时延假阴性);
- **持仓查询失败≠空仓: 该 venue 本轮跳过, 而非视为 flat**;
- FILLED 订单 venue 查不到→静默忽略(venue 常删已完成订单);
- 重试预算共享+单查询限速+每 (instrument,account) 独立重试计数, 反复不一致则停该对暂停主动对账。

**确定性 ID 生成机制** — `crates/execution/src/reconciliation/ids.rs#L44-L142@72f53d4a82` (全文精读):
- 种子 = 固定命名空间前缀(如"reconciliation-fill") + 竖线分隔的逻辑字段序列: account_id | instrument_id | client_order_id | venue_order_id | side | order_type | filled_qty | last_qty | last_px | position_id | **ts_last(来源方时间戳, 非本地时钟!)**;
- 种子 → 稳定哈希(FNV-1a 64位, 双哈希拼 128位) → 形如 UUID 的字符串(手工设 version/variant 位);
- account_id 入种子防跨账户碰撞; ts_last 用 venue 上报时间使"同形状的两次事故"得到不同 ID, 而"同一逻辑事件重启重放"得到相同 ID;
- 另一族合成 ID 格式 `S-{hex时间戳}-{哈希后缀}`——"S-"前缀让人眼/grep 一眼识别合成记录 (ids.rs#L42-L61)。
- 回测引擎同样纪律: trade_id 恒确定性(venue|raw_id|ts_init 哈希+序号), use_random_ids 只影响 venue_order_id/position_id (`crates/execution/src/matching_engine/ids_generator.rs#L146-L157@72f53d4a82`)。

**双层去重** — `docs/concepts/execution.md#L554-L592@72f53d4a82`:
- 核心层: 4 字段全等(trade_id+side+px+qty)=良性重放, 优雅跳过+warn; trade_id 同但字段异("脏重放")→apply 抛错→引擎捕获、记日志、丢弃该 fill, 不崩;
- 对账层: 生成 fill 事件**之前**仅按 trade_id 预过滤(数据不同也跳过+报警"venue 数据质量")。
- 订单模型内 trade_ids 列表硬校验重复即错误 (`crates/model/src/orders/mod.rs#L837-L841@72f53d4a82`)。

**容差细节** — `crates/execution/src/reconciliation/orders.rs#L890-L945@72f53d4a82`: venue 累计成交量 < 本地缓存→**不生成任何事件, 保留本地态**(亚单位差按精度噪声吞掉, 大差只 warn); venue > 本地且订单未关→按差额生成推断 fill; 订单已关→只 debug 不补。`positions.rs#L410-L446`: 数量比对允许"合约 size 精度下正负一个最小单位"容差。`positions.rs#L464`起: 合成价按合约 max_price 封顶(尘埃量除法爆价防御, 文档级注释解释了为何只封上界)。

### 1.2 我方设计稿: 补跑合同 v1 (tushare/akshare 补跑/回填脚本统一规则)

现状差距(对照 inventory §1/§3): 我方有 reconcile 竞态兜底与收盘感知告警, 但补跑脚本(fix_missing.py、AI 回填、S3 补跑、panel 回补)**无幂等 ID、无统一容差、超时曾被误判为无数据**(0710 GREENFIELD 死源事故同族)。

**四条规则(翻译版, 写入每个补跑脚本)**:

| # | 规则 | 上游依据 | 我方验收条件 |
|---|---|---|---|
| R1 | 不变式显式化: 补跑后 行数=交易日历应有数; 价格字段与既有重叠段逐行比对, 相对差≤1e-4 通过, 超差=硬告警+拒绝落库 | live.md#L336-L350 | 负向测试: 人工造 0.05% 均价差样本必须被拒 |
| R2 | 确定性补跑ID: `backfill_id = stable_hash("backfill-{表名}" \| trade_date \| ts_code \| source \| 字段集签名 \| source_asof_ts)`; 同一逻辑补跑重跑→同 ID→UPSERT 幂等; source_asof_ts 用数据源侧时间(如 tushare 返回的 trade_date), 禁用本地 now() | ids.rs#L71-L101 | 同参数连跑两遍, 第二遍新增行数=0 |
| R3 | 查询失败≠数据为空: 接口超时/异常→标记 `source_query_failed`+跳过本轮该源, 绝不写空值/不判"该日无数据"; 连续 N 轮失败才升级硬告警 | live.md#L290-L291 | 断网模拟下, 库内行数与补跑前一致且告警触发 |
| R4 | 终态判定前靶向复查: 批量拉取缺某 (ts_code,date) 时, 先对该单键做一次独立请求兜底, 仍缺才标 `confirmed_missing`; 补跑标记带 "S-"式前缀让合成/回填行可 grep | live.md#L287-L289; ids.rs#L42-L49 | 抽 10 个 confirmed_missing, 人工核实至少 9 个真缺 |

**接口形状(自有记号伪代码)**:
```
backfill_run(table, date_range, source) ->
  for key in expected_keys(calendar, universe):
    if exists(key) and within_tolerance(existing, fetched): skip
    elif fetch_failed(key): mark source_query_failed; continue   # R3
    elif missing_in_bulk(key): single_key_probe(key)             # R4
    row.backfill_id = stable_hash(ns|key|source|fields_sig|src_ts) # R2
    upsert_by(backfill_id)
  assert invariants(R1) else abort + alert                        # fail-closed
```

成本: 3-5 天(在现有 fix_missing.py / 回填脚本上加合同层, 不新建基建)。

---

## 2. 订单状态机 + typed deny + TradingState 三态 → 我方"候选→发布→AI→复盘"状态表

### 2.1 上游机制

**穷举转移表** — `crates/model/src/orders/mod.rs#L209-L279@72f53d4a82` (行号与卡一致, 已复核):
单函数 match 覆盖全部合法 (状态,事件) 对约 70 条, 兜底分支返回 InvalidStateTransition 错误——**非法转移是错误值, 不是日志**。三类值得抄的显式分支:
- 真实世界分支: Canceled+Filled→Filled (L244 注释"Real world possibility"——撤单指令与成交竞态);
- 外部订单入口: Initialized 可直接接 Rejected/Accepted/Canceled/Expired/Triggered (对账生成的外部订单不走正常提交路径);
- Pending 态语义: PendingUpdate/PendingCancel 允许重复请求(幂等), ModifyRejected/CancelRejected 由专门 handler 恢复 previous_status (mod.rs#L820-L834: previous_status 保存规则显式排除三类事件)。
- Filled/PartiallyFilled 判定不在转移表内, 在 fill handler 里按累计量判 (mod.rs#L966-L971), 并带 debug 断言三连: 状态必为 P.Filled|Filled、ID 三件套必置、filled+leaves>=quantity (mod.rs#L997-L1017)。

**typed deny + 文档即合同** — `crates/model/src/events/order/denied_reason.rs#L45-L295@72f53d4a82`: 38 个带结构化字段的枚举变体(数量/名义额/余额/保证金/GTD/追踪止损/限速/三态闸/流对账中等), Display 输出统一 `CATEGORY_CONDITION: key=value` 格式。`docs/concepts/execution.md#L79-L131@72f53d4a82`: 文档中的 deny 码表**由枚举经 cargo test 自动生成**(HTML 注释明示再生成命令)——文档漂移被机器消灭。
风控引擎每种拒绝发 typed OrderDenied 事件留痕: 余额类检查在 `crates/risk/src/engine/mod.rs#L990起` (NotionalExceedsFreeBalance L1600, CumNotionalExceedsFreeBalance L1636/L1668/L1709/L1782——单笔与**累计在途名义额**双检); 限速超限直接 Denied 不排队 (L214)。

**TradingState 三态闸** — `crates/risk/src/engine/mod.rs#L1956-L2043@72f53d4a82` (execution_gateway 全文精读):
- HALTED: 新提交一律 deny(TradingHalted), 撤单仍放行;
- REDUCING: 逐单判方向——增加净敞口方向的提交 deny(TradingStateReducing{side,instrument}), 减仓方向放行进限速器; 改单同理, 加量且同净方向拒 (L877-L900);
- ACTIVE: 正常进限速器。
文档合同: `docs/concepts/execution.md#L228-L241`。

### 2.2 我方设计稿: 推荐状态机 v1 + deny_reason 落库 + 管线三态开关

现状差距(对照 inventory §3): 发布合同 v2 有 17 族检查但**拒绝原因是日志文本非枚举**; candidate_state 有字段但转移合法性无穷举表(隐式状态机); 无全局三态开关(只有交易日收盘前禁裸跑护栏)。

**状态表(设计稿, 逐格判决)**:

状态集: `CANDIDATE / PUBLISHED / AI_PENDING / AI_COMPLETED / AI_FAILED / REVIEWED / DENIED / RETIRED`
事件集: `publish_ok / publish_deny(reason) / ai_start / ai_ok / ai_fail(reason) / ai_backfill_ok / review_settle / retire`

| 当前态 \ 事件 | publish_ok | publish_deny | ai_start | ai_ok | ai_fail | ai_backfill_ok | review_settle | retire |
|---|---|---|---|---|---|---|---|---|
| CANDIDATE | PUBLISHED | DENIED | ✗ | ✗ | ✗ | ✗ | ✗ | RETIRED |
| PUBLISHED | ✗(幂等重放=warn) | ✗ | AI_PENDING | ✗ | ✗ | ✗ | ✗ | RETIRED |
| AI_PENDING | ✗ | ✗ | AI_PENDING(幂等) | AI_COMPLETED | AI_FAILED | ✗ | ✗ | ✗ |
| AI_FAILED | ✗ | ✗ | AI_PENDING(重试) | ✗ | AI_FAILED | AI_COMPLETED | REVIEWED(带失败标记) | RETIRED |
| AI_COMPLETED | ✗ | ✗ | ✗ | ✗(重复=按ID去重跳过) | ✗ | ✗ | REVIEWED | RETIRED |
| REVIEWED | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗(结算只一次) | RETIRED |
| DENIED/RETIRED | 终态: 任何事件=违规告警 | | | | | | | |

✗ = 非法转移 → 抛错+落 `state_violation` 表+Discord 告警, **绝不静默继续**(治 0709 prebreakout base_only 慢性降级一类"隐式状态漂移")。
仿上游三细节: ①REVIEWED 后禁再结算=我方"结算-污染重置铁律"的状态机化; ②AI_FAILED→REVIEWED 合法(带失败标记复盘, 不假装有 AI); ③ai_ok 携带 generation_id, 重复应用按 ID 去重(仿 trade_id 去重)。

**deny_reason 枚举 v1** (格式仿 `CATEGORY_CONDITION: key=value`):
`STALE_DATA: data_date=X,calendar_date=Y` / `COVERAGE_BELOW_MIN: pct=,min=` / `CLOSE_MISMATCH: field=,pub=,src=` / `AI_COVERAGE_BELOW_MIN` / `FINAL_ACTION_FORBIDDEN` / `LINEAGE_BROKEN: manifest=` / `CONTRACT_SCHEMA_VIOLATION: family=` / `PIPELINE_HALTED` / `PIPELINE_REDUCING` / `RATE_OR_BUDGET_EXCEEDED` …(17 族检查逐一映射, 每族至少一码)。
落库表 `publication_denials(run_id, date, deny_code, context_kv, ts)`——**拒绝历史成为可查询资产**(现在只在日志里)。文档表由枚举定义自动生成进 research-lab 页(仿 docs-as-contract)。

**管线三态开关** `pipeline_trading_state ∈ {ACTIVE, REDUCING, HALTED}` (单行状态文件+修改留痕):
- HALTED: stage6 发布/S3 上墙/前端推送全拒(deny=PIPELINE_HALTED), 复盘与数据采集照跑;
- REDUCING: 只允许"不新增推荐"的动作——复盘/回填/结算放行, 新候选发布拒(deny=PIPELINE_REDUCING)——**正是"只复盘不新增"运维模式的合同化**(如数据源存疑期);
- ACTIVE: 正常。红线7: 开关变更属发布行为变更, 需用户拍板后才接自动触发器, 先落手动档。

成本: 状态表+违规告警 3-4 天; deny 枚举+落库 3-4 天; 三态开关 2 天。合计 1-2 周, 全在现有 Python 发布链路上加层, 零新基建, LGPL 零复制(表是我方语义, 仅借"穷举+typed+三态"三个形)。

---

## 3. 存储纪律: 区间不相交护栏 + ts_init 可见时刻合同

### 3.1 上游机制 (行号已精确重定位, 治卡的近似行号自白)

**catalog.rs 精确锚点** (`crates/persistence/src/backend/catalog.rs@72f53d4a82`, 5002 行):
- `are_intervals_disjoint` **定义于 #L4806** (文档注释自 L4780; 卡写 ~L4798 为 doctest 行, 现已精确); 排序后相邻比较, 闭区间 end1>=start2 即重叠;
- `are_intervals_contiguous` #L4854: 相邻文件必须 end1+1==start2 (无缝无叠);
- `query_interval_diff` #L4900: 给定查询区间与既有文件区间, 返回**未覆盖缺口列表**——增量补拉的标准算法;
- 写入路径 `write_to_parquet` #L529: ①先校验数据按 ts_init 升序 (check_ascending_timestamps #L1080); ②文件名由首尾 ts 生成 (timestamps_to_filename #L4313), **文件名即区间元数据**; ③同名文件已存在→跳过写入(天然幂等); ④非跳过模式下取目录现有区间+新区间做 disjoint 校验, **重叠即 bail 拒写**(fail-closed), 逃生舱是显式参数 skip_disjoint_check (#L573-L585);
- 调用点 L579/L668/L788/L1221/L3966 (写入/合并/重整全路径均校验)。

**ts_init 合同** — `docs/concepts/backtesting/bar-execution.md@72f53d4a82`:
- warning 块 #L23-L35: bar 执行严格要求 ts_init=**收盘时刻**; ts_event 可为开或收, 开盘戳数据须加 bar 时长位移;
- #L59-L60: "用小样本核实你数据源的时间戳口径"——口径审计是用户责任但平台给了明确合同;
- note 块 #L190-L194: **官方拒绝提供"next-bar-open"执行模式**, 理由: bar 的 ts_init 是收盘时刻, 开盘价在信号时刻不可知, 按上根信号成交本根开盘=前视——这是"平台用类型系统拒绝前视"的范例(对照我方前瞻账本 4 规则同源)。

### 3.2 我方规格: parquet 写入护栏 v1 (升级差距, 治 #52 滚动窗覆盖事故族)

现状差距(inventory §1): backtest_cache/*.parquet 按日落盘但**无 Raw 不可变层、可被盘中裸跑污染、无区间校验**; panel_full 靠冻结保护+人工纪律。

规格(在我方 parquet 写入器统一入口加, 约 1-2 天):
1. **文件名承载区间**: 已按日分文件的表天然满足(文件名=交易日); 多日文件(panel 分块)改为 `{start}-{end}.parquet` 或 sidecar manifest 记区间;
2. **写前 disjoint 校验**: 新文件区间 vs 目录现有区间, 重叠→拒写+告警; 覆盖式更新必须走显式 `--allow-overwrite` 逃生舱并留痕(仿 skip_disjoint_check 的"乐观需显式opt-in");
3. **写前升序校验**: 行内 trade_date 单调;
4. **缺口检测采用 query_interval_diff 算法**: 补拉范围=期望区间−现有区间, 输出缺口清单驱动 R4 靶向补拉;
5. **行级可见时刻列(中期)**: 数据行加 `available_at`(源侧口径: 盘中快照=抓取时刻, 收盘数据=收盘时刻), PIT 验收 SQL 化: `SELECT count(*) WHERE available_at > as_of` 必须=0 (红线2);
6. **验收**: 负向测试——重放 #52 事故的重叠写入序列, 必须在第一笔重叠处被拒。

---

## 4. G7 盲区补读判决 (matching_engine 内部 + accounting + 两篇文档)

### 4.1 matching_engine 内部 (`crates/execution/src/matching_engine/engine.rs@72f53d4a82`, 7742行, 策略性精读)

此前卡只读了 exchange.rs 外壳(撮合结论只能按文档级降权引用), 现补代码级:

- **bar 撮合的真实分解** (#L1661-L1737 process_trade_ticks_from_bar 全读): OHLC 拆 4 笔合成 trade tick, **四笔共享 ts_init=bar 收盘时刻**(bar 内无时间分辨率, 仅序列先后); 量按 25%×4 均分、余数归收盘笔, 量/4 小于最小步进时用最小步进 (bar-execution.md#L121-L125, BarTickSizes #L6594);
- **缺口语义**: 上根收盘≠本根开盘时置 fill_at_market 标志——挂单在缺口开盘按**市价(开盘价)成交而非限价**, 高/低/收阶段则按触发价成交 (#L1671-L1698 gap-open 分支+#L1739-L1772 high/low 分支)——跳空穿越限价的成交价语义是诚实的(不给你优于开盘的价);
- **限价成交概率模型作用点** (#L4650-L4727 fill_limit_order): 仅当 maker 单**恰好在限价位**(未穿越)时掷 prob_fill_on_limit 模拟排队位置; 穿越则必成交。queue_position 高级模型(默认关, config.rs#L45)真实跟踪同价位前方量、按 trade 量消耗推进队列 (#L616-L659 determine_trade_fill_qty);
- **默认参数代码级确认** (config.rs#L21-L84 + 测试锁定 Cython 默认一致): bar_execution=true, liquidity_consumption=**false**, queue_position=**false**, use_random_ids=false——卡的"乐观默认"反模式实证升级为代码级;
- **停牌/市场状态闸** (#L2208-L2233 process_status + #L2996-L3005): market_status≠Open 时新订单直接拒绝(带格式化 reason)——A股停牌可用状态事件表达, 但**价格涨跌停带在引擎中无一等公民建模**(全文 grep 无 price limit band 撮合约束; price_protection_points 仅是市价单保护点差), 卡结论维持: 涨跌停必须自写。

**判决**: 撮合思想可引用等级=代码级; 直接采用=否(与 Phase1 判决一致, A股日频组合场景收益/成本比不划算)。可借形: ①bar 内四笔分解+缺口按开盘价成交的诚实语义, 写进我方回测/复盘的成交假设文档; ②"乐观参数默认关"纪律。

### 4.2 accounting (G7 第四对象, 实际位置 `crates/model/src/accounts/`)

注: 议程与卡所称"accounting crate"实为 model crate 的 accounts 模块(base/cash/margin/betting/any/margin_model 六文件); **`docs/concepts/accounts-and-margin.md` 在冻结 SHA 不存在**(404), 实际文档为 `docs/concepts/accounting.md`(399行, 全读)——议程文件名勘误。

- **三账户类型合同** (accounting.md#L12-L54): Cash(现货/股票: 锁**待成交订单将开仓位的名义额**)/Margin(锁初始+维持保证金)/Betting; reduce-only 单不占锁定额;
- **余额三元恒等式** (accounting.md#L56-L80): AccountBalance{total,locked,free}, **total==locked+free 在币种精度下恒成立**; Rust 侧提供两个派生构造器(from_total_and_locked / from_total_and_free), 派生字段钳位到 [0,total]——"venue 舍入瞬时超调不留破损状态"; 
- **cash 账户锁定/PnL/佣金语义** (`crates/model/src/accounts/base.rs#L241-L365@72f53d4a82` 精读): 买单锁名义额(quote 币), **卖单锁数量(base 币, 即股票份额)**; fill 的余额冲击=买(+base数量,−quote名义额)/卖(−base数量,+quote名义额), 不按持仓量截断; 佣金=名义额×maker/taker 费率;
- **重算与借贷开关** (`accounts/cash.rs#L157-L190, L65-L121@72f53d4a82`): recalculate_balance 汇总各合约锁定额, locked>total 且 total≥0 时钳位(free=0); allow_borrowing=false(默认)时**负余额直接报错**——fail-closed;
- **快照替换陷阱** (accounting.md#L108-L113): MarginAccount.apply() **整体替换**两个 margin 存储而非合并——发部分快照的 adapter 会静默丢条目, 官方以 note 警示。这是"事件语义(增量) vs 快照语义(全量)"混用的经典坑, 我方 candidate_state/复盘表更新同样要二选一并写死;
- **A股适配硬判决(账户级实证)**: cash 账户**无 T+1 持仓锁概念**(卖单锁份额=默认当日买入当日可卖)、无结算滞后(即时结算)、无印花税单向性内建(费率仅 maker/taker 二元, 单向税须自定义 FeeModel)。卡的"T+1 需自写"从推断升为账户对象代码级实证。

**判决**: 账户对象=只借三元恒等式+钳位构造器+fail-closed 借贷开关三个形; 账户体系整体不采用(我方无实盘账簿需求, 复盘账本是读模型——Portfolio 模式已在卡中判决借鉴)。

### 4.3 execution.md 补读增量 (#L16-L131, L204-L241, L472-L616)

- 执行流分叉图: Strategy→(Emulator|ExecAlgo|RiskEngine)→ExecEngine→Client, **撤单/查询不过风控**——"撤退永远畅通"与三态闸的 HALTED 仍放行撤单相互印证, 我方 HALTED 态照抄该原则(复盘/回滚动作永不被闸);
- overfill 处理 (#L472-L552): allow_overfills 默认 False=拒纳超额 fill 保守值; True 则应用+overfill_qty 显式追踪——"异常不假装不存在, 给它一个字段";
- 对账竞态成因清单 (#L515-L533): 阈值调太小/轮询太频/启动延迟太短都会放大双通道竞态——参数下限与竞态概率的显式权衡文档, 我方补跑频率设计时对照。

---

## 5. 明确不投入 (维持 Phase1 判决, 深读零时间分配)

| 领域 | 理由 |
|---|---|
| 双核架构 (crates/ vs nautilus_trader/ Cython) | 其最大反模式(卡 anti_pattern#2), 三语言双实现是商业公司全职维护型架构债, 个人系统零借鉴价值 |
| 17 个 crypto adapter + defi 模块 | 与 A股零交集; defi 目录在 model/backtest crate 里都有渗透, 反而是其架构噪音 |
| event_store / cache / msgbus / network crate 内部 | 事件溯源基建与我方 SQLite+parquet 体量不匹配; 若 Wave 需事件留痕, 用状态机违规表+denials 表已够 |
| order_emulator / order_manager | 模拟订单类型(合成 OCO 等)是实盘撮合需求, 我方无 |

---

## 6. 逐文件判决表

| 文件 (均 @72f53d4a82) | 读取深度 | 判决 | 采纳物 |
|---|---|---|---|
| docs/concepts/live.md (#L95-L371) | 全节精读 | **采思想-高优** | 补跑合同 v1 四规则 (§1.2) |
| crates/execution/src/reconciliation/ids.rs (全209行) | 全文 | **采思想-高优** | 确定性补跑ID规格 R2 |
| crates/execution/src/reconciliation/orders.rs (#L890-L945等) | 关键函数 | 采思想 | 容差方向性: 源<本地→保本地只告警 |
| crates/execution/src/reconciliation/positions.rs (#L39,L183-L446) | 关键函数 | 采思想 | 0.0001 容差常数+单位容差+合成价封顶防御 |
| crates/model/src/orders/mod.rs (#L209-L279,L820-L1017) | 转移表全文+apply | **采思想-高优** | 状态表 v1+违规即错误+去重防重放 (§2.2) |
| crates/model/src/events/order/denied_reason.rs (全720行结构) | 枚举全览 | **采思想-高优** | deny_reason 枚举 v1+CATEGORY:k=v 格式 |
| crates/risk/src/engine/mod.rs (#L877-L931,L990起,L1956-L2043) | 三态闸全文 | **采思想-高优** | pipeline 三态开关+撤退不闸原则 |
| crates/persistence/src/backend/catalog.rs (#L529-L585,L1080,L4313,L4780-L4940) | 写路径+校验函数全文 | **采思想-高优** | parquet 写入护栏 v1 (§3.2), 行号已精确 |
| docs/concepts/backtesting/bar-execution.md (全222行) | 全文 | 采思想 | ts_init=收盘合同+"拒绝next-bar-open"的前视论证 |
| crates/execution/src/matching_engine/engine.rs (7742行, 策略读~15%) | bar/fill/status 路径 | 不采用, 结论升级 | 撮合语义引用升为代码级; 涨跌停无内建确认 |
| crates/execution/src/matching_engine/config.rs (全83行) | 全文 | 反面清单 | 乐观默认代码级实证(liquidity_consumption=false等) |
| crates/execution/src/matching_engine/ids_generator.rs (#L84-L205) | 主体 | 采思想 | 回测侧 ID 确定性纪律(与对账侧同构) |
| docs/concepts/accounting.md (全399行) | 全文 | 部分采 | 三元恒等式+钳位构造器+快照替换陷阱警示 |
| crates/model/src/accounts/base.rs (#L241-L365) / cash.rs (#L65-L190) | 计算核心 | 不采用, G7闭合 | A股 T+1/涨跌停/印花税缺位的账户级实证 |
| docs/concepts/execution.md (#L16-L131,L204-L241,L472-L616) | 大部 | 采思想 | deny 表文档自动生成范式+overfill 显式字段+竞态参数权衡 |

---

## 7. G条目闭合声明

**G7 (accounting 未读 + catalog.rs 近似行号): 闭合。**
- accounting: 实际位置 `crates/model/src/accounts/`(非独立 crate), base.rs 计算核心(#L241-L365)+cash.rs(#L65-L190,L157-L233)+accounting.md 全文已读; 第四对象(Account)语义=三元恒等式+锁定/PnL/佣金计算+快照替换 apply, 判决"不采用整体、借三个形"; A股缺位(T+1/涨跌停/单向税)从推断升为代码级实证。
- 近似行号: are_intervals_disjoint 精确定位 catalog.rs#L4806(卡的 ~L4798 是 doctest 注释行), 另补 contiguous #L4854、query_interval_diff #L4900、写路径 #L529-L585 全部精确锚点。
- 附带闭合卡自白盲区: matching_engine/engine.rs 内部(bar 分解/缺口语义/概率成交作用点/市场状态闸, §4.1)、execution.md、accounting.md 已读。order_emulator/order_manager 维持未读(判决"明确不投入", 见 §5, 不影响 G7 主张)。
- 议程勘误: `docs/concepts/accounts-and-margin.md` 在冻结 SHA 不存在(404), 该内容实为 `docs/concepts/accounting.md`——引用议程该文件名的后续文档应改。

## 8. coverage_confession

**本轮读过**: live.md 对账全节(#L95-L371)+runner metrics 标题级; reconciliation 四文件中 ids.rs 全文、orders.rs/positions.rs 关键函数(~40%)、types.rs 未读(仅 FillSnapshot 用途从 ids.rs 推知); orders/mod.rs 转移表+apply+fill handler(~25% of 2302行); denied_reason.rs 枚举结构全览(Display 实现细节未逐条); risk_engine.rs 三态闸+modify 校验+余额检查锚点(~30%); catalog.rs 写路径+校验函数(~15% of 5002行, 但议程点全覆盖); matching_engine/engine.rs ~15%(process_bar 族/fill_limit_order/process_status/determine_trade_fill_qty 精读, **iterate/trigger_stop_order/apply_fills/fill_order 主体未读**——若要引用止损触发或多档成交细节需再补); accounts base/cash 计算核心, **margin.rs(52KB)/betting.rs/margin_model.rs 未读**(与 A股现金账户无关); execution.md ~60%(执行算法/own books 节未读); bar-execution.md/config.rs/ids_generator.rs 全文。
**不确定**: ①reconciliation/tests.rs(173KB)与 proptests.rs 未读——四不变式的边界断言(如容差恰好在界上)未从测试核实, 我方落地 R1 时建议速读其性质测试思路(2小时); ②queue_position 模型的 L1 快照延迟处理(queue_pending)只看了入口; ③live.md 的 Rust runner metrics 快照字段未核对代码。
**引证卫生**: 全部行号来自冻结 SHA 快照本地 grep -n, 无近似行号。

## 9. Wave 映射

| 产出 | Wave | 粗成本 |
|---|---|---|
| parquet 写入护栏 v1 (区间不相交+升序+逃生舱留痕+缺口检测算法, §3.2) | **Wave1 数据可信** | 1-2天 |
| 行级 available_at 可见时刻列+PIT 验收 SQL 化 (§3.2-5) | Wave1 数据可信 (中期) | 2-3天 |
| 补跑合同 v1: 四规则+确定性补跑ID (§1.2) | **Wave2 组合风险契约** (数据侧合同) | 3-5天 |
| 推荐状态机 v1: 穷举表+违规告警 (§2.2) | **Wave2 组合风险契约** | 3-4天 |
| deny_reason 枚举+publication_denials 落库+文档自动生成 (§2.2) | Wave2 组合风险契约 | 3-4天 |
| pipeline 三态开关 (ACTIVE/REDUCING/HALTED, 手动档先行) (§2.2) | Wave2 组合风险契约 | 2天(自动触发需用户拍板) |
| 成交假设诚实文档(bar四笔分解/缺口按开盘价/乐观默认关清单) (§4.1) | Wave3 AI消融的复盘口径附注 | 0.5天 |
| 上游迁移项目 | 无——不引入 nautilus 依赖, LGPL 边界零风险 | 0 |

执行顺序建议: parquet 护栏(Wave1, 最小成本最大止血)→补跑合同→状态机+deny→三态开关。
