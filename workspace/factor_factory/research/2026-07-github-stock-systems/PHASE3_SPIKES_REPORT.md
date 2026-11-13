# PHASE3 五 spike 收账报告 (2026-07-11)

- 收账人: Phase3 收账会话（独立）
- 输入: `spikes/{spike1_upstream,spike2_pit,spike3_provider,spike4_protection,spike5_parity}/SPIKE_REPORT.md` + PHASE2_TIERA_REPORT.md §2 实施清单/§3 spike 规格
- 判定通则(沿用 §3 规格): 任一验收条件不满足即 spike 判负 → 实测无一判负; 全部 spike 满足隔离铁律(additive, 生产零写入, 各报告隔离核查节在案)

---

## 1. 五 spike 判决表

### 1.1 总判决

| Spike | 判决 | 验收 PASS/FAIL/PENDING | 承载 Wave 条目 | 备注 |
|---|---|---|---|---|
| Spike-1 上游迁移 (housekeeping + PhaseContext backport) | **通过** | 6 / 0 / 1 (验收6b) | U-1/U-2(已完成)、W1-8 | 1 条实质规格修订(F-1, 接入点②); PENDING 系日历绑定, 非缺陷 |
| Spike-2 PIT 双时间+revision+as_of | **通过** | 7 / 0 / 0 | W1-1、W1-2 | 真实 akshare 数据 21,488 行; 前视零记录 pandas+SQL 双断言 |
| Spike-3 provider contract | **通过** | 6 / 0 / 1 (验收3 EM 子项) | W1-4、W1-5、W1-6、W1-7(部分)、W1-11 | 1 条规格数字勘误(yjyg 键数); EM PENDING 系源间歇封锁, 非契约层缺陷 |
| Spike-4 protection shadow 三层闸 | **通过** | 6 / 0 / 0 | W2-8、W2-9、W2-11(19/24条) | MARKET_STRESS 三档分布已产出(§4 拍板附件); 91 交易日真实台账重放 |
| Spike-5 表达式 parity 5 因子 | **通过** | 7+1★ / 0 / 0 | W2-1、W1-3(口径 API 部分) | ★=#52 永久回归资产; 附 2 项真实数据新发现(§2.5) |

**合计: 32 PASS · 0 FAIL · 2 PENDING**(均为观察类, 不阻塞 Wave 排期)。五 spike 均无"证伪"判定; 规格层面产出 1 条**必须修订**(Spike-1 F-1, 字面落地会弱化护栏)+2 条**数字勘误**(Spike-3 yjyg 键数、Spike-4 §3.1 字段数)+若干增量收紧建议(§2)。

### 1.2 逐条验收汇总

