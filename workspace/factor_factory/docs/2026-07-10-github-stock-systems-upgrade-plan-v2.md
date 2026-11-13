<!-- 定稿版(Phase 0 落盘, 2026-07-11): 由批准的 v3 plan 冻结而来。
执行修订: 用户 2026-07-11 拍板取消 Phase -1 的3交易日观察等待, 立即全量推进(生产观察与研究并行)。
研究合同工件: factor_factory/research/2026-07-github-stock-systems/{manifest,schemas,local}/ -->

# GitHub 头部股票系统研究 + 系统全面升级 — 最终方案 v3(我方 v1 × codex v2 评审综合)

## Context

用户要求研究 GitHub 前 10 股票系统项目并提出全面升级实施方案。我方 v1 方案经 codex 评审(v2, REVISE)后,我对其全部事实主张做了实测核验:**4/4 可验条目全部命中**——包括最关键的"今晚 0710 生产运行确实失败"(实测:AI 11/20 且 9 只 failed、db_sync 失败、validation 失败、base_with_partial_ai)。据此逐项裁决:**codex v2 的骨架全盘采纳**,少数条目按我方既定约束修正。完整评审文档在 `/Users/fisher/Documents/openclaw/.omx/plans/2026-07-10-github-stock-systems-upgrade-plan-v2.md`,本 plan 为裁决与执行版。

## 裁决表(v1 → v3 变更)

