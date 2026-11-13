# Phase 1 综合报告 — 14 仓广度扫描 (2026-07-11)

输入: cards/ 全部 14 张六维卡 + local/system_inventory.md (Phase 0 冻结) + local/red_lines.md。
本报告只归纳卡内已有主张, 不新增结论; 引证格式 (repo, path), 行号与 SHA 以各卡 citations 为准。
11 痛点取自各卡 relevance_to_us 的 pain_point 枚举全集: ①PIT数据可信 ②provider契约与跨源对账 ③上游迁移 ④组合/风险/订单契约 ⑤protection保护机制 ⑥表达式引擎与因子parity ⑦调度与失败恢复 ⑧run_manifest与血统 ⑨实验管理与防重测 ⑩AI角色边界 ⑪存储分层。

---

## 1. 能力-痛点矩阵初稿

格式: 机制名 (代价提示)。"反:"=反面教材/排雷动作, 同样是可落地条目。"—"=该卡无此痛点主张。
防重复建设提示(对照 system_inventory): ②整体属 Phase4 provider contract 正治范围; ⑦中"收盘感知"我方已有 a6698e8, 各仓条目一律按"升级差距"读; ⑪的成分 PIT 区间我方已有 S3 冻结人群, 仅作对照校验。

### 1a. Tier-A 五仓

| 痛点 | qlib | OpenBB | NautilusTrader | Lean | freqtrade |
|---|---|---|---|---|---|
| ①PIT数据可信 | 双时间PIT契约: (公告日,报告期,值,修订链)+算子层硬禁未来期 (中1-2天; 只借数据模型, 勿借其原型级实现) | — | ts_init=收盘时间戳合同+parquet文件名时间区间不相交校验 (低1-2天) | raw永存+复权因子/map改名退市独立辅助层+读取端口径枚举 (中2-4天) | — |
| ②provider契约/跨源对账 | — | 标准模型+TET三段fetcher+Fetcher.test()逐段契约断言+standard/extra字段切分 (中1-2天; 我方须反其道 extra='forbid') | 对账4规则: 不变式显式/合成ID确定性/查询失败≠空/终态前靶向查询 (低3-5天) | 装配期源组合合法性硬校验+DataMonitor数据缺口自动登记 (低0.5-1天) | — |
| ③上游迁移 | — | VCR录制回放cassette, 重录+diff定位上游漂移 (低半天) | — | 反: 九年无release滚动master→锁版本入manifest+探针红灯 (低0.5天) | — |
| ④组合/风险/订单契约 | tradability装载期预计算布尔契约(limit_buy/limit_sell/suspended)+表达式元组分板块阈值+参与率²冲击成本 (低-中2-3天) | — | 四对象契约: Order穷举状态机/RiskEngine typed deny/Portfolio纯事件读模型/TradingState三态闸 (中1-2周; LGPL全重写) | 按标的粒度可插拔 Fee/Slippage/Fill/Settlement/BuyingPower 接口+默认值反转为保守 (低-中1-2天) | — |
| ⑤protection保护机制 | — | — | crash-only+fail-fast: stage入口不变式断言+Denied事件落库, "发布失败优于发布污染" (低2-3天) | Isolator时间+RAM双限额stage guard包装器 (低1天) | ProtectionReturn四元组{lock,until,reason,side}+strategy_locks落库+显式恢复条件+影子回放+配置fail-fast (低1-3天; GPL零代码复制) |
| ⑥表达式引擎/因子parity | 算子树自动推导回看窗+装载器扩窗再截断→任意窗口取值恒等, required_lookback元数据 (中2-3天) | — | — | — | — |
| ⑦调度与失败恢复 | TaskManager"领取即改态/异常回滚waiting"状态机 (低1天, SQLite即可) | 反: amake_requests有部分成功即吞异常→审计我方批量拉取, 禁partial=success, 强制(results,failures)双通道 (低半天) | — | — | 锁/暂停状态落库重启即恢复+心跳+sd_notify watchdog (低0.5-1天) |
| ⑧run_manifest与血统 | start_run自动dump git diff/status/argv三件套进run目录 (低半天) | — | — | — | — |
| ⑨实验管理与防重测 | Experiment→Recorder目录学: 闯关机每run自动落params_hash+代码diff, necromap降为自动索引 (低1-2天, 不引MLflow) | — | — | — | — |
| ⑩AI角色边界 | — | — | — | — | — |
| ⑪存储分层 | — | — | — | — | — |

### 1b. 其余仓 (一)