| Spike | # | 验收条件(缩写) | 判定 |
|---|---|---|---|
| 1 | 1 | housekeeping: status 仅剩 fisher_bridge.py / FREEZE.md 在根 / grep=1 | PASS |
| 1 | 2 | 14:59→closing_auction+前一交易日 / 15:01→postmarket+当日 | PASS (注入时钟实测+11 组边界单测) |
| 1 | 3 | 节假日→non_trading+拒进取数 stage | PASS (真周日+合成周中假日双测) |
| 1 | 4 | 日历不可得→退出码≠0+告警 (fail-closed) | PASS (告警走 Discord stub 缝, 已诚实声明) |
| 1 | 5 | 0710 型盘中缓存污染回放→写前断言拦截 | PASS (+S6: 07:47 盘前事故时刻被 effective 日期语义结构性消除) |
| 1 | 6a | 测试套件全绿 | PASS (分支 1685 passed / main 1657 passed; 验收器 21/21) |
| 1 | 6b | AI 链 19:30 管线连续 2 交易日绿灯 | **PENDING** (见 §1.3) |
| 2 | 1-7 | 前视零记录/毒样本/修订一致性/未来期禁引/双口径/450天拖带守卫/增量幂等 | 全 PASS (124 次 as_of 重放 436,913 行返回违例=0; 全量重拉第二遍 appended=0) |
| 3 | 1 | 三接口自检 harness 全绿(T恒等/E隔离/T2勾稽) | PASS (live 5/5 + 离线负向注入必红) |
| 3 | 2 | 毒样本三件全拒(kline 列漂移/未来日期/yjyg 键漂移) | PASS (键数按运行时勘误 26/28, 语义等价 §2.3) |
| 3 | 3 | 跨源对账 ≤1e-4 + 0.05% 毒样本必拒 + 不一致清单落 manifest | PASS; **EM 通道落盘子项 PENDING** (见 §1.3) |
| 3 | 4 | PIT as-of 三表 available_at>as_of ==0 | PASS (6 组重放 violations=0) |
| 3 | 5 | 涨跌停例外表正反(科创第3日+35%豁免 / 主板+25%必拒) | PASS (+ipo_day1/北交所30cm/豁免窗外/豁免不豁OHLC 四加测) |
| 3 | 6 | 血统尝试链落 manifest(含失败尝试)+写失败产生 WARN | PASS |
| 4 | 1 | §5 单测清单指定条目全绿(含†反转注释) | PASS (19/19; 两处测试构造修正如实记录) |
| 4 | 2 | 两次重放逐字节一致 | PASS (6 文件 SHA256 全 IDENTICAL) |
| 4 | 3 | 与生产零共享状态, MemLockStore 零残留 | PASS (recommendations.db size+mtime 不变, ro 写拒) |
| 4 | 4 | 三列报告 ≥60 交易日+逐条溯源 | PASS (91 交易日, 660 条被挡, 溯源违例 0/660) |
| 4 | 5 | DbLockStore SIGKILL 重启判定一致 | PASS (7 组判定全一致) |
| 4 | 6 | INSUFFICIENT_DATA 与 NO_DRAWDOWN 分别可观测 | PASS (互斥断言+真实事件流 72 条可审计) |
| 5 | 1 | 恒等回放 rtol=0 (两查询窗重叠段逐元素相等) | PASS (主测 5 因子+辅测生产 cache 截面) |
| 5 | 2 | 欠窗必炸 LookbackUnderflow | PASS (+真实数据洞 PanelHoleError 加测) |
| 5 | 3 | 低报审计 lookback±1 (CI 拒低报) | PASS (5 因子 plus1 全等/minus1 全变+2 负向对照) |
| 5 | 4 | 停牌 NaN 不 ffill, 复牌差异可解释 | PASS (真实停牌 688981.SH+合成对照+生产 cache 三路) |
| 5 | 5 | 复权 raw vs hfq 显式断言 + qfq 无 asof 即 raise | PASS (52 处真实除权事件, 恒等式 rtol=1e-12) |
| 5 | 6 | ties 声明锁定+换 pandas 版本值不变 | PASS (pandas 3.0.0 与 2.3.3 双 venv 实测, 非 PENDING) |
| 5 | 7 | 未注册禁入 | PASS (装载器与闯关闸同门+4 种残缺注册全拒) |
| 5 | ★ | #52 永久回归三道闸 | PASS (PERMANENT ASSET, 不随回滚删除) |

### 1.3 PENDING 观察计划汇总 (2 项)

| # | 事项 | 观察窗口 | 通过判据 | 失败处置 |
|---|---|---|---|---|
| P-1 | **Spike-1 验收6b: AI 链(19:30 管线)连续 2 交易日绿灯** | **2026-07-13(周一)、07-14(周二)** 各日 19:30 管线后 | aiBackfill stage 绿灯: run_manifest 无 critical + Discord pipeline alert 无 critical, 连续两日 | 红灯即回滚 `git -C daily_stock_analysis revert 7c463d2`(或从 patch/archive 移回); 风险评估为低(生产 AI 链 2026-07-08 起直连 generate_prebreakout_ai.py 不经 fork src/, housekeeping 未动 venv+.env) |
| P-2 | Spike-3 验收3子项: EM 通道(stock_zh_a_hist)对账数据落盘 | 每日非交易时段跑 `run_em_recon.py 4`, 至多 5 个日历日(至约 07-16) | 任一次成功即自动闭合(脚本落 `data/daily_akshare_em.parquet`+`em_reconciliation` manifest) | 5 日仍 0 键 → 判"本机对 push2 族不可用", **sina 通道正名为 akshare 日线对账主源**(FetchMeta 源枚举本就含 akshare_sina); 不阻塞 W1-4/W1-5 采纳 |

