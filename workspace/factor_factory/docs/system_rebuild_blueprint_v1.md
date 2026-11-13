# 选股系统重造蓝图 v1

生成日期: 2026-07-02 | 依据: 本季全部冻结实测证据 + 近一周运行体检 + 管线代码风险扫描 + 三策略改造方案及对抗复核意见
性质: 施工蓝图,直接可执行。所有改动限定在改造铁规允许的四类内: 角色重定位 / 流程与运行加固 / 前瞻OOS验证机制 / 诚实展示与成本口径。零因子重调。

---

## 0. 近一周运行体检结论 (20260622–20260702)

**一句话结论: 系统正在"裸奔靠人工触发"的状态下劣化——数据库停更 6 个交易日,核心策略连续 3 天选股为 0,前端展示的是 6/26 的旧数据。**

关键数字:
- recommendations.db 最后成功 sync 停在 **2026-06-26 20:06**,至体检时点落后 **6 个交易日**;
- openclaw 内部 cron 自 **2026-04-12** 被手动暂停 (`/Users/fisher/.openclaw/control/cron.paused`),jobs.json 为空,近 3 个月每日管线实际靠人工/会话零星触发;6/29(周一)**整日零运行**;
- prebreakout_v41 连续 3 个交易日 (6/30, 7/1, 7/2) 选股 **0 只**,日志"特征覆盖 20260702: 4703/0"——全市场被硬过滤清零;根因链: **Tushare token 报 'Token不存在'** + cyq_perf akshare 降级 **198 次全部超时 (matched=0, 120 秒上限耗尽)** → 回退本地代理值 → 数据劣化到 hard_filter 系统性剔除全市场;
- greenfield_o2c_v1 仍出 20 只,但 `ai_publish_readiness.json` 显示 source_date 锁在 **20260626** (matched_trade_date=false),AI ready_ratio 仅 **25% (5/20)**——前端展示的不是当日新鲜数据;
- 6/24 DB 无任何记录、6/26 prebreakout AI 覆盖仅 40% (8/20)、6/25 O2C AI 覆盖 0%;
- watchdog (7/1 20:30) 已正确检测到"落后约 3 个交易日",但**只静默写 JSON,没有人工介入闭环**,滞后继续恶化到 6 天;
- 卫生: workspace 16GB,29182 个 .pyc、2674 个 __pycache__、backups/ 下 4 份 1.4GB 的 recommendations.db 全量拷贝 (约 5.6GB) 无轮转。

问题分级与修法(细化落点见第 2 节):

| 级别 | 问题 | 修法 |
|---|---|---|
| P0 | 调度真空: cron.paused 近 3 个月未恢复,无任何稳定触发 | 明确决策: 恢复内部 cron,或把主 orchestrator 迁到 OS 级 launchd/crontab,二选一,不许继续裸奔 |
| P0 | Tushare token 失效 + akshare 降级全超时 → 全市场清零 | 立即更新 token (`daily_stock_analysis/.env`,只改不读出);backtest.py 特征覆盖分母为 0 时显式跳过+告警,而非静默 0/0 |
| P0 | DB 6 天未 sync,watchdog 告警无闭环 | watchdog 接入 stage6 已有 `_send_discord` 通道,连续 N 天未同步升级告警 |
| P1 | O2C 发布陈旧 (source_date=6/26 仍当"最新"展示) | 合同硬校验 matched_trade_date,不匹配则前端强制标注"数据来自 6/26" |
| P1 | 6/24、6/29 整日缺口无独立告警 | "当日完全零运行"建独立强告警级别(比覆盖率不足更高) |
| P2 | 16GB 仓库卫生 + 7GB 冗余 DB 副本 | .gitignore 补规则 + git rm --cached;backups 加轮转 |

---

## 1. 系统角色重定位

