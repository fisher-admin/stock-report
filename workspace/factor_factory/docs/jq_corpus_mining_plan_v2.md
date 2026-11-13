# 聚宽语料(2020-2026)因子元素图谱 + 创造性合成方案 v2.0

## Context(为什么做)

用户下载了聚宽 2020-2026 年度精选策略源码(`~/Downloads` 8 目录,713 个 txt,每个 = 完整聚宽策略 Python 源码)。**用户明确校准的目标**:"学习这些策略的因子组成、所考虑的各种要素,通过此优化出几个最佳的因子组合策略——不是逐个研究现成策略的好坏,要创造性地开展工作。"

因此本方案的核心产出是两层:
1. **因子元素图谱(Factor Element Atlas)**——把 713 个策略拆解成原子要素(信号因子/universe过滤/时点结构/风控出场/仓位管理),统计频率、共现、按年演化,形成"全市场实战者到底在用什么要素"的完整地图;
2. **创造性合成 2-5 个我们自己的因子组合策略**——从图谱要素 + 我们的独有数据资产(分钟/竞价/滑点标定) + 我们的证伪知识库中合成新组合,经预注册+五道闸+holdout 产出判决,最优者上前瞻观察看板。

**第一原则不变**:语料 = 想法池/要素库,不是证据(实测:滑点普遍设 ~0,"精选"=发表偏差)。所有证据由我们的引擎重新产生,管线本身不产出任何收益数字。

**用户已拍板**:非个股类(ETF/期货/可转债/北交所)只编目归档不深挖;最终报告整合进 GitHub 发布页。

## 已核实的语料画像

- 713 文件 / ~11MB;~80% 头部带克隆 URL/标题/作者(正则可提取);~5% 完全重复
- 家族词频:涨停 257 / ETF 127 / 轮动 106 / 尾盘 102 / 分钟 94 / 小市值 83 / 期货 44 / 多因子 35 / ML 26 / 打板 25 / 北交所 21 / 竞价 19
- 买入时点:**14:30-14:59 共 48 个**(14:50、14:30 各 25),09:30 共 17,86% 日度调仓
- 接口:get_fundamentals 43% / technical_analysis 17% / get_industry 17% / get_call_auction 1.6%
- **头号结构性发现(本人复核原文)**:尾盘决策(14:45-14:50 用当日 1min 数据)→尾盘入场→持仓过夜→次日卖。**决策点挪进尾盘 = 把我们证伪为"不可成交"的隔夜段溢价(涨停溢价+1.75%所在)变为可成交的唯一合法路径,registry 从未测过该决策点。** 这是合成阶段最重要的结构要素。
- ⚠️ 编目必须确定性 AST 解析(Explore agent 曾误报"尾盘次日卖=0个",实际语料就有该文件)

## 与冻结证据的关系(纪律不变)

已 REJECTED/证伪:日频价量启动形态、涨停打板/一进二、分钟级追启动、S6/S5/S4、基本面慢风格长多。在席:S3(CANDIDATE 前瞻中)、WTS(裁判席禁碰)、S2(DEFERRED)。**合成的新组合必须与死者有明确区别**(区别度落在:新决策点结构/新信息集如14:45分钟级增量/新执行段),说不清区别=不予注册。

## 管线设计(六阶段)

```
P0 冻结快照 → P1 编目+要素拆解(AST全量713) → P2 图谱构建(去重/聚类/频率/共现/年度演化)
→ P3 要素过滤(可成交段/数据可得/死者标记) → P4 创造性合成(2-5个组合设计卡,fable主笔)
→ P5 预注册+前置工程+五道闸+holdout → P6 报告+图谱上发布页
```

### P0 语料冻结快照(机械)
713 txt 只读复制 → `stock_data/03-working/jq_corpus/raw/` + `MANIFEST.json`(sha256/字节/原路径/编码探测)。此后只引用 file_id。编码失败单独列清单。

### P1 编目 + 要素拆解(确定性解析;质量门=全管线唯一不可妥协)
- **Tier A 机械脚本** `jq_corpus/scripts/catalog_parser.py`(`ast.parse`):每文件提取 → 元数据 / 调度表(run_daily 时刻归一化) / 下单点(调用图回溯→执行时刻) / **决策-执行结构(decision/entry/exit/overnight)** / universe 过滤链 / **信号原子 signal_atoms**(数据源+字段+窗口+运算+原文片段) / 风控出场原子(止盈止损/移动止损/时间出场/条件出场) / 仓位原子(等权/满仓轮动/分批) / 接口依赖 / 成本设置(滑点≈0 打 `cost_dishonest`) / parse_status。产物:`catalog/files/*.json` + `catalog.parquet`。
- **Tier B sonnet agent** 补录 AST 失败/ambiguous 文件(~10-15%),同 schema,标 `parse_tier:"B"`。**要素拆解的完备性在 Tier B 加强**:signal_atoms 是图谱的原料,机械抽不全的由 agent 按模板补齐(仍带原文片段溯源)。
- **验收先于全量**:20 文件金标准,关键字段 ≥95% 才放行;金标准含《尾盘买入-次日早盘卖出策略》必须编目为 `{尾盘决策,尾盘入场,次日卖,overnight=true}`。

