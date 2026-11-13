# Spike-4 · protection shadow: 三层闸影子 — 验收报告

- 日期: 2026-07-11 · 建造者: spike4 subagent · 判定: **通过 (6/6 验收 PASS)**
- 规格: PHASE2_TIERA_REPORT.md §Spike-4; 输入: deep/freqtrade_protections.md §2(语义表19条)/§3.1-3.5/§5(单测清单), deep/lean.md §2.3(风险状态落库), deep/nautilus.md §2.2(deny 格式 CATEGORY: k=v)
- 环境: /Users/fisher/.openclaw/venv/bin/python (pytest 缺失 → unittest)
- GPL-3.0 纪律: 全部代码自写, 零 freqtrade 代码复制; §2 语义表为行为基准, † 反转条目带上游行号对照注释

## 0. 交付物清单 (全部 additive, 本目录内)

| 文件 | 内容 |
|---|---|
| calendar_util.py | TradingCalendar (注入式, S2/S5 顺延 / S3 左开右闭窗) |
| lock_store.py | strategy_locks DDL (§3.1 16列+2索引, 独立 sqlite 文件) + LockStore 协议 + MemLockStore/DbLockStore 双实现 (共用 _BaseLockStore 判定逻辑, **构造注入实例态, 修 freqtrade use_db 类属性单例反模式**) + UnlockSpec 物化 + O1/O2/O3/S1/S4/S6/G4 语义 |
| protection_rules.py | IProtectionRule 协议 + 首批4保护器: ConsecutiveLossGuard(mtm 口径)/StrategyDrawdownBrake(**equity 唯一口径, 无 ratios 开关**)/LowEdgePause/PostFailureCooldown + 类型化事件 (INSUFFICIENT_DATA/NO_DRAWDOWN/PROTECTION_TRIGGER, display 对齐 nautilus `CATEGORY: k=v`) |
| protection_engine.py | ProtectionEngine: 配置顺序评估 → store 落锁(吸收留痕) → 事件; G2 scope_flags 硬边界 fail-fast |
| shadow_replay.py | 影子重放器: recommendations.db 只读(mode=ro) → as-of 截断台账 → MemLockStore 注入重放 → 三列报告+事件流+MARKET_STRESS 多档分布 |
| tests/test_protections.py | 19 条单测 (unittest) |
| accept_2/3/4/5/6_*.py | 验收 2-6 机械复跑脚本 |
| out/run1/ | 影子产出: shadow_report.md / blocked_recommendations.csv / locks_fired.csv / protection_events.csv / market_stress.csv / SHA256 |

历史台账数据源(只读): `/Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db` — recommendation_fact 1560 条推荐 / 77 个推荐日 (20260224~20260710, 重放区间 91 交易日 ≥60); price_daily_cache 提供 mtm/后验超额/日历派生。

## 1. 逐条验收记录

### 验收1 — 单测清单指定条目全绿 (含†反转注释): **PASS**

复跑: `cd spikes/spike4_protection && /Users/fisher/.openclaw/venv/bin/python -m unittest discover -s tests -v`
实测: `Ran 19 tests ... OK` (19/19 绿)。条目映射:

| §5 清单# | 测试 | 结果 |
|---|---|---|
| 1 (S1) | TestS1LockBoundary ×2 (左闭右开 + next_close 相位判定) | 绿 |
| 2 (S2/S5) | TestS2S5CalendarRoll ×2 (假日顺延正反对照 + 非交易日锚) | 绿 |
| 3 (S3) | TestS3LookbackWindow (窗左边界不计/右边界计) | 绿 |
| 4 (S4) | TestS4ExpiredLockNoResurrection (re-fire 落过去, 不复活) | 绿 |
| 5 (O1) | TestO1Dedup (同因同end同侧仅一条) | 绿 |
| 6 (O2+S6) | TestO2S6AbsorbAndLongest ×2 (先长后短吸收/先短后长并存取最长) | 绿 |
| 10† | test_10_breakeven_zero_counts — 保本 0.0 计入, 注释对照 stoploss_guard.py#L64-65@2bd60670f4 truthiness 排除 | 绿 |
| 11 | test_11_none_excluded_with_event — None 排除+INSUFFICIENT_DATA 事件 | 绿 |
| 12 | test_12_lowedge_sum_zero_in_none_out — 0.0 入和/None 出+事件, 注释对照 low_profit_pairs.py#L63-65 | 绿 |
| 13 | test_13 — equity 回撤手工值==规则值 (12位精度); 断言无 ratios/drawdown_calculation 开关 | 绿 |
| 14† | test_14 — 回撤恰等于阈值(精确 0.5)触发, 注释对照 max_drawdown_protection.py#L96 严格> 不触发 (T1 反转) | 绿 |
| 15 | test_15 — 阈值∉(0,1] 构造即抛 LockConfigError (兑现上游 TODO) | 绿 |
| 16 | test_16 — 无回撤→NO_DRAWDOWN, 样本不足→INSUFFICIENT_DATA, 两类互斥断言 | 绿 |
| 附: 7 (G1/O3) | TestG1O3Global — global 覆盖任意 key + O3 吸收 | 绿 |
| 附: 21 (T6) | TestConfigFailFast — 互斥参数/非法相位/非法类型 6 例全拒 | 绿 |
| 附: 24 (§2.6) | TestAssertionHygiene — 源内禁"不等号接布尔字面量"自查 | 绿 |