注: P-2 的契约层断言(12 列冻结契约)已在一次间歇成功中 live 通过, PENDING 仅指对账数据的持久化证据; 失败链 4 条已完整落 `manifest/run_manifest_em_recon.json`。

---

## 2. 规格修订 (实测驱动, 对应 Wave 条目)

无验收 FAIL, 故本节全部为"实测勘误+增量收紧"两类; 其中 R-1 为**必须修订**(字面落地会引入回归), 其余为 Wave 实施稿必吸收的收紧项。

### R-1 (必须) W1-8 / U-3 切片二 — 接入点②字面移植会弱化盘前护栏
- 证据: spike1 §F-1 + S6 实测。upstream_diff.md §4.3 接入点②原文"盘中裸跑护栏改读 is_partial_bar", 但 premarket 的 is_partial_bar=False(上游语义), 07:47 型盘前裸跑将放行; 现生产护栏(run_pipeline.py L433-440)覆盖 00:00-15:05 全窗。
- 修订(二选一, PROPOSED_PATCHES.md 已采用前者): ②写成 `is_partial_bar or phase is PREMARKET`; 或规格明文**①②④三接入点绑定同一 commit**(S6 已证盘前跑数据自动标为前一完成日, 联动落地才安全)。

### R-2 W1-1 / W1-2 (PIT 统一规格) — 四条收紧 (spike2 §4)
1. 增加公告滞后**下界** sanity 守卫(如 announce_date ≥ period−400 天, 可配置): 真实表实测 -481 天上游污染样本(920099 疑招股书盈利预测混入); 越界=显式告警+隔离列, 不静默丢。
2. `available_at_precision` 列({'date','minute','second'})并入 Wave1 正式字段表: "禁降精度"原文只约束类型, 精度诚实性需显式列承载(NOTICE_DATE 源头精度=日, 探针实测恒 00:00:00)。
3. 乱序回填(迟到的更早公告)规格明文: 唯一合法操作=整组重建入新表+双表对账, 禁止原地重排 revision_seq(spike 实现为 hard fail)。
4. 同日多公告 (period,announce_date) 重复键处置明文: 源带盘中时刻以 available_at 细分, 日精度源人工裁决入库。
- 另附实施注记(非规格): 东财 yjyg surface 是 latest-only(IS_LATEST 恒'T', 10 期自然修订链=0)→ **revision 链只能靠持续增量摄取累积, 无法事后回补**, W1-1 排期须尽早上线摄取。

### R-3 W1-4 / W1-5 (provider 契约) — 四条修订 (spike3 §3)
1. DailyBarV1.pre_close "必填且来自源"需配**源能力表**: akshare sina 日线无 pre_close 列 → 放宽 `float|None`(None 仅当源不供给, 落 FetchMeta.notes); EM 通道 pre_close=同行收盘−涨跌额的推导合法性边界需规格明示。
2. **yjyg 列数断言改"27 命名键集合等值"**(数字勘误): 规格"27/29 列 vs 28"计入了 akshare 自产 index 列, 服务端真实载荷 27 命名键(直连抓包+akshare 1.18.24 实测); 命名集合断言同时覆盖加列/减列/换列且不随 akshare 小版本漂移(实证: 本 venv 1.18.24 该接口仅返回 11 列, 与审计冻结 SHA 的 28 列表述不同——版本 pin+契约按运行时标定被踩实)。
3. "自检全绿"明确为**契约合规口径**, 另设源健康度阈值(如 ok 率<50% 降级告警): harness 绿≠源活(EM 间歇日 0 ok+20 failed 勾稽仍成立)。
4. 名称对账加归一化规约: NFKC 归一+交易所状态前缀(XD/N/DR/-UW)剥离后比对, 原始差异(79/5530)仍全量落 manifest。