| 痛点 | TradingAgents | ZhuLinsen/dsa | vnpy | backtrader | zipline |
|---|---|---|---|---|---|
| ①PIT数据可信 | 反: 公告日≠财政期末锚点; 所有PIT守卫配"生效性负向测试"(喂未来数据断言被拒) (低1天) | — | — | — | as-traded原始价+独立SQLite复权层+按ex_date套ratio; asset生命周期+as_of符号解析防幸存者 (中, 原型1-2天) |
| ②provider契约/跨源对账 | route_to_vendor三纪律: 配置链即全链不静默回退/类型化错误分类/NO_DATA哨兵+stale陈旧拒收 (中低2-3天) | record_provider_run血统流水(源/延迟/error_type/fallback_to)+3失败5分钟熔断半开恢复 (1-2天) | — | — | — |
| ③上游迁移 | — | 上游fork冻结detached, 只定点移植三小块: 熔断降级骨架/日历phase语义/decision_signal结构化评估 (评估0.5天+各1-2天; 整体rebase不推荐) | — | — | 反: 死于默认源停更+运行时远程benchmark断供+依赖钉死→全离线降级演练+源健康探针cron (低半天) |
| ④组合/风险/订单契约 | — | — | target-position契约(set_target/execute_trading解耦)+yd/td/frozen期货台账作T+1可卖余额骨架 (1天) | 9态订单状态机+两阶段"伪执行先算现金"资金预检+CommInfo式可插拔费用对象 (低-中1-3天; GPL思想借鉴) | — |
| ⑤protection保护机制 | — | — | 反: ±10%硬编码/停牌平bar恒可成交/无T+1→作我方protection回归测试反例集 (半天) | — | TradingControl控件注册表+每笔validate一次+fail-or-log可配+宇宙边界控件 (低0.5-1天) |
| ⑥表达式引擎/因子parity | — | — | 表达式字符串+ts_/cs_算子注册命名空间+表达式哈希作因子身份接necromap (0.5-2天; 弃其eval, 换AST白名单) | next/once双实现+黄金值截点parity测试惯例+minperiod沿DAG传播(窗口未满输出NaN) (低0.5-1天) | Pipeline term DAG+workspace引用计数释放+chunked分段+domain数据可见时点 (高2-3天; 只借思想, 现行参照zipline-reloaded) |
| ⑦调度与失败恢复 | — | MarketPhaseContext: 收盘感知+盘中phase+is_partial_bar结构化随管线传递(升级我方a6698e8; 坚持fail-closed) (0.5-1天) | 反: 驱动缺失回退SQLite/datafeed空stub静默降级→背书我方硬告警 (0) | 反: 引擎零持久化零恢复→背书外置分层; 正面仅TradingCalendar日历对象化 (低0.5天) | — |
| ⑧run_manifest与血统 | 续跑身份=运行形状签名(config-hash折进恢复键, 参数变即作废旧断点) (低0.5-1天) | — | — | — | — |
| ⑨实验管理与防重测 | — | — | — | — | — |
| ⑩AI角色边界 | verified market snapshot唯一事实源注入提示词+确定性parse_rating(禁二次LLM提取) (低1-2天) | 反面(文本推断当度量)+正面(decision_signal结构化)→维持v2合同, 禁文本推断兜底 (0天, 决策确认) | — | — | — |
| ⑪存储分层 | — | — | BarOverview库存表(symbol×interval的count/start/end)作覆盖合同物化视图; 成分shelve→PIT区间与S3对照校验 (极低2-4小时) | — | 时间戳版本化ingestion目录+most_recent_data+keep_last回收=可回滚快照血统原语 (低1天) |

### 1c. 其余仓 (二)