### P2 因子元素图谱(本方案的"学习"交付物)
- 去重:sha256 + token 流 5-shingle Jaccard≥0.8 成簇(~25万对精确算)。
- **要素归一化**:signal_atoms 映射到受控词表(如 量比/换手率分位/N日动量/均线关系/市值排序/涨停回踩/竞价高开率/资金流向/PEG/ROE筛选…预计 60-120 个原子词条),每词条带:出现文件数、所在家族分布、**按年演化曲线**(2020-2026 谁兴起谁退潮=拥挤度/regime信号)、典型参数取值分布、原文示例。
- **共现矩阵**:哪些要素总是成对出现(如 小市值×ST过滤×月度轮动;尾盘量比×次日早盘卖),挖掘实战者的"组合语法"。
- **结构轴统计**:决策时点×入场时点×出场规则×隔夜与否 的结构细胞分布。
- 产物:`atlas/factor_atlas.parquet` + `atlas/ATLAS.md`(人读版图谱:词条/频率/共现/演化/示例)+ `families/clusters.parquet`。
- 验收:图谱词条覆盖率(签名原子被词表覆盖的比例)≥85%;交叉表与词频画像量级吻合。

### P3 要素级三重过滤(给图谱上色,不是删数据)
每个词条/结构细胞打三个标签(图谱保留全量,标签供合成时取用):
- **可成交段**:绿=T+1开盘可执行或尾盘可执行;黄=需重构(如"收盘价成交"幻觉→重构为14:45决策);红=不可成交(封板买入/隔夜段无入场)
- **数据可得**:绿=panel/minute/auction 现有资产;黄=需新工程(如收盘竞价须 stk_mins 14:57-15:00 聚合+语义审计)或待权限确认(moneyflow/龙虎榜/两融);红=不可得
- **死者关联**:标注与 registry REJECTED/冻结证伪的信息集重叠度(necromap.json 死亡签名三元组比对);红=与死者同构
- 产物:`filters/necromap.json` + 图谱上色列 + `filters/filter_report.md`

### P4 创造性合成(fable 主笔,opus 对抗评审)——本方案的核心创造环节
从图谱中合成 **2-5 个组合设计**,每个 = 一张 `synthesis_cards/SC_*.yaml`(信号组合定义/决策-执行结构/经济学先验/vs_necromap 区别论证/数据需求/容量粗估/格点草图)。合成的原料三源:
1. **语料图谱要素**(绿标优先;共现语法借鉴;年度演化避开已退潮拥挤要素)
2. **我们的独有资产**(全市场竞价数据/800只分钟深度/B1滑点表/S3已验证的分时形态特征——语料作者们没有这些)
3. **我们的证伪知识**(死者名单=负空间地图,告诉我们哪里不用去)

合成方向预判(最终以图谱为准,此处仅示例):
- **SC-A 尾盘决策组合**:14:45 可知分钟级要素(量能结构/路径形态/尾盘承接)× 图谱高频尾盘要素 → 尾盘入场持隔夜 → T+1 卖。吃的是隔夜段溢价的可成交重构,registry 空白决策点。
- **SC-B S3 增强组合**:图谱要素中与 S3 三特征正交的候选(如竞价确认/位置结构)→ 作为 S3 前瞻之外的独立新格(新 id,不碰 S3 冻结定义)。
- **SC-C 图谱共识组合**:跨家族最高频共现的"组合语法"(universe 净化链 + 时点结构 + 1-2 个信号原子)的最诚实版本——检验"群体共识要素在真成本下还剩多少"。
- 每张卡必须通过监督者死者比对签字才能进 P5;卡片模板结构性排除收益数字。

