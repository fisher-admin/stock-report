# Phase2 深读报告 — 轨道: 上游 diff 迁移审计 (G1)

- 对象: ZhuLinsen/daily_stock_analysis 冻结 SHA `d08374898c25f4718d61b1779ac4c1fedc9aa9a2`(下称 @d08374898c) vs 本地深改克隆 `/Users/fisher/.openclaw/workspace/daily_stock_analysis`(HEAD `c7059b5`)
- 方法: 本地 git status/diff + 逐文件 raw.githubusercontent 冻结版对读 + GitHub compare/commits API 量化 churn。**禁克隆遵守**(全程 curl raw + gh api, 下载 14 个文件约 1.2MB 至 scratchpad)。
- 许可: MIT — 三个移植切片**允许直接复制代码**，无 GPL 类限制。
- 撰写: 2026-07-11, Tier-A 深读研究员。

---

## 0. 执行结论(先说判决)

1. **本地拓扑真相与 Phase1 卡的口径修正**: "本地深改克隆 9改+5自有"实际结构是——base=上游 `650a1c3`(2026-05-02) + **1 个已提交本地 commit**(c7059b5, 仅 config.py +6 行) + **9 个未提交工作区修改**(+233/-82 行) + 5 个未跟踪文件(3 个桥接脚本 + 2 个 .bak)。上游冻结点领先 base **恰好 309 commits**(compare API: ahead_by=309, behind_by=0 — "~309 为任务给定值"实测精确成立)。本地改动总量远小于"深改"印象: 除 analyzer.py 的 211 行外全部是 ≤8 行小补丁。
2. **整体判决维持并强化 Phase1 卡结论: fork 冻结 detached, 不整体 rebase**。新证据: (a) 本地 9 改中 6 个可直接 drop 或已被上游取代; (b) 真正有价值的本地资产只有一块(analyzer 诚实性护栏), 且上游至今未修同类问题, backport 到任何未来版本都要重做而非 cherry-pick; (c) **三切片的新增组件——ProviderRun 血统/record_provider_run、MarketPhaseContext/is_partial_bar、evaluate_decision_signal/decision_signal 三表评估链——在本地 base(650a1c3)与我方 factor_factory 均 grep=0**(git grep 逐项实测), 移植方向是 **上游冻结版 → 我方 factor_factory**, 与 fork 升级解耦——fork 永远不用升级。注意两个**非新增**的例外(修正初版"三切片完全不存在"的过强断言): ①切片1 的 `CircuitBreaker` 在本地 base 已存在(realtime_types.py L270@650a1c3, 默认 3/300/1), 且类体与冻结版 L284@d08374898c **逐字一致**(155 行 diff=0, 实测)——Wave1 移植可直接从本地盘取, 无需网络取上游; ②切片2 的 `get_effective_trading_date` 在 base 已有 214 行早期版(src/core/trading_calendar.py L119@650a1c3, 含同款 fail-open, docstring L71 自述"Fail-open: returns True if exchange-calendars unavailable"), 移植时同样必须做 §4.2 的 fail-closed 反转, 不论从哪个版本取。
3. **三切片验证全部通过, 依赖边界清晰可剥离**, 详见 §3-§5(接口形状+字段表+排雷+验收条件)。
4. **一个此前未记录的雷(修订定性)**: 上游 `_parse_text_response`(analyzer.py **L4649-4716**@d08374898c, 初版误写作 `_extract_from_text`, 该符号冻结版 grep=0, 特此更正) 在 JSON 解析失败时用关键词计数从纯文本**制造** sentiment_score(50/65/35 三档)+trend/advice/decision_type, 并经 `populate_decision_action_fields(result, align_with_score=False)` 把造出的分数对齐进动作字段。**但危险性需如实降级**: 冻结版在 L4711-4712 显式设 `success=False` + `error_message='LLM response is not valid JSON; analysis result will not be persisted'`, 即上游自带两道闸(失败标记+声明不落库), 造分结果不是"成功结果"。剩余风险面: 制造的分数/动作仍活在返回的 AnalysisResult 对象里, 任何**不检查 success 标志**的消费路径(如直接读 result.sentiment_score 渲染推送)会把伪造值当真——本审计未逐一核验所有消费点是否都检 success(见 confession)。结论不变: 若未来从上游取任何 analyzer 相关代码, 此函数列入**禁移植清单**(键名: `_parse_text_response`), 理由从"污染源头"降为"造分兜底模式与我方诚实性护栏哲学冲突, 且依赖消费端自觉检 success 才无害"。