| 痛点 | akshare | myhhub/stock | FinGPT | FinRL |
|---|---|---|---|---|
| ①PIT数据可信 | 排雷: qfq是服务端请求时点口径, 每次除权全历史回变→缓存禁存qfq面板; 静态日历只到20261231→"日历尽头<90天"硬告警 (低-中, 审计半天+告警1小时) | 反: 禁实时spot接口盖历史日期戳/回补必须走时点源否则fail-loud→写进red_lines+闯关机故障注入用例 (0.5天) | 反: 禁report-period join(数据适配层assert公告日)+AI prompt每字段as-of日期标注 (低1-2小时) | 反: 复权因子单独存列/按评估时点重建价格的对账断言 (低1-2小时) |
| ②provider契约/跨源对账 | 每依赖接口冻结契约: 列名/dtype/单位/量级断言+第二源抽样对账, 破裂即告警而非静默NaN (中, ~10接口一周) | — | 东财search-api per-symbol新闻源可接AI上下文; 教训: 时间戳须保留到分钟+收盘前/后切割 (中1-2天+健康探针) | — |
| ③上游迁移 | 版本pin入run_manifest+升级前探针(schema/行数/双源对照)+releases流静默>4周即断供预警 (低半天) | 东财反爬双降级手法: proxy池+cookie双通道+分页随机延迟, 做成可选钩子 (1天) | — | — |
| ④组合/风险/订单契约 | — | 警示: OCR直连同花顺无契约层→维持"夜间选股→人工执行"边界, 不为自动下单立项 (0天) | — | — |
| ⑤protection保护机制 | — | 反: 全链路log-and-continue把失败洗成"当日成功"→pipeline-alert加2-3个故障注入回归用例 (0.5天) | — | turbulence指数(252日协方差Mahalanobis)市场应激闸门 (低半天; necromap先查regime类+五道闸预注册+替换为T+1感知的"只停买不强卖") |
| ⑦调度与失败恢复 | 核心接口timeout=None+无重试→data_adapter统一timeout/重试/熔断包装, 不信任库内部; 可借其utils/request退避+抖动模式 (低1-2小时) | — | 反: 失败静默空answer落盘→给AI生成器失败记录加按日/板块缺口分布报表, 防隐性偏斜 (低半天) | — |
| ⑩AI角色边界 | — | — | 红线: 禁label-conditioned rationale(看着结果写分析); 其中文三段prompt schema([积极发展]/[潜在担忧]/[预测和分析])可对照打磨 (低半天) | — |
| ⑪存储分层 | — | cache按YYYYMM/YYYYMMDD两级日期目录+按月prune的retention布局(治我方backtest_cache爆盘后欠缺) (0.5天) | — | — |

未入矩阵的"其他"条目: backtrader滑点三件套公理(成交价必须落bar区间内+成交量参与率上限)直接写入我方回测断言与B1标定 (backtrader, brokers/bbroker.py+fillers.py, 低); FinGPT本地情感LoRA配方(高成本, 仅litellm成瓶颈时考虑, 且其F1须按时间重切后再信); FinRL的RL范式盘点结论=不实施仅研究观察(零成本, 避免投入)。

---

## 2. 每仓电梯结论

