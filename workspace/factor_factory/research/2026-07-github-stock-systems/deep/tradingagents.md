# Tier-A 深读: TauricResearch/TradingAgents — G2 补账(辩论内核全读)

- 冻结 SHA: `01477f9afb7a47b849ed4c9259d3a9a4738d9fda` (v0.3.1, 2026-07-05 push); 引证格式 `path#Lx-Ly@01477f9afb`
- 许可: Apache-2.0 —— 可借鉴可改写, 本报告允许含代码/提示词片段
- 任务: PHASE1_GAPS.md G2 —— "辩论提示词/工具内核整块未读; C 方案设计不得引用 Phase1 卡做依据"。本轮逐行读完(全部经 raw.githubusercontent 按冻结 SHA 取原文): agents/ 全部 13 个角色文件、agents/schemas.py、agents/utils/ 的 agent_utils.py + 7 个拆分工具文件 + agent_states/rating/structured/memory、graph/ 9 文件中的 7 个核心文件(setup/conditional_logic/analyst_execution/propagation/reflection/signal_processing/trading_graph; **checkpointer.py 本体与 __init__.py 未读**, checkpointer 仅经 trading_graph 调用面, 见 confession)、根目录 default_config.py。
- 修订 v2(2026-07-11): A 股 tradability/停牌语义入 EvidencePack(§3.2)、后验度量声明继承去生存者偏差口径(验收 3a)、T+1 约束适配裁决新增 §3.3、graph/ 阅读范围表述与 schemas.py 等引证路径修正。
- 修订 r3(2026-07-11, 采纳对抗复核 v2 唯一拦截项): 验收 3a 与 §3.3 的度量口径改为两层——日历对齐/停牌 ffill 两条**继承**#38 既有实现(引证核实属实), 出场侧跌停锁死与 T+1 起算两条改标**新增设计要求**(复核实测我方 scripts 无此实现, o2c_strategy_backtest.py L244 自白为现状缺口证据), 消除"继承"夸大。
- 修订 r4(2026-07-11, 采纳对抗复核 r3 三条 inventory_check): §6 微机制经实读我方 generate_o2c_ai.py / generate_prebreakout_ai.py 逐条核对后改判——**M1 降级为"无行动"**(我方无结构化输出调用且等价 None 守卫已存在), **M2 降级为"无作用面"**(现行两 schema 均无可选数值字段, prebreakout ai_score 的 clamp 已覆盖), **M3 收窄**(名称/行业注入已存在, 真实差距仅"不得替换标的"硬指令±板块字段); §6 表头、Wave 映射表、§2 与 §3.2 中两处"直接可用"表述同步改写。教训: 微机制立项前必须实读自家落点代码, 不得凭 inventory 摘要判缺口。
- 结论先行: **辩论内核的机制含量远低于其 9.2 万星声誉**。默认每场辩论只有 1 轮、无收敛检测、无风险 veto、裁判只读辩论文本。该仓真正的工程价值(verified snapshot / 结构化输出 / 确定性 parse / 无静默回退路由)全部独立于辩论且 Phase1 已收割。**判决: 完整 C 方案不值得做; 消融缩减为 A vs B(+B 内嵌单调用自辩变体), 理由见 §5。**

---

## 1. 产出①: 角色-提示词-工具矩阵(逐文件判决表)