本系统不再是、也从来不曾是一台 alpha 发动机。本季横跨价值、基本面、慢风格、日内、短线启动、分钟级抢板的全部实测已冻结为铁律: **免费数据 + A股个股长多 + 真实成本下,无可部署的系统化 edge**——强信号在不可做空的空头侧,涨停溢价在不可成交的隔夜段,可成交段的信号比成本低一个量级。系统的真实价值在于它已经建成的另一样东西: 一套**可信测量 + 诚实展示的选股信息系统**——每日候选清单、逐股 AI 深度解读、去幸存者偏差的真实复盘、以及一整套防自欺的方法武器(三段分解、可成交口径、独立复现、安慰剂、FDR、review_gate、发布合同校验)。重造的目标不是让它赚钱,而是让它**每天准时跑、说真话、并且结构上不可能说假话**。

---

## 2. 运行加固优先级清单

### P0 (立刻,本周内前两天)

1. **恢复调度**。决定 (a) 删除 `/Users/fisher/.openclaw/control/cron.paused` 并在 `.openclaw/cron/jobs.json` 重配 stock pipeline 每日收盘任务,或 (b) 为 `skills/stock-system-orchestrator/scripts/run_pipeline.py` 建 OS 级 launchd job (参照已有 `ai.openclaw.stock.freshness-watchdog`)。这是 6/29 整日断档的根因,其余一切修复在没有调度的前提下都是空谈。
2. **修数据源**。更新 Tushare token (`daily_stock_analysis/.env`——只改文件不读出内容);在 `skills/stock-analyzer/backtest.py` 特征覆盖计算处: `len(results)==0` 时该策略显式标记 skipped + 单独告警,替换掉当前 `covered/max(len(results),1)` 掩盖 0/0 的写法(日志出现 469900.0% 即此 bug 征兆);给 prebreakout_hard_filter 加降级路径——来自 proxy/degraded 数据源的字段不参与硬过滤或放宽阈值,防止数据源降级直接清零全市场。
3. **接通复盘引擎(全局根因,复核意见第一优先)**。`generate_o2c_review.py` 已通用化为多策略引擎(STRATEGY_REGISTRY 含 prebreakout_v41,支持 `--all`),但 **grep 证实它完全不在 run_pipeline.py 的 build_stages() 里**——这是 review_state_o2c.json 空转、prebreakout review_gate 恒 n/a 的共同根因。落点: 在 `skills/stock-system-orchestrator/scripts/run_pipeline.py` 的阶段序列中、latestStateSync **之前**加入 `generate_o2c_review.py --all`。
4. **消灭 review_state 写序竞态**。`generate_latest_states.py:778-782` 用只含 row_count 的 `o2c_factor_summary.json` 整体覆盖 `review_state_o2c.json`,把真实 review_gate/date_stats 抹掉(现场已复现)。修法: 确立**唯一写者**——只有 `generate_o2c_review.py` 写 review_state_*.json,`generate_latest_states.py` 改为纯读透传,`build_o2c_review_fallback()` 仅在 DB 完全无复盘行时兜底;**同步修 recommendation_state 内嵌的 review_gate n/a 占位 dict**(它当前正靠占位绕过合同第 14 条的非 null 断言,只修顶层文件不修内嵌会两处自相矛盾)。
5. **堵 prebreakout 的 main 升级结构性缺口**。当前 strategy_gate=pass/tradeable、`generate_latest_states.py:403` 仅凭 ai_score>=60 即可升 main——虽然此刻实际 main 数为 0,但 IR -7.71 的冻结证据没有转化为任何发布侧压制,随时可能冒出买入。修法见第 3 节 prebreakout 表。
6. **run_pipeline 加阶段超时**。`run_pipeline.py:180-196` 的 `subprocess.run` 无 timeout,任一子脚本卡死(Tushare/akshare 无显式网络超时,`backtest.py:730,745,761`)即整条流水线永久挂起且无告警。修法: run_script() 加按 stage 的 timeout 映射(如 dataPreparation=7800s),捕获 TimeoutExpired → 写 timed_out=true + 复用 notify_fatal。同步落点 `stage3_strategy_suite.py:43`、`stage4_ai_publish.py:735`。
7. **watchdog 告警闭环**。`data_freshness_watchdog.py` 接入 stage6 的 `_send_discord`,连续 2 天 stale 升级告警级别;其 launchd job 上次退出码为 1,顺手排查。

### P1 (本周)