---

## 1. 本地改动逐文件判决表

判决语义: **keep**=保留在冻结 fork 里不动; **backport**=该思想/代码值得进我方主系统或未来上游 PR; **rewrite**=保留意图但需按上游新结构重做(仅当未来真要升级 fork 时); **drop**=删除/放弃。

| # | 文件 | 本地改动内容 | 上游@d08374898c 状态 (churn=650a1c3 以来该路径 commits 数) | 判决 | 依据 |
|---|---|---|---|---|---|
| 1 | src/analyzer.py (工作区, +211行) | ①诚实性护栏: sentiment_score 缺失不再占位 50, 标 success=False; 次要字段占位记入 `placeholder_fields`/`is_placeholder_filled` 并进 to_dict; `_safe_sentiment_score` 缺失/null→None ②提示词加长(字段字数要求/“## 分析要求”块) | churn=26, 文件 106KB→204KB 近翻倍。**占位 50 反模式仍在**: `apply_placeholder_fill` L427 `result.sentiment_score = 50`; 解析 L4539 `int(data.get('sentiment_score', 50))`; 第三处 L4660 `_parse_text_response` 文本兜底也造 50(此处冻结版已标 success=False+声明不落库, 见 §0.4 修订定性)。上游 `check_content_integrity` L330 已会把 `sentiment_score is None` 判缺失(半收敛), 但解析默认 50 使其永不触发 | ①**keep(冻结fork) + 思想已在我方主系统实现**(v2 合同硬校验), 若升级 fork 必须 rewrite 到 3 个占位点(含新增的 L4660); ②提示词改动 **keep但修 bug**: 本地“## 分析要求”块被重复插入 3 次(grep=3, 上游=0), 冻结前应去重 | 上游未修 → 本地是净资产; 但 hunks 所在函数已重写(多语言 placeholder/ko、`_is_blank_text` 守卫), cherry-pick 不可行 |
| 2 | tests/test_report_integrity.py (+73行) | TestPlaceholderTransparency 5 个回归测试 | churn=3, 上游同文件已扩到 21KB(新增 TestApplyPlaceholderFill 等), 无同类测试 | **keep**, 与 #1 绑定同生命周期 | 我方护栏的验收测试, 冻结 fork 内自洽 |
| 3 | src/config.py (commit c7059b5 +6 行 gpt-5.4; 工作区再 +6 行 gpt-5.5, 均加进 `_FIXED_TEMPERATURE_LITELLM_MODELS`) | 给 GPT-5.x 钉 temperature=1.0 | churn=43。**`_FIXED_TEMPERATURE_LITELLM_MODELS` 已从 config.py 消失**, 迁至 `src/llm/generation_params.py` L18(只剩 kimi-k2.6); gpt-5 家族改由 `_should_omit_litellm_temperature` L189-196 **整体省略 temperature 参数**(gpt-5*/o1/o3/o4), 语义更优 | **drop(升级场景)** — 已被上游更好的方案取代; 冻结 fork 内 keep 不动(仍在用) | generation_params.py L189-196@d08374898c |
| 4 | requirements.txt (1 行) | json-repair `>=0.55.1`→`>=0.40.0`(本地环境装不到新版) | churn=14, 上游仍 `>=0.55.1`(L36) | **keep**(环境钉), 迁移无关 | 纯本地 venv 兼容 |
| 5 | src/data/stock_index_loader.py (2 行) | `str \| None`→`Optional[str]` + import | churn=3, 上游 L269/L284/L325 **仍用 `str \| None`** | **keep**; 根因治理见 §6 备注(系统 python3=3.9.6 vs venv=3.11.14) | 上游没修, 我方跑 3.9 就永远要补; 正解是统一用 venv 解释器 |
| 6 | src/data/stock_mapping.py (2 行) | 同上 | churn=0(上游从未再动) | **keep** | 最干净的一个: 零冲突面 |
| 7 | src/market_analyzer.py (4 行) | 同上 ×3 方法 | churn=16, 上游 L172/L370/L404 仍 `str \| None`, 三个方法签名原样存在 | **keep**; 注: 本地把 `from typing import Optional` 插在了 coding 声明注释之前(风格瑕疵, 无碍运行) | 同 #5 |
| 8 | src/notification.py (4 行) | 推送截断放宽: buy_reason 80→200, key_points 60→150, risk_warning 50→150, one_sentence 60→150 | churn=25, 文件 80KB→124KB; 但四个截断点原样存在(上游 L1682/L1687/L1692/L1760 仍 80/60/50/60) | **keep**; 若升级 fork 可无脑重放(锚点文本未变, 仅行号漂移 ~400 行) | 与 #1② 配套(字段加长后截断也要放宽) |
| 9 | docs/CHANGELOG.md (2 行) | 记录 #1 的变更 | churn=100(上游改动最频文件) | **keep**(文档) | 无冲突意义 |
| 10 | ai_analysis_with_retry.py (未跟踪, 10.5KB) | 自有: 逐股重试分析器, 依赖 `src.config.get_config`+`src.core.pipeline.StockAnalysisPipeline` | 两接口在 @d08374898c 均存活(config.py L3370, pipeline.py L165), 构造函数向后兼容(新参全 optional) | **drop→归档**: 已被 generate_prebreakout_ai.py(直连 litellm)取代, 生产链无引用 | 见 §6 桥接考古 |
| 11 | bridge_selection_to_analysis.py (未跟踪, 19.7KB) | 自有: 选股→分析桥 | 同上接口存活 | **drop→归档**: stage4_ai_backfill_loop.py 2026-07-08 起改调 generate_prebreakout_ai.py(其 L727-736 注释明说"替换易卡死的老桥接"); 仅 stage4 顶部常量 FISHER_BRIDGE_PATH 残留(死引用) | 同上 |
| 12 | fisher_bridge.py (未跟踪, 18.1KB) | 自有: 报告合并/回填 | 接口存活 | **半活, keep 待收口**: `scripts/stock_ai_backfill_incremental.py` L147 仍 `from fisher_bridge import ...`, 该脚本被 `scripts/stock_pipeline_gate.py` L24/L227 引用; gate 本身在 skills/scripts/ops 无调用点(疑仅手动触发)。收口动作: 确认 gate 退役后与 #10/#11 一起归档 | 唯一残存 import 链 |
| 13 | src/analyzer.py.bak.202605031208 (106KB) | 手工备份 | — | **drop**(git 本身就是备份) | 占空间且污染 status |
| 14 | src/notification.py.bak.202605031212 (80KB) | 手工备份 | — | **drop** | 同上 |

**判决表附带的 housekeeping 规格**(0.5 天, 属"上游迁移项目"Wave):
- A. 把 9 个工作区修改提交为一个本地 commit(建议 message: `local-freeze: honesty-guardrail + py39-compat + truncation-widen @base-650a1c3`), 提交前删除 analyzer.py 里重复的两个"## 分析要求"块。当前未提交状态一次误 `git checkout .` 就会丢掉诚实性护栏——这是本轨道发现的最紧急运维风险。
- B. 删 2 个 .bak; 归档 #10/#11 到 `patch/`(仓内已有此目录)或 factor_factory 附件; #12 待 gate 退役判定。
- C. 在 fork 根写一页 `FREEZE.md`: base=650a1c3, 冻结原因, 本判决表链接, "禁 git pull"声明。
- 验收: `git -C daily_stock_analysis status` 输出 clean(或仅剩 fisher_bridge.py 一个未跟踪); AI 链(19:30 管线 aiBackfill stage)连续 2 个交易日绿灯。

---

## 2. 冲突面总量结论(回答"整体 rebase 要多少)"

- 309 upstream commits; 本地 9 改文件的上游 churn: CHANGELOG 100 / config 43 / analyzer 26 / notification 25 / market_analyzer 16 / requirements 14 / stock_index_loader 3 / test_report_integrity 3 / stock_mapping 0。
- 内容级三方对比结论: 9 改中只有 analyzer.py 是**真冲突**(hunks 所在函数被上游重写为多语言版), 其余 8 个要么锚点原样存活(notification 四截断点、market_analyzer 三签名)、要么整块代码已消失(config 温度表)、要么零 churn(stock_mapping)。
- 但 rebase 的真实成本不在这 9 个文件, 而在**验证面**: 上游两个月内 storage schema 从 2174→3566 行(decision_signal 三表+索引迁移)、pipeline 从 59 行处类定义膨胀到 165 行处、新增 src/llm/ 子包与 60+ services——升级后我方 AI 链(venv 解释器+.env+生成器)全要回归。**性价比判决不变: 不升级**。

---

## 3. 移植切片一: provider 骨架(优先候选链 + provider run 血统 + 熔断)

### 3.1 冻结版结构(全部验证过, 引证 @d08374898c)

三件套解剖(与 Phase1 卡宣称一致, 完整边界如下):

1. **priority 候选链**: `BaseFetcher.priority: int = 99`(data_provider/base.py L342, 数字越小越优先) + `DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT`(L615-627, 每 fetcher 声明支持市场集合) + `get_daily_data` 主循环(L1242-1491): 归一化代码→市场路由(美股走专用 source_order 硬编码链 L1305-1314, 其余按 priority 循环)→逐源尝试。
2. **provider run 血统**: 每次尝试前 `record_provider_run_started`(L1415-1419), 成功/空/异常三分支各落一条 `record_provider_run`(L1428+/L1447+/L1464+)。字段契约=`ProviderRun` dataclass(src/services/run_diagnostics.py L143-159): `trace_id/data_type/provider/operation/success/latency_ms/error_type/error_message_sanitized/fallback_from/fallback_to/cache_hit/stale_seconds/record_count/created_at`。**关键设计**: 记录函数是 context-scoped 的(`get_current_diagnostic_context()` 为 None 即静默 no-op, L884-886)且外层 try/except 吞错(L906-907 "defensive fail-open guard")——观测永不阻塞取数。
3. **熔断**: `CircuitBreaker`(data_provider/realtime_types.py L284-…): 显式三态机 CLOSED→(连续 fail≥threshold)→OPEN→(冷却 cooldown_seconds)→HALF_OPEN→(单次成功)→CLOSED / (失败)→OPEN; 默认 `failure_threshold=3, cooldown_seconds=300, half_open_max_calls=1`(L305-313); 按 `daily_data:{market}:{fetcher}` 键粒度(base.py L798-799), RLock 保护, 类级单例挂在 manager(L628)。熔断中的源在循环里被跳过并记 `(CircuitOpen)` 错误串(L818-819)。

### 3.2 依赖边界(能否剥离)

base.py 顶部 import(L17-33): 标准库+pandas/numpy + `src.data.stock_index_loader`/`stock_mapping`(名称解析) + `src.services.market_symbol_utils` + `src.services.run_diagnostics` + 同包 fundamental_adapter/realtime_types。**三件套本身的最小可移植集 = realtime_types.CircuitBreaker(自包含, 仅 std lib) + ProviderRun dataclass + 循环模式**, 不需要名称解析/多市场/fundamental 任何部分。MIT, 可直接抄。**取用捷径(实测)**: `CircuitBreaker` 类体在本地 base 与冻结版逐字一致(155 行 diff=0; 本地 `git show 650a1c3:data_provider/realtime_types.py` L270 起 = 上游 L284@d08374898c 起), Wave1 实施可直接从本地盘取, 无需网络; 真正只能从上游冻结版取的是 ProviderRun dataclass + record_provider_run 三分支落点 + context-scoped 记录护栏(base grep=0)。

### 3.3 我方落地设计稿(只提升级差距, 对照 system_inventory §1)

我方现状: tushare 代理+akshare 双源, 粗粒度日志, **provider 候选列表无新鲜度契约**(inventory 明标 Phase4 治理)。GREENFIELD 死源复活(0710)与 0710 盘中裸跑污染两事故都属"源健康度无状态机+取数无血统"。

新组件 `factor_factory/data/provider_runtime.py`(建议):

```
ProviderAttempt 字段表(≈ProviderRun 裁剪+加严):
  run_id          str   # 接 run_manifest 的 run_id(替代其 trace_id)
  data_type       str   # daily|auction|minute|calendar|name
  provider        str   # tushare_proxy|akshare|...
  operation       str
  success         bool
  latency_ms      int
  error_type      str?  # 枚举: timeout|empty|schema|rate_limit|circuit_open|exception
  fallback_to     str?
  record_count    int?
  freshness_lag_days int?   # 我方新增: 返回数据最大日期 vs 期望有效交易日之差(>0 即陈旧)
  created_at      iso8601
```

差异化决定(反其道三条): ①记录 sink 不做 fail-open 吞错——写不进血统表要 WARN 到 pipeline_alert(我方哲学: 观测断了也是事故); ②新增 freshness_lag_days 字段, 直接服务新鲜度契约(上游没有此概念, 它允许陈旧兜底); ③熔断状态在进程内即可(我方是 cron 批处理, 单 run 内防抖动足够; 跨 run 持久化熔断反而会把夜间单次抖动放大成次日跳源)。

**验收条件**: (1) 任一日线拉取在 run_manifest 附表能查到完整尝试链(含失败尝试); (2) 人工注入 3 连失败后第 4 次调用被跳过并记 circuit_open, 300s 后半开恢复; (3) freshness_lag_days>0 时触发既有 pipeline_alert 硬告警; (4) 血统写入失败本身产生 WARN 告警。
**成本**: 1.5-2 天(含单测)。

---

## 4. 移植切片二: 日历语义(get_effective_trading_date / MarketPhaseContext / is_partial_bar)

### 4.1 冻结版结构(src/core/trading_calendar.py, 597 行, @d08374898c)

- `get_effective_trading_date`(L203-249): 非交易日→上一 session; 交易日收盘前→上一完成 session; 收盘后→当日 session。用 exchange-calendars `session_close` 精确到收盘时刻并转市场时区(L232-241)。**与我方 a6698e8"交易日落后判定加收盘感知"同构**, Phase1 卡判断正确。注: 本地 base 已含此函数的 214 行早期版(trading_calendar.py L119@650a1c3, 含同款 fail-open——docstring L71 自述"Fail-open: returns True if exchange-calendars unavailable"); 本切片真正的**新增**组件是 MarketPhase/MarketPhaseContext/is_partial_bar/build_market_phase_context(base grep=0)。
- `MarketPhase` 枚举(L76-86): premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown; 收盘竞价窗按市场硬编码(cn=3min, L61-68)。
- `MarketPhaseContext` dataclass(L88-123): `market/phase/market_local_time/session_date/effective_daily_bar_date/is_trading_day/is_market_open_now/is_partial_bar/minutes_to_open/minutes_to_close/trigger_source/analysis_intent/warnings[]` + `to_dict()` JSON 安全序列化。
- `is_partial_bar` 语义(L363-379 `_phase_booleans`): phase∈{intraday, lunch_break, closing_auction}→True; postmarket/premarket→False; **UNKNOWN→三布尔全 None**(不是 False!)。
- 组装器 `build_market_phase_context`(L461-527): 日历失败降级为 warnings 里的 `calendar_unavailable`/`calendar_error` 码, 不抛异常; 支持显式 analysis_phase 覆盖推断。
- 消费点: pipeline.py L331-338 断点续传(`_resolve_resume_target_date`→L2683 直调 get_effective_trading_date + `has_today_data` 跳过取数), L393 起 phase context 随分析管线传递。

### 4.2 fail-open 排雷表(移植时必须反转的语义, 逐处核实)

| 位置@d08374898c | 行为 | 我方处置 |
|---|---|---|
| is_market_open L163-165 | xcals 不可用→True(当开市) | **反转 fail-closed**: 日历不可得→拒跑+硬告警(我方已有此哲学, 移植时不得引入上游版) |
| is_market_open L166-168 | 未知市场→True | 我方只有 cn, 删市场参数, 不适用 |
| is_market_open L172-175 | 任意异常→True | 反转: 异常→抛给调度层 |
| get_effective_trading_date L216-224, L246-248 | xcals 不可用/异常→返回**自然日**(盘中相当于把 partial 当 complete) | 反转: 解析失败→返回 None/抛错, 由调用点 fail-closed |
| get_market_for_stock L130 docstring | 识别不了代码→None→"treat as open" | 不适用(cn only) |
| infer_market_phase L285-357 | **fail-closed→UNKNOWN**(刻意, docstring L291-295 自述与上两函数哲学相反) | 保留此半边 |
| _phase_booleans UNKNOWN→(None,None,None) | 消费者若把 None 当 False 会误判"非交易日" | 我方版本 UNKNOWN 时直接硬告警, 不产 None 三元组 |

### 4.3 我方落地设计稿

我方现状(inventory §4): 收盘感知基线已在 pipeline_alert(a6698e8), 盘中裸跑护栏是**分散的**(各消费点各自判)。升级差距 = 把护栏收敛成一个随 run 传递并落 manifest 的结构。

`factor_factory` 新增 `PhaseContext`(cn 单市场裁剪版, 字段表):

```
phase                    enum: premarket|intraday|lunch_break|closing_auction|postmarket|non_trading  # 无 unknown: 推断失败即 abort
now_local                iso8601 (Asia/Shanghai)
effective_trade_date     YYYYMMDD   # 收盘感知有效交易日(即我方既有基线的结构化形态)
is_partial_bar           bool       # phase∈{intraday,lunch_break,closing_auction}
calendar_source          str        # akshare|cached
warnings                 [str]
```

接入点(各一行判断): ①19:30 管线 stage0 生成一次, 写入 run_manifest; ②盘中裸跑护栏改读 `is_partial_bar`(替代散装时间判断); ③backtest_cache 写入前断言 `not is_partial_bar or explicit_intraday_flag`(直接堵 0710 缓存污染事故的结构性复发面); ④pipeline_alert 落后判定改读 `effective_trade_date`。
日历源: 我方 akshare 交易日历(已主用)——**不引入 exchange-calendars 依赖**, 收盘时刻 cn 固定 15:00, 竞价窗 14:57-15:00, 无需 session_close 查询。
**验收条件**: (1) 14:59/15:01 两个时点跑管线, manifest 里 phase 分别为 closing_auction/postmarket 且 effective_trade_date 分别为前一交易日/当日; (2) 节假日跑→non_trading+管线拒绝进入取数 stage; (3) 日历取不到→进程退出码非 0+Discord 告警(fail-closed 验证); (4) 现有分散护栏删除后 0710 型事故回放测试仍被拦截。
**成本**: 0.5-1 天。

---

## 5. 移植切片三: 结构化决策评估(evaluate_decision_signal + 存储链)

### 5.1 冻结版结构(@d08374898c, 全链验证)

- **纯函数核**: `BacktestEngine.evaluate_decision_signal`(src/core/backtest_engine.py **L276-365**, 议程给的 L276-285 是签名区): 输入 `direction_expected/anchor_date/start_price/forward_bars/EvaluationConfig(eval_window_days, neutral_band_pct=2.0, engine_version)`; docstring 原文 "Evaluate a structured DecisionSignal action **without text inference**"。不足窗/坏锚价/坏收盘价→`eval_status="unable"+unable_reason` 枚举(invalid_anchor_price/insufficient_forward_bars/missing_end_close/invalid_end_close), 正常→`eval_status="completed"+outcome+direction_correct+start/end/max_high/min_low/stock_return_pct`。
- **方向语义**(_classify_signal_outcome L643-675): `up`(±band 三分 hit/miss/neutral)、`not_down`(r≥0 hit, r≤-band miss)、`not_up`(r≤band hit, 否则 miss)。动作→方向映射在服务层(decision_signal_outcome_service.py L442-449): buy/add→up, hold→not_down, reduce/sell/avoid→not_up。
- **存储链**(src/storage.py): `DecisionSignalRecord`(L984-1076, 表 decision_signals): 结构化动作资产——`action/action_label/confidence/score/horizon/entry_low/entry_high/stop_loss/target_price/invalidation/watch_conditions/reason/risk_summary/evidence_json/plan_quality/status/expires_at` + 5 组复合索引; `DecisionSignalOutcomeRecord`(L1078-1116, 表 decision_signal_outcomes): **UniqueConstraint(signal_id, horizon, engine_version)**——同一信号×窗口×引擎版本恰好一行, 引擎升级=新 engine_version 全量重评而不覆盖旧口径(可比性设计, 全切片最值得抄的一笔)。
- **服务层纪律**(decision_signal_outcome_service.py): `SUPPORTED_OUTCOME_HORIZONS={1d:1,3d:3,5d:5,10d:10}`(L36-41); `RETRYABLE_UNABLE_REASONS`(L48-54)把 unable 分为可重试(行情迟到类)与永久(non_directional_action/unsupported_horizon/missing_anchor_date), 重跑只捡可重试的——增量回填的幂等基础。
- 上游自证: 该链是对 Phase1 卡反模式#1(文本反推意图)的官方替代路线, 锚定文本推断的旧 `evaluate_single`(L158)仍并存但新链完全绕开。

### 5.2 与我方的重叠与净差距(防重复建设审查)

我方已有(inventory §3): 发布合同 v2 17 族硬校验(AI 只产结构化字段, final_action 禁买入级)、O2C/三策略统一复盘+真指数基准超额、review_gate 6 条件。**方向判定/复盘计算本身不缺**。净差距只有三样:
1. **unable 语义结构化**: 我方复盘对"评不了"(停牌/上市不足/行情迟到)目前是跳过或 ad-hoc 标记, 无 `eval_status/unable_reason/retryable` 三元契约——回填幂等靠脚本自觉。
2. **engine_version 口径版本化**: 我方复盘口径改过(如真基准接入#27), 历史行没有版本列, 新旧口径混在一起。
3. **(signal, horizon, engine_version) 唯一键**思想: 我方按笔/按日争议(S3 60 笔裁判待用户拍板)之外, 复盘行的幂等键并未显式建模。

### 5.3 落地规格(borrow shape, not code)

对我方 `recommendations.db` 复盘表(及 O2C review 表)做一次 additive migration:
```
+ eval_status     TEXT NOT NULL DEFAULT 'completed'   # completed|unable
+ unable_reason   TEXT NULL                           # 枚举同上游+我方补: suspended|delisted|halted_limit
+ engine_version  TEXT NOT NULL DEFAULT 'review-v1'   # 现口径回填 review-v1
+ UNIQUE(rec_id, horizon, engine_version)
```
回填脚本改造: 只重试 RETRYABLE 集合; 口径变更时 bump engine_version 而非 UPDATE 旧行。
**验收条件**: (1) 停牌样本产生 unable 行而非缺行, 复盘覆盖率分母可解释; (2) 同一 rec_id×horizon 重跑不产生第二行(唯一键冲突→跳过); (3) 模拟口径升级 review-v2 后, v1 行原样保留且前端/报表按版本过滤; (4) 与红线1兼容——前瞻账本历史行只读, 新列仅 additive。
**成本**: 1 天(migration+回填脚本改造+单测)。

---

## 6. 桥接考古与 fork 的真实生产角色(划界内顺带查清)

- fork 对我方生产链的**活依赖只有两样**: `daily_stock_analysis/venv/bin/python3`(3.11.14, 被 stage4_ai_backfill_loop.py L32 与两个生成器指定为解释器)和 `daily_stock_analysis/.env`(LLM 凭据, generate_o2c_ai.py L45)。**src/ 代码本身已不在关键路径上**(2026-07-08 起 aiBackfill 改调 generate_prebreakout_ai.py 直连 litellm)。
- 残存 import 链: `scripts/stock_ai_backfill_incremental.py` L147 → fisher_bridge(见判决表 #12)。
- `str | None` 补丁的根因: 系统 python3=3.9.6 而 venv=3.11.14——凡不经 venv 跑的入口都会炸。治本=所有入口统一 venv 解释器(已基本做到), 而非继续给上游文件打 Optional 补丁。
- 推论: fork 冻结后可进一步**降格为"venv+.env 宿主"**, src/ 树完全只读。这使"永不升级"从权衡变成零成本事实。

---

## G条目闭合声明

- **G1(上游迁移决策缺 delta 实证)**: **闭合**。①309 commits 实测精确(compare API ahead_by=309); ②本地 9改+5自有逐文件冲突面已产出判决表(§1, 内容级三方对比而非仅 stat); ③三个移植切片在冻结 SHA 下全文/关键路径验证完毕, 依赖边界与 fail-open 雷区落表(§3-§5); **三切片的新增组件(ProviderRun 血统链/MarketPhaseContext+is_partial_bar/decision_signal 评估存储链)在本地 base(650a1c3)与我方 factor_factory 均 grep=0 实测**——初版"三切片在 base 完全不存在"为过强断言, 已更正: CircuitBreaker 在 base 逐字存在(可直接本地取用, 见 §3.2), get_effective_trading_date 在 base 有含同款 fail-open 的早期版(见 §4.1); 对新增组件而言"移植与 fork 升级解耦"结论成立; ④Phase1 卡的迁移建议(冻结 detached+定点移植三块)经实证**维持**, 并新增两条修正: config 温度补丁已被上游取代应 drop、未提交工作区是最紧急运维风险应立即固化提交。唯一未按字面完成的是"逐 commit 对比"——以逐路径 commit 计数(9 文件 churn 表)+内容级三方 diff 替代, 决策信息量等价或更高, 理由见 confession。

## coverage_confession

- **未做逐 commit 走查**: 本地无上游 history(fetch 会拉大量对象, 与磁盘纪律冲突; gh api 逐 commit 读 309 个 commit 详情约 300+ 次调用, 收益低)。替代法: compare API 总量 + 每路径 commits API 计数 + 冻结版全文对读。若某个中间 commit 引入后又撤销了与我方相关的行为, 本审计看不见(终态对比盲区), 但对迁移判决无影响。
- **analyzer.py(204KB/4600+行)未通读**: 只精读占位/完整性/解析/文本兜底/提示词锚点五个区域。上游新增的 phase_decision 字段族、多语言(ko)体系、signal_attribution 只见其名。`_parse_text_response` 本次修订已通读全函数体(L4649-4716, 含关键词表与 success=False/不落库两道闸); **未核**的是其两个调用点(L4505/L4587)下游所有消费路径是否都检查 success 标志——若存在不检 success 直接读 sentiment_score 的渲染/推送路径, §0.4 的危险定性需再升回一档。
- **config 体系按划界只读到切片依赖边界**: config.py 只核温度/get_config/setup_env 三处 + src/llm/generation_params.py; config_registry/system_config_service 未碰。agent/webui/api/notification 全栈按议程排除(notification.py 仅读 4 个截断点)。
- **storage 链**: 只精读 decision_signal 三表模型+outcome service+extractor 主路径; decision_signal_repo.py(21KB)/decision_signal_service.py(50KB)未读, 上游 signal 的 status 流转(active→expired/invalidated/closed)细节未核。
- **run_diagnostics 持久化终点未追**: 确认 context-scoped 记录+吞错护栏, 但 context 最终落 SQLite 表还是报告 JSON 未验证(对我方设计稿无影响, 我方直接落 run_manifest 附表)。
- **上游测试文件**(test_trading_calendar.py/test_backtest_engine.py/test_run_diagnostics_p*.py)确认存在但内容未读——若移植切片实施, 应先读其边界断言当验收基准(各 1-2 小时)。
- **未跑任何本地测试**; 5 个自有文件读了 import+关键函数, 未逐行审计其 48KB 全文。
- 不确定: stock_pipeline_gate.py 是否仍有隐蔽调度入口(openclaw.json 解析未命中, 但 gateway cron 的实际 job 定义我未能定位)——影响判决表 #12 fisher_bridge 的最终归档时点。

## Wave 映射

| 产出 | 映射 | 粗成本 |
|---|---|---|
| §1 判决表 + housekeeping(提交固化/去重/归档/FREEZE.md) | 上游迁移项目 | 0.5 天 |
| §3 provider 骨架(ProviderAttempt 血统+熔断+freshness_lag_days) | **Wave1 数据可信**(兼 Phase4 provider contract 的先行实现) | 1.5-2 天 |
| §4 PhaseContext(cn 裁剪版, fail-closed 反转, 堵 0710 缓存污染复发面) | **Wave1 数据可信** | 0.5-1 天 |
| §5 复盘表 eval_status/unable_reason/engine_version/唯一键 migration | **Wave2 组合风险契约**(复盘度量的可比性地基; 亦服务 Wave3 消融时的口径隔离) | 1 天 |
| §0.4 `_parse_text_response` 禁移植清单 + 上游占位 50 三点位记录 | **Wave3 AI 消融**(负面契约: 任何消融基线不得含文本推断/伪中性兜底, 即使上游版自带 success=False 闸) | 0 天(决策记录) |
| §6 fork 降格为 venv+.env 宿主 | 上游迁移项目 | 随 housekeeping 顺带 |

---
*引证约定: 上游一律 path#Lx-Ly@d08374898c; 本地为 working-tree 或 @c7059b5; 本地 base 引证为 path#Lx@650a1c3(经 `git show 650a1c3:<path>` 读取)。原始下载快照留存于 scratchpad(会话级, 不入库)。*

*修订记录(2026-07-11, 对抗复核后 r2): ①禁移植清单符号名更正 `_extract_from_text`→`_parse_text_response`(def L4649@d08374898c, 原行号/行为描述不变, 三处引用均改); ②该函数危险定性降级——冻结版 L4711-4712 实测含 success=False+"will not be persisted"两道闸, "污染源头/成功结果返回"表述撤回, 禁移植结论保留但改列理由; ③"三切片在本地 base 完全不存在"过强断言更正为"三切片新增组件在 base 与 factor_factory 均 grep=0"——实测 base 650a1c3 已含逐字一致的 CircuitBreaker(realtime_types.py L270, 155 行 diff=0)与早期版 get_effective_trading_date(trading_calendar.py L119, 214 行, 含同款 fail-open), 并据此新增"Wave1 熔断器可直接本地取用无需网络"的实施捷径。三处修正均经本轮 git show/curl raw/grep 复测。*