### R-4 W2-8 / W2-9 / W2-10 (保护链) — 五条修订 (spike4 §3)
1. §3.1 "15 字段"勘误 → **16 列**(unlock_time/unlock_by 规格表并作一行所致), 按 16 列实施。
2. 影子→生产接线时日历改 akshare 主日历(spike 用 price_daily_cache 派生日历, 20260408-0410 缺行, 影子内自洽但不可带入生产)。
3. 后验超额基准换复盘引擎真基准(spike 用全市场等权日均复利, 非沪深300/中证500)。
4. PostFailureCooldown 的 verdict 带 incident_id 幂等键(窗内每日 re-fire 由 O1 去重吸收, 行为正确但吸收计数累积)。
5. 三列报告可选加 all_covering_locks 列(longest_lock 归因下 1 日 DRAWDOWN_BRAKE 锁被 3 日策略锁遮蔽, 数据已有, 一行改动)。
- W2-11 注记: 单测清单 24 条中 #8/#9(G 系联动)/#17-20(A股退出受阻三件套)/#22(生产管线重启往返)属 spike 规格外, Wave2 实施期补齐。

### R-5 W2-1 / W1-3 (parity) — 三条修订 (spike5 §5)
1. 验收1 追加: **"因子算子实现须窗口决定论(每窗独立归约), 禁用起点依赖的滑动累加实现"** — 实测 pandas rolling 的 roll_mean/roll_var 浮点累积态依赖序列起点, 同窗口可差最后 ulp, rtol=0 恒等回放会破功(spike 以 sliding_window_view 每窗独立归约达成); 我方脚本因子若混用 pandas rolling 需换算子, 或放宽 rtol≤1e-12 并显式声明。EWM 类必须"硬窗口化"(定义本身截断到 K 根)而非仅声明保守 lookback, 否则 +1 扰动审计立即抓出。
2. 装载器加 `require_full_sessions` 开关(全市场面板默认开)+新错误类型 PanelHoleError: 生产 backtest_cache 实测 **31 个开市日数据洞**(缺 20251015-1118 连续 25 日+20260408/09/10+0417+0427/28), 中段洞不触发起点守卫、只静默整日 NaN 传播, 是 #52 同型风险。
3. 日历 shift 前向越界拆独立 CalendarOutOfRange(spike 复用 LookbackUnderflow 系省类型, 语义错位)。

---

## 3. Wave 采纳就绪度

### 3.1 可直接采纳为实施基础 (代码质量=验收级, 搬运+接线即可)

| spike 资产 | 采纳去向 | 说明 |
|---|---|---|
| spike1 `code_mirror/phase_context.py` + 28 单测 + `PROPOSED_PATCHES.md` | **W1-8** | 模块本体直接采纳(fail-open 7 处已反转, 时钟/日历双注入); 生产 4 接入点按 PROPOSED_PATCHES.md 施工图执行, ①②④绑定同一 commit(R-1); `mini_pipeline.py` 为演练件**不采纳** |
| spike1 fork housekeeping (`7c463d2` + FREEZE.md) | **U-1/U-2 已完成交付** | Wave 清单可划掉; 待 P-1 两日观察确认 |
| spike2 `pit_store.py`(schema 11 列/validate_store/append_rows/pit_asof) + `ingest_yjyg_pit.py` | **W1-1/W1-2** | 直接采纳; ingest 侧补 R-2 四条收紧后即可为生产 PEAD 摄取的替换基础(修 keep='first' 压扁修订链+contains("净利润")混扣非两处生产口径问题, spike2 §3 发现3) |
| spike3 `contract/` 全包(core/models/fetchers/lineage/reconcile/pit/calendar_provider)+`contracts.yaml` | **W1-4/W1-5/W1-6/W1-11** + W1-7 血统半 | 直接采纳; contracts.yaml 按运行时版本标定的做法定为规范(R-3.2) |
| spike4 `lock_store.py`/`protection_rules.py`/`protection_engine.py` + 19 单测 | **W2-8/W2-11** | 直接采纳(Mem/Db 双实现共判定逻辑、†反转带上游行号注释、GPL 零复制); 发布合同第 18 族接线按规格待用户确认单独 commit |
| spike4 `shadow_replay.py` | **W2-9** | 骨架直接采纳; 日历与后验基准两处按 R-4.2/R-4.3 换生产口径 |
| spike5 `parity_lib/`(registry/loader/tswin/audit/panel/calendar) + `tests/test_52_regression.py` | **W2-1** | 直接采纳; #52 回归为永久资产已就位; 生产因子脚本切调用点=后续单一 commit(spike 规格原文) |
| spike5 `parity_lib/prices.py`(load_prices mode 必填/qfq 强制 asof/恒等式验收) | **W1-3 口径 API 半** | API 形状直接采纳; W1-3 大头(raw 不可变层落地+消费端 grep 改显式口径)仍需实施 |