8. **T1 停跑三处**(名义退役、事实每日运行的自相矛盾): 删 crontab 里 `35 15 * * 1-5 daily_t1_alpha191.sh` 一行(先 `crontab -l` 备份);脚本移入 `factor_factory/scripts/_retired/` 加退役注记;`stage3_strategy_suite.py` steps 里删 build_gtja_alpha191_panel 与 build_t1_portfolio 两行,RETAINED 收为两策略;`generate_t1_execution.py` main 顶部加早退,停止伪造 45-88 假 AI 分。
9. **合同校验升级**: `validate_publication_contract.py` 把 14e(review_state_o2c 一致性)从 warns 升为 errs;新增 prebreakout 专项闸与退役策略标准字段校验(见第 3 节);**注意复核提醒**——T1 items=[] 需确认不会误触第 6 条(推荐数≠20)与第 2 条缺字段,校验分 ACTIVE_SIDS / RETIRED_SIDS 两套口径。
10. **O2C 发布新鲜度硬闸**: matched_trade_date=false 时禁止以"当日"名义发布,前端强制标注真实 source_date。落点 `strategy_publication_layer.py` / `validate_publication_contract.py`。
11. **废弃入口拦截**: `/Users/fisher/daily_stock_analysis/fisher_bridge.py` (老版本,绕过全部门槛直接改写发布产物) 顶部加 DeprecationWarning + `sys.exit(1)`,注释指向真正在用的 `WORKSPACE/daily_stock_analysis/bridge_selection_to_analysis.py`。
12. **网络调用统一超时**: 给 `pl.init_tushare()` 的 pro 对象配 session 超时或用 `future.result(timeout=N)` 包一层;akshare 调用参照已有 `_fetch_cyq_perf_from_akshare` 子进程隔离范式。落点 `backtest.py:722-773`、`stage1_data_prepare.py:72-91`。

### P2 (以后)

