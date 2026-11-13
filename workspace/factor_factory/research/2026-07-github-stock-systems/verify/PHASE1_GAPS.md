# PHASE1 完整性缺口清单（批评家复核）

- 复核人: 完整性批评家（独立会话）
- 复核时间: 2026-07-11
- 复核范围: cards/ 全部 14 张 .card.json ↔ manifest/repos.json 14 仓 ↔ schemas/repo_finding.schema.json 六维契约
- 方法: 机械校验（repo/head_sha 一致性、六维齐全度、anti_patterns≥1、citations≥5、pain_point 枚举、confession 长度、维度文件路径线索）+ 逐卡对照 manifest 入选理由（why/role/tier）的内容深度审读

## 总判定

**无缺卡、无 schema 违规、无 SHA 漂移。** 14/14 卡通过机械校验：六维全部有实质内容且带文件路径，反模式全部带 SHA 绑定 evidence，coverage_confession 全部详实（无一张敷衍——最好的几张还记录了取数通道污染与改走 gh api 的过程）。发现的缺口全部是**内容深度级**：卡自认的盲区恰好压在该仓 manifest 入选理由上、或 manifest 承诺的范本只有一半被兑现。按严重度排序如下。

---

## S1 高严重度（缺口压在该仓的入选理由上，Phase2 前应补）

### G1. ZhuLinsen/daily_stock_analysis — 上游迁移决策缺 delta 实证
- **缺口**: 该仓 tier=A-upstream，入选唯一理由是"本地 fork（local_head c7059b5，9改+5自有文件）的迁移决策"。卡给出了明确建议（detached 冻结+定点移植三块资产），但 confession 自认"未逐 commit 对比本地 fork c7059b5 与上游差异（~309提交仅为任务给定值）"。
- **为何要紧**: 迁移建议建立在零 diff 实证上——本地 9 个改动文件与上游对应文件的冲突面、5 个自有文件依赖的上游接口是否已被重构，都未验证；"定点移植三块"清单可能漏项或不可行。
- **建议动作**: Phase2 补一次窄 diff 审计：仅对本地 9 改文件 + data_provider/trading_calendar/decision_signal 三块移植目标做 c7059b5↔d083748 对比，半天量级。

### G2. TauricResearch/TradingAgents — 多 agent 辩论核心（提示词+工具语义）整块未读
- **缺口**: 入选理由是"Wave3 AI 消融试验的 C 方案原型"，但 confession 自认 agents/analysts、researchers、risk_mgmt 的**具体提示词文本**全部未读，agent_utils.py（工具定义主体）未读，graph/setup.py 建图细节仅据 CHANGELOG。
- **为何要紧**: 六维卡对"辩论框架"只有结构级描述（几轮、谁对谁）；C 方案消融真正要抄/要证伪的是辩论提示词设计与工具喂给方式——原型的核心恰在盲区里。AI角色边界维度的借鉴（verified snapshot/结构化输出）成立，但属外围。
- **建议动作**: Phase2 若 C 方案立项，先补读 agents/ 提示词全文与 agent_utils.py（约 1 天）；若不立项，在卡或 verify 记录中显式标注"辩论机制内核未验，C 方案设计不得引用本卡做依据"。

