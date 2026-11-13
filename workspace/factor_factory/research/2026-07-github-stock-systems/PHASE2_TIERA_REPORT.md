# PHASE2 Tier-A 深读收账报告 (2026-07-11)

- 收账人: Phase2 收账会话（独立）
- 输入: deep/ 全部 8 份轨道报告 + verify/PHASE1_GAPS.md + PHASE1_SYNTHESIS.md
- 职责: G1-G13 逐条对账、跨轨道实施项综合、Phase3 spike 规格草案、轨道状态判定
- 引证约定: 沿用各轨道报告的 `path#Lx-Ly@SHA` 冻结引证; 本报告只引轨道报告章节, 不新增仓内主张

---

## 1. G1-G13 逐条闭合对账表

| G | 主题 | 判定 | 证据位置 | 处置说明 |
|---|---|---|---|---|
| G1 | ZhuLinsen 上游迁移决策缺 delta 实证 | **闭合** | deep/upstream_diff.md §0-§6 + G条目闭合声明 | 309 commits 实测精确(compare API ahead_by=309); 本地 9改+5自有逐文件三方判决表(§1); 三移植切片在冻结 SHA 全链验证+依赖边界+fail-open 排雷表(§3-§5)。"逐 commit 走查"以逐路径 churn 计数+内容级终态 diff 替代, confession 已声明该替代法的盲区(中间引入又撤销的行为不可见)且论证对迁移判决无影响——**接受该替代**。附带发现最紧急运维风险: 诚实性护栏在未提交工作区, 一次误 checkout 即丢(→上游迁移 Wave 首项)。 |
| G2 | TradingAgents 辩论提示词/工具内核未读 | **闭合(重做后)** | deep/tradingagents.md §1-§5 + 修订记录 r2-r4 | 13 角色文件+schemas+agent_utils 及 7 工具文件+graph 7/9 核心文件按冻结 SHA 逐行读完; 产出角色-工具矩阵/收敛机制解剖/C 方案总判决(不值得做, 消融缩减为 A vs B vs B+)。初版经 4 轮对抗复核修订(r4 教训: 微机制立项前必须实读自家落点代码), 终版 M1/M2 降级、度量口径两层化(继承 vs 新增)诚实。Phase1 卡的"C 方案禁引"禁令解除, 以本报告为准。 |
| G3 | Lean 风险模型接口范本零引证 | **闭合** | deep/lean.md §2 + 判决表 | Algorithm/Risk 5 文件+Framework/Risk 5 模型+QCAlgorithm.Framework 接线+IPortfolioTarget 全部带行号读透; manifest why 后半句兑现, 无需降级。附带产出三条设计结论(target overlay/Insights.Cancel 防秒买回/状态不持久化反面→坐实锁落库路线)。 |
| G4 | OpenBB AI/MCP 面+SEC/FRED 未读 | **闭合** | deep/openbb.md §0/§3/§4 | AI/MCP 半: mcp_server 全读, 判决"治理范本借三小件, 无 AI 写入路径可裁决"; PIT 半: SEC provider 读穿至 _extraction 层, 卡预言证实且超预期(pit_mode 双口径/450 天拖带守卫/预披露标注三机制+accepted_date 截断反面); FRED 定性 latest-only 无 PIT(防误借注记)。LICENSE 附带完成全文机器级复核=纯净 AGPL-3.0-only。 |
| G5 | vnpy 结论边界限于核心仓+吞吐主张无锚 | **闭合(标注处置, 本报告执行)** | PHASE1_SYNTHESIS §5.3(明确不补理由) + 本表 | 补读 vnpy 插件仓已在 Phase1 综合报告明确放弃(不在冻结 SHA 内, 定位仅反例对照+两个小骨架)。G5 规定的两条标注动作在此以收账裁定生效: ①**后续任何文档引用 vnpy 反模式(±10% 硬编码/停牌平bar/无 T+1)必须带"核心仓 alpha 模块限定"脚注**; ②**"事件引擎吞吐"社区主张即日起降级为"背景推断", 禁作为引证使用**。 |
| G6 | qlib parity 范本横截面半边未验证 | **闭合** | deep/qlib.md §3 + G条目闭合声明 | dataset/handler+processor+contrib/data 逐段精读: 横截面算子定位(CSZScoreNorm 等)、无状态⇒PIT 安全/窗口无关论证、DataHandlerLP 类型门的 train/serve parity 结构保证、Alpha158 无状态化路线——parity 完整方案(时序扩窗+截面无状态)两半拼齐。附带闭合两处卡自白: online/update+task/gen 已读(§4); executor 内层 T+1 遗漏全核, "无股票层 T+1"升级为 CONFIRMED(§5.1)。 |
| G7 | nautilus accounting 未读+catalog.rs 近似行号 | **闭合** | deep/nautilus.md §4/§7 | accounting 实际位置 crates/model/src/accounts/ 已读(三元恒等式+钳位构造器+A股 T+1/涨跌停/单向税缺位升级为代码级实证); are_intervals_disjoint 精确重定位 #L4806 并补 contiguous/query_interval_diff/写路径全锚点; 附带补读 matching_engine 内部(撮合结论从文档级升代码级)。议程勘误: accounts-and-margin.md 在冻结 SHA 不存在, 实为 accounting.md——后续引用议程该文件名的文档应改。 |
| G8 | akshare 我方在用竞价/分钟接口未审计 | **闭合(盲区收窄+移交)** | deep/akshare_audit.md §0-§5 + G条目闭合声明 | 11 调用点/10 在用接口清单落成文件, 每接口冻结 SHA 实现全读+契约表(§2); **竞价/分钟/停复牌实测不走 akshare**(Tushare stk_auction_o/c+stk_mins, stock_tfp_em 零调用)——原怀疑面收窄为"tushare 代理契约未审计", 已作范围移交注记(Wave1 新条目, 建议 Phase4 tushare contract), 本轨道不越界。运行时/冻结版本 diff=0 实测(判定为运气, 版本 pin+探针仍必做)。 |
| G9 | freqtrade 保护边界语义未从测试核实 | **闭合(超额)** | deep/freqtrade_protections.md §2/§5 | test_protections.py 全文 1079 行逐断言核实, 产出 19 条冻结语义表(左闭右开/边界推一根K线/lookback 左开右闭/end 锚定事件/side='*' 只命中双侧锁/严格不等→我方反转)+24 条我方单测清单。附带发现: falsy-zero 在官方测试无负向覆盖(只抄测试仍会漏), 单列 §5.C。pairlist 姊妹体系维持不读(非 G9 规定动作+防重复建设)。 |
| G10 | manifest 与 OpenBB AGPL 结论不同步 | **闭合** | manifest/repos.json manifest_amendments(openbb.md §0 与 qlib.md 双轨道核实在案) | 勘误已回写 manifest_amendments, 引证分叉消除。**遗留一项待用户拍板**(超出 G10 范围, 不影响闭合): red_lines.md 第 3 条追加勘误行"OpenBB=AGPL-3.0-only 全文核验, 按 GPL 同级红线执行"——冻结文件不擅动, 建议随 Phase3 开工时一并拍板。 |
| G11 | 三处 citation 锚点可核性弱 | **部分闭合(1/3 修复, 2/3 明确放弃)** | nautilus.md §3.1/§7(已修); 本表(放弃裁定) | nautilus catalog.rs 近似行号已精确化(#L4806)。akshare 卡 citation#11(path="github issues")与 myhhub Dockerfile 无行号两处**明确放弃修卡**: Phase1 卡属冻结产物不回改, 两处均可按 claim 内容定位(抽检口径已在 GAPS 认可), 严重度低。规范前瞻生效: Phase3 起新产出引证 path 必须为仓内路径或完整 URL+行号/函数名锚。 |
| G12 | 痛点归类两处偏松(滑点素材恐漏检) | **闭合(聚合处置完成)** | qlib.md §5.2/Wave 映射(参与率²冲击接 B1); lean.md §1.3(R-LIMIT-2 参与率上限对齐 backtrader 三件套与 B1) | G12 唯一实质风险"滑点标定素材漏检"已被规避: backtrader 滑点三件套公理经 qlib/Lean 两轨道分别纳入 Wave2/Wave1 清单(见 §2)。FinGPT 东财新闻条目维持"新数据源引入"定性, 不纳入本轮实施(挂起至 AI 上下文扩源立项时)。不改卡, 与 GAPS 处置一致。 |
| G13 | 防重测/血统痛点样本薄(Lean 三子系统/NT event_store 未读) | **闭合(样本判足+显式放弃补读)** | qlib.md §6(血统/防重测三小件补强); nautilus.md §5(event_store 明确不投入判决) | qlib 轨道产出 recorder git-diff 三连拍/SQLite 任务状态机(领取即改态+异常回滚+KeyboardInterrupt 双捕获)/check_data_health 四检查, 加 TradingAgents 形状签名(Phase1 已录), 两痛点的实施样本已足支撑 Wave1。NT event_store 已判"体量不匹配, 状态机违规表+denials 表即够"; Lean ObjectStore/Report/Research 同理放弃——**理由: 更多范本对已定型的实施稿边际价值≈0; 若 Phase3 实施中证明不足, 补读入口已知(GAPS 原文), 届时按需开洞**。 |

**对账小结**: 13 条中 12 条闭合(含 3 条处置型闭合: G5 标注/G8 收窄移交/G13 判足放弃), 1 条部分闭合(G11, 已修 1/3, 余 2/3 明确放弃并立新规范)。**无条目遗留进 Phase3 作为"欠账"**; 进入 Phase3 的只有两个非欠账事项: ①red_lines.md OpenBB 勘误行(用户拍板, 1 行); ②tushare 代理契约审计立项(G8 移交产物, 新范围非旧缺口)。

---

## 2. 跨轨道综合: 实施项清单 (按 Wave 分组)

聚合规则: 逐轨道 Wave 映射表全量收录后**跨轨道合并同类项**——四处合流点先行声明, 防重复排期:
- **合流A · PIT 统一规格**: qlib §1(双时间字段表+pit_asof)+OpenBB §3.1(available_at 禁降精度/as_first_reported/450天守卫/预告不覆写)+Lean §5.2(raw 不可变层+口径枚举 API+status map)+NT §3.2-5(行级 available_at+SQL 化验收) 合为一份规格, 由 **Phase3 spike-2** 先行验证。
- **合流B · provider 契约**: OpenBB §2(三张标准模型+TET+自检 harness)+akshare §5(10 接口冻结契约+三族断言)+upstream_diff §3(ProviderAttempt 血统+熔断)+OpenBB §5(批量七条检查单) 合为一层, 由 **Phase3 spike-3** 先行验证。
- **合流C · 状态机/保护**: NT §2.2(穷举转移表+deny 枚举+三态闸)+Lean §3.3(终态带因由/两阶段撤回/二次校验增补)+freqtrade §3-§5(strategy_locks+影子回放+退出受阻) 合为一族, 保护影子由 **Phase3 spike-4** 先行验证。
- **合流D · tradability/可交易性**: qlib §5.2(掩码 parquet+is_tradable 单入口+T+1 守卫)+freqtrade §4(退出受阻检测三态)+TradingAgents §3.2/§3.3(EvidencePack tradability 字段+T+1 度量窗口) 共用同一份分板块涨跌停/停牌/一字板判定资产, 只建一次。

### 2.1 Wave1 数据可信

| # | 机制 | 来源引证 | 接口形状要点 | 粗成本 | 风险 |
|---|---|---|---|---|---|
| W1-1 | PIT 双时间契约: (symbol,field,period,value,announce_date,revision_seq,source,ingested_at) 字段表 + pit_asof(df, as_of, period, lag) 单入口 | qlib.md §1.2; openbb.md §3.1(增量); lean.md §5.2 | 排序即修订链(弃 _next 指针); 未来期禁引 raise; value 强制 float64; as_first_reported 无更早申报即置缺失(收紧于 OpenBB 兜底) | 1-2天(spike-2 承载) | period 季度编码口径需自定(qlib confession 未核其格式); 新数据生效不回填历史修订链 |
| W1-2 | 公告 PIT 增量: available_at 带时区 datetime 禁降精度 + 450 天拖带守卫 + 预告不覆写(revision 链相连) + 负向测试四件 | openbb.md §3.1 | is_preliminary 标记列; 衍生值 source 行级可区分 | 0.5天规格+1天断言 | pit_mode 系 2026 新代码, 450 天等参数按"机制思想"采信非验证默认值 |
| W1-3 | raw 不可变层+adj_factor 独立层+口径枚举 API(qfq 强制 asof)+status map(状态区间史)+装配一致性断言 | lean.md §5.2 | load_prices(mode∈{raw,qfq_asof,hfq}, asof) 无默认必填; 恒等式 qfq(t,asof)=raw×adj(t)/adj(asof); stage0 断言 raw/adj_factor/日历三者最新日一致 | 3.5-4.5天 | 大头在消费端 grep 改显式口径; 与红线1兼容(panel_full 不动, 新层 additive) |
| W1-4 | 三张标准模型 DailyBarV1/TradingCalendarV1/AuctionV1: extra='forbid'+StrictRow+FetchMeta 必填+(results,failures) 双通道+PIT as-of 验收 | openbb.md §2 | TET 三段(to_query/pull_raw/to_rows)+自检 harness 阶段隔离断言(raw 中禁出现 Row 实例); 涨跌停例外表 4 类(limit_exempt 行级标记) | 1.5-2.5天(spike-3 承载) | AGPL 禁复制, 全自研(报告已零片段); 例外表后两类靠白名单允许滞后维护 |
| W1-5 | akshare 10 在用接口冻结契约文件+探针(三族分断言: 新浪族可用性/行情族单位量级/数据中心族列数 schema) | akshare_audit.md §2/§5 | akshare_contracts.yaml 每接口: frozen_columns/units/sanity/expected_ncols/http 统一包装; fetch→(df, meta) fail-closed ContractBroken | 2-3天(字段表已产出, 比卡估低) | 位置契约固有脆弱(yjyg 28列 columns=ALL 最危); 服务端字段语义未抓包(confession) |
| W1-6 | 日历 provider 统一: 4 处调用收敛+每日落盘冻结快照+失败读昨快照+告警+尾部<90天硬告警+timeout=30+重试 | akshare_audit.md §2.1/§3.2 | 单一 calendar provider, 消费端禁自算 prev/next(物化列, 学 Lean) | 1天 | 日历是全系统单点; 现状 3 调用点完全裸奔(最高共性风险判决三) |
| W1-7 | ProviderAttempt 血统流水+熔断(CLOSED/OPEN/HALF_OPEN)+freshness_lag_days | upstream_diff.md §3 | 反其道三条: 血统写入失败→WARN 告警(不 fail-open 吞); 新增 freshness_lag_days 接新鲜度契约; 熔断进程内即可。CircuitBreaker 可直接从本地 base 650a1c3 取(155 行 diff=0 实测, MIT) | 1.5-2天 | 与合流B统一 FetchMeta 字段, 防两套血统表 |
| W1-8 | PhaseContext cn 裁剪版: phase 枚举(无 unknown, 推断失败即 abort)+effective_trade_date+is_partial_bar, 随 run 传递落 manifest | upstream_diff.md §4 | fail-open 排雷表 7 处**逐处反转**为 fail-closed; backtest_cache 写入前断言 not is_partial_bar(堵 0710 结构性复发面) | 0.5-1天(spike-1 承载) | 上游同名函数含同款 fail-open, 不论从哪个版本取都必须做反转 |
| W1-9 | parquet 写入护栏: 文件名承载区间+写前 disjoint/升序校验+覆盖需显式 --allow-overwrite 留痕+query_interval_diff 缺口检测 | nautilus.md §3.2 | 负向测试=重放 #52 重叠写入序列必须首笔被拒 | 1-2天 | LGPL 零复制(报告已合规); 多日文件需 sidecar manifest |
| W1-10 | 行级 available_at 可见时刻列+PIT 验收 SQL 化(count WHERE available_at>as_of == 0) | nautilus.md §3.2-5 | 盘中快照=抓取时刻, 收盘数据=收盘时刻 | 2-3天(中期) | 与 W1-1 合流, 勿双列口径 |
| W1-11 | 批量拉取七条审计检查单: 禁 partial=success/failures 全量落 manifest/空载荷单独计数勾稽/按 key 返回 dict/失败分级显式枚举 | openbb.md §5 | 勾稽断言 len(keys)==成功+失败+显式空载荷 | 0.5天审计+挂断言 | 预期违例集中在 #3(空载荷不计数)与 #4(身份丢失) |
| W1-12 | check_data_health 四检查(缺失/双向大跳变 pct_change(fill_method=None)/必需列/复权因子缺失)接 pipeline_alert | qlib.md §6 | fill_method=None 防隐式 ffill 穿越停牌——细节照抄(MIT) | 0.5天 | 分钟频适用性未验(先日频) |
| W1-13 | run_manifest 血统三连拍(git diff/status/cached+argv+env 白名单)+SQLite 任务状态机(领取即原子改态/异常回滚/KeyboardInterrupt 显式双捕获) | qlib.md §6 | 血统采集 fail-soft 合理照抄; subprocess 带 errors=replace(0708 通治) | 0.5+1天 | 状态机接夜间 9 stage, 补跑=重新领取 |
| W1-14 | index_global ÷100 量级 sanity 区间断言+midday as_of 修复; 指数基准双源对账(|ak−ts|<0.01)+缺失 fail-closed; yjyg/gdhs 列数断言; run_manifest 记 akshare 版本+实现文件 sha256 | akshare_audit.md §2.7-2.11/§3 | 基准缺失=复盘拒绝生成而非超额=None | 合计 1.5天 | index_global 是"100 倍静默错"最高危单点, 2 小时/性价比最高 |
| W1-15 | R-LIMIT-1/2 限价触碰规则(bar 起点早于订单生效即不可用)+乐观默认反转八处清单→闯关机回归用例 | lean.md §1.3/§6.4 | 我方日频现状已满足, 写成回归测试钉死防分钟级撮合破窗 | 0.5+0.5天 | 案 B 裁决为静态代码级证据(未实跑 Lean, confession 已标) |
| W1-16 | DataMonitor 式缺口登记: miss 记录器+收盘缺口日报, universe 级/单票级分级告警, 辅助数据豁免 | lean.md §6.1 | — | 0.5-1天 | — |
| W1-17 | INSUFFICIENT_DATA/NO_DRAWDOWN 事件区分(数据不足≠未触发); 停牌 as_of 显式声明(data_confession.suspension, 禁 ffill 冒充新鲜) | freqtrade_protections.md §5; tradingagents.md §3.2 | — | 0.25+含前置 | — |
| W1-18 | float64 落盘审计(value 列禁 float32); FRED latest-only 防误借注记; tushare 代理契约立项注记(竞价/分钟移交) | qlib.md §7-3; openbb.md §3.2; akshare_audit.md 移交条 | — | 0.25天+两条 0 成本记录 | tushare 契约审计本体另计(Phase4) |

### 2.2 Wave2 组合风险契约

| # | 机制 | 来源引证 | 接口形状要点 | 粗成本 | 风险 |
|---|---|---|---|---|---|
| W2-1 | required_lookback/required_forward/lookback_basis 因子注册元数据+load_panel_for 统一装载器+欠窗 LookbackUnderflow 硬失败 | qlib.md §2.2 | 恒等回放验收(两查询窗重叠日逐元素相等)=#52 回归测试; lookback+1 扰动审计 CI 拒低报; 未注册禁入闯关机 | 2-3天(spike-5 承载) | 我方因子是脚本非表达式树, 靠声明式元数据+双向验收替代自动推导; qlib 自身"欠窗静默降级"是反面依据 |
| W2-2 | tradability 掩码 parquet(board/pct_limit 分层/limit_frozen/one_word_board/suspended/participation_cap)+is_tradable(symbol,date,direction)→(bool,reason) 单入口+T+1 显式守卫(sell_earliest=buy_date+1交易日断言) | qlib.md §5.2(合流D) | reason 枚举统一(limit_up/limit_dn/suspended/one_word/not_listed); 停牌日行情列必须 NaN 禁 ffill(契约压给上游); 缺列 hard fail(反 qlib fail-open) | 2-3天 | 一字板判定修收盘价口径一半盲区, 另一半留分钟数据升级位; 是 freq §4 与 TA §3.2 的公共地基, 先建 |
| W2-3 | 复盘表 additive migration: eval_status/unable_reason(含我方补 suspended/delisted/halted_limit)/engine_version+UNIQUE(rec_id,horizon,engine_version) | upstream_diff.md §5 | 只重试 RETRYABLE 集合; 口径变更 bump engine_version 不 UPDATE 旧行; 红线1 兼容(历史行只读) | 1天 | 复盘可比性地基, 亦服务 Wave3 口径隔离 |
| W2-4 | 补跑合同 v1: R1 不变式显式(重叠段 1e-4 容差)/R2 确定性补跑ID(stable_hash, source_asof_ts 禁本地 now)/R3 查询失败≠空/R4 终态前靶向单查+"S-"前缀可 grep | nautilus.md §1.2 | backfill_run 伪代码形状; 同参数重跑第二遍新增行=0 | 3-5天 | 建议落地前速读 NT reconciliation 性质测试思路(confession 建议, 2h) |
| W2-5 | 推荐状态机 v1: 8 状态穷举转移表+非法转移=错误值落 state_violation 表+告警; Lean 增补三件(终态带因由/两阶段撤回 pending→confirmed/fallback/候选冻结时点快照确认) | nautilus.md §2.2; lean.md §3.3(合流C) | REVIEWED 后禁再结算=结算-污染重置铁律状态机化; ai_ok 带 generation_id 去重 | 3-4天+0.5天增量 | 治 0709 prebreakout 慢性降级类"隐式状态漂移" |
| W2-6 | deny_reason 枚举(CATEGORY_CONDITION: key=value 格式, 17 族检查逐映射)+publication_denials 落库+文档由枚举自动生成 | nautilus.md §2.2 | 拒绝历史成为可查询资产 | 3-4天 | — |
| W2-7 | pipeline 三态开关 ACTIVE/REDUCING/HALTED(手动档先行)+撤退不闸原则(复盘/回滚永不被闸) | nautilus.md §2.2/§4.3 | REDUCING="只复盘不新增"运维模式合同化 | 2天 | 自动触发需用户拍板(红线7) |
| W2-8 | strategy_locks 表(scope 三层/reason_code 枚举/unlock_condition 四型/lock_end_date 物化/显式时区)+ProtectionEngine+发布合同第 18 族检查(发布清单∩活动 entry 锁=∅) | freqtrade_protections.md §3(合流C) | 19 条冻结边界语义日历化: 左闭右开/相位边界/lookback 左开右闭/end 锚定事件/去重吸收/阈值反转为含等号 | 1-2天(spike-4 先影子) | GPL 零复制全自研; LockStore 协议注入两实现修类属性单例反模式 |
| W2-9 | 影子回放(MemLockStore+历史台账重放, 三列报告: 锁生效日/被挡推荐/被挡后验超额) | freqtrade_protections.md §3.5 | 确定性验收: 同输入两次逐字节一致; 与生产零共享状态 | +1天 | 保护参数由此可回测调参 |
| W2-10 | 退出受阻三件套: ExitBlockedSentinel 检测(LIMIT_DOWN_SEALED/INTRADAY/SUSPENDED)+side='exit_blocked' 状态标注三联动(复盘顺延保守出场价+持有警示+entry 联动锁)+条件式解锁 first_tradable_close(评估失败当日不解锁) | freqtrade_protections.md §4 | ConsecutiveLossGuard 计数改 mark-to-market 口径(修 freqtrade"止损成交计数"在 A股失真); MARKET_STRESS 占比闸只停买不强卖(阈值用户拍板) | 1.5-2天+0.5天 | freqtrade 完全空白域, 纯自研; 依赖 W2-2 tradability 地基 |
| W2-11 | 保护单测清单 24 条(S/O/G/T 系+falsy-zero 专防+双口径专防+A股退出受阻+工程卫生; 断言卫生 CI grep 禁"对象≠布尔") | freqtrade_protections.md §5 | 标 † 条目为对 freqtrade 语义的显式反转, 注释对照 | 0.5-1天 | 验收基准, spike-4 前置 |
| W2-12 | RiskOverlay 协议(target overlay+按 symbol 覆写合并+risk_vetoed 同步作废候选防秒买回)+风险状态一律落库 | lean.md §2.3 | 与 EntryLock 正交互补(Lean=持仓侧, freq=入场侧, 我方两者都要) | 1-2天 | — |
| W2-13 | BuyingPower 三断言(减仓/风控卖出恒足额不被资金闸拦/部分成交 pro-rate/最小订单闸防尘埃单)+T+1 台账插槽定位(归台账层非 FillModel) | lean.md §4 | — | 0.5天 | — |
| W2-14 | Isolator 双限额 stage guard(持续限额+2×尖峰限额分设, 防 GC 尖峰误杀) | lean.md §6.2 | — | 1天 | — |
| W2-15 | 参与率²冲击成本对照口径接 B1(除零回退按最大冲击, 方向保守可照抄); 无状态截面算子库(cs_zscore/cs_rank/cs_fillna)收拢; 发布链 transform 白名单(is_for_infer 等价门, 发布合同+1族) | qlib.md §5.1/§3.2 | 冲击口径并列输出不改冻结账本历史行(红线1) | 0.5+0.5+0.5天 | — |
| W2-16 | cyq fallback_schema 下游消费点排查(近似 90 带分位不得触发真阈值决策) | akshare_audit.md §3.4 | — | 2小时 | — |

### 2.3 Wave3 AI 消融

| # | 机制 | 来源引证 | 接口形状要点 | 粗成本 | 风险 |
|---|---|---|---|---|---|
| W3-1 | **消融格局裁定: A vs B vs B+**(现行单调用/快照硬化/快照硬化+单调用自辩两段式); C'(3 调用真辩论)不预先立项, 仅当 B+ 显示对抗视角有效但深度不够时按设计稿补开+成本拍板 | tradingagents.md §5 | 判决依据: 辩论收敛=22 行计数器、默认 1 轮、裁判只见转述、风险角色无 veto——机制空心; 真价值(snapshot/结构化/确定性 parse)已收割且与辩论正交 | **省 1-2 周实施+3 倍推理成本** | C' 全部设计工作已完成(§3.2), 随时可启, 无沉没风险 |
| W3-2 | A股约束公共前置(四臂全用): EvidencePack tradability 字段+停牌 as_of 显式+T+1 被迫持有窗口语义进提示词+度量窗口自 T+1 首个可执行出场时点起算 | tradingagents.md §3.2/§3.3 | 度量口径两层: 日历对齐/停牌 ffill 继承#38; **出场侧跌停锁死+T+1 起算为新增设计要求**(现无实现, o2c_strategy_backtest.py L244 自白为证), 消融评测器须新写+单测 | ≈1天(字段来自现有面板/late_features) | 缺此四条, 消融把停牌/锁死类真风险从度量静默剔除, 污染唯一立项假设 |
| W3-3 | 红线兼容四道机械闸: evidence_pack_hash 封印+输出限 ai_* 命名空间+ai_stance 枚举无升级档+合同校验"只降不升"单向映射 | tradingagents.md §4 | 残余风险(前端把 ai_stance 渲染得比 verdict 显眼=事实改判)归 DESIGN-V3 诚实性规范管辖, 设计稿注明 | 0.5天(随 B+ 立项做) | 原样移植一票否决已裁定 |
| W3-4 | C' 最小内核预备件: EvidencePack 字段表+3 调用+AIAnnotation(ai_risks tag 联动机器校验)+验收 4 条(封印/毒样本/消融度量/成本护栏) | tradingagents.md §3.2 | structured 失败→ai_failed 禁自由文本回退(他方 fallback 在我方合同下是漏洞) | 若立项 2-3天+拍板 | 条件触发, 不排期 |
| W3-5 | AI 工具面三借(会话级最小工具面默认全关+渐进激活/描述裁剪 token 预算/allowed 硬门与 default 双层类目=AI 可见数据面白名单) | openbb.md §4.2 | 冻结分数类目不进 allowed | 决策记录 0; 实施<1天 | 仅当 Wave3 给 AI 挂工具时生效 |
| W3-6 | 微机制拾遗(r4 实核后): M3 残项"不得替换标的"硬指令±板块字段(≤1h)+M4 占位语检查(1h); M1/M2 降级为条件纪律不排期 | tradingagents.md §6 | 教训固化: 微机制立项前必须实读自家落点代码 | ≤2小时 | — |
| W3-7 | 负面契约与备用件: _parse_text_response 禁移植清单(任何消融基线不得含文本推断/伪中性兜底); RollingGen 段生成+embargo 形状(仅当消融需滚动重放); 成交假设诚实文档(bar 四笔分解/缺口按开盘价/乐观默认关) | upstream_diff.md §0.4; qlib.md §4; nautilus.md §4.1 | — | 0+0+0.5天 | — |

### 2.4 上游迁移项目

| # | 机制 | 来源引证 | 接口形状要点 | 粗成本 | 风险 |
|---|---|---|---|---|---|
| U-1 | **housekeeping(最紧急)**: 9 个工作区修改固化为一个本地 commit(先删 analyzer.py 重复"## 分析要求"块×3)+删 2 个 .bak+归档桥接脚本 #10/#11+FREEZE.md(base=650a1c3, 禁 git pull 声明) | upstream_diff.md §1 附带规格 | 验收: git status clean(或仅剩 fisher_bridge.py); AI 链连续 2 交易日绿灯 | 0.5天 | 当前未提交状态一次误 checkout 即丢诚实性护栏——**建议 Phase3 开工首日执行** |
| U-2 | fork 降格为 venv+.env 宿主: src/ 树只读, 活依赖仅解释器与凭据(2026-07-08 起代码已不在关键路径) | upstream_diff.md §6 | "永不升级"从权衡变成零成本事实 | 随 U-1 顺带 | fisher_bridge 残存 import 链待 stock_pipeline_gate 退役判定(confession 未定位其调度入口) |
| U-3 | 三切片移植(方向: 上游冻结版→factor_factory, 与 fork 升级解耦): 切片一 provider 骨架→W1-7; 切片二 PhaseContext→W1-8; 切片三 decision_signal 评估链→W2-3 | upstream_diff.md §3-§5 | MIT 可直接复制; CircuitBreaker 从本地 base 取(diff=0); 移植时上游测试文件先读边界断言当验收基准(confession 建议) | 已计入各 Wave | fail-open 反转清单必须逐处执行(§4.2 排雷表 7 处) |
| U-4 | VCR 录制回放: 3 个最脆 akshare 接口 pytest-recording 模板+升级前重录 diff 探针+CI --block-network 离线回放 | openbb.md §6 | 验收: 断网三测全绿/人为改 cassette 必须红/cassette 无真实 token | 0.5天 | 用社区 pytest-recording 非 OpenBB 自研包(规避 record_curl 不确定性) |
| U-5 | eastmoney_patch 列为"被我方生产依赖的上游文件": backport 时冻结其接口+patch 加载失败从静默改显式告警 | akshare_audit.md §3.5 | 现状: em provider patch 失败静默裸奔限流, cyq fallback 则直接崩——两种坏法都要修 | 1小时 | 跨项目依赖是隐形 SPOF |
| U-6 | 上游禁移植负面清单: _parse_text_response(造分兜底)+占位 50 三点位+config 温度补丁 drop(已被上游 generation_params 更优方案取代) | upstream_diff.md §0.4/§1 | — | 0(决策记录) | — |

**成本汇总(粗)**: Wave1 ≈ 20-28 人天; Wave2 ≈ 22-30 人天; Wave3 ≈ 2-4 人天(另省 1-2 周); 上游迁移 ≈ 1.5-2 人天。Phase3 五个 spike(§3)合计 ≈ 9-13 人天, 覆盖四大合流点的可行性验证, spike 通过后各 Wave 余量才排期。

---

## 3. Phase3 spike 规格草案 (5 个)

通则: 每个 spike 在 scratch worktree 内实施(红线4), 产出=可运行代码+验收记录+一页结论(通过/证伪/修正规格); 任一验收条件不满足即 spike 判负, 对应 Wave 条目规格必须修订后才可排期。全部 spike 不改冻结资产、不动 panel_full、不触发布链(除 spike-1 的 fork housekeeping)。

### Spike-1 · 上游迁移: PhaseContext 能力切片 backport + 全量测试

- **目标**: 验证"fork 冻结 detached + 定点移植"路线端到端可执行——以最小切片(PhaseContext, 上游新增组件 base grep=0)完成一次真实 backport, 同时完成 fork housekeeping, 消除未提交工作区运维风险。
- **输入**: upstream_diff.md §1 判决表+housekeeping 规格 A-D; §4.1 冻结版结构(trading_calendar.py 597 行@d08374898c); §4.2 fail-open 排雷表(7 处反转清单); §4.3 我方 PhaseContext 设计稿(cn 裁剪, 无 unknown 档, akshare 日历源, 不引 exchange-calendars); 上游 test_trading_calendar.py 边界断言(先读, confession 建议)。
- **验收条件(可判真伪)**:
  1. housekeeping: `git -C daily_stock_analysis status` clean(或仅剩 fisher_bridge.py); FREEZE.md 在仓根; analyzer.py "## 分析要求"块 grep 计数=1。
  2. 14:59/15:01 两时点跑管线: manifest 内 phase 分别=closing_auction/postmarket, effective_trade_date 分别=前一交易日/当日。
  3. 节假日运行→phase=non_trading 且管线拒绝进入取数 stage。
  4. 日历取不到(模拟断网)→进程退出码≠0+Discord 告警(fail-closed 反转生效)。
  5. 0710 型盘中缓存污染事故回放→backtest_cache 写入前断言拦截。
  6. 全量测试: 我方现有测试套件+新增单测全绿; AI 链(19:30 管线)连续 2 个交易日绿灯。
- **预计时长**: 1.5 天(housekeeping 0.5 + backport 0.5-1)。
- **回滚方式**: PhaseContext 为新增独立模块, 4 个接入点各一行判断——revert 单个 commit 即全退; fork 侧 housekeeping 只做提交固化与归档(.bak/桥接脚本移入 patch/, 可原样移回), 不删任何历史。

### Spike-2 · PIT: 双时间+revision+as_of (选 PEAD 业绩预告)

- **目标**: 在真实数据(PEAD 业绩预告, akshare stock_yjyg_em——PIT 锚已核验正确)上验证合流A统一 PIT 规格: 双时间字段表+revision_seq+pit_asof 单入口, 证明"前视零记录"可机械断言。选预告而非股东户数的理由: 预告有"预告→正式财报"天然修订链与 is_preliminary 语义, 能同时压测 revision 与双口径; 户数无修订仅单锚, 压测面窄。
- **输入**: qlib.md §1.2 字段表+pit_asof(df, as_of, period, lag) 签名+5 验收条件; openbb.md §3.1 增量规格(available_at datetime 禁降精度/as_first_reported 无更早即缺失/450 天拖带守卫/预告不覆写两行并存); akshare_audit.md §2.11 yjyg 契约(28 列数断言+公告日期 PIT 锚)与我方 ingest_pead_preannounce.py 现行 keep='first' 逻辑。
- **验收条件(可判真伪)**:
  1. 前视零记录: 任意 as_of 重放, `announce_date > as_of` 返回记录数==0(SQL/pandas 断言)。
  2. 毒样本: 注入 announce_date=as_of+1 记录, pit_asof 必不返回(负向测试)。
  3. 修订一致性: 同 (symbol,field,period) 内 revision_seq 与 announce_date 排序严格一致; 重复 (period,announce_date) hard fail。
  4. 未来期禁引: lag=-1 或等价"取未来期"直接 raise。
  5. 双口径: 预告+正式并存样本, as_first_reported 取预告且带 is_preliminary 标记; as_latest 取正式; 仅有晚于参考日申报的期→as_first_reported 置缺失(不复现 OpenBB 更晚兜底)。
  6. 拖带守卫: 喂"期末后 500 天重述值"断言被 450 天守卫拒。
  7. 增量友好: 新数据 append 带新 revision_seq, 重跑幂等(第二遍新增行=0)。
- **预计时长**: 2-3 天。
- **回滚方式**: 新 parquet 表+新 API 均为 additive; 现行 PEAD 研究读取路径不改, spike 判负则删除新表/新模块, 零残留。

### Spike-3 · provider contract: 日线+行业+公告各一, 统一 schema+跨源对账

- **目标**: 验证合流B契约层在三类异构接口上可统一: ①日线(tushare 代理 daily + akshare stock_zh_a_hist 双源)、②行业(股票基础/行业归属, tushare stock_basic+行业字段 vs akshare stock_info_a_code_name 名称对照)、③公告(stock_yjyg_em, 数据中心族)。证明 StrictRow+FetchMeta+双通道+跨源对账在现有拉取入口外可包一层而不动存量。
- **输入**: openbb.md §2.0 共通骨架(StrictQuery/StrictRow extra='forbid'/FetchMeta 必填/TET 三段/自检 harness 阶段隔离断言)+§2.1 DailyBarV1 字段表含涨跌停例外表+§5 批量七条检查单; akshare_audit.md §5 契约 yaml 形状+三族断言设计+§3 对齐核查点(单位契约/implied-hand 自检原样纳入); upstream_diff.md §3.3 ProviderAttempt 字段表(血统与熔断挂同一 FetchMeta); nautilus.md §1.2 R1 容差规则(重叠段 1e-4)。
- **验收条件(可判真伪)**:
  1. 三接口自检 harness 全绿: T 段入参恒等取回/E 段 raw 中无 Row 实例/T2 段行级不变式+failures 勾稽(len(keys)==成功+失败+显式空载荷)。
  2. 毒样本三件全拒: 喂 12 列 kline 假响应→日线契约拒; 喂未来日期行→日历外日期拒; 喂 27/29 列 yjyg 假响应→列数断言拒。
  3. 跨源对账: 同日同票日线, tushare 与 akshare 不复权价重叠段 |相对差|≤1e-4 通过, 人工造 0.05% 差样本必须被拒+硬告警; 行业/名称双源不一致清单落 manifest(允许存在, 必须显式)。
  4. PIT as-of 断言: 三表 `available_at > as_of` 记录数==0。
  5. 涨跌停例外表正反: 科创板上市第 3 日 +35% 行通过且带 limit_exempt; 普通主板 +25% 行必须 failure。
  6. 血统: 任一拉取在 run_manifest 附表可查完整尝试链(含失败尝试); 血统写入失败本身产生 WARN。
- **预计时长**: 2.5-3 天。
- **回滚方式**: 契约层为现有拉取入口外的包装(不动 parquet 存量与 data_adapter 签名), 带直通开关——关闭开关即回旧路径; spike 判负删除包装层即可。

### Spike-4 · protection shadow: 三层闸影子

- **目标**: 验证合流C保护链(检测→状态→执行三层+通知)在影子模式下端到端成立: strategy_locks 表+ProtectionEngine+MemLockStore 对历史台账重放, 回答"如果当时有保护会挡掉什么", 不接生产发布闸。
- **输入**: freqtrade_protections.md §3.1 表 schema(15 字段+2 索引)+§3.2 十条冻结边界语义(日历化)+§3.3 LockStore/IProtectionRule/ProtectionEngine 接口形状+§3.4 首批 4 保护器(ConsecutiveLossGuard/StrategyDrawdownBrake equity 唯一口径/LowEdgePause/PostFailureCooldown)+§3.5 影子三列报告+§5 单测清单; §2 语义表 19 条(实现基准); lean.md §2.3(风险状态落库定稿佐证); nautilus.md §2.2(deny 格式 CATEGORY: k=v 对齐, 影子期先不建 denials 表)。
- **验收条件(可判真伪)**:
  1. §5 单测清单中 S 系 6 条+O 系去重吸收 2 条+falsy-zero 3 条(10-12)+双口径 4 条(13-16)全绿, 其中 †反转条目(阈值含等号/回撤恰等于阈值触发)带对照注释。
  2. 确定性: 同一历史台账两次影子重放输出逐字节一致。
  3. 隔离: 影子运行与生产 DB 零共享状态(MemLockStore 注入, 进程结束无残留文件)。
  4. 产出三列报告(锁生效日/被挡推荐/被挡推荐后验超额)且至少覆盖 60 个交易日台账; 报告中被挡样本可逐条溯源到触发规则+reason_code。
  5. 重启恢复(DbLockStore 分支): 落锁→杀进程→新进程判定一致。
  6. INSUFFICIENT_DATA 与 NO_DRAWDOWN 事件分别可观测(修 fail-open T3)。
- **预计时长**: 2.5-3 天(表+引擎 1-2 + 影子 1 + 单测 0.5)。
- **回滚方式**: 影子模式定义上不接发布链, 生产零影响; strategy_locks 表独立于 recommendations.db 主表区, 判负 drop table+删模块即回滚。发布合同第 18 族检查在 spike 通过并用户确认后才接线(单独 commit)。

### Spike-5 · 表达式 parity: 5 因子含 warm-up/缺失/停牌/复权/ties

- **目标**: 验证合流(W2-1+W1-3 交叉)的 parity 机制: required_lookback 声明式元数据+统一装载器+欠窗硬失败, 在 5 个真实因子上证明"任意查询窗取值恒等", 并把 warm-up/缺失/停牌/复权/ties 五类边界钉进回归测试——直接了结 #52 滚动窗事故债。
- **输入**: qlib.md §2.2 注册表字段(required_lookback/required_forward/lookback_basis)+load_panel_for 形状+4 验收条件+§2.1 qlib 反面(min_periods=1 欠窗静默漂移=我方硬失败的反向依据); qlib.md §3(横截面无状态算子, cs_rank 承载 ties); qlib.md §5.1 停牌契约(停牌日 NaN 禁 ffill); lean.md §5.2 口径 API(复权敏感因子强制声明 mode+asof); backtrader parity 惯例(黄金值截点断言, PHASE1_SYNTHESIS 电梯结论)。
- **因子选择(覆盖五边界)**: ①长窗滚动类(MA60+diff → warm-up); ②EWM/expanding 类(保守截断值+lookback_basis 注明 → warm-up 边界形态二); ③含缺失值处理的量价因子(NaN 传播 → 缺失); ④横截面 rank 因子(cs_rank → ties 处理声明+锁定); ⑤复权敏感动量因子(hfq 口径 → 复权)。停牌边界对全部 5 因子统一压测。
- **验收条件(可判真伪)**:
  1. 恒等回放: 每因子在 [T-250,T] 与 [T-5,T] 两查询窗下重叠日值逐元素相等(浮点 rtol=0)。
  2. 欠窗必炸: 面板起点裁到 ext_start 之后, 装载器 raise LookbackUnderflow 而非返回(毒样本)。
  3. 低报审计: 每因子跑 lookback+1 vs lookback 扰动, 值变则 required_lookback 低报, CI 拒绝。
  4. 停牌: 构造停牌样本(行情列 NaN), 因子输出为 NaN 且不被 ffill 污染; 复牌日值与"无停牌对照序列"差异可解释。
  5. 复权: 因子⑤在除权日前后, raw 与 hfq 口径信号差异显式断言(值域或符号), qfq 不带 asof 的调用直接 raise。
  6. ties: 因子④对并列值的 rank 语义(average/min/dense)在注册表声明并单测锁定, 换 pandas 版本重跑值不变。
  7. 未注册禁入: 无 required_lookback 的因子进闯关机被拒。
- **预计时长**: 2-3 天。
- **回滚方式**: registry 元数据+新装载器为新增模块; 现有因子计算脚本不改, 直至全部验收通过后由单一 commit 切换调用点——revert 该 commit 即回滚; #52 回归测试保留为永久资产(不随回滚删除)。

---

## 4. 轨道状态

```json
[
  {"track": "upstream_diff", "status": "pass"},
  {"track": "qlib", "status": "pass"},
  {"track": "openbb", "status": "pass"},
  {"track": "nautilus", "status": "pass"},
  {"track": "lean", "status": "pass"},
  {"track": "freqtrade_prot", "status": "pass"},
  {"track": "tradingagents", "status": "pass_after_redo"},
  {"track": "akshare_audit", "status": "pass"},
  {"track": "gaps_ledger", "status": "12_closed_1_partial_no_debt_to_phase3"}
]
```

补充判定依据: tradingagents 轨道经 r2-r4 三轮修订后通过——修订内容(度量口径两层化、微机制实核降级、切片断言修正)全部有实测证据支撑, 且留下"立项前实读自家代码"的流程教训, 已在 §2.3 W3-6 固化。8 轨全部满足: G 条目闭合声明+coverage_confession+Wave 映射三件齐备、引证绑定冻结 SHA、许可纪律声明在案(GPL/LGPL/AGPL 三仓零代码复制)、防重复建设对照 system_inventory 只提升级差距。

## 5. 遗留事项清单 (非欠账)

1. **用户拍板项**: red_lines.md OpenBB AGPL 勘误行(1 行, G10 附带); MARKET_STRESS 占比阈值(W2-10); pipeline 三态开关自动触发(W2-7); C' 立项与成本(W3-1, 条件触发)。
2. **移交立项**: tushare 代理契约审计(stk_auction_o/c 单位+stk_mins 8000 行翻页边界, G8 移交, 归 Phase4 provider contract)。
3. **实施期速读件**(各轨道 confession 建议, 挂对应 Wave 条目开工前): 上游 test_trading_calendar/test_backtest_engine 边界断言(U-3); NT reconciliation 性质测试思路(W2-4); freqtrade DB migrations 演化史不查(接受, 影响面=表 schema 设计已独立)。
4. **引证规范前瞻生效**(G11 处置): Phase3 起 path 必须仓内路径或完整 URL+行号/函数名锚。

---

## 6. 用户拍板决议 (2026-07-11)

1. red_lines.md OpenBB AGPL 勘误行: **批准**(已回写)。
2. MARKET_STRESS 阈值: **看 spike-4 影子分布再定**(3%/5%/8% 三档历史触发分布+被挡样本后验超额, 数据定档后接线)。
3. pipeline 三态开关: **手动档先行**, 自动触发延后至保护器影子验证成熟。
4. AI 消融格局: **同意 A vs B vs B+**; C' 不预先立项, 条件触发时另拍成本。