### P5 预注册 + 前置工程 + 闯关(纪律照旧)
- 预注册:新 id(S7 起),逐字沿用 registry 惯例;event_universe 默认口径+尾盘组合加"14:45 未封板(价<涨停价×0.99)、T+1 非一字";格点全入账(每组合 ≤12-18 格);success_criteria 沿用五道闸;**格点冻结先于数据接触**;registry 写入仅监督者。
- 前置工程(硬依赖):**B2 尾盘滑点标定**(复用 `calibrate_slippage.py` 方法论,决策价=14:45 bar 收价,成交价=14:50-14:55 VWAP,尾盘不利漂移计入成本,输出 `health/slippage_calibration_late.json`,B1 不动);**尾盘特征库** `late_features.parquet`(仿 `build_intraday_shape_features.py`,全部"14:45 可知",bar 时间戳语义审计先行,holdout 特征 gate6 前禁建);若用收盘竞价先做 stk_mins 尾段语义审计。
- gauntlet 接入(照 S3 模板):`load_setup_s7()`+`run_s7()`+dispatch;闸1 对"持仓过夜"结构的规则改写预注册冻结;成本=8bp单边×2+B2/B1 双侧滑点;`--selftest-random` 必须 REJECTED;med 主判 p75 敏感;幸存格 gate6 holdout 一次性复现;CANDIDATE 接前瞻 ledger 60 笔审判 + 观察看板(同 S3 制)。

### P6 报告 + 上发布页
- `CORPUS_MINING_REPORT.md`:漏斗全账(713→图谱→合成卡→预注册→判决,全部入账)+ 图谱精华 + **永久防重测地图**(家族×结构细胞→死者/冻结证据交叉映射;零幸存也交付)。
- 发布页整合(用户已拍板):图谱精华+合成判决进 research-lab/setup-engine 版块(照因子策略重造方案先例);只改 `workspace/stock-report` 克隆;发布前过 `validate_publication_contract.py`。
- memory 更新:图谱结论+合成判决写入 `data-assets-and-wts.md` 关联条目或新条目。

## 模型/agent 分配(沿用用户既定规则)

| 工作 | 执行者 |
|---|---|
| P0/P1-TierA/P2统计/P3标注/necromap/B2/特征库(脚本编写运行) | sonnet |
| P1-TierB 补录 / 要素归一化兜底 / 深读溯源(多 agent 按文件所有权并行) | sonnet |
| 代码评审 / 图谱质量评审 / 合成卡对抗评审 / 判决复核 | opus |
| **P4 创造性合成主笔** / 死者比对裁决 / 格点冻结 / 预注册 / 终审 / 全程监督 | fable(本人) |

## 复用的既有资产

`setup_registry.json`(预注册惯例) / `setup_gauntlet.py`(五道闸+SlippageTable) / `calibrate_slippage.py`(B2 模板) / `build_intraday_shape_features.py`(特征库模板) / `minute_cache*`+`auction_cache`+`panel_full` / B1 滑点表(同构扩展)。

## 产出物

```
stock_data/03-working/jq_corpus/{raw+MANIFEST, scripts/, catalog/, atlas/(factor_atlas.parquet+ATLAS.md), families/, filters/, synthesis_cards/, CORPUS_MINING_REPORT.md}
factor_factory/docs/jq_corpus_mining_plan_v2.md(方案定稿)
factor_factory/config/setup_registry.json(新增 2~5 条,监督者写)
stock_data/03-working/health/slippage_calibration_late.json(B2)
stock_data/03-working/late_features.parquet
stock_data/03-working/gauntlet_reports/setup_S7_*
stock-report 发布页 research-lab/setup-engine 版块更新
```

## 验证方式

- P1:20 文件金标准 ≥95%(含尾盘锚点文件)才放行全量;P2:词条覆盖率 ≥85%+量级核对
- 每 .py 过 `py_compile`;`--selftest-random` 必须 REJECTED;B2 量纲自检(尾盘滑点应<开盘档,反常即审计)
- gate6:冻结先于拉数;registry 判决带署名日期;发布前合同校验 0 fail
- 全程条令 v1.1(磁盘产物为准/断言配证据/禁趴窝/卡两次上报)

## 诚实预期

图谱是**确定能交付的**(713 个策略的要素解剖学,本身就是资产:拥挤度演化+组合语法+防重测地图)。合成组合的闯关先验成功率低是结构性的(高频家族多落在已证伪区);**最可能结局 = 图谱 + 2-5 个合成组合全账判决,其中 0-2 个 CANDIDATE**。头号合成方向(尾盘决策)的死穴按概率:①尾盘不利漂移+滑点吃掉隔夜溢价(B2 见分晓);②14:45 特征退化为日频价量换皮(死者比对拦截);③时序自相关假阳(安慰剂闸拦截)。三者均有闸门,死了也如实入账。