### G3. QuantConnect/Lean — manifest 承诺的"风险模型接口范本"只兑现了一半
- **缺口**: manifest why="数据分层 Raw->Canonical **与风险模型接口范本**"。数据分层做得范本级（factor file/map file/SecurityIdentifier 三层，5 条 citation）；但风险模型接口只有 architecture 维度一句带过（"RiskManagementModel 接口（如 MaximumDrawdownPercentPortfolio/TrailingStop）"），**零 citation 锚定 Algorithm/Framework/Risk 任何文件**，relevance_to_us 也没有 Framework 五段流水线（Selection→Alphas→Portfolio→Execution→Risk）的对应条目。confession 未把该目录列入已读清单。
- **为何要紧**: 抽检者无法在冻结 SHA 下定位风险接口主张；Phase2 若按 manifest 理由引用 Lean 的风险模块设计，引证链是断的。
- **建议动作**: 补读 Algorithm.Framework/Risk/*（文件很小，2-3 小时），给卡补 1-2 条 citation 与一条 relevance；或降级 manifest 该仓 why 的后半句。

### G4. OpenBB — AI/MCP 面整块缺失（description 主打方向未覆盖）
- **缺口**: confession 自认"mcp_server/platform_api 扩展只看了目录名未读实现（**AI角色边界维度因此没写进卡**）"；且 33 个 provider 只细读 yfinance、fmp 片段，卡自己点名"SEC/FRED 等 PIT 相关度更高的官方源未读，SEC provider 可能有公告日 PIT 处理值得后续补读"。
- **为何要紧**: 该仓 description 即"Open Data Platform for analysts, quants **and AI agents**"，OBBject.to_llm/MCP server 是其 2026 年主打演化方向，与我方 AI角色边界痛点直接相关，却在 14 卡中唯一主打 AI 数据接口的仓上留白。SEC provider 的公告日处理是 provider 契约范本（Phase4）+ PIT 两个痛点的交集样本。
- **建议动作**: Phase2 provider 契约设计前补读 openbb_sec 的 filings fetcher 与 mcp_server 入口（半天）。

---

## S2 中严重度（自认盲区削弱该卡结论的适用边界，引用时需带脚注）

### G5. vnpy — market_assumptions 结论只覆盖核心仓，插件生态未验
- 卡自认：带滑点参数的 CTA 回测器在 vnpy_ctastrategy/vnpy_ctabacktester 等 50+ 插件仓，不在冻结 SHA 内；"涨跌停/停牌/T+1 缺陷是否在插件仓被修补未验证"。反模式#2（±10% 硬编码、无 T+1）的表述对 vnpy 生态整体不成立的风险已由卡自认。另：反模式#5"社区长期抱怨事件引擎吞吐"无 issue 链接（卡自认背景知识），是 14 卡全部反模式中唯一 evidence 不含可定位锚点的社区主张。
- 动作：引用 vnpy 反模式时限定"核心仓 alpha 模块"；吞吐主张要么补 issue 链接要么降级为推断。

### G6. microsoft/qlib — parity 范本的横截面半边未验证
- 卡自认：qlib/data/dataset（handler/processor/Alpha158-360 特征工程）未读，"表达式之上的横截面处理层，与 parity 维度相关但未验证"。而 manifest why 三重范本之一就是"表达式 parity"——卡已明确 Rank 是时序 rank、横截面在 DataHandler 层，恰好把 parity 最关键的横截面环节留在盲区。
- 动作：我方 #52 滚动窗事故的结构性解法引用（扩窗机制）证据充分可用；但"因子 parity 完整方案"引用前需补读 handler/processor（半天）。

### G7. nautechsystems/nautilus_trader — "四对象契约"的第四对象与撮合内核未验
- 卡自认：accounting crate 未读（账户/结算对象实际未验证）、matching_engine 内部未读（只看 exchange.rs 外壳）、order_emulator/order_manager 未读。manifest why 是"portfolio/risk/order 四对象契约范本"——Order/Risk/Portfolio 三对象引证扎实，账户对象缺位。另：catalog.rs 的 are_intervals_disjoint 定义行号为 grep 近似（~L4798），抽检可能定位失败。
- 动作：Phase2 若临摹四对象契约，补读 accounting crate 的 Account/结算语义（半天）；近似行号改为函数名锚。

### G8. akfamily/akshare — 我方在用接口清单未逐个覆盖（竞价/分钟盲区）
- 卡精读了日线主链路（stock_hist_em）+HTTP 三层+日历，结论扎实；但 manifest why 点名"竞价数据前科（stk_auction_c）、新接口纳入按 spike 流程"，而我方实际重仓的集合竞价/分钟/停复牌接口（如 stock_zh_a_hist_pre_min_em、stock_tfp_em）未审计——1268 接口只读约 10 个，futures/option/fund 等子树全部未读（后者与我方无关，可接受）。
- 动作：卡在 relevance 里已把"存量约 10 个依赖接口逐个建冻结契约"列为一周动作——建议把"我方依赖接口清单"本身先落成文件（1 小时），防止 Phase2 审计范围再漂移。

### G9. freqtrade — 专项边界略窄：pairlist 姊妹体系与保护测试未读
- 该仓 tier=A-专项（protections），聚焦合理；但卡自认 pairlist 过滤器体系（protections 的姊妹插件，承担 universe 级保护）未读，tests/plugins/test_protections.py 未读——卡建议我方"零 GPL 代码自研临摹契约"，而锁的边界语义（截止时刻闭开、去重条件）未从官方测试断言核实，临摹时有踩语义歧义的风险。
- 动作：自研 strategy_locks 前速读 test_protections.py 的边界用例（2 小时），把断言翻译进我方单测。

### G10. manifest 与卡结论不同步 — OpenBB 许可门已核实但未回写
- OpenBB 卡完成了 manifest 留的作业：LICENSE 明文 AGPL-3.0（GitHub NOASSERTION 系自定义序言所致），应按 copyleft 红线处理。但 manifest/repos.json 的 license_gate 仍写"NOASSERTION(自定义许可)——待深读核实条款"。冻结契约要求 Phase1/2 引证绑定 manifest，两处不一致会让后续引用者各信一边。
- 动作：在 manifest 追加 license_gate_verified 字段或在 freeze_note 下补一行勘误（不改冻结时点值），1 分钟。

---

## S3 低严重度（引证卫生与归类瑕疵，不阻塞）

### G11. 个别 citation 锚点可核性偏弱
- akshare 卡 citation#11 path="github issues"（非仓内路径、非完整 URL，anchor 里才有 issue 号）；myhhub 卡 Dockerfile citation 无行号（以"RUN echo crontab 段"描述定位）；nautilus catalog.rs 近似行号（见 G7）。均可按 claim 内容定位，但不符合"anchor 足以让抽检者在冻结 SHA 下定位"的严格口径。
- 动作：抽检时对这三处按内容搜索；后续卡规范 path 填完整 issue URL。

### G12. 痛点归类两处偏松（跨卡汇总时会散失）
- backtrader relevance#3 把滑点/成交量参与度三件套归入"其他"（更贴切的是"组合/风险/订单契约"，且与 B1 滑点标定直接相关）；FinGPT relevance#3 把东财新闻接口接入归入"provider契约与跨源对账"（实为新数据源引入，对账属性弱）。枚举合法，非违规，但 Phase2 按痛点聚合可借鉴项时，滑点标定素材会漏检。
- 动作：Phase2 聚合时对"其他"桶人工过一遍即可，不必改卡。

### G13. 集合层面的痛点样本薄区（非单卡缺陷，Phase2 综合时注意）
- 14 卡汇总后："实验管理与防重测"仅 qlib 一家提供正面范本；"run_manifest与血统"仅 qlib+TradingAgents 两家。恰好 Lean 的 ObjectStore/Report/Research 三子系统、nautilus 的 event_store 均在各卡自认未读清单里——这两个痛点若 Phase2 需要更多样本，补读入口已知。
- 另注：backtrader"社区另立 backtrader2 fork"、zipline"Quantopian 2020-11 关停"等公域常识主张未在仓内二次验证（卡已自认），引用时保持"公域常识"定性。

---

## 汇总表

| # | 仓 | 缺口类型 | 严重度 | 建议耗时 |
|---|---|---|---|---|
| G1 | ZhuLinsen/daily_stock_analysis | 迁移决策无 diff 实证 | 高 | 0.5天 |
| G2 | TauricResearch/TradingAgents | 辩论提示词/工具内核未读 | 高 | 1天(或标注禁引) |
| G3 | QuantConnect/Lean | 风险模型接口范本零引证 | 高 | 2-3小时 |
| G4 | OpenBB-finance/OpenBB | AI/MCP 面+SEC provider 未读 | 高 | 0.5天 |
| G5 | vnpy/vnpy | 结论边界限于核心仓;1条社区主张无锚 | 中 | 标注即可 |
| G6 | microsoft/qlib | parity 横截面层未验证 | 中 | 0.5天 |
| G7 | nautechsystems/nautilus_trader | accounting 未读+近似行号 | 中 | 0.5天 |
| G8 | akfamily/akshare | 我方在用竞价/分钟接口未审计 | 中 | 先1小时列清单 |
| G9 | freqtrade/freqtrade | 保护边界语义未从测试核实 | 中 | 2小时 |
| G10 | manifest 同步 | OpenBB AGPL 核实结论未回写 | 中 | 1分钟 |
| G11 | akshare/myhhub/nautilus | citation 锚点可核性弱 | 低 | 抽检时留意 |
| G12 | backtrader/FinGPT | 痛点归类偏松 | 低 | 聚合时过滤 |
| G13 | 集合层面 | 防重测/血统痛点样本薄 | 低 | Phase2 择读 |

## 批评家结论

Phase1 广度扫描**整体合格，可进入抽检/Phase2**。14 卡无一缺失、无一 schema 违规、confession 全部诚实（多张卡的 confession 质量本身就是范本）。四条高严重度缺口的共性是：**卡的深读预算流向了"六维好答的部分"，而 manifest 给每仓定制的入选理由（迁移 delta、辩论内核、风险接口、AI 数据面）恰是最贵的部分被 confession 合法豁免了**。建议 Phase2 深读优先级直接按 G1-G4 排，合计约 2.5-3 天可全部堵上；G10 的 manifest 勘误应立即做，防引证分叉。