### 3.2 需新写或补齐 (spike 未覆盖)

| 条目 | 缺口 | 素材 |
|---|---|---|
| **W2-2 tradability 掩码+is_tradable 单入口** | 无 spike 承载, 需新建 | spike4 的 exit_blocked 代理口径(LIMIT_DOWN_SEALED 分板块阈值规则表达式+SUSPENDED 判定)与 spike3 的涨跌停例外表 4 类可直接复用为判定内核 |
| W2-10 退出受阻三件套 | 依赖 W2-2, spike4 仅做了代理口径 | freqtrade_protections.md §4 规格不变 |
| W1-7 熔断器(CLOSED/OPEN/HALF_OPEN) | spike3 只留 circuit_open 枚举位 | CircuitBreaker 从本地 base 650a1c3 直取(155 行 diff=0, MIT) |
| W2-11 余 5 条单测(#8/#9/#17-20/#22) | spike4 规格外 | 实施期补 |
| W1-3 raw 不可变层+消费端切换 | spike5 只验 API 形状 | 3.5-4.5 天估算维持 |

### 3.3 采纳顺序建议 (依赖驱动)

1. **W1-6 日历 provider**(spike3 calendar_provider 直取)——全系统单点, 且 spike4/spike5 均实测踩到日历质量问题(缺行/数据洞); 同时消化 spike3 发现4(新浪日历尾部 173 天, 2026Q4 起 <90 天护栏将开始报警, 需提前定降级路径)。
2. **W1-8 PhaseContext 生产接入**(P-1 绿灯后, 按 PROPOSED_PATCHES ①②④同 commit)——堵 0710 结构性复发面, 改动最小。
3. **W1-4/W1-5/W1-7/W1-11 contract 层**(spike3 直取+熔断补齐)——W1-1 摄取与 W2-1 装载的取数底座。
4. **W1-1/W1-2 PIT 店上线摄取**(spike2 直取)——latest-only 源决定修订链只能从上线日起累积, **每晚一天少一天 vintage**(R-2 注记), 排期优先级实测抬升。
5. **W1-3 口径 API**(spike5 prices.py 为形状基准)→ **W2-1 parity 切换**(单一 commit)。
6. **W2-2 tradability**(新建, 复用 spike3/4 判定内核)→ **W2-10 退出受阻**(W2-2 前置, 规格原文依赖成立)→ W2-8/W2-9 影子常态化(spike4 直取)→ 第 18 族接线(用户确认后单独 commit)。
7. Wave3 各条不受 spike 影响, 维持 PHASE2 §2.3 与用户决议 4(A vs B vs B+)。

---

## 4. 遗留拍板项重列

| # | 事项 | 状态 | 本轮新增材料 |
|---|---|---|---|
| B-1 | **MARKET_STRESS 占比阈值**(W2-10) | 待拍板(用户决议 2: 看 spike-4 影子分布再定 → **分布已到货**) | 见下方实测附件; spike4 参考意见: **5% 档**介于过敏与迟钝之间 |
| B-2 | pipeline 三态开关自动触发(W2-7) | 维持"手动档先行"(用户决议 3); 保护器影子已通过验收, 但"成熟"应指生产影子期观察, 建议 W2-9 常态化运行 ≥1 个月后再议 | spike4 影子净效果: 该参数档下轻度为正但不显著(被挡 660 条中有后验值 579 条, 平均 5 日超额 −0.16%, 中位 −0.55%, 负超额占比 54.9%)——与"三策略无样本外边际"既有结论一致; 保护参数可回测调参正是影子价值 |
| B-3 | C' 真辩论立项(W3-1) | 条件触发, 不排期(用户决议 4 维持) | 无新材料 |
| B-4 | 发布合同**第 18 族检查接线**(发布清单∩活动 entry 锁=∅) | spike-4 已通过, 按规格"用户确认后单独 commit" — **现可拍板** | spike4 §4 结论 |
| B-5 | (新增) 生产 PEAD 摄取切换到 PIT 店 | 建议随 W1-1 排期拍板 | spike2 §3 发现3: 生产 `ingest_pead_preannounce.py` 两处口径问题(归母/扣非可能混杂+keep='first' 丢修订链); 且 latest-only 源使 vintage 不可回补, 晚切换=永久丢数据 |
| B-6 | (新增) backtest_cache 31 开市日数据洞是否回补 | 待生产侧核实 | spike5 §4 发现1: 缺 20251015-1118 连续块+20260408/09/10+0417+0427/28; 建议回补或管线加日历完整性断言(#52 同型风险) |
| B-7 | (新增, 已旗标移交) 生产 EM 兜底路径静默退化 | 观察 P-2 结果后处置 | spike3 §4 发现1: push2his 族本机间歇性连接层拒绝(非 NID 层, 疑 IP/TLS 指纹级), datacenter 族与新浪族正常; `enrich_review_quotes.py`/`fetch_market_snapshot_fallback.py` 的 EM 兜底可能正在静默退化 |
| — | red_lines.md OpenBB AGPL 勘误行 | **已批准回写, 闭合**(用户决议 1) | — |

### MARKET_STRESS 多档分布实测附件 (spike-4, 供 B-1 拍板)

口径: exit_blocked 代理 = SUSPENDED(持有窗内交易日无行情行) + LIMIT_DOWN_SEALED(close==low 且 pct_chg≤−(板块涨跌幅−0.15), 分板块阈值主板10/创业板科创板20/北交所30/ST5)。日度占比样本: n=90 交易日, max=10.0%, p95=4.0%, 非零 19 天。

| 阈值 | 触发天数/90 | 触发日 | 评注 |
|---|---|---|---|
| 3% | 8 | 20260303, 0313, 0520, 0608, 0624, 0625, 0626, 0629 | 比 5% 档多 2 个孤立日(0313/0520/0629), 偏过敏 |
| **5%** | **5** | **20260303, 0608, 0624, 0625, 0626** | 与 0624-0626 连续应激段吻合, spike4 推荐档 |
| 8% | 3 | 20260303, 0624, 0625 | 漏掉 0608 与 0626, 偏迟钝 |

复跑: `cd spikes/spike4_protection && /Users/fisher/.openclaw/venv/bin/python shadow_replay.py --out out/run1`(逐字节确定性已验), 分布文件 `out/run1/market_stress.csv`。

---

## 5. spike 状态

```json
[
  {"spike": "spike1_upstream", "status": "pass"},
  {"spike": "spike2_pit", "status": "pass"},
  {"spike": "spike3_provider", "status": "pass"},
  {"spike": "spike4_protection", "status": "pass"},
  {"spike": "spike5_parity", "status": "pass"}
]
```

判定依据: 5/5 spike 全部验收条件无 FAIL; 2 个 PENDING 均为环境/日历绑定的观察项且各有明确闭合判据与失败处置(§1.3), 不构成判负条件(规格原文判负条件="任一验收条件不满足", PENDING 项的可提前实测部分——spike1 测试套件/spike3 契约断言——均已 PASS)。

## 6. 下一步

1. **07-13(一)/07-14(二)**: 执行 P-1 观察(AI 链绿灯确认), 每日非交易时段跑 P-2 `run_em_recon.py`。
2. 用户拍板 B-1(MARKET_STRESS 档位)与 B-4(第 18 族接线); B-5/B-6/B-7 随对应 Wave 条目处置。
3. Wave1 按 §3.3 顺序开工: 规格修订 R-1~R-5 并入各 Wave 条目实施稿后排期(PHASE2 §3 通则"修订后才可排期"已满足——本报告即修订文本)。

---

## 附: 用户拍板决议 (2026-07-11 第二批)

- B-1 MARKET_STRESS: **5% 档**(90日触发5天, spike-4推荐)。
- B-4 第18族接线: **影子常态化一周无误拦后单独 commit 接线**。
- B-5 PEAD 切 PIT 店: **批准**——Wave1 首项, 双写一周验证后切读, vintage 自切换日起积累。
- B-6 backtest_cache 31 开市日洞: **批准回补**(tushare代理按日重拉+全量对账验证)。
- B-7 EM 兜底: 随 P-2 五日观察处置(未到期)。