修正记录(如实): 初版 test_14 用 0.10 阈值因浮点表示(1-0.9/1.0=0.0999…8)假红, 改用二进制可精确表示的 0.5 构造"恰等于"; 初版卫生自查正则被自身注释文本自匹配, 改拼接构造。两处均为测试构造问题, 非实现缺陷。

### 验收2 — 两次重放逐字节一致: **PASS**

复跑: `/Users/fisher/.openclaw/venv/bin/python accept_2_determinism.py`
实测输出: 6 个输出文件 (SHA256/blocked/locks/market_stress/events/report) 全部 `IDENTICAL`, `ACCEPT-2: PASS`。组合摘要 a042d9ba…(out/run1/SHA256 同值)。确定性来源: 无墙钟(lock_time 由 as_of 派生)、全部迭代排序、浮点定长格式化。

### 验收3 — 影子与生产零共享状态, MemLockStore 零残留: **PASS**

复跑: `/Users/fisher/.openclaw/venv/bin/python accept_3_isolation.py`
实测: ①no-write 全量重放前后 spike 目录递归清单零新增/零变更(排除 pycache); ②recommendations.db (size=1492815872, mtime_ns) 前后不变; ③ro 连接写入被拒 `attempt to write a readonly database`; ④MemLockStore 无 _con/_path 文件后端属性, 49 条锁全在内存。`ACCEPT-3: PASS`。

### 验收4 — 三列报告 ≥60 交易日 + 逐条溯源: **PASS**

复跑: `python shadow_replay.py --out out/run1 && python accept_4_report.py`
实测: 重放 91 交易日(≥60); blocked_recommendations.csv 660 行, 三列(lock_effective_date/blocked_rec/posthoc_excess_5d)齐备, 溯源违例 0/660 (reason_code 非空+created_by=protection:规则名+lock_id 可关联 locks_fired.csv)。
影子结论摘要(参数=对本台账分布 p90 定标, 见 shadow_replay.py 头注, **非生产定稿**):
- 落锁 49 次: LOW_EDGE 28 / DRAWDOWN_BRAKE 10 / CONSECUTIVE_LOSS 9 / POST_FAILURE_COOLDOWN 2 (0709 prebreakout AI 事故+0710 管线事故, 人工编目; 计数复跑: `awk -F, 'NR>1{c[$5]++} END{for(k in c) print k, c[k]}' out/run1/locks_fired.csv`);
- 被挡 660 条推荐 (42%), 有后验值 579 条: 平均 5 日超额 −0.16%, 中位 −0.55%, 负超额占比 54.9% —— 该参数档下保护净效果轻度为正但不显著, 与"三策略无样本外边际"既有结论一致, 保护参数本身可回测调参正是影子模式的价值;
- 归因注: 被挡行按 longest_lock 归因, DRAWDOWN_BRAKE(1日锁) 常被同日更长的策略锁(3日) 盖过, 故明细含 3 种 reason_code 而落锁含 4 种。

### 验收5 — DbLockStore 杀进程重启判定一致: **PASS**

复跑: `/Users/fisher/.openclaw/venv/bin/python accept_5_restart.py`
实测: 子进程落 2 条锁(strategy 2日/global 3日 next_close)→写判定快照→SIGKILL(-9, returncode=-9)→新进程重开同一 sqlite 文件, 7 组 (key,day,phase) 判定全部一致(含 global 覆盖与相位边界), 重启后可见锁记录 2/2。`ACCEPT-5: PASS`。锁库为独立文件 out/restart_locks.db, 每操作即时 commit。

### 验收6 — INSUFFICIENT_DATA 与 NO_DRAWDOWN 分别可观测: **PASS**