13. **仓库卫生**: `.gitignore` 加 `__pycache__/`、`*.pyc`、`*.pyo`,`git rm -r --cached` 清掉 `skills/stock-analyzer/__pycache__/` 四个被跟踪的 .pyc;backup/bak 文件清理。
14. **备份轮转**: `ops/backup_databases.sh` 给月度 compact 归档加保留策略(如只留 6 份);确认该脚本实际调度来源(crontab -l 查不到但备份文件存在,运维路径不透明——补 last_run 时间戳自检交 watchdog 检查);backups/ 现存 4 份 1.4GB 副本清到 3 份以内。
15. **累积型状态文件统一 TTL 巡检**: `ai_backfill_giveup.json` (stage4_ai_backfill_loop.py:58-67) 只留最近 30 个 trade_date;排查 health/*.json 同类"只增不删"模式。
16. **阶段超时策略抽象**: run_pipeline 引入 STAGE_TIMEOUT_SECONDS 映射表,新增 stage 默认继承超时保护。

---

## 3. 三策略处置与改造表 (已吸收对抗复核修正)

### 3.1 prebreakout_v41 → 降级为"形态观察流 + AI 结构化解读"

判词: 去幸存者偏差复盘 IR **-7.71**,因果链在可成交口径下无净边际。保留每日出票与 AI 解读的信息价值,但推荐身份剥夺,final_action 钳死在 watch/avoid。**复核修正: 当前实际 main 数为 0,危险性是"随时可能出买入的结构性缺口"而非"每日在出买入"——定性为结构性堵漏,优先级不降。**

| # | 改动 | 落点 | 量级 |
|---|---|---|---|
| 1 | compute_strategy_gate 加硬门 `research_frozen={'status':'fail','note':'IR-7.71,永久压研究观察'}` → research_only=true;generate_latest_states 的 final_action 接入 research_only 钳制,raw_action 保留、final 强制 watch | `strategy_publication_layer.py` (compute_strategy_gate 约 445-511 行)、`generate_latest_states.py:399-410,503` | M |
| 2 | 合同专项硬闸: research_only 必须 true、无任何 final_action=main、strategy_gate.verdict=research_only、携带 frozen_evidence;strict 下阻断 deploy | `validate_publication_contract.py` (第 5 条附近新增第 15 条) | S |
| 3 | **(复核修正: 不新建脚本)** 前瞻 OOS 复盘直接复用已通用化的 `generate_o2c_review.py --strategy prebreakout_v41`(STRATEGY_REGISTRY 49-60 行已注册,`review_state_prebreakout.json` 已存在);只需随 P0#3 接入 pipeline,并让 compute_strategy_gate 的 prebreakout review_doc 指向该文件。工作量 L→**S** | `run_pipeline.py`、`strategy_publication_layer.py:492-499` | S |
| 4 | 前端诚实字段: display_role='形态观察流'、tradeable=false、headline_disclaimer、frozen_evidence(IR=-7.71 可见)、restore_path | `strategy_publication_layer.py` (build_strategy_public 附近 519-531 行) + 前端灰底警示条、badge"观察·非推荐" | M |
| 5 | **(复核修正: 大部分约束已存在)** `generate_prebreakout_ai.py:73` advice_rule 与 :109 clamp 已实现非 main 不得买入;仅需新增 research_only 显式入参 (OPENCLAW_PREBREAKOUT_RESEARCH_ONLY) 与一行提示语"形态/因子教学解读,不得给出买入结论",不重写 | `generate_prebreakout_ai.py` build_prompt/clamp | S |

停止做: ai_score>=60 直升 main;把 oos_icir/review_gate 长期挂 n/a 当合规;在污染的 prebreakout_v41.db 历史上重调参数;用 AI 信心分冒充有效性证据。
恢复路径(唯一合法): 新版本 (prebreakout_v42...) 先过四道闸(三段分解/可成交口径/独立复现/安慰剂),再进前瞻 review_gate 累积期(≥20 有效日、IR≥0.5、超额>0、不连负),期间恒 research_only;research_frozen 硬门须人工在证据更新后显式撤除。

### 3.2 greenfield_o2c_v1 → "候选池内弱排序信号 + 前瞻纸面跟踪装置(纯证伪监测)"

判词: 完整优化弧已收口,净口径下无可部署边际,锁研究观察。现实角色 = 截面 IC≈0.02 的弱排序信号,只影响候选清单内先后,不承诺收益。

**复核修正一(最重要,与冻结证据抵触的后门必须封死)**: 原方案的"40 个不重叠周期 → deploy_eligible → 人工复议部署"用**毛超额 IR** 做判据,等于给已在净口径证伪的信号留"毛超额翻身"后门。修正为双保险: ① 40 周期累积的定性**改为"持续证伪监测",不构成任何部署预期**,honesty 文案同步改写;② 若保留 deploy_eligible 字段,其判据**必须用扣费后净口径**(avg_excess_net_of_cost>0 且净 IR≥门槛),与冻结结论同一口径,达标也只触发"人工重新评审证据",不自动解锁任何交易语义。

**复核修正二**: 冻结签名拆成**两个独立指纹**——回测签名(CLEAN_FACTORS / HOLDS=[3,5,10] / COST_BPS=30 / top-frac,`o2c_strategy_backtest.py`)与复盘签名(hold_days=0,`generate_o2c_review.py`),各脚本只断言自己的指纹,不再跨口径混签。

| # | 改动 | 落点 | 量级 |
|---|---|---|---|
| 1 | 修 review_state_o2c.json 空转: 唯一写者归 generate_o2c_review,generate_latest_states 纯读透传;**同步修 recommendation_state 内嵌 review_gate 的 n/a 占位**(当前正绕过合同第 14 条) | `generate_latest_states.py:778-782,803`、`run_pipeline.py`(执行顺序) | M |
| 2 | 40 周期门槛落地(按修正一口径): review_gate 新增 monitoring_only=true、valid_review_days、periods_to_review=max(0,40-N)、扣费后净口径判据;GATE_REVIEW_SAMPLES_MIN=3 降级为最低可展示门槛 | `generate_o2c_review.py:73` 附近、`strategy_publication_layer.py:425` 与 compute_strategy_gate | M |
| 3 | 参数冻结双签名: 新建 `freeze_o2c_signature.py`,分别产出 backtest_signature 与 review_signature 写入 `stock_data/03-working/strategy_research/greenfield_o2c_v1/param_freeze.json`(含 frozen_at/git_commit);两脚本启动各自断言,漂移则标 param_drift=true 供合同拒发 | 新文件 + `o2c_strategy_backtest.py`、`generate_o2c_review.py` 启动断言 | L |
| 4 | holdout OOS 结果落 JSON: main() 末尾写 `o2c_oos_state.json`(cost_bps=30/net_excess/sharpe/param_signature);net_excess<0 进 compute_strategy_gate 记 fail | `o2c_strategy_backtest.py:206-215` 后、`strategy_publication_layer.py` signal_gate 分支 | M |
| 5 | 角色字段: role_type='weak_ranking_signal',role_label"候选池内弱排序信号(IC≈0.02,仅影响清单先后,不承诺收益、不构成买入)";research_only 下 final_action 绝不 main | `strategy_publication_layer.py` O2C 分支 + 前端 decision-candidates.html / recommendation-review.html | M |
| 6 | 成本口径双栏展示: review 展示同列毛超额与"扣费后示意 -30bp/往返";**注**: 展示层双栏 + 判据层净口径(修正一),不再存在"判定用毛、展示用净"的选择性诚实 | `generate_o2c_review.py` build_review_state + 前端 | S |
| 7 | 合同收口: review_gate 非 null 且非占位、含 monitoring_only/periods_to_review、param_drift!=true,失败 strict 拒发 | `validate_publication_contract.py`、`stage6_deploy_and_notify.py` | M |

停止做: 在已有 28 个复盘日的历史上重扫因子/权重/持有期;让 review_state 停在 null 空转;把 O2C 表述成任何形式的"弱买入";未满 40 周期因几天 IR 好看就提部署;为好看悄改判定口径。

### 3.3 t1_factor_v1 → "退役档案 / 一次诚实证伪的教育样本"

判词: 已永久证伪(OOS IR -7.71),唯一价值是向用户证明"这套系统会诚实关停自己失败的策略"。当前"名义退役、事实每日运行"——crontab + stage3 两处每日跑完整 T1 弧,`generate_t1_execution.py:56` 每天伪造 45-88 假 AI 分并 git push,违反自家合同,**P0 停跑**。

| # | 改动 | 落点 | 量级 |
|---|---|---|---|
| 1 | 杀 crontab `35 15 * * 1-5`,脚本归档 `_retired/` 加退役注记 | crontab + `factor_factory/scripts/daily_t1_alpha191.sh` | S |
| 2 | generate_t1_execution.py main 顶部早退,停伪 AI 分 | `factor_factory/scripts/generate_t1_execution.py` | S |
| 3 | stage3 摘除两个 T1 步骤,RETAINED 收为 2,新增 RETIRED 常量 | `skills/stock-system-orchestrator/scripts/stage3_strategy_suite.py:65-66` | M |
| 4 | 发布层退役短路: T1 入 RETIRED_IDS,跳过 adjustments/gate/weights/overlap,t1_weight 置 0,输出固定退役快照 (retired:true, items=[]) | `strategy_publication_layer.py:1279-1349,1740-1750` | L |
| 5 | 合同退役标准字段: lifecycle_status='retired'、retired_date、retirement_reason='no_tradeable_edge'、evidence_ref、tradeable=false、items=[]、ai_score 全 null(升级现有 85-88 行为 error 级);**复核提醒: SIDS 拆 ACTIVE/RETIRED 两套,确认 items=[] 不误触"推荐数≠20"校验** | `validate_publication_contract.py:30,85-90` | M |
| 6 | 前端: T1 从"研究预览"改为"已退役"徽章,归入 review 历史页独立"退役档案"区块,不进当日 Tab | `stock-report/DESIGN-V3.md:112`、`assets/scripts/stock-data-hub.js` | M |
| 7 | 证据冻结: 最后一版 research_state_t1.json / portfolio_spec.json / walkforward_report.json 冻结到 `factor_factory/outputs/t1_alpha191/_frozen_retired/`,前端改读冻结快照,停止每日三处复制+rsync | `factor_factory/outputs/t1_alpha191/` | S |

防复活闸: 任何未来 commit 让 t1_factor_v1 重新产出 items/非 null ai_score/main,strict 下非零退出阻断 deploy。真要复活必须换新 strategy_id 从零走四道闸,绝不复用被污染的命名与权重。

---

## 4. AI 深度分析链路优化

1. **保持 mimo 直连 litellm 路径**(已验证的坑: 推理模型 max_tokens 要大、绕网关直连,见 `daily_stock_analysis/generate_o2c_ai.py` 既有实现)——prebreakout 与 O2C 两条 AI 生成线统一沿用此范式,不回退网关。
2. **覆盖率的根因是上游不是 AI**: 体检证实 GIVEUP_PATH 不存在、低覆盖源于选股为空或数据陈旧(7/2 prebreakout 0/0 是结构性归零)。因此先修第 2 节 P0 的数据/调度,再谈覆盖率;AI 侧只需两件事: ① stage4_ai_backfill_loop 的 timed_out(6/28 快照已出现)加告警升级,超时不再静默;② 6/26 那种 8/20 半途而废的日子,回填循环在次日开盘前补跑一轮(挂进恢复后的调度)。
3. **giveup 机制保留现状语义**(按日计数、次日自动重置,复核已澄清非永久拉黑),只加轮转: `_save_giveup()` 保留最近 30 个 trade_date 键 (`stage4_ai_backfill_loop.py:49-72`)。
4. **成本控制**: T1 停跑后每日省一整条 qlib 弧 + 一次 git push;prebreakout research_only 后 AI 文案收敛为形态解读,单股 prompt 更短;O2C partial→ready 的补跑只针对增量股票,不整表重生成。
5. **诚实约束不放松**: AI 抬头注记"教学解读,非买卖建议,不含目标价与消息面";ai_advice 受 final_action 钳制(已有,保持);ai_score 永不用于驱动动作分层。

---

## 5. 四周路线图

**第 1 周 — 让系统活过来 + 堵最危险的洞 (P0)**
- 恢复调度(cron 决策落地)、更新 Tushare token、backtest.py 分母为 0 显式告警、hard_filter 降级路径;
- `generate_o2c_review.py --all` 接入 run_pipeline;review_state 唯一写者改造 + recommendation_state 占位同步修;
- prebreakout research_frozen 硬门 + main 钳制 + 合同第 15 条;
- run_pipeline 阶段超时;watchdog 接 Discord。
- 验收: 连续 3 个交易日 DB 当日 sync 成功、review_state_o2c.json 带真实 review_gate、prebreakout 页面出现"观察·非推荐"。

**第 2 周 — T1 干净退役 + O2C 角色落地 (P1)**
- T1 七项改动全落(停跑三处→发布层短路→合同退役字段→前端退役档案→证据冻结);
- O2C role_label/40 周期证伪监测门槛(净口径)/成本双栏展示;
- fisher_bridge.py 拦截;O2C 新鲜度硬闸。
- 验收: strict 校验全绿,T1 无任何每日算力消耗,O2C 页面出现"纸面跟踪期,已累积 N/40"。

**第 3 周 — 冻结签名与 OOS 沉淀 (P1 尾 + P2 头)**
- freeze_o2c_signature.py 双指纹落地 + 两脚本启动断言;
- o2c_oos_state.json 落盘接门槛;合同收口(param_drift/monitoring_only 校验);
- 网络调用统一超时适配层。
- 验收: 手工改一个 O2C 参数,管线自动标 param_drift 并被拒发(演练一次)。

**第 4 周 — 卫生与长效 (P2)**
- .gitignore/pyc 清理、备份轮转、giveup 轮转、累积型文件 TTL 巡检;
- STAGE_TIMEOUT_SECONDS 抽象;backup_databases.sh 调度来源查清并补自检;
- 全链路演练: 模拟 Tushare 失效一天,确认降级路径不清零全市场、告警响、前端诚实标注。
- 验收: 连续两周零人工干预正常运行,体检脚本复跑全绿。

---

## 6. "明确不做"清单 (防自欺)

1. **不在已污染的历史数据上重调任何因子权重/参数/阈值/持有期并宣称改进**(铁规①)——包括 prebreakout_v41.db 复盘、O2C 的 28 个复盘日、T1 全部历史。
2. **不给任何策略恢复"推荐/买入"身份**,除非新版本先过四道闸(三段分解 + 可成交口径 + 独立复现 + 安慰剂/FDR)再在前瞻 review_gate 累积达标——且判据口径必须是**扣费后净口径**,不接受毛超额翻身。
3. **不把 O2C 的 40 周期累积当作部署倒计时**——它是持续证伪监测,不构成任何部署预期。
4. **不复活 t1_factor_v1** 这个 strategy_id,不复用其历史权重;不展示任何历史伪造的 45-88 假分。
5. **不用 ai_score 驱动动作分层**——AI 信心分与净边际无关。
6. **不追免费数据没有的东西**: 一致预期、北向个股、L2、分钟级抢板,统统不碰(铁规④)。
7. **不新建 generate_prebreakout_review.py**——引擎已存在,复用,不重复造轮子(复核修正)。
8. **不做"判定用毛超额、展示用净超额"式的选择性诚实**——判定与冻结结论同口径。
9. **不在无 tradeable 门槛通过时用"推荐/精选/买入"措辞呈现任何个股**。
10. **不再新增无轮转的累积型状态文件**——新文件默认带 TTL。

---

## 7. 风险与前提

1. **前提: 调度决策必须由系统主人拍板**。cron.paused 是用户主动写入的,恢复内部 cron 还是迁 launchd 需要人做决定;蓝图其余全部工作在没有稳定调度时价值减半。
2. **前提: Tushare token 可续期**。若免费额度/token 不可恢复,akshare 降级路径需扩容(当前 120 秒上限 + 198 超时说明现状不可依赖),数据质量将长期受限,hard_filter 降级路径(P0#2)从"保险"变成"常态"。
3. **风险: 合同校验改造的连锁误拒**。T1 items=[]、O2C 新增字段、prebreakout 新闸都会触碰现有 12+ 项校验的隐含假设(如推荐数=20),第 2、3 周每步落地后必须跑 `test_strategy_gate.py` 回归 + strict 演练,先在非 deploy 分支验证。
4. **风险: 唯一写者改造遗漏第三写者**。复核已点名 review_state_o2c 的双写者,但 review_state_prebreakout.json / research_state_t1.json 可能有同类竞态,第 1 周改造时需 grep 全量排查所有 latest/*.json 的写入方。
5. **风险: 观察流的用户误读**。即便措辞诚实,每日 20 只 + AI 解读天然带"推荐感";灰底警示条与"观察·非推荐"badge 是底线,不可被后续前端美化悄悄弱化——列入合同校验(headline_disclaimer 字段必须存在)。
6. **风险: 磁盘**。16GB 且每月 +1.4GB 压缩归档,若第 4 周轮转不落地,半年后备份将吞掉 10GB+。
7. **兜底认知**: 本蓝图不承诺产生任何交易收益。它承诺的是一个每天准时运行、测量可信、展示诚实、结构上无法自欺的信息系统——这正是本季全部实测证据所支持的唯一定位。

---

关键文件落点索引:
- 调度: `/Users/fisher/.openclaw/control/cron.paused`、`.openclaw/cron/jobs.json`
- 管线: `skills/stock-system-orchestrator/scripts/run_pipeline.py`、`stage1_data_prepare.py`、`stage3_strategy_suite.py`、`stage4_ai_backfill_loop.py`、`stage4_ai_publish.py`、`stage6_deploy_and_notify.py`
- 发布与合同: `strategy_publication_layer.py`、`generate_latest_states.py`、`validate_publication_contract.py`
- 复盘引擎: `generate_o2c_review.py`(多策略,已含 prebreakout 注册)
- 策略: `skills/stock-analyzer/backtest.py`、`o2c_strategy_backtest.py`、`generate_prebreakout_ai.py`、`factor_factory/scripts/generate_t1_execution.py`、`daily_t1_alpha191.sh`
- 运维: `ops/backup_databases.sh`、`data_freshness_watchdog.py`、`/Users/fisher/daily_stock_analysis/fisher_bridge.py`(废弃拦截)
- 密钥: `daily_stock_analysis/.env`、`.secrets/`(只改不读出)