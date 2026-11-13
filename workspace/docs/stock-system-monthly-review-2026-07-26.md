# 选股系统 + AI 深度分析系统 月度体检报告（2026-06-26 ~ 2026-07-26）

> 生成：2026-07-26（周日）· 方法：7 轨并行深读 + 3 轨对抗复核（10 agent，447 次工具调用）
> 复核结论：运行台账 8/9 主张坐实、AI 系统 6/8 精确吻合、策略质量 8/8 全部经独立重算确认。
> 本报告所有数字均带证据路径，关键数字经第二个独立 agent 重算。

---

## 一、总体判断（TL;DR）

1. **系统活着，且工程诚实性建设是真实资产**：21 个交易日里发布链跑到最后一个交易日 20260724，合同校验 18 组检查阻断式生效，诚实性护栏（禁买入 clamp、伪覆盖拦截、收盘一致性）全部在岗。
2. **但近月运行带三处结构性带病**：① O2C AI 合同不匹配 bug 导致每晚 45 分钟空转 + 约 30 个无效 push（占管线墙钟 68%）；② 7/24（最后交易日）主管线中途死亡且**三层告警全部漏报**；③ 调度器已从 openclaw gateway 迁到 hermes gateway，CLI 诊断链路失效、Discord 投递 7/26 全挂——**周一 7/27 开盘链路能否起跑未经验证**。
3. **策略质量结论残酷但清晰：近一月零可执行信号，所有走到前瞻 OOS 的策略全部为负**。瓶颈不在工程，在 edge 本身。系统当前的实质产出是"每日研究观察名单 + 一套持续判负的证伪账本 + 诚实性基础设施"。
4. **治理断档**：主仓 git 静默 15 天（7/11 起），7/15 事故修复等 6 处生产脚本改动以未提交形态裸奔；7/11 拍板的决议中"需要人拍板的全部停滞，机器自动的全部在跑"；PEAD PIT 摄取停摆导致 **约 9 个交易日 vintage 不可逆丢失且仍在持续**。
5. **一处高危安全隐患**：`~/.openclaw` 配置仓（git 历史含 credentials/*.json）的 origin 指向**公开仓** fisher-admin/stock-report——一次误 push 即泄露凭证历史。

---

## 二、近一月运行台账（2026-06-26 ~ 2026-07-24，21 个交易日）

| 指标 | 数值 |
|---|---|
| 晚间收盘链 | 完整成功 12 天 / 带缺陷成功 5 天 / 失败 4 天 |
| 晨判 08:00 | 成功 17/21 |
| 午盘 12:00 | 成功 16/21 |
| 发布模式 | full 11 天 / base_only 4 天 / partial AI 2 天 / 未发布 3 天 |
| 事故 | 11 起，8 起已修复+护栏落地，3 起未闭环 |
| 发布仓 chore 提交 | 375 个（21 个发布日均 17.9 个/日，峰值 7/17 达 89 个） |
| 真实告警发送 | 6 次（全部来自 data_freshness_watchdog，Discord+macOS 双达） |

**失败集中在两段**：6/29-7/1 三日停摆（strategySuite 失败、无晨判/午盘/晚链）；7/8-7/10 爆盘三连锁 + 盘中误发布（已回滚+护栏）。7/3 双事故（指数虚高、普涨日误判 risk_off）当周即修复并加收盘一致性硬告警。

**未闭环事故（3 起）**：
- **O2C 回填空转风暴**（7/15 起每晚）：`generate_o2c_ai.py:49` 写 `ai_source_kind='o2c_ai_analysis'`，而 `stage4_ai_backfill_loop.py:591` 判完成要求 `=='o2c_factor_llm_analysis'`——20 只 O2C 永远 pending，循环每 180s 空转一轮直到 2700s 预算耗尽，rc=1，每晚约 30 个无效 publish push。0708-0723 共 10 个 run 的 aiBackfill 因此失败。**一行修复可拿回每天 45 分钟。**
- **7/17 并发重跑风暴**：管线四次重跑（validation 三次失败）叠加产生单日 89 个发布提交——主管线无跨进程互斥锁。
- **7/24 尾日静默死亡（critical）**：run `stock-20260724T114234Z` 数据准备 19:42 成功、AI 20:00 达 full，但主管线中途被杀：run 文件从未写出、orchestrator_run/validation/warehouse/recommendations.db 全停在 0723，线上 `run_manifest.trade_date=20260723` 与 `system_verdict.decision_trade_date=20260724(halt)` 撕裂至今。**看门狗 7/24 20:30 报 healthy=true**（容忍 1 个交易日滞后 + 基于 0723 的 run 文件），收盘硬告警寄生在管线内随管线一起死——三层告警同时失明。

**调度现状（周一风险）**：cron 已迁入 `~/.openclaw/state/openclaw.sqlite`（21 任务，股票链 6 个 enabled：0800/1200/1930/2000/2045/2105，7/24 全部 ok）。但 openclaw CLI 报 GatewayTransportError(1006)、18789 无监听，实际运行的是 ai.hermes.gateway(8642，7/26 17:20 才重启)；新闻类 cron 7/25-26 连续 error（出站投递层故障，调度本体仍在跑：备份/compact 正常）。Discord 是唯一投递渠道，gateway.log 自 5/27 累计 13 万次 ws 1006 断连，7/26 当天投递 4/4 重试全失败。

---

## 三、AI 深度分析系统

**架构换血已完成**：老 `daily_stock_analysis` 桥接 7/11 正式冻结（FREEZE.md，降格为 venv+.env 宿主），生产 AI 自 7/8 起改为 orchestrator 直连 litellm（`generate_prebreakout_ai.py` / `generate_o2c_ai.py`），产物写 `stock_data/03-working/ai_analysis/`。记忆中的 O2C 修复态（max_tokens 4500 / 直连 / timeout 120s / 4 并发 / 断点续跑）已全部落实。

| 指标 | 数值 |
|---|---|
| 窗口覆盖率（两轨合计） | 662/840 = 78.8%（复核修正值） |
| 修复后半窗（7/07 起） | 95.9%（prebreakout 100%、O2C 91.8%） |
| 最大失败段 | 6/29-7/6 六个交易日 prebreakout 全缺（老桥接慢性失效期） |
| 条目级失败率 | ≈1.1% |
| 单只吞吐 | prebreakout ≈12s/只、O2C ≈20s/只（4 并发） |
| 模型 | 100% mimo-v2.5-pro，**无 fallback**（.env LITELLM_FALLBACK_MODELS 为空，注释声称回退 gpt-5.5 是假的） |
| token 成本 | **完全不可审计**：老路径 1788 次调用 token 全记 0，新路径根本不写用量表 |

**建议分布被诚实性护栏强约束**（这是特性不是 bug）：新路径 prebreakout 300 条 100%「观察」、O2C 96% 被 clamp 为「研究观察」。

**giveup 台账假阳性**（复核修正后）：成功日仍留失败计数（多为每码 1 次，未到 3 次上限），与 store 20/20 ready 矛盾——台账已丧失失败信号价值，且 60%-99% 覆盖区间的缺口无告警。

---

## 四、选股策略质量（全部经对抗复核确认）

**近一月零可执行信号**：18 个有裁决交易日 = 只观察 9 / 谨慎执行 5 / 暂缓 4 / **可执行 0**；全部 449 个快照中 main 与 conditional_long **恒为 0**。4 个 halt 日中至少 3 个是运维性阻断（VALIDATION_STALE）而非市场判断。

| 策略/账本 | 状态 | 关键数字 |
|---|---|---|
| prebreakout_v41（主策略） | 无市场相对边际 | 86 日/1700 笔：t1 胜率 41.4%、日均 -0.055%、对等权市场超额仅 +0.14pp/日 |
| 近月"超额" | **防御 beta 假象** | t5 超额 +4.21pp，但同窗市场基线 -4.36%（暴跌窗），绝对收益仍 -0.14%；常熟银行被重复推荐 30 次、平均 -10.9% |
| S3 分时形态（唯一 CANDIDATE） | 前瞻严重判负 | 15/60 日：累计净(med) **-18.3%**、命中 6/15，与回测 +0.15%/日方向相反；预注册 p75 执行敏感警告兑现中 |
| WTS 竞价弱转强 | **触发 kill 线未执行** | 1085 笔、均值净 -1.243%/笔、胜率 34.2%；R2/R3/R4 均已过 60 笔阈值且为负，按冻结规则应永久关闭 |
| O2C | 冻结（与记忆一致） | IR=-1.374（29 日），7/2 后不再更新 |
| T1 | 冻结 | 无复盘数据 |
| 剧本引擎八席 | 纪律执行完整 | 5 REJECTED / 1 DEFERRED / 1 UNTESTED / 1 CANDIDATE（即 S3，正在判负） |
| 真实生产 Top20 O2C 对照 | 持续为负 | 57 日日均 -0.34%、sharpe -5.9 |
| 保护闸影子 | 方向对但弱 | 103 日重放，被拦者事后超额中位 -0.58%、负占比 54.6% |

**前端口径 bug**：`strategy_evaluation.json` 显示 prebreakout 累计 -100%、nav=0、日均亏 -12.19%——与 verify_history_v2 的日度量级差两个数量级（±10% 涨跌停下单日 -12% 不可能），结算/复利口径 bug，会同时"过度悲观损害可信度"和"掩盖真实问题"。

**结论**：工程与诚实性基础设施是真实积累；edge 至今为零。全部前瞻 OOS 账本为负。也要记住 regime 依赖警告：当前证据全部来自 2026 年 2-7 月单一市场环境（含 7 月中旬暴跌），负结果不宜外推为永久证伪。

---

## 五、治理与数据资产

**决策落地断层（7/11 拍板五项）**：
- ✅ B-6 回补 31 日洞：已闭环（daily 现 194 天零洞）
- ✅ B-1 压力闸 5% 档 + 影子常态化：11 个交易日连续归档，从未断档
- ❌ B-4 18 族接线：条件 7/18 已满足，拍板未发生
- ❌ **B-5 PEAD 双写 PIT 店：摄取自 7/11 一次未跑**，东财 yjyg 是 latest-only 源，**约 9 个交易日 vintage 已永久丢失且每天继续丢**；切读未做（若此时切读会读到 7/11 静态快照 = 隐性 PIT 污染）
- ❌ P-1 合并（7/13、7/14 两晚绿灯已达成）未执行；P-2 EM 对账一次未跑；批次 2 六项议程零开工

**静默期真相（7/12-7/24）**：系统没停，但"没人记账"——7/15 真实事故（aiBackfill SIGTERM→pre-push 锁死→Pages/S3 停更）、7/16 五处联修、7/16 新开"启动前夕二次加工厂/影子研究"工作线、7/20 午盘修复，**全部以未提交形态存在**（1334 个未提交路径）。memory 日记 7/03-7/23 断档 20 天，ACTIVE_TASKS 停在 7/02，task_watchdog 死于 3/16。

**数据资产**：生产缓存全部追平 20260724；研究面板 _full 冻结 20260702（刻意保护，落后 16 个交易日）；7/21 分钟数据仅 .partial、S3 账本该信号日 0 行（7/6 同类事故复发）；auction_close 停 20260702（已知低危）。磁盘：stock_data 16G，其中 backups 独占 11.5G（recommendations.db 1.4G×7 份），Data 卷已用 82% 余 38G。主仓 .git 已 712M（跟踪 922MB 滚动 parquet 是根因）。

**安全/备份两处高危**：
1. `~/.openclaw` 配置仓 origin → 公开仓 fisher-admin/stock-report，而该仓 git 历史含 credentials/*.json——一条 `git push` 即泄密。
2. 主工作区 git 仓**无任何 remote**，唯一的 5 分钟备份 cron 自 4/12 被暂停且只覆盖配置仓——7/11 以来全部成果零异地副本。

**发布层**：stock-report main==origin（df6d003a）、Pages 200、线上数据 20260724。线上实为 v3 信息架构 + v4 铜金视觉；"publish(v2)" 指数据合同 v2（18 组阻断式检查，7/24 两次发布 0 fail）。长期记忆"c11c726 待 push"已过时（已修正记忆）。

---

## 六、优化升级方案

### P0 — 周一（7/27）开盘前必须做（止血，半天）
1. **验证调度真相**：确认 6 个股票 cron 在 hermes gateway(8642) 内存在且 enabled，08:00 前做一次投递烟测；修 openclaw CLI 指向或记录新诊断入口。7/9 教训：调度器即全系统单点。
2. **一行修复 O2C 合同不匹配**：`stage4_ai_backfill_loop.py:591` 完成判定改为 `in {'o2c_factor_llm_analysis','o2c_ai_analysis'}`（或读 ai_coverage_counted），并加"连续 N 轮 pending 集不变即提前退出"护栏——立刻拿回每天 45 分钟 + 消除每晚 30 个无效 push。
3. **补 0724 尾账**：跑 warehouse_refresh+validation_refresh 修复组（REMEDIATION_GROUPS 已有现成映射），补齐 recommendations.db 20260724、重发 run_manifest，清除线上日期撕裂。
4. **摘除泄密引信**：`git -C ~/.openclaw remote remove origin`（或改指私有仓）。
5. **恢复 PEAD 摄取**：手动跑 `ingest_pead_preannounce.py` 并挂入收盘链——每拖一天永久丢一天 vintage。
6. **收编两周未提交改动**：按主题分 commit（7/15 事故修复 / 7/16 联修 / 7/20 午盘 / prebreakout factory 新线），随后给 workspace 加私有 remote 或 git bundle 异地副本。

### P1 — 本周（可靠性，2-3 天）
7. **告警链解耦**：stock_pipeline_alert 从管线末级 stage 拆出独立调度（交易日 21:00）；watchdog 加"当日档"（交易日 21:30 后 lag≥1 即告警）；告警加 append-only 历史留痕；接入第二渠道（飞书 skill 已就绪未接网关）。修 Discord 代理 ECONNRESET（或统一走 watchdog 同款 bot HTTP 直发——该路径 7/22 实测可达）。
8. **主管线加固**：mkdir 原子锁（防 7/17 式并发重跑）+ caffeinate 包裹 + run manifest 每 stage 增量落盘（防 0724 式无痕死亡）。
9. **发布顺序护栏**：stage4 公网 push 延后到 validation 通过之后（或 push staging 分支、deploy 再 fast-forward）——消除"先发布后校验"窗口（0710 盘前污染事故的结构根因）。
10. **回填 push 改终态单 push**：中间轮次仅本地 commit。
11. **AI 层补账**：litellm response.usage 写回用量表（成本现在是黑箱）；配置 LITELLm_FALLBACK_MODELS 消除 MIMO 单点；修 giveup 台账假阳性；prebreakout 生成器去掉无条件 --force。
12. **修 strategy_evaluation.json 的 -100%/nav=0 口径 bug**（strategy_publication_layer.py 溯源），与 verify_history_v2 统一口径后再上前端。

### P2 — 两周内（治理 + 效率）
13. **悬空决议集中拍板**（需要 Fisher）：B-4 18 族接线（看影子分布后决定）；**WTS R2/R3/R4 按预注册 kill 规则正式关闭或书面复议**——预注册制度的可信度取决于第一次严格执行；P-1 合并或作废；P-2 补跑或直接裁决 B-7；批次 2 六项逐项"开工/搁置"。
14. **管线提速**：非阻塞研究段（prebreakoutDailyPan 8.6min + Shadow）移出关键路径并行跑或拆独立 cron；marketSnapshot 与 strategySuite 并行——66min → 约 35min（连同 #2 的 45 分钟，总墙钟可砍近半）。
15. **仓库与磁盘瘦身**：滚动 parquet 移出 git（.gitignore + rm --cached + 历史瘦身，.git 712M 根因）；backups 相同内容去重（省约 1.6G）+ KEEP_DAYS 语义修正 + 查月度 compact 未产出。
16. **清理死代码**：prebreakout_factory_search 三个旧变体归档（2158 行）、两个 .backup 移出生产目录、回填循环内约 500 行不可达旧 O2C 路径删除。
17. **记忆纪律恢复**：日记/ACTIVE_TASKS/latest.md 快照链路修复；task_watchdog 重新挂调度或正式归档。
18. **回补数据洞**：7/21 分钟数据断点续拉 + S3 账本补结算或显式标注缺日；排查断点护栏为何复发。

### P3 — 策略方向（研究层，供决策参考，非投资建议）
- **执行证伪纪律优先于寻找新 edge**：S3 跟踪到 60 日审判点严格执行；WTS 到点即关；已冻结的 O2C/T1 不再投入算力（与既有决议一致）。
- **给近月"正超额"加防御 beta 归因注记**（或改行业中性基准），防止暴跌窗假象误导仓位判断。
- **前端第一句话诚实化**：把"策略层无一达可交易门槛"升为面向读者的第一结论；halt 的运维性阻断与市场性判断分开呈现。
- **下一个 edge 赌注的合理去处**：PEAD/PIT 事件研究是当前唯一还在积累独占 vintage 资产的方向（前提是 P0-5 先把摄取救活）；WTS 保留年表 regime 依赖警告，等 regime 变化再复审而非永久否定。

---

## 附录：审计证据位置
- 各轨道完整 JSON（含逐条 evidence 路径与对抗复核记录）：workflow 运行目录 `~/.claude/projects/-Users-fisher--openclaw-workspace/8df400b2-96b5-4803-b3c7-03751fb6bd4b/subagents/workflows/wf_d3c0dc09-e65/journal.jsonl`
- 关键单据：`stock_data/03-working/health/`（runs/、ai_backfill_loop.json、ai_publish_readiness.json、wts_tracking_ledger.csv、protection_shadow_latest.json）、`stock-report/data/latest/`（s3_watchlist.json、review_state_o2c.json、strategy_evaluation.json、setup_engine_status.json）、`verify_history_v2/summary_latest.json`