**全盘采纳(codex 正确)**:
1. **新增 Phase -1 恢复生产绿灯**——今晚 0710 实测在坏,升级前必须先修(9 只 AI failed 根因诊断/db_sync 失败/发布仓收口),连续 3 交易日无同类故障才进研究
2. **名单**:主 10 = TradingAgents/OpenBB/daily_stock_analysis上游/freqtrade/qlib/vnpy/**NautilusTrader(24.6k,LGPL)**/backtrader/akshare/**Lean(20.4k,Apache)**;FinRL/myhhub/FinGPT/zipline 降为补充样本(必须披露不得悄删);所有引用绑定冻结 SHA
3. **上游差异 = 独立迁移项目**(非 quick-win cherry-pick):本地 9 改+5 自有文件+自有提交(compare 404 证实),先共同基线→能力目录→冲突矩阵→scratch worktree 逐切片 backport+tests 回归
4. **PIT 字段设计升级**:`event_time/effective_time/available_at/observed_at/ingested_at/revision/source` 双时间+修订,as-of API 过滤未来可得;但保留渐进路线(先单数据集 spike,禁全量迁移)
5. **评分改硬门+加权**:先过 5 道硬门(不破冻结/无未来数据/许可证/可回滚/不重复建设),再加权评分(痛点缓解25%/数据可信20%/A股适配15%/可靠性15%/可逆性10%/维护5%/开发成本5%/运行成本5%),废除 价值÷(成本×风险) 公式
6. **许可证硬门**:GPL(backtrader)/LGPL(NautilusTrader) 默认思想借鉴/重写,不复制代码;研究卡强制许可证字段
7. **AI 多角色辩论降为最终可选消融试验**(Phase 6):上游 d083748 已实现 multi-agent 分歧摘要——先评估上游能力再谈自建;消融=A单pass/B上游multi-agent/C TradingAgents式,同一证据包盲测,只评结构化成功率/风险覆盖/成本/延迟,**不宣称 alpha**;AI 永不改冻结量化分数与 verdict
8. **akshare 新接口降为 spike**(PIT/单位/历史可得性审计先行,stk_auction_c 前科)
9. **数据分层目标架构**:Raw(不可变原始层)→Canonical PIT→Feature(definition_hash 血缘)→Operational metadata(SQLite WAL)→推荐仓库(append-only 幂等);DuckDB 只读侧车为后置可选
10. **补齐 research-live parity 清单/protection contract(reason code+恢复条件+shadow 先行)/anti-pattern 强制章节/run manifest 单源化**
11. **研究 schema 改 6 必答维度 100% 覆盖**(storage/architecture/computation/market-assumptions/adoption/anti-patterns),替代逐仓条数配额;保留我方 citation 抽检退回协议
12. **时间修正**:2-3 天广度扫描+3-5 天 Tier-A 深读+3-5 天 spike,总 7-11 专注工作日(不含 Wave 实施)

**修正采纳(按我方既定约束)**:
13. 发布页研究摘要:保留上页(我方发布页=诚实研究记录,有 honesty banner 与合同校验先例),内容限方法+路线图,无任何收益暗示——codex 的实质关切由既有合同保障
14. 模型分配(sonnet深读/opus红队+qlib、TradingAgents深读/fable裁决):保留在执行编排层(用户既定工作流),不写入长期架构契约——与 codex "不入契约"一致

**驳回**:无。

## 执行阶段(codex v2 §6 为准,要点)

- **Phase -1(立即, 0.5-2天+3日观察)**:修今晚 0710 故障(9 只 AI failed 根因:generator 已跑但 LLM 失败,查 litellm 稳定性/超时;db_sync UTF-8 解码错误;发布仓收口);DONE=validation_ok=true+20/20 ready+db_sync ok+连续 3 交易日无同类+发布仓 ahead=0
- **Phase 0(0.5-1天)**:研究合同冻结——`manifest/repos.json`(14 仓 SHA/星标/许可证/纳排理由)、`schemas/repo_finding.schema.json`、`local/system_inventory.md`(能力盘点防重复建设)、`local/red_lines.md`;落盘 `factor_factory/docs/2026-07-10-github-stock-systems-upgrade-plan-v2.md` 定稿版
- **Phase 1(2-3天)**:广度扫描——API/源码快照优先不全克隆,证据绑 SHA;14 张卡 6 维度全覆盖+每仓≥1 条 anti-pattern;磁盘≤2G/free<6G 停
- **Phase 2(3-5天)**:Tier-A 深读(上游 diff/qlib/OpenBB/NautilusTrader/Lean+freqtrade protections 专项)——上游差异报告(能力目录+冲突矩阵+keep/backport/rewrite/drop 逐文件判决)、数据库血缘 ADR、parity 清单、能力-痛点矩阵、许可证与安全审查(myhhub 拼接 SQL 等列反例)
- **Phase 3(3-5天)**:隔离 spike(不改生产,3-4 个并行):上游迁移 spike(1 能力切片 backport+全量测试)/PIT spike(PEAD 预告或股东户数,双时间+revision+as_of)/provider contract spike(日线+行业+公告各一,统一 schema+跨源对账)/protection spike(shadow portfolio 三层闸)/表达式 spike(5 因子 parity 含 warm-up/缺失/停牌/复权/ties)
- **Phase 4 Wave1(1-2周)**:数据可信与可复现——run manifest 单源化/PIT 上线(目标数据集)/provider 标准 schema+对账/推荐库幂等+备份+integrity/上游第一批已验证 backport
- **Phase 5 Wave2(2-4周)**:signal→target portfolio→risk decision→simulated order 四对象契约/protections shadow 生效/回测-shadow-复盘共用 A 股执行规则/Recorder 式实验目录+necromap 自动关联
- **Phase 6 Wave3(可选,1-2周+shadow)**:AI 消融试验(见裁决7)

## 验收(codex §8 全盘采纳)
PIT:50 事件×5 as-of 时点 available_at>as_of 记录=0+修订穿越测试;provider:20股×20日双源对账+差异超阈值进 quarantine 不静默选源;表达式:5 因子 60 日对账+NaN/warm-up/ties 全同;组合风险:T+1/涨停买不进/跌停卖不出/停牌/成本合成测试+protection reason code+故障注入;上游:逐文件判决+worktree 三重测试;AI 消融:结构化成功率≥99%+P95 成本延迟上限+risk veto 契约 100%+20 交易日×5 股 shadow 不宣称收益。

## 铁律(不变)
开源项目=想法与工程范本非证据;策略想法全过 necromap(说不清区别=死),幸存者 S9 起预注册五道闸;冻结契约/holdout/ledger/panel 只读;成本模型/阈值放宽+cron/发布行为变更+花钱=用户拍板清单;生产链不受扰,一切实验 scratch worktree/shadow;全程条令 v1.1(文件:行证据/抽检退回/禁趴窝/卡两次上报)。

## 风险
磁盘 9.1G(API快照优先/≤2G/串行蒸馏删除);上游冲突(worktree 隔离);许可证污染(硬门);多 Agent 成本(消融+回退);研究×生产互扰(独立目录+只读+shadow);重复建设(capability inventory 前置硬门);**策略层预期大量判死+0-2 幸存=合格产出,确定性价值在工程层**。