复跑: `/Users/fisher/.openclaw/venv/bin/python accept_6_events.py`
实测: 场景A(样本1<limit3)→`INSUFFICIENT_DATA: rule=StrategyDrawdownBrake,as_of=20260305,samples=1,required=3` 且无 NO_DRAWDOWN; 场景B(样本4全正)→`NO_DRAWDOWN: …,samples=4` 且无 INSUFFICIENT_DATA; 真实重放事件流 INSUFFICIENT_DATA=72 条可从 protection_events.csv 直接审计。NO_DRAWDOWN 在真实亏损台账上计 0 条属预期(回撤恒>0), 可观测性由场景B+单测#16 证明。事件 display 统一 `CATEGORY: k=v` (nautilus §2.2 对齐; 影子期未建 denials 表, 按规格)。

### MARKET_STRESS 多档阈值 (未拍板 → 并列输出, 不定死): **完成**

exit_blocked 代理口径 = SUSPENDED(持有窗内交易日无行情行) + LIMIT_DOWN_SEALED(close==low 且 pct_chg≤-(板块涨跌幅-0.15), 分板块阈值主板10/创业板科创板20/北交所30/ST5, 规则表达式非一刀切)。日度占比: n=90, max=10.0%, p95=4.0%, 非零 19 天。

| 阈值 | 触发天数 | 触发日 |
|---|---|---|
| 3% | 8 | 20260303,20260313,20260520,20260608,20260624,20260625,20260626,20260629 |
| 5% | 5 | 20260303,20260608,20260624,20260625,20260626 |
| 8% | 3 | 20260303,20260624,20260625 |

拍板参考: 5% 档介于"过敏"与"迟钝"之间, 90 日触发 5 天, 与 0624-0626 连续应激段吻合; 3% 档多出 2 个孤立日; 8% 档漏掉 0608/0626。

## 2. 隔离核查 (git status)

`git -C /Users/fisher/.openclaw/workspace status --short` — 本 spike 新增文件全部为 `??`(untracked) 且全部位于 `factor_factory/research/2026-07-github-stock-systems/spikes/spike4_protection/` 内; `M`/`D` 条目 166 个与会话起点快照一致(既有工作区漂移, 非本 spike 产生); grep `recommendation_warehouse|panel_full|setup_registry|wts_frozen|s3_frozen|holdout` 无任何 `M` 命中(命中项均为既有 `??`); recommendations.db size+mtime 经 accept_3 实测不变。**未触碰任何生产文件/冻结资产。**

## 3. 已知限制与规格修订建议 (非验收阻塞)

1. **日历缺口**: price_daily_cache 派生日历在 20260408-20260410 缺行(源缓存即缺), 影子内自洽; 生产接线时日历应改用 akshare 主日历(已有基建)。台账末端锁 end 物化用了 15 个工作日投影(仅锁 end, 报告已标注)。
2. **后验超额基准**: 用全市场等权日均收益(pct_chg 均值)复利作基准, 未用沪深300/中证500 真基准——生产接线时应换复盘引擎的真基准口径。POST_FAILURE_COOLDOWN 被挡 20 条(0710 尾日)后验值 NA 属预期(5日窗未走完)。
3. **PostFailureCooldown 重复 re-fire**: 事故在窗内每日重评估产生同 end verdict, 由 O1 去重吸收(表内仅一条)——行为正确但吸收计数会累积; 生产版建议 verdict 带 incident_id 幂等键。
4. **DRAWDOWN_BRAKE 归因遮蔽**: longest_lock 归因下短 global 锁被长 strategy 锁盖过; 若拍板需要"逐锁归因"报告, 三列报告可加 all_covering_locks 列(影子已有数据, 一行改动)。
5. **§3.1 字段数勘误**: 规格称"15 字段", 表格实列 16 列(unlock_time/unlock_by 并作一行)——按 16 列实施, 建议规格下版更正措辞。
6. **单测清单未覆盖条目**(本 spike 规格外, Wave2 实施期补): #8/#9(G 系 scope_flags 与 exit_blocked 联动)/#17-20(A股退出受阻 §4 三件套)/#22(生产管线重启往返)/#23 后半(与 DbLockStore 零串态已由验收3/5 分别覆盖)。

## 4. 结论

**通过**。三层闸(检测 IProtectionRule → 状态 strategy_locks/LockStore → 执行影子闸)+类型化事件在影子模式下端到端成立; freqtrade 19 条冻结语义中 S/O/G/T 系关键裁定全部复现或按规格显式反转(含等号阈值/falsy-zero 修复/equity 唯一口径/数据不足不静默); 双实现(Mem/Db)共享判定逻辑消除语义分叉; 对 91 交易日真实台账的重放确定性、隔离性、可溯源性全部实测过关。发布合同第 18 族检查接线按规格留待用户确认后单独 commit。