| 仓 | 最值得学的一件事 | 最不该学的一件事 |
|---|---|---|
| microsoft/qlib | 双时间PIT数据契约+表达式自动扩窗的因子parity机制 (qlib/config.py pit_record_type; qlib/data/data.py L851-856) | PIT的原型级实现(整文件重读+线程不安全)与A股涨跌停0.095一刀切 (qlib/data/data.py L746; qlib/config.py L296-303) |
| OpenBB-finance/OpenBB | TET三段fetcher+Fetcher.test()逐段契约断言的provider契约骨架 (openbb_core/provider/abstract/fetcher.py L36-85, L173-178) | extra='allow'+告警默认关+部分失败吞异常的契约软化三件套 (abstract/data.py L77-85; utils/helpers.py L445-456); 且实为AGPL, 禁碰代码 |
| nautechsystems/nautilus_trader | 启动/运行期对账子系统: 不变式+确定性合成ID+查询失败≠空仓 (docs/concepts/live.md L336-350) | 双核三语言并行维护多年+默认乐观成交(触价必成交/流动性可重复消耗) (README L85-87; docs/concepts/backtesting/fill-models.md L52-58) |
| QuantConnect/Lean | PIT三层分离: raw永存+factor file+map file+读取端口径枚举 (Common/Data/Auxiliary/CorporateFactorProvider.cs L42-76; MapFile.cs) | 全套乐观默认(零滑点+市价单全量成交)与九年无release的滚动master (Common/Brokerages/DefaultBrokerageModel.cs L281-284; EquityFillModel.cs L174-177) |
| freqtrade/freqtrade | 保护契约: 检测→锁表→入场闸→通知四层分离, 锁带reason落库+显式恢复条件+可影子回放 (freqtrade/plugins/protections/iprotection.py L17-22, L124-143) | 风控指标默认用已知不准口径(MaxDrawdown legacy ratios)+类属性全局单例状态 (plugins/protections/max_drawdown_protection.py L25-26; persistence/pairlock_middleware.py L21-24); GPL禁复制 |
| TauricResearch/TradingAgents | verified market snapshot: 确定性代码先算"唯一事实源"表, AI只准解读冻结快照 (tradingagents/dataflows/market_data_validator.py L62-123) | "模拟交易所"叙事与代码脱节+财报PIT锚财政期末且守卫静默失效18个月 (README L98; dataflows/alpha_vantage_fundamentals.py L25; CHANGELOG #1115) |
| ZhuLinsen/daily_stock_analysis | provider run级血统流水+MarketPhaseContext收盘感知语义结构化 (data_provider/base.py L1406-1480; src/core/pipeline.py L331-338) | 交易日检查fail-open(日历坏了当开市继续跑)+用否定词启发式反推LLM意图当回测ground truth (src/core/trading_calendar.py L151-175; src/core/backtest_engine.py L113, L545-583) |
| vnpy/vnpy | 表达式因子哈希身份+yd/td/frozen台账作T+1可卖余额骨架 (vnpy/alpha/dataset/utility.py; vnpy/trader/converter.py L20-41) | 裸eval执行因子表达式+关键依赖缺失静默降级(回退SQLite/空stub) (vnpy/alpha/dataset/utility.py L252; vnpy/trader/database.py L151-155, datafeed.py L49-66) |
| mementum/backtrader | next/once双实现+黄金值截点断言的因子parity测试惯例 (tests/test_ind_*.py; linebuffer.py minperiod传播) | 元类魔法全家桶+默认无限流动性成交; 项目本体已是弃船 (metabase.py L66-90; brokers/bbroker.py L126-127) |
| quantopian/zipline | as-traded原始价+独立复权层+as_of资产解析+版本化ingestion目录四件套 (zipline/data/adjustments.py; assets/assets.py L958; data/bundles/core.py) | 依赖焊死(pandas<=0.22/bcolz)+uint32越界静默置零+运行时远程基准API (etc/requirements.in L21/L47; data/bcolz_daily_bars.py L69-115) |
| akfamily/akshare | 把它当"随时会断的上游"管理: 版本pin+升级前探针+releases流监控; 其utils/request.py退避+抖动模式可抄进fetcher (akshare/utils/request.py L15-64) | 位置硬编码列契约(成交量误标amount实锤#7328)+核心接口timeout=None裸奔+测试剧场(1268接口2条断言) (stock_feature/stock_hist_em.py L958/L992; tests/test_func.py) |
| myhhub/stock | 两级日期目录retention布局+东财反爬proxy/cookie双降级钩子 (instock/core/stockfetch.py L368-401; proxy.txt/eastmoney_cookie.txt) | 实时spot快照盖历史日期戳伪造PIT+吞异常当控制流+字符串拼接SQL (instock/core/stockfetch.py L94-108; instock/lib/database.py L117-154, L172-206) |
| AI4Finance/FinGPT | A股中文结构化prompt schema+东财per-symbol新闻源(须补分钟级时间戳切割) (FinGPT-Forecaster-Chinese/Ashare_data.py SYSTEM_PROMPT L152-153; Inference_datapipe.py L24-85) | 后见之明rationale蒸馏(把真实未来收益告诉GPT-4让它编支持理由)+report-period join前视 (FinGPT_Forecaster/data.py L276-285; Ashare_data.py L97, L157-162) |
| AI4Finance/FinRL | turbulence市场应激闸门思想(~50行, MIT可参考; 上线须过necromap+五道闸) (finrl/meta/preprocessor/preprocessors.py L282-334) | 数据管线内建幸存者偏差(dropna删票)+bfill未来回填+发布无CI门禁 (finrl/meta/preprocessor/preprocessors.py L184-187, L170; .github/workflows/test.yml) |

---

## 3. Tier-A Phase2 深读议程修订建议

红线通提(全部深读对象适用): freqtrade/backtrader=GPL、NautilusTrader=LGPL、OpenBB=AGPL(卡已在冻结SHA核实LICENSE明文, GitHub NOASSERTION系自定义序言所致)——四者一律只读思想禁贴代码, 笔记不得含可复制片段; 磁盘<6G继续禁克隆, 全程 gh api 纯文本+冻结SHA引证; 深读产出的每条机制提案先对照 system_inventory 只提升级差距。

### 3.1 上游 diff (ZhuLinsen/daily_stock_analysis, 本地深改克隆 c7059b5 vs 上游 ~309 提交)
Phase1 卡已给出总判决: 整体 rebase 性价比极低, fork 冻结 detached + 定点移植。Phase2 差异报告应聚焦:
- **必做**: 逐 commit 对比本地 c7059b5 与上游 d083748——卡自白"未逐commit对比"(~309为任务给定值), 这是 Phase2 差异报告的核心欠账。先对我方 9 改+5 自有文件求冲突面。
- **移植切片一(provider骨架)**: `data_provider/base.py`(159KB, 卡只读头部+fetch循环)——priority候选链/record_provider_run/CircuitBreaker 三件套的完整实现边界。
- **移植切片二(日历语义)**: `src/core/trading_calendar.py`(get_effective_trading_date/MarketPhaseContext/is_partial_bar; 同时确认其 fail-open L151-175 不被带入)。
- **移植切片三(结构化评估)**: `src/core/backtest_engine.py` L276-285 evaluate_decision_signal 及 decision_signal 存储链路(src/storage.py 相应模型)——只借"without text inference"的接口形状。
- **划界即止**: `src/analyzer.py`(204KB)与 config 体系(config.py/config_registry.py/system_config_service.py 合计566KB)只读到"确认切片依赖边界"为止, 不通读; agent/webui/notification 全栈明确排除。

### 3.2 microsoft/qlib
- **PIT契约(借模型不借实现)**: `qlib/config.py` L227-238 pit_record_type、`qlib/data/pit.py` P/PRef算子(L25-49)、`qlib/data/data.py` LocalPITProvider(L744起, searchsorted+修订链)、`scripts/dump_pit.py` 头部TODO——目标是提炼一页"双时间字段+查询语义"规格给我方 parquet 财务/事件缓存。
- **parity机制**: `qlib/data/data.py` LocalExpressionProvider L851-856 扩窗+`ops.py` get_longest_back_rolling/get_extended_window_size——直接对接我方 #52 滚动窗事故的结构性解法(required_lookback 元数据)。
- **必补盲区(卡自白)**: `qlib/data/dataset/` handler/processor(Alpha158-360 横截面处理层, 卡承认与 parity 维度相关但未验证); `qlib/workflow/online/update.py` 与 `task/gen.py`(滚动再训练, 只看了目录)。
- **可交易性契约**: `qlib/backtest/exchange.py` L258/L281-292 表达式元组版 limit_threshold(20cm/10cm/5cm/ST分段规则的逃生舱)+参与率²冲击(L887-892); 顺带核 `position.py` settle 之外 executor 内层是否有 T+1 特殊处理(卡自认小概率遗漏)。
- **血统/调度小件**: `qlib/workflow/recorder.py` L354-378、`qlib/workflow/task/manage.py` safe_fetch_task L289-305、`scripts/check_data_health.py`——三者均为低成本即抄清单, 深读只需确认边界条件。

### 3.3 OpenBB-finance/OpenBB
- **契约骨架**: `openbb_platform/core/openbb_core/provider/abstract/fetcher.py`(TET全文+test() L115-186)、`abstract/data.py`、`registry_map.py` L71-106 standard/extra 切分——产出我方日线/日历/竞价三张标准模型的设计稿(反其道: extra='forbid'+告警默认开)。
- **必补盲区(卡自白, PIT相关度最高)**: 33 个 provider 中未读的 **SEC/FRED 官方源**——卡明言"SEC provider 可能有公告日PIT处理值得后续补读"。
- **VCR运维资产**: providers/yfinance/tests/record 目录+scrub_string 钩子的实现细节(卡只看了模式), 落成我方 3 个最脆 akshare 接口的 pytest-recording 模板。
- **许可复核**: LICENSE 全文逐条(卡只读前80行, AGPL声明之外是否有附加条款未核完)——深读第一步做完再动别的。
- **反面清单固化**: `utils/helpers.py` amake_requests L393-461 吞异常路径, 写成我方批量拉取审计的对照检查单。

### 3.4 nautechsystems/nautilus_trader
- **对账子系统(全仓最高价值)**: `docs/concepts/live.md` §Reconciliation invariants L336-350+§Runtime caveats L277-295, 翻译成我方 tushare/akshare 补跑脚本的 4 条规则+确定性补跑ID规格。
- **订单/风控契约**: `crates/model/src/orders/mod.rs` OrderStatus::transition L209-279 穷举转移表、`crates/risk/src/engine/mod.rs` typed OrderDenied+TradingState 三态(L877-931)——映射到我方"候选→发布→AI完成/失败→复盘"状态表+deny_reason枚举。
- **存储纪律**: `crates/persistence/src/backend/catalog.rs` are_intervals_disjoint(注意: 卡自白该函数定义行号~L4798为grep近似, 深读须先重定位)+`docs/concepts/backtesting/bar-execution.md` ts_init 合同。
- **必补盲区(卡自白)**: matching_engine 内部实现(卡只看了 exchange.rs 外壳)——若要引用其撮合语义必须补; `docs/concepts/execution.md` 与 `accounts-and-margin.md`; accounting crate。
- **明确不投入**: 双核架构、17 个 crypto adapter、defi——与我方零交集且是其最大反模式。

### 3.5 QuantConnect/Lean + freqtrade protections
Lean:
- **PIT三件套**: `Common/Data/Auxiliary/CorporateFactorProvider.cs` L42-76 五口径、`MapFile.cs` L106-121、`Common/SecurityIdentifier.cs` L451-494——产出我方 adj_factor 独立层+DataNormalizationMode 枚举+退市/ST状态史 map 层的设计稿。
- **可插拔契约与守卫**: `Common/Orders/Fills/EquityFillModel.cs`(L162-179 stale守卫+全量成交假设)、`Common/Brokerages/DefaultBrokerageModel.cs`、`Engine/LeanEngineAlgorithmHandlers.cs` L235-276 装配校验+L190-194 DataMonitor、`Engine/Engine.cs` L346-366 Isolator。
- **必补盲区(卡自白)**: TransactionHandlers(订单状态机/部分成交)、SecurityMarginModel/BuyingPower 实现、**LimitFill 在日线分辨率下 bar 内高低点可知性(潜在前视)争议——卡只读了方法签名附近注释, 深读必须裁决**。
freqtrade protections:
- **契约全链**: `freqtrade/plugins/protections/` 全 5 文件+`protectionmanager.py` L50-125+`persistence/pairlock.py`/`pairlock_middleware.py`+`strategy/interface.py` L1131-1184 锁API+`optimize/backtesting.py` L353-355/L1326-1329 影子接线——落成我方 strategy_locks 表+交易日历版恢复条件的实施稿。
- **必补盲区(卡自白)**: `tests/plugins/test_protections.py`——保护边界行为断言未核, 这恰是我方最该抄的部分(A股"退出受阻"场景要在我方版本补, freqtrade 没有)。
- **反例复核**: stoploss_guard.py L64-65 falsy-zero、max_drawdown_protection.py 双口径——写进我方保护实现的单测清单。

---

## 4. 反模式汇总 (全部卡归类)

### A. 乐观默认值即隐性立场 (回测/撮合)
- NautilusTrader: 流动性可重复消耗+prob_fill_on_limit=1.0+零滑点默认 (docs/concepts/backtesting/fill-models.md L52-58, L142-158)。
- Lean: 零滑点 NullSlippageModel+市价单"assume completely filled" (Common/Brokerages/DefaultBrokerageModel.cs L281-284; Common/Orders/Fills/EquityFillModel.cs L174-177)。
- backtrader: filler 默认 None=整单一次成交无限流动性 (backtrader/brokers/bbroker.py L126-127)。
- freqtrade: MaxDrawdown 默认 legacy ratios 口径, 修正版不设默认 (plugins/protections/max_drawdown_protection.py L25-26)。
**教训**: 护栏默认开启, 乐观模式必须显式 opt-in——我方回测/复盘默认恒为"有滑点+涨跌停不可成交+部分成交"。

### B. 静默失败/吞异常/fail-open
- OpenBB: 批量请求有部分成功即丢弃全部异常 (utils/helpers.py L445-456); 不支持参数静默过滤+show_warnings默认False (app/query.py L52-62)。
- vnpy: DB驱动缺失回退SQLite、datafeed缺配返回空stub (trader/database.py L151-155; datafeed.py L49-66)。
- ZhuLinsen: 交易日检查 fail-open 当开市继续跑 (src/core/trading_calendar.py L151-175)。
- FinRL: 实盘下单裸except只print+resp无人消费 (meta/paper_trading/alpaca.py L347-371)。
- FinGPT: LLM重试耗尽写空answer进训练CSV无告警 (FinGPT_Forecaster/data.py L222-238)。
- myhhub: 全链路catch-log-continue, 错误系统性变脏数据 (instock/lib/database.py L172-206; web/dataIndicatorsHandler.py L61-66)。
- qlib: redis缺席磁盘缓存静默关闭仅warning+读锁被取消留FIXME (qlib/config.py L465-481; data/cache.py L224)。
**教训**: 无人值守管线里"可用性优先"必然演化成数据空洞事故; 我方 fail-closed+硬告警路线被 7 仓反向验证。

### C. PIT违规/前视谱系
- FinGPT: report-period join+"截止今天"市值注入历史prompt+后见之明蒸馏 (Ashare_data.py L97/L157-162; data.py L276-285)。
- TradingAgents: fiscalDateEnding锚点+前视过滤器静默失效18个月 (dataflows/alpha_vantage_fundamentals.py L25; CHANGELOG #1115)。
- myhhub: 实时spot盖任意历史日期戳+今日universe回补历史 (instock/core/stockfetch.py L94-108)。
- FinRL: bfill未来回填+取数日基准全历史回溯复权+dropna删票幸存者面板 (meta/preprocessor/preprocessors.py L170, L184-187; yahoodownloader.py)。
- akshare: qfq为请求时点口径且不吐复权因子 (stock_feature/stock_hist_em.py L979/L987)。
- Lean: 日线订阅下盘中市价单按昨收成交潜伏多年(#9518, 2026-06才修) (Common/Orders/Fills/EquityFillModel.cs L162-170)。
- ZhuLinsen: stock_daily upsert覆盖式更新无as-of (src/storage.py L141-144)。
**教训**: 前视不止在数据层, 也藏在守卫失效/复权口径/成交时点; 守卫必须配"毒样本必须被拒"的负向测试。

### D. A股市场语义一刀切或缺失
- qlib: limit_threshold=0.095单一浮点+收盘价判定漏盘中炸板 (qlib/config.py L296-303; backtest/exchange.py L287-292)。
- vnpy: ±10%硬编码+停牌合成平bar恒可成交+无T+1 (alpha/strategy/backtesting.py L599-654)。
- Lean/backtrader/NT/zipline/freqtrade: 无涨跌停/无T+1(各卡market_assumptions), 其中freqtrade保护只闸入场无"退出受阻"概念。
**教训**: 涨跌停分层/T+1/停牌三件必须我方自建为显式契约, 无一仓可直接搬。

### E. 数值精度静默损失
- qlib: 行情全量float32, 大数值字段丢尾数 (data/storage/file_storage.py L310-372)。
- zipline: uint32量化+越界winsorise置零仅warn (data/bcolz_daily_bars.py L69-115, L367-372)。

### F. 架构债: 上帝模块/双实现/魔法
- NautilusTrader: Rust/Cython双核三语言并行多年 (README L85-87; ROADMAP L21-26)。
- backtrader: 五段元类构造链+死代码双路径共存 (metabase.py L66-90; cerebro.py L1377/L1443)。
- ZhuLinsen: 7个136-222KB上帝模块+周更回归外溢(#1968) 。
- OpenBB: 安装期4340行codegen+linter当运行时依赖 (app/static/package_builder.py)。
- FinRL: 复制粘贴债(加密列名残留/重复方法定义/死tushare下载器) (env_stocktrading.py L511-522; data_processor.py L58-72)。
- akshare: 三套HTTP层并存互不统一 (request.py/utils/request.py/裸requests.get)。
- FinGPT: monorepo范围漂移+商业捕获(无关稳定币平台整体灌入/厂商bot PR直入主线)。
- vnpy: 事件引擎单线程串行无背压 (event/engine.py L55-78); 核心包钉死GUI栈。

### G. 治理/发布/供给链
- Lean: 九年无语义化release, 升级成本外部化 (releases停于v2.4.0.1/2017)。
- backtrader: 治理式弃养(关issues不archive, CI指向停服Travis)。
- zipline: 依赖焊死+默认数据源(Quandl WIKI)早亡+运行时远程benchmark断供 (etc/requirements.in; issue #2530)。
- qlib: 官方CN数据集停发指向个人社区源 (README L212)。
- akshare: 测试剧场(1268接口2条断言)+bus factor≈1+静默6周 (tests/test_func.py)。
- FinRL: CI仅每日cron无push/PR门禁, tagged release核心算法跑不通(#1395)。
- myhhub: 无测试无CI+SpreadJS评估版授权雷+调度用注释当配置 (job/execute_daily_job.py L45-56)。

### H. 度量口径不诚实
- ZhuLinsen: 否定词启发式反推LLM意图当回测ground truth+止损止盈精确价必成交 (src/core/backtest_engine.py L113, L736-768)。
- FinGPT: 时序语料随机shuffle切分train/test (FinGPT_Benchmark/utils.py L206-209)。
- freqtrade: falsy-zero把保本止损静默排除出计数 (plugins/protections/stoploss_guard.py L64-65)。
- myhhub: rate回填用事后更新的qfq基准, 信号日与评估日复权基不同 (core/backtest/rate_stats.py L12-50)。

### I. 全局可变状态/并发
- freqtrade: PairLocks类属性单例靠调用方纪律reset (persistence/pairlock_middleware.py L21-32)。
- myhhub: 无参单例+多日期并发互相踩踏, 下游手工覆写date列补救 (lib/singleton_type.py L15-20; job/strategy_data_daily_job.py L47-50)。

### J. 安全
- myhhub: f-string拼SQL+root/root默认凭据+supervisord管理口无认证 (lib/database.py L117-154, L14-19; supervisor/supervisord.conf L5-8)。
- vnpy: 因子表达式裸eval任意代码执行 (alpha/dataset/utility.py L252)。
- FinRL/Lean: 明文凭据(config_private.py / 820行config.json凭据槽位)。

### K. 文档与实现脱节
- TradingAgents: README宣称"订单发送到模拟交易所执行", 代码无任何执行模块 (README L98)。
- myhhub: README宣传全管线, 冻结HEAD总调度四步被注释禁用 (job/execute_daily_job.py L45-56)。

---

## 5. 覆盖自白汇总与 Phase2 必补清单

### 5.1 各卡承认没看的部分 (要点)
- **qlib**: contrib全部/qlib/rl/qlib/model; **dataset handler/processor(Alpha158-360横截面层, 卡自认与parity相关未验证)**; online/update.py与task/gen.py只看目录; T+1结论有executor内层遗漏小概率; issue区仅计数。
- **OpenBB**: **31/33个provider未读(含SEC/FRED等PIT相关官方源)**; package_builder细节; **LICENSE只读前80行**; logging遥测落点; technical/quantitative算法。
- **NautilusTrader**: **matching_engine撮合核心内部**; accounting/analysis/cache/msgbus各crate; execution.md与accounts-and-margin.md; 全部17个adapter; Cython v1层; catalog.rs关键函数行号为grep近似。
- **Lean**: **TransactionHandlers(订单状态机/部分成交)**; **LimitFill日线bar内可知性(潜在前视)未裁决**; SecurityMarginModel/BuyingPower; AlgorithmManager主循环; LiveTradingDataFeed容错; Python集成层。
- **freqtrade**: **tests/plugins/test_protections.py(保护边界断言)**; pairlist体系; exchange/ccxt执行层; FreqAI/hyperopt内部; DB migrations。
- **ZhuLinsen**: analyzer.py(204KB)/config体系(566KB)/agent子系统/notification/alphasift/docs全指南; **未逐commit对比本地fork与上游**; 涨跌停/T+1结论若analyzer提示词内有软约束则未计入。
- **TradingAgents**: cli/全部; analysts/researchers提示词正文; agent_utils工具定义主体; llm_clients实现; 各社媒数据源; "无执行模块"判断基于包树枚举(cli误判可能性低)。
- **vnpy**: **实盘CTA/组合引擎与带滑点参数的回测器都在vnpy_ctastrategy等插件仓, 不在冻结SHA内——涨跌停/停牌/T+1缺陷是否被插件修补未验证**; trader/engine.py函数体; alpha/model实现; 算子实现细节。
- **backtrader**: 122个指标实现; analyzers绩效公式(Sharpe年化口径未核); stores/brokers实盘接入; resampler算法; yahoo复权处理只凭文档; A股缺口结论未实测运行。
- **zipline**: algorithm.py全文; finance/metrics与ledger; AdjustedArray底层; loaders与内置因子; limit/stop具体成交判定仅侧面确认; reloaded fork生态未考察。
- **akshare**: 1000+接口只精读约10个; futures/option/fund/macro全子树; #7328的tx单位bug直接采信issue正文未自行复算; docs一致性未核; dev分支活动未核。
- **myhhub**: cyq筹码分布py+js双实现未细读; eastmoney_fetcher注入方式为推断; trade/robot内部; 大部分策略实现; issue仅30条标题。
- **FinGPT**: RAG/MultiAgentsRAG/FinancialReportAnalysis/Sentiment各子项目; Benchmark逐数据集评测脚本(宣传F1未从代码复核); Forecaster-Chinese train侧为同构推断。
- **FinRL**: agents三套适配器内部(DRLEnsembleAgent滚动重训); applications全部教程; 其余env变体; turbulence与T+1交互为推理未实测。

### 5.2 Phase2 必须补的 (与 §3 深读议程绑定)
1. **ZhuLinsen 逐 commit 上游 diff + 三个移植切片文件全读**——Phase2 差异报告的定义性欠账 (§3.1)。
2. **qlib dataset handler/processor**——parity 借鉴若缺横截面处理层视角会借半套 (§3.2)。
3. **OpenBB SEC/FRED provider + LICENSE 全文法务级复核**——前者是 PIT 借鉴最可能的增量, 后者是红线3的硬门 (§3.3)。
4. **NT matching_engine 内部 + execution/accounts 文档**——不补则其撮合/对账结论只能按"文档级证据"降权引用 (§3.4)。
5. **Lean TransactionHandlers + LimitFill 日线前视裁决; freqtrade test_protections.py**——前者关系到我方订单状态表的成熟形态, 后者是保护契约移植的验收基准 (§3.5)。

### 5.3 明确不补 (记录理由, 防止范围回弹)
- vnpy 插件仓(vnpy_ctastrategy 等): 不在冻结SHA内, 且 vnpy 定位仅为反例对照+两个小骨架, 不值一次新冻结; 若未来引用其撮合结论, 须加"核心仓限定"脚注。
- backtrader/zipline 引擎本体细节(指标实现/algorithm.py): 两仓均为弃船, 只取思想件, 已足。
- FinGPT/FinRL 各子项目与训练教程: 两仓价值以反面教材为主, 卡内证据已够支撑红线条目; 本地小模型路线(FinGPT)挂起至 litellm 成本成为瓶颈。
- TradingAgents 提示词正文与 cli/: verified snapshot 与 vendor 路由两个机制的证据已闭环; 提示词工程属我方自有迭代, 无需对照全文。
- akshare 其余 990+ 接口: 契约审计只对我方~10个依赖接口做(矩阵②条目), 属 Phase4 provider contract 范围, 不在深读阶段展开。