图拓扑(setup.py#L113-154@01477f9afb): `START → [4 分析师串行, 各自带工具循环+消息清除] → Bull ⇄ Bear → Research Manager → Trader → Aggressive → Conservative → Neutral(轮转) → Portfolio Manager → END`。分析师串行执行(并行旋钮 v0.3.0 作为 no-op 移除, CHANGELOG #979)。

双 LLM 分级(setup.py#L83-92): **deep_think 只给两个裁判**(Research Manager / Portfolio Manager), 其余全部 quick_think(默认 `deep=gpt-5.5, quick=gpt-5.4-mini`, default_config.py#L82-83)。

| 角色 | 文件 | LLM | 工具(bind_tools) | 提示词核心指令 | 输出形状 | 对我方判决 |
|---|---|---|---|---|---|---|
| Market Analyst | agents/analysts/market_analyst.py#L25-53 | quick | get_stock_data, get_indicators, get_verified_market_snapshot | 从固定菜单(11 个指标, 每个带用法+陷阱注解)选≤8 个互补指标; **写报告前必须调 verified snapshot 并把它当唯一事实源, 冲突时"标注差异而非编造调和数"**(L51); 结尾附 markdown 汇总表 | 自由文本 report(仅当无 tool_calls 时收割, L87-88) | **指标菜单+快照合同两段可改写**给我方 AI 注释器; 指标本身我方 parquet 已有 |
| Sentiment Analyst | agents/analysts/sentiment_analyst.py#L131-183 | quick | **无工具调用** —— news/StockTwits/Reddit 三源在 LLM 调用前确定性预取, 以 `<start_of_X>...<end_of_X>` 定界块注入提示词(L66-80) | 8 条分析纪律: 多空比作领先信号+样本量声明、跨源背离本身是信号、按互动量加权、区分观点与事件、**"对数据极限诚实"(占位符→confidence=low)**、"过去情绪不预测价格" | 结构化 SentimentReport{band 6 档, score 0-10, confidence 3 档, narrative}(agents/schemas.py#L273-325) | 源全是美股社媒→剪; **"预取注入+定界块+confidence 绑数据质量"模式可借**; 前身因"提示词要社媒但工具只有新闻"导致 LLM 编造 Reddit 内容(#557, 模块 docstring L3-7)——工具与提示词承诺必须一致的实证教训 |
| News Analyst | agents/analysts/news_analyst.py#L28 | quick | get_news, get_global_news, get_macro_indicators(FRED), get_prediction_markets(Polymarket) | 写"过去一周对交易相关的世界状态"报告; 提示词内联每个工具的参数语义 | 自由文本 report | 美股宏观特化→剪; 注意 ToolNode 装了 get_insider_transactions 但分析师未 bind(见 §1.1 尸检) |
| Fundamentals Analyst | agents/analysts/fundamentals_analyst.py#L26-29 | quick | get_fundamentals, get_balance_sheet, get_cashflow, get_income_statement | 一段泛泛的"写全面基本面报告"; 无 PIT 语义提醒(PIT 全靠 dataflows 层过滤, 且锚错为 fiscalDateEnding——Phase1 反模式#1) | 自由文本 report | 剪; 我方基本面走 PEAD 公告日 PIT 契约 |
| Bull Researcher | agents/researchers/bull_researcher.py#L27-45 | quick | 无 | "为投资该股构建最强证据链"; 5 个论证维度(成长/护城河/正面指标/反驳空方/对话式交锋); 输入=4 报告全文+辩论 history+空方最新发言 | 自由文本, 前缀 `Bull Analyst:` 拼进 history | **C' 若立项, 此对是唯一值得保留的辩论对**(改写为基于我方冻结证据包) |
| Bear Researcher | agents/researchers/bear_researcher.py#L27-47 | quick | 无 | 镜像: 风险/竞争弱点/负面指标/拆穿多方过度乐观 | 同上 | 同上 |
| Research Manager | agents/managers/research_manager.py#L25-43 | **deep** | 无 | 裁判: 5 档评级定义表(Buy/Overweight/Hold/Underweight/Sell 各带语义)+"该表态就表态, Hold 只留给证据真正均衡时"; **输入只有辩论 history, 不含 4 份原始报告**(信息瓶颈, 全靠辩手转述) | 结构化 ResearchPlan{recommendation 枚举, rationale, strategic_actions}(agents/schemas.py#L73-102) | 评级语义表+"反 Hold 倾向"指令可借; 信息瓶颈是反面教材 |
| Trader | agents/trader/trader.py#L28-49 | quick | 无 | 把投资计划翻译成交易提案; 输入只有 investment_plan(不见报告不见辩论) | 结构化 TraderProposal{action 3 档, reasoning 2-4 句, entry_price?, stop_loss?, position_sizing?}(agents/schemas.py#L121-155); 渲染尾行保留 `FINAL TRANSACTION PROPOSAL: **X**` 兼容 grep | **剪**: 我方无执行, entry/stop 数字由无行情工具的 LLM 凭空给出——正是我方红线要防的"AI 产数" |
| Aggressive 风险辩手 | agents/risk_mgmt/aggressive_debator.py#L24-37 | quick | 无 | "积极拥护高风险高回报, 质疑保守与中立的谨慎错失机会"; 输入=trader 提案+4 报告+risk history+另两人最新发言; "无特殊格式, 像说话一样输出" | 自由文本拼 history | **剪且引以为戒**: 一个专职鼓吹加仓的角色与我方诚实性规范对冲 |
| Conservative 风险辩手 | agents/risk_mgmt/conservative_debator.py#L24-37 | quick | 无 | "保护资产, 指出决策让公司暴露于不当风险之处, 逐点反驳" | 同上 | **唯一有残值的风险角色**: 改写为"只许追加 ai_risks/只许降级"的红队 pass |
| Neutral 风险辩手 | agents/risk_mgmt/neutral_debator.py#L24-37 | quick | 无 | "指出双方各自过度之处, 主张平衡" | 同上 | 剪: 修辞填充, 无决策权重 |
| Portfolio Manager | agents/managers/portfolio_manager.py#L42-64 | **deep** | 无 | 终裁: 5 档评级表+研究计划+trader 提案+**past_context 记忆注入(仅此角色可见, L35-40)**+risk 辩论 history; "果断, 每个结论锚定分析师的具体证据" | 结构化 PortfolioDecision{rating 枚举, executive_summary, investment_thesis, price_target?, time_horizon?}(agents/schemas.py#L188-228) | 评级产出=verdict 本体→与我方红线正面冲突(§4); 结构化+字段描述即输出指令的模式可借 |

### 1.1 工具层全貌(agent_utils.py 及 7 个拆分文件, G2 点名对象)

`agent_utils.py#L9-47@01477f9afb` 现在只是**聚合门面**: 数据工具已拆到 7 个文件, 每个工具体统一为一行 `return route_to_vendor("<category_method>", *args)`——工具语义=类别路由键, 数据源可换而工具面不变(与 Phase1 卡对 route_to_vendor 的机制描述完全吻合, 此处闭环):

| 工具文件 | 工具 | 签名要点 | 备注 |
|---|---|---|---|
| core_stock_tools.py#L8-24 | get_stock_data(symbol, start, end) | OHLCV 字符串化 DataFrame | |
| technical_indicators_tools.py#L9-35 | get_indicators(symbol, indicator, curr_date, look_back=30) | **L26-28: LLM 常传逗号分隔多指标→自动 split 逐个路由, 单个失败以错误字符串占位不炸整调用** | 工具参数防 LLM 误用的实战补丁 |
| market_data_validation_tools.py#L8-23 | get_verified_market_snapshot(symbol, curr_date, look_back=30) | docstring 即角色合同: "对精确数字主张先调我, 当唯一事实源" | Phase1 已收割的核心机制的工具面 |
| fundamental_data_tools.py#L25-79 | get_fundamentals / balance_sheet / cashflow / income_statement(ticker, freq, curr_date=None) | curr_date 默认 None→PIT 责任下沉到 dataflows | |
| news_data_tools.py#L8-60 | get_news / get_global_news / get_insider_transactions | global_news 的 look_back/limit 缺省继承 config | |
| macro_data_tools.py#L9-36 | get_macro_indicators(FRED 别名表) | 美股特化 | |
| prediction_markets_tools.py#L9-31 | get_prediction_markets(Polymarket) | 美股/事件特化 | |

非数据类角色辅助(agent_utils.py 本体): ① `resolve_instrument_identity`(L78-119)——**运行开始时用确定性查询解析"公司名/行业/交易所"注入所有提示词, 防 LLM 从价格形态幻想成另一家公司(#814)**, lru_cache+fail-open; ② `build_instrument_context`(L122-169)——"用这个精确 ticker, 保留后缀, 除非工具证伪否则不得换公司"; ③ `create_msg_delete`(L190-214)——分析师间清上下文, 占位消息**不能是裸 "Continue"**(有 provider 会把它当任务本体, #888), 必须锚定 ticker+日期; ④ `get_language_instruction`(L52-65)——输出语言可配置但**内部辩论默认英文保推理质量**(default_config.py#L107-108)。

尸检两处(工具面与图的脱节, 均无害但说明维护脱节): "social" ToolNode 装了 get_news 但 Sentiment Analyst 根本不做工具调用(trading_graph.py#L203-208 vs sentiment_analyst.py L102 注释"No bind_tools"), 死节点; "news" ToolNode 装了 get_insider_transactions(trading_graph.py#L209-218)但 news analyst 未 bind(news_analyst.py#L20-25), LLM 永远调不到——**ToolNode(可执行面)与 bind_tools(可见面)是两张清单, 会静默漂移**, 我方若做 C' 应单清单双用途。

分析师系统提示词共享一段协作前导(market_analyst.py#L62-70): "你在与其他助手协作…若有 FINAL TRANSACTION PROPOSAL 前缀则团队知道停止"——**纯化石**: 实际路由只看 `last_message.tool_calls`(conditional_logic.py#L14-50), 该停止信号对图流转零作用。

---

## 2. 产出②: 辩论收敛机制 —— 几轮/怎么裁决/veto 在哪

**几轮**: 纯计数器封顶, 无任何内容感知。
- 研究辩论: 每条发言 count+1, `count >= 2*max_debate_rounds` → Research Manager(conditional_logic.py#L52-61)。**默认 max_debate_rounds=1(default_config.py#L110)→全场只有多方 1 条+空方 1 条**。代码注释"3 rounds of back-and-forth"是陈旧错误注释(实际=2×rounds 条消息)。
- 风险辩论: `count >= 3*max_risk_discuss_rounds` → PM(#L63-73); 默认 1→ Aggressive/Conservative/Neutral 各 1 条共 3 条。
- 顺序钉死: 多方永远先手(分析师链末端 clear 节点直连 Bull Researcher, setup.py#L135); 风险顺序由 latest_speaker 字符串前缀轮转 A→C→N→A(#L69-73)。**无提前终止、无一致性检测、无投票、无置信度聚合**; #1088 修的是 path_map 不全导致的崩溃, 不是收敛逻辑。

**怎么裁决**: 两级"独任裁判", 都是 deep LLM 一次结构化调用。
- Research Manager: 只读辩论转录, 产 ResearchPlan(5 档枚举); 提示词的裁决指令全文只有一句约束: "Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced"(research_manager.py#L38)。
- Portfolio Manager: 读风险转录+研究计划+trader 提案+记忆, 产 PortfolioDecision(5 档枚举)。
- 信号提取: `parse_rating` 确定性两遍正则(先找 `Rating: X` 标签行, 再退化为全文第一个评级词, 都无则**默认 "Hold"**——agents/utils/rating.py#L28-48)。结构化输出保证渲染后必有 `**Rating**:` 头, 所以正常路径确定; 但 structured 失败会**回退自由文本**(agents/utils/structured.py#L72-79), 此时默认 Hold 兜底=把解析失败洗成中性信号, 静默失败谱系的一员。
- 值得注意的真修复: structured 调用返回 None(思考型模型答了文本没调 schema 工具)被显式当 miss 抛出再回退(agents/utils/structured.py#L64-70); 可选数值字段的 "None"/"N/A"/"TBD" 字符串在 validator 前置强转 None(agents/schemas.py#L30-36, #1058)。**r4 核对收窄**: 我方现行生成器不走结构化输出(全 scripts grep response_format/with_structured_output/json_schema 零命中), 走自由文本+extract_json+显式失败(generate_o2c_ai.py L369-371 obj is None→json_parse_failed 重试后落 ai_failed; generate_prebreakout_ai.py L128-135 同构), 等价保护已在位——这两条的适用面是**未来若迁移到结构化工具调用(如 C' 立项)时的设计纪律**, 不是现行链路的缺口(§6 M1/M2)。

**风险 veto 在哪: 不存在。** 全图无一处允许风险角色阻断或强制降级——三个风险辩手只产生修辞文本, PM 可全盘无视; PM 产出评级后无任何下游校验其与 conservative 论点的一致性; 图上 `Portfolio Manager → END`(setup.py#L154)。所谓"风控角色"是**辩论修辞里的风控, 不是控制流里的风控**。与 freqtrade protections(真锁表+入场闸)或 Nautilus RiskEngine(typed deny)相比, 这是三种"风控"中唯一不具备强制力的。README 宣称的"订单送模拟交易所"依旧无实体(Phase1 反模式#2, 本轮包树复核维持)。

**记忆/反思环**(闭环全貌, 补 Phase1 卡): PM 独家可见 `past_context`=同 ticker 最近 5 条完整决策+跨 ticker 3 条教训(agents/utils/memory.py#L70-95); 下次同 ticker 运行开局先补算 pending 条目的固定 5 日收益+基准超额(trading_graph.py#L296-334), quick LLM 按"引用 alpha 数字/论点哪部分成立/一条教训"模板写 2-4 句(reflection.py#L14-29)回写 markdown 日志。单样本、固定持有期、无显著性——Phase1 反模式#3 维持, C' 不引入。

**成本形状**(消融预算依据): 默认配置一次运行 ≈ 4 个分析师阶段(各 1-3 次 LLM+工具往返) + 2 辩论 + 1 RM + 1 Trader + 3 风险 + 1 PM ≈ **12-16 次 LLM 调用/票**; 且每个辩手提示词都整段重嵌 4 份完整报告+滚动 history(bull_researcher.py#L37-43 等), 轮数↑时 token 近似平方增长。我方现行 AI 注释为 1 调用/票。

---

## 3. 产出③: 映射到我方 Wave3 消融 —— 剪裁判决与最小 C' 设计稿

### 3.1 逐角色剪裁判决

| 他方角色 | 判决 | 理由 |
|---|---|---|
| Market Analyst 工具循环 | 剪(机制已收) | 我方指标在 parquet 特征层确定性预算, 不需要 LLM 选指标调工具; verified snapshot 合同(Phase1 已立项)即其全部残值 |
| Sentiment/News/Fundamentals Analyst | 剪 | 数据源全为美股(StockTwits/Reddit/FRED/Polymarket/AlphaVantage/yfinance); 我方证据包由确定性管线生成 |
| Bull/Bear Researcher | **改写保留(若 C' 立项)** | 对抗性双视角是该仓相对我方单次生成的唯一净新机制 |
| Research Manager | 并入"裁判" | 两级裁判在我方无 Trader/无执行的场景下没有存在理由, 合并为一级 |
| Trader | 剪 | 无执行域; 且其 entry/stop 由无行情工具的 LLM 产数, 违我方"AI 不产数"边界 |
| Aggressive/Neutral 风险辩手 | 剪 | 鼓吹加仓的角色与诚实性规范对冲; 中立是填充 |
| Conservative 风险辩手 | **降维保留为红队 pass(可选)** | 改造成"只许追加风险/只许建议降级"的单向角色 |
| Portfolio Manager | 改写为"注释裁判" | 产出改为 ai_* 注释字段, 绝不产 verdict(§4) |
| 记忆/反思环 | 剪 | Phase1 反模式#3; 我方复盘走 review_gate 统计口径 |
| crypto 分支/benchmark_map/多语言 | 剪 | asset_type=stock 定死; 基准我方已有真指数复盘 |

### 3.2 最小 C' 内核(若立项): 3 调用, 接口形状

**输入 EvidencePack(确定性代码冻结, AI 只读)** —— 对齐他方"4 报告+verified snapshot+instrument identity"的注入面, 但全部换成我方已有资产:

| 字段 | 类型 | 来源 | 对应他方机制 |
|---|---|---|---|
| symbol, name, industry, board | str | stock_list 双源(23ab7ef) | resolve_instrument_identity 防串股(#814) |
| trade_date, data_as_of | str | 收盘感知管线(a6698e8) | trade_date="now"锚定 |
| frozen_quant | {total_score, factor_scores{}, rank, verdict} | 冻结分数, **哈希封印** | (无对应——他方评级即产出, 我方是只读输入) |
| verified_snapshot | {last_ohlcv, indicators{}, recent_closes[]} | parquet 渲染(Phase1 已立项) | build_verified_market_snapshot |
| strategy_context | {strategy, holding_days, entry_rule, t_plus_1: true} | O2C/prebreakout manifest | instrument_context(+T+1 被迫持有窗口声明, §3.3) |
| tradability | {is_suspended, limit_status∈{none, limit_up, limit_down, one_word(一字)}, consecutive_limit_days, entry_executable, block_reason?} | 面板日线 OHLC/pct + late_features 涨跌停判定(确定性代码产出, AI 只读) | **无对应——他方美股域无涨跌停/停牌/T+1 概念, A 股适配净新增(§3.3)** |
| data_confession | {missing[], stale[], suspension: {is_suspended_asof, last_trade_date}} | data_adapter | sentiment 的 confidence 绑数据质量纪律 |

**停牌语义显式承载(上一版评审否决点修正)**: 停牌不归入 missing——它是"约束事实"而非"数据缺失"。停牌候选的行情/快照字段以 `last_trade_date` 为真实 as_of 并在 `data_confession.suspension` 显式声明, 禁止静默 ffill 冒充新鲜数据; `verified_snapshot` 渲染时对停牌股标注 "as of last_trade_date"。一字/连板/停牌等约束事实必须进 pack: 否则 bull/bear 会对根本买不进(一字板)或撤不出(停牌)的候选进行盲辩, 直接掏空 C'/B+ 唯一立项假设"风险提示质量"的地基。

**三次调用**(quick=mimo, judge 可用同模型加深推理):
1. bull_case = LLM(pack, 角色="为该候选写最强支持论证, 精确数字只准引 verified_snapshot")
2. bear_case = LLM(pack, bull_case, 角色="逐点拆穿+列可证伪风险, 每条风险须绑 pack 内证据或明示为推断")
3. annotation = LLM_structured(pack_digest, bull_case, bear_case) → **AIAnnotation**

**输出 AIAnnotation(只写 ai_* 命名空间)**:

```
ai_stance:      enum{SUPPORT, NEUTRAL, CAUTION, FLAG_RISK}   # 无任何"升级"语义档位
ai_thesis:      str
ai_risks:       list[{tag, text}]   # tag∈{suspension, limit_locked, entry_infeasible, overnight_gap, liquidity, fundamental, other}; stance∈{CAUTION,FLAG_RISK} 时 >=1, 合同硬校验
ai_confidence:  enum{low,medium,high}   # 绑 data_confession, 缺数据强制≤medium; is_suspended 时强制=low
ai_downgrade_suggestion: bool  # 只向下; 无对偶字段
meta: {schema_version, prompt_hash, evidence_pack_hash, model_id, debate_transcript_ref}
```

**tradability 联动硬校验(机器检查, 不靠自由文本)**: `pack.tradability.is_suspended=true` → ai_risks 必须含 tag=suspension 条目且 ai_confidence=low; `entry_executable=false` → 必须含 tag=entry_infeasible 条目(注释器此时的合同角色是"解释为何按 entry_rule 不可成交+若等待复牌/开板的风险", 而非辩"该不该买"); 违反 → 记 `ai_failed`, 不发布。

辩论转录落盘存档(研究可查)但**不进发布 JSON**。structured 解析失败→`ai_failed`(沿用现行失败记录链路), **禁自由文本回退**——他方 structured.py 的 free-text fallback 在我方合同下是漏洞不是韧性(自由文本无法过 ai_stance 机器校验)。若 C' 立项且第 3 调用走结构化工具调用, 有一个可搬工程件: None-parse 显式当 miss(agents/utils/structured.py#L64-70); nullish 强转 None(agents/schemas.py#L30-36)则仅当 AIAnnotation 未来引入**可选数值字段**时才有作用面(当前 schema 无一个数值字段, §6 M2)。

**验收条件**(闯关机口径, 预注册):
1. 封印测试: 注释写回前后 `evidence_pack_hash` 不变; 发布合同校验新增"ai_stance→final_action 映射只降不升"(现有"final_action 禁买入级"的推广)。
2. 毒样本负向测试: 给 pack 注入 as_of 之后的行情/公告, 装载器必须拒绝(PIT 守卫生效性测试, TradingAgents #1115 十八个月静默失效的直接教训)。
3. 消融度量(A=现行单调用, B=快照硬化单调用, C'=3 调用辩论): 同一冻结日集+同一模型, 比较 (a) ai_risks 对后验 5/10 日最大回撤或破位事件的召回/误报——度量口径分两层(r3 修正: 原文误把四条全说成"继承任务#38口径", 实测我方代码只实现前两条): **继承层(既有实现)**——市场日历对齐而非个股行号、停牌窗口不丢弃样本按 ffill 最后成交价标记市值+复牌缺口一次性计入回撤(o2c_strategy_backtest.py `_add_calendar_fwd` docstring L62-67 / factor_gauntlet.py L106-109, 任务#38 修的 bug 不得复发); **新增设计要求(我方现无实现, 消融评测器须新写并配单测)**——出场侧跌停锁死日不得视作可出场时点(锁死延续期跌幅计入回撤直至首个可执行出场日)、风险实现窗口自 T+1 首个可执行出场时点起算(§3.3); 现状缺口证据: o2c_strategy_backtest.py L244 自白"成本/涨跌停可成交未精算", 全 scripts 目录无出场侧锁死处理(setup_gauntlet.py L1022-1023 的锁死排除属入场侧事件域)。四条缺一, 消融就会把"停牌/锁死类真风险"从度量里静默剔除, 恰好污染 C'/B+ 唯一立项假设的判定; (b) stance 与后验超额的方向一致性(**仅观察, 不宣称 alpha, 不进任何门**); (c) 数字幻觉率(注释中的精确数字能否在 pack 内找到出处, 抽样人检); (d) token 成本。research/holdout 切分沿用闯关机。
4. 成本护栏: C' ≈3 倍调用量(40 票/日: 40→120 调用), 需用户拍板(红线 7)。

### 3.3 A 股约束对注释机制的显式适配裁决(T+1/涨跌停/停牌)

砍 Trader 的理由是"我方无执行域", 但这**不豁免注释机制本身受 A 股交易约束作用**——约束进入的是输入面与度量面, 不是执行面。裁决如下:

1. **T+1 重定义注释目标语义**: O2C 当日开盘买入当日不可卖, 候选一旦成交即被迫持有至 T+1 首个可执行出场时点。注释目标因此不是抽象的"该股好不好", 而是"**被迫持有窗口内有哪些不可控风险**"——隔夜缺口、次日低开/一字跌停无法出场、持有期内停牌公告悬置。bull/bear/annotator 提示词须显式声明该窗口("你在为一笔 T+1 之前无法撤出的持仓写风险论证"), ai_risks 的 tag 词表(overnight_gap/limit_locked/suspension)即按此窗口设计。TradingAgents 无此概念(美股 T+0, 其 Trader 的 stop_loss 隐含随时可撤), 原样照搬其"风险辩论"语料会系统性错焦到日内风险。
2. **度量面**: 验收 3(a) 的风险实现窗口自 T+1 首个可执行出场时点起算——此条与出场侧锁死处理均为**新增设计要求**(我方现无实现, 须在消融评测器新写+单测); 日历对齐与停牌 ffill 两条继承任务#38 既有口径(层次划分见验收 3a 正文)。若不如此, 消融会奖励"日内波动"类对 T+1 持仓不可操作的废话注释, 而漏掉真正不可控的隔夜/流动性/锁死风险。
3. **可成交性判定前置于辩论**: "能不能买"(一字板/涨停按 entry_rule 无法成交、停牌)由确定性 tradability 字段判定, AI 不辩这个问题; entry_executable=false 时注释器角色按 §3.2 联动校验退化, 辩论(若 C' 立项)对此类候选可直接跳过 bull 段——买不进的票不需要多头论证。
4. **四臂一致性**: tradability/T+1 语义是 pack 与度量口径的公共适配, 对 A/B/B+/C' 全部生效, 不构成 C' 专属成本, 因此**不改变 §5 判决方向**; 但它是任何一臂开工前的前置项(字段来源全部是现有面板/late_features 确定性产出, ≈1 天)。

---

## 4. 产出④: 与"AI 永不改冻结分数/verdict"红线的兼容性裁决

**原样移植: 不兼容, 一票否决。** TradingAgents 的存在目的就是让 LLM 委员会**产生** verdict——PM 的 rating 是系统唯一终产物, 且 Trader 层还让 LLM 凭空产 entry/stop 数字。移植整链 = AI 生成 verdict = 直接违红线 6("任何 AI 环节不宣称 alpha、不改冻结量化分数")与 inventory 铁律("AI 永不改冻结量化分数与 verdict")。

**改造后可兼容, 且有结构性保障可依**: 该仓自己的架构恰好提供了兼容改造的支点——它已经把"确定性事实"(verified snapshot 唯一事实源)与"LLM 解读"分离, 把终产物钉进枚举 schema+确定性 parse。我方只需再挪一根轴: **把"评级=终裁"改成"评级=注释", 把辩论从 verdict 上游挪到 verdict 下游**。冻结分数进 pack 只读、输出被限制在 ai_* 命名空间、stance 枚举无升级档、合同校验器做哈希封印+单向映射检查——四道机械闸后, C' 与红线兼容。剩余残余风险: 前端把 ai_stance 渲染得比 verdict 更显眼造成"事实上的改判"——归前端诚实性规范(DESIGN-V3)管辖, 非本轨道范围, 但设计稿须注明。

---

## 5. 判决: C 方案值不值得做

**完整 C 方案(多角色委员会移植): 不值得。** 理由:

1. **机制空心**: 辩论收敛=计数器, 默认 1 轮; 双方读的是同一冻结报告, 多轮无新信息进入机制(无新工具调用权), 追加轮次≈复述; 裁判不见原始证据只见转述; 风险角色无 veto。9.2 万星的"多 agent 辩论框架", 其辩论的全部控制流是 conditional_logic.py 里 22 行计数比较。
2. **真价值已收割且与辩论正交**: verified snapshot、route_to_vendor 三纪律、结构化输出+确定性 parse、断点形状签名——Phase1 卡四条借鉴全部不依赖辩论存在, 本轮深读确认无遗漏的第五条辩论专属机制。
3. **我方先验**: T1/O2C/短线三轮已证伪"AI/策略边际"叙事, AI 角色已锁定为注释+诚实性而非 alpha 源; 为注释质量引入 3-16 倍调用成本的委员会, 先验收益低。
4. **可测残余假设只有一条**: "对抗性第二视角能否提升风险提示质量(召回后验风险)"。这条假设有更便宜的一等测法: **在 B 方案的单次调用内强制两段式输出(先写 bear_case 再给注释)**, 成本 1x, 即"单调用自辩"变体 B+。B+ 的输入面与度量口径同受 §3.3 A 股适配约束(tradability 字段+T+1 被迫持有窗口+去生存者偏差后验度量)——"风险提示质量"这个假设本身就是围绕 T+1 被迫持有窗口定义的, 没有 §3.3 适配则 B+ 与 C' 都测不出真东西。

**建议的消融格局**: 主线 **A vs B vs B+**(单调用: 现行 / 快照硬化 / 快照硬化+自辩两段式)。**C'(3 调用真辩论)不预先立项**, 仅当 B+ 显示对抗视角对风险召回有效但单调用深度不够(bear 段质量差、召回率停在半途)时, 按 §3.2 设计稿+验收条件补开, 且过红线 7 成本拍板。这样 Wave3 消融从"三方案并行实施"缩为"两实施+一变体+一预备设计稿", 省 ≈1-2 周实施与 3 倍推理成本, 而 C' 的全部设计工作(本报告 §3.2)已完成、随时可启。

---

## 6. 微机制拾遗(独立于 C 判决; r4 已实读我方两生成器源码逐条核对——M1 降级无行动、M2 降级无作用面、M3 收窄, 真实可立项仅 M3 残项与 M4 检查)

| # | 机制 | 引证 | 我方落点(r4 实核后) | 成本 |
|---|---|---|---|---|
| M1 | structured 返回 None(思考模型答文本不调 schema)显式当 miss 处理 | agents/utils/structured.py#L64-70@01477f9afb | **已存在等价保护, 无行动(r4 降级)**: 我方无任何结构化输出调用(全 scripts grep response_format/with_structured_output/json_schema 零命中), 现行自由文本链路的等价守卫已在位——generate_o2c_ai.py L369-371 `obj is None`→"json_parse_failed"显式失败+重试+耗尽落 ai_failed, generate_prebreakout_ai.py L128-135 同构("parse_or_validate"), 均无自由文本回退。残值仅为设计纪律: 未来若迁移到结构化工具调用(如 C' 第 3 调用), None 必须显式当 miss, 禁静默回退 | 0(纪律, 随 C' 立项才生效) |
| M2 | 可选数值字段 nullish 字符串("None"/"N/A"/"TBD")validator 前置强转 None | agents/schemas.py#L30-36 (#1058) | **无作用面, 无行动(r4 降级)**: O2C 输出 schema(generate_o2c_ai.py validate_result L272-284)只有 ai_summary/ai_points/ai_risks/ai_advice, 无数值字段; prebreakout 唯一数值字段 ai_score 为**必填**且其 clamp()(L111-114)的 try/except 已把非数值串("N/A"等)转 None、None→"no_score"显式失败——所提 coercion 不改变任何失败率; §3.2 拟议 AIAnnotation 亦无可选数值字段。仅当未来 schema 引入**可选数值字段**时才适用 | 0(条件纪律) |
| M3 | 确定性身份注入: 运行前解析 名称/行业/板块 进提示词+"除非工具证伪不得换公司" | agent_utils.py#L78-169 (#814) | **收窄(r4)**: 名称与行业注入两生成器**均已存在**——generate_o2c_ai.py build_prompt L232/L235 "股票：{name} {ts} \| 行业：{industry}", generate_prebreakout_ai.py L81 "行业：{s[sector]}"; 非缺口。真实升级差距仅剩: ①"不得替换标的/不得引用其他公司的事实"硬指令一句(对应 #814 防串股的提示词侧); ②可选补板块(board)字段 | ≤1 小时(两处提示词各加一句) |
| M4 | 上下文清除占位不得用裸 "Continue", 须锚定标的+日期 | agent_utils.py#L190-214 (#888) | 我方多段 AI 调用若有续写/重试提示, 检查一遍占位语 | 1 小时 |
| M5 | 工具参数防 LLM 误用: 逗号分隔多值自动拆分逐个处理, 单项失败字符串占位 | technical_indicators_tools.py#L26-35 | 仅当我方给 AI 开工具面时适用(现无), 记录备用 | 0 |
| M6 | ToolNode(可执行面)与 bind_tools(可见面)双清单漂移 | trading_graph.py#L203-218 vs news_analyst.py#L20-25 | 反面: 若做 C' 工具面, 单一清单派生两处 | 0(设计纪律) |
| M7 | 内部推理英文/输出语言可配的分离 | agent_utils.py#L52-65, default_config.py#L107-108 | 研究观察: 我方中文注释链可 A/B"英文推理+中文成稿"是否提升质量(不立项, 挂消融附注) | 0 |

---

## G 条目闭合声明

- **G2(TradingAgents 辩论提示词+工具内核未读): 闭合。** 本轮按冻结 SHA 逐行读完: analysts 5 文件、researchers 2、risk_mgmt 3、managers 2、trader、agents/schemas.py、agents/utils/ 的 agent_states/agent_utils 及全部 7 个拆分工具文件/rating/structured/memory(复核)、graph/ 的 setup/conditional_logic/analyst_execution/propagation/signal_processing/reflection/trading_graph(复核)(graph/ 9 文件中的 7 个, checkpointer.py 本体与 __init__.py 未读——见 confession, 均不承载辩论/裁决逻辑, 不影响 G2 闭合)、根目录 default_config.py。Phase1 卡的"C 方案设计不得引用本卡"禁令可解除, 以本报告为准。产出①②③④齐备, 并给出 C 方案总判决(缩减为 A vs B vs B+)。
- 顺带复核维持 Phase1 卡三条反模式(fiscalDateEnding 锚/无执行模块/轶事式自学习)成立, 无需修卡。

## coverage_confession

- **没读**: cli/ 全部 8 文件(交互流程, 与内核无关); llm_clients/ 各 provider client 与 model_catalog 实现(工具只看到 create_llm_client 调用面); dataflows/ 各 vendor 实现细节(y_finance/reddit/stocktwits/alpha_vantage 等——Phase1 卡已覆盖路由层, 本轮只确认工具→route_to_vendor 接线); graph/checkpointer.py 本体(仅经 trading_graph 调用面+Phase1 卡)与 graph/__init__.py; reporting.py; 53 个测试文件正文; Dockerfile/scripts。
- **不确定**: ①"无提前终止"结论基于 conditional_logic.py 全文+setup.py 全部边, 若 cli/ 有额外中断逻辑会遗漏(可能性低, cli 不参与图构建); ②bull/bear 提示词在 llm.invoke(prompt) 传裸字符串时各 provider 的 system/user 角色映射细节未查 llm_clients 实现, 不影响机制结论; ③默认模型名 gpt-5.5/gpt-5.4-mini 仅照抄 default_config, 未核其 model_catalog 有效性; ④成本估算(12-16 调用/票)是结构推算, 未实跑计量。
- 未运行任何代码; 全部结论来自冻结 SHA 静态阅读。

## Wave 映射

| 产出 | 映射 | 成本 |
|---|---|---|
| ①角色-工具矩阵 + ②收敛机制解剖 | Wave3 AI 消融(设计输入+范围裁剪依据) | 已完成(本报告) |
| ⑤C 方案判决→消融缩减为 A vs B vs B+ | Wave3(直接砍掉多代理实施臂) | **省 1-2 周+3 倍推理成本** |
| ③C' 最小内核设计稿(EvidencePack 含 tradability+停牌语义/AIAnnotation 带 tag 联动校验/3 调用/验收 4 条) | Wave3 预备件(条件触发再立项) | 若立项 2-3 天+成本拍板 |
| ③' §3.3 A 股约束适配(tradability 字段+T+1 度量窗口+去生存者偏差后验口径) | Wave3(A/B/B+ 公共前置项, 不依赖 C' 立项) + Wave1 数据可信(停牌 as_of 显式声明) | ≈1 天(字段全部来自现有面板/late_features) |
| ④红线兼容裁决(哈希封印+ai_* 命名空间+单向映射校验) | Wave3 + Wave2 组合风险契约(发布合同校验器扩一族检查) | 0.5 天(随 B+ 或 C' 立项时做) |
| M1+M2(r4 降级: 等价保护已存在/无作用面, 不排期) | Wave3 附注(仅作未来结构化迁移/新增可选数值字段时的设计纪律) | 0 |
| M3(r4 收窄: 仅"不得替换标的"硬指令±板块字段; 名称/行业注入已存在非缺口) | Wave3 | ≤1 小时 |
| M4/M5/M6/M7 | Wave3 附注(设计纪律与备用) | ~0 |
| 毒样本负向测试纪律(#1115 教训, 验收条件 2) | Wave1 数据可信(PIT 守卫生效性测试, 与 Phase1 卡借鉴#4 同项不重复计) | 1 天内(Phase1 已立项) |
