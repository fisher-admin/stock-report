# Phase2 深读 · freqtrade protections 专项 (轨道G9)

- 对象: freqtrade/freqtrade @ 冻结SHA `2bd60670f4c64e392c6ff604fc2c9de3fbbe4cf9` (manifest/repos.json)
- 读法: 全程 raw.githubusercontent + 冻结SHA 纯文本, 未克隆; 引证格式 `path#Lx-Ly@2bd60670f4`
- **GPL-3.0 纪律 (最严格适用)**: 本文件不含任何可复制的 freqtrade 代码片段。所有内容为机制描述、接口形状(字段名/参数名/类型)、判定条件的数学记号、与我方自己记号的伪代码。实施时 strategy_locks 及全部保护器必须从零自写, 禁止对照抄写。
- 防重复建设硬门: 已对照 local/system_inventory.md; 全部提案均标注"新建/升级差距"。

---

## §1 保护契约全链判决表 (逐文件)

深读范围 = 5 保护文件 + protectionmanager + pairlock/pairlock_middleware + strategy 锁API + freqtradebot 触发与入场闸 + backtesting 影子接线 + usedb_context + docs/includes/protections.md + tests/plugins/test_protections.py(全文, §2)。

| 文件 | 职责 | 判决 | 关键证据 |
|---|---|---|---|
| plugins/protections/iprotection.py | 契约核心: ProtectionReturn 四元组 {lock: bool, until: datetime, reason: str\|None, lock_side: str='*'}; 能力旗标 has_global_stop/has_local_stop; 双抽象方法 global_stop/stop_per_pair(均入参 date_now/side/starting_balance, 出参 ProtectionReturn\|None); 恢复条件计算 calculate_lock_end | **抄形状**。检测器只声明"锁到何时+为何锁", 不碰状态——检测/状态分离是全链最值钱的一刀。恢复条件两式: 定时(锚定 max(平仓时刻)+时长)或定点(HH:MM, 过点顺延次日); 对 DB 来的 naive 时间戳读取端补 UTC | iprotection.py#L17-22, L26-29, L104-122, L124-143@2bd60670f4 |
| plugins/protections/stoploss_guard.py | 数 lookback 窗内 exit_reason∈{止损/追踪止损/链上止损/强平} 且 close_profit<required_profit 的笔数, ≥trade_limit 即锁; only_per_pair 关全局, only_per_side 单边计数+单边锁 | **抄机制, 修两处**: ① falsy-zero(§5); ② A股止损跌停不成交, "止损成交计数"信号失真, 我方须换 mark-to-market 口径(§4) | stoploss_guard.py#L44-87 (falsy-zero 在 L64-65)@2bd60670f4 |
| plugins/protections/max_drawdown_protection.py | 全局熔断(has_local_stop=False): 窗口内平仓数≥trade_limit 时算回撤, 双口径 ratios(默认,利润率累加)/equity(重构窗口起点权益=starting_balance+窗口前累计绝对利润, 相对峰谷回撤); 回撤>阈值(严格)即全局锁; 无回撤时计算抛 ValueError 被吞→静默不触发 | **只抄 equity 口径**; ratios 默认是既证反模式(卡+synthesis §4A/H)。"等于阈值不触发"的严格>我方要反转为≥(护栏保守默认); ValueError 静默吞掉=fail-open 微反例, 我方须显式区分"数据不足"与"无回撤" | max_drawdown_protection.py#L23-26, L46-111 (TODO 在 L26, 严格>在 L96)@2bd60670f4 |
| plugins/protections/low_profit_pairs.py | 单标的(has_global_stop=False): 窗口内平仓数≥trade_limit 时求 close_profit 简单和, <required_profit 即锁该标的; only_per_side 单边 | 抄机制。**边界暗坑**: trade_limit 计数在 side 过滤**之前**、求和在过滤之后——单边模式下计数门槛混入对侧交易(L58 vs L62-66)。我方若保留分侧语义须冻结"计数与求和同口径" | low_profit_pairs.py#L42-82@2bd60670f4 |
| plugins/protections/cooldown_period.py | 单标的: 取窗口内最近一笔平仓, 锚定其平仓时刻算锁 end; 无全局形态; 不看盈亏 | 抄形状(我方 PostFailureCooldown 对应, §3) | cooldown_period.py#L28-53@2bd60670f4 |
| plugins/protectionmanager.py | 按配置顺序遍历 handler; 全局路径: handler 返回锁且提议 until 时点尚无覆盖锁(以 until 为查询时点查 is_global_lock)才落 '*' 锁; 单标的路径同构(is_pair_locked 含全局锁, 故全局锁抑制冗余单标的锁); validate_protections 启动即抛 ConfigurationError | **抄两层重叠锁抑制**(manager 层"已覆盖不落新锁"+middleware 层"同因同 end 同侧去重")与配置 fail-fast(互斥参数/HH:MM 格式) | protectionmanager.py#L50-88, L90-125@2bd60670f4 |
| persistence/pairlock.py | 锁 ORM 表: pair(索引)/side(默认'*')/reason(自由文本255)/lock_time/lock_end_time(索引)/active(索引,软删除); 活动锁查询条件 = lock_end_time>now(严格) AND active; side 过滤: 请求 side='长' 命中 {该侧,'*'}, 请求 side='*' 只命中 '*' | **抄表形状+索引**; reason 自由文本是短板, 我方升级为 reason_code 枚举+detail 双字段。lock_end_time 落库 tz-naive+读取端补 UTC 是坑(PR#12835 前科), 我方全链显式时区 | pairlock.py#L19-30, L32-35, L54-64@2bd60670f4 |
| persistence/pairlock_middleware.py | 状态中间件: lock_pair(end 上取整到下一K线)/get_pair_locks/get_pair_longest_lock/unlock_pair/unlock_reason(按原因批量解锁)/is_global_lock('*'记录)/is_pair_locked(自身∨全局); use_db 类属性翻转 DB/内存双模式 | 抄 API 面; **不抄类属性单例**(反模式I, 卡已判)——我方 LockStore 接口+显式注入两实现 | pairlock_middleware.py#L21-32, L34-76, L136-165, L167-190@2bd60670f4 |
| strategy/interface.py 锁API | lock_pair/unlock_pair/unlock_reason 手工操作面; is_pair_locked 可选 candle_date: 传入信号K线时刻→用下一K线开时刻查锁, 消除"锁 14:00:00 到期、新K线 14:00:02 才到"的 2 秒缝隙; docstring 明言"锁只能向上加长" | **抄缝隙防护思想**(日频版=解锁只对齐交易日相位边界, §3)与 unlock_reason 运营面 | interface.py#L1131-1163, L1165-1184@2bd60670f4 |
| freqtradebot.py 触发/入场闸 | 事件驱动: 平仓成交(含链上止损)→handle_protections: ①无条件落 "Auto lock"(until=当下, 上取整→当根K线末)防同K线旧信号重进 ②stop_per_pair ③global_stop, 触发即发类型化 RPC(PROTECTION_TRIGGER/_GLOBAL); 主循环先查 side='*' 全局锁(两侧全停才跳过整轮, 单边全局锁留给 create_trade 内按信号方向查); 入场前 is_pair_locked(candle_date=信号K线, side=信号方向) | **抄事件驱动触发+告警同源**。Auto lock 本质是 live 对齐回测"每K线至多一动作"语义的装置——我方日频等价物: 当日已出推荐的标的当日不得重发 | freqtradebot.py#L111, L635-648, L688-698, L1484, L2432, L2440-2462@2bd60670f4 |
| optimize/backtesting.py 影子接线 | enable_protections 开关(默认 False); 装载 _load_protections 复用同一 ProtectionManager; 每轮 reset: 关 DB(use_db=False, 设 timeframe)+清内存锁+清交易; 回测主循环入场条件含 "该标的该方向在该K线时刻未被锁"; 平仓处理后调 run_protections(先单标的后全局) | **抄"同一套保护代码两栖"**: 检测/状态/闸三层全部复用, 仅存储后端切换。这是保护逻辑本身可被回测检验的范本 | backtesting.py#L243, L353-355, L486-507, L853, L1326-1329, L1555@2bd60670f4; persistence/usedb_context.py#L6-24@2bd60670f4 |
| docs/includes/protections.md | 成文契约: "所有锁截止上取整到下一K线以避免K线内突发买入"; 各保护参数表; ratios=legacy 承认可偏离账户级回撤但仍默认 | 文档与代码一致(逐条对过); "修正口径不设默认"的反模式实锤 | docs/includes/protections.md#L4, L28-31, L70-99@2bd60670f4 |

**四层分离总判决**: 检测(protections/*)→状态(pairlocks 表)→执行(入场闸只读锁表)→通知(类型化消息), 加影子回放和启动 fail-fast, 整链无一处"检测器直接改状态"或"闸内做计算"。这是我方 strategy_locks 的结构蓝本。

---

## §2 G9 核心: test_protections.py 边界行为语义表 (全文 1079 行逐条核实)

测试文件全读; 下表为断言核实后的**冻结语义表**, 每条=我方自研临摹时必须复现或显式反转的行为。记号: `[a,b)` 闭开区间; `now`=评估时刻。

### 2.1 锁的起止边界

| # | 语义 | 核实结果 | 证据 |
|---|---|---|---|
| S1 | **锁区间左闭右开 [lock_time, lock_end_time)**: 活动判定为 lock_end_time>now 严格不等; now 恰等于 end 时**已解锁** | 代码 DB 路径与内存路径均严格>; 测试直接踩点: unlock_at="05:00"、5m K线下, 05:05(=上取整后的 end)时点断言已解锁 | pairlock.py#L55; pairlock_middleware.py#L99; test_protections.py#L563-567@2bd60670f4 |
| S2 | **锁 end 一律上取整到下一K线边界; 恰在边界的时刻再推一整根K线** | unlock_at="05:00" 场景: 05:01 仍锁(注释明言"05:00 K线 05:05 才结束"), 05:05 解锁——证明边界时刻 05:00 被推到 05:05, 不是原地保留 | pairlock_middleware.py#L54; test_protections.py#L552-567; docs/includes/protections.md#L4@2bd60670f4 |
| S3 | **lookback 窗左开右闭 (now-lookback, now]**: 平仓时刻的过滤为 close_date>窗左端 严格; 恰在窗边界平仓的交易**不计数** | 台账查询两路径(DB/内存)均严格>; 测试以"关得太久的第2笔不计数"覆盖(30min 计/100min 不计, lookback 60) | persistence/trade_model.py#L1507-1510, L1831-1832; test_protections.py#L282-304@2bd60670f4 |
| S4 | **锁 end 锚定触发事件(平仓时刻), 不锚定评估时刻; 重评估不延长锁** | 锁过期 5 分钟后再次评估: 保护仍会 re-fire(交易仍在 lookback 窗内), 但 until=max(平仓时刻)+时长 落在过去, 断言此时无全局锁——**过期即过期, 不存在"复读机续锁"** | iprotection.py#L124-143; test_protections.py#L321-325 (注释明言 end-time 应为先前 end-time)@2bd60670f4 |
| S5 | **unlock_at 定点解锁: 触发时刻已过定点→顺延次日** | 21:50 平仓+unlock_at="05:00"→锁到次日 05:00(再上取整 05:05); 23:00 锁、次日 04:59 锁、05:05 解 | iprotection.py#L134-140; test_protections.py#L530-567@2bd60670f4 |
| S6 | **锁只增不减** ("locks can only count up"): 更长的新锁与旧锁并存, 判定取最长(get_pair_longest_lock 按 end 降序取首) | 接口 docstring 冻结该语义; 去重只挡"完全同因同 end 同侧", 不挡更长锁 | interface.py#L1137; pairlock_middleware.py#L108-116@2bd60670f4 |

### 2.2 重叠锁

| # | 语义 | 核实结果 | 证据 |
|---|---|---|---|
| O1 | **middleware 去重**: 同 reason+同(上取整后)end+同 side → 返回已有锁, 不落新记录 | 去重比较用请求 side 精确匹配: '*' 锁不会吸收单侧锁请求, 反之亦然 | pairlock_middleware.py#L55-62@2bd60670f4 |
| O2 | **manager 抑制**: handler 提议锁时, 以**提议的 until 为查询时点**查已有锁; 已有锁在 until 时点仍活跃(其 end>until 严格)→不落新锁 | 即"新锁被现有更长锁完全覆盖时静默吸收"; 若现有锁 end==提议 until, 严格>不成立→走 O1 去重兜底 | protectionmanager.py#L61-65, L83-87@2bd60670f4 |
| O3 | 全局锁抑制单标的锁: 单标的路径的已锁判定含全局锁(is_pair_locked=自身∨全局) | pairlock_middleware.py#L179-190; protectionmanager.py#L84@2bd60670f4 |

### 2.3 全局 vs 单标的 vs 分侧

| # | 语义 | 核实结果 | 证据 |
|---|---|---|---|
| G1 | 全局锁=pair='*' 的**一条**记录, 不展开成逐标的锁; is_pair_locked 对任意标的返回真 | test: 3 笔止损触发后 is_global_lock 为真 | test_protections.py#L317-319@2bd60670f4 |
| G2 | **能力旗标硬边界**: has_global_stop=False 的 handler 其 global_stop 恒 None, 反之亦然; MaxDrawdown 无单标的形态(触发时断言"标的锁不存在, 只有全局锁"), CooldownPeriod/LowProfitPairs 无全局形态 | test_protectionmanager 对全部 4 保护逐旗标断言; MaxDrawdown 触发场景显式断言 pair 未锁 | test_protections.py#L100-105, L772-776, L466-484@2bd60670f4 |
| G3 | StoplossGuard 的 only_per_pair=True **关闭全局形态**(计数只看单标的) | 参数化测试: global_stop 触发与 only_per_pair 互斥 | stoploss_guard.py#L98-99; test_protections.py#L391@2bd60670f4 |
| G4 | **side 查询语义不对称**: 查 side=某侧 命中 {该侧,'*'} 锁; 查 side='*' **只命中 '*' 锁**——side='*' 查询的含义是"双侧均被锁?"而非"任一侧被锁?" | 单侧锁存在时: 按该侧查=真, 按'*'查=假(test 两处断言); 主循环因此先查 side='*' 全局锁(双侧全停才跳过整轮), 单侧全局锁由入场处按信号方向查询兜住(代码注释明言) | pairlock.py#L61-64; test_protections.py#L412-416, L434-438, L638-639, L655-656; freqtradebot.py#L635-638@2bd60670f4 |
| G5 | only_per_side=True: 仅计数该侧交易且仅锁该侧; =False: 双侧合并计数, 锁 side='*'。LowProfitPairs 合并模式下, 对侧盈利可抵消本侧亏损使保护**不触发**(测试: 加入盈利空单后, 合并口径不锁、单侧口径锁多头) | test_protections.py#L570-658 (关键断言 L639: 单侧锁存在性==only_per_side)@2bd60670f4 |

### 2.4 阈值与计数边界

| # | 语义 | 核实结果 | 证据 |
|---|---|---|---|
| T1 | **MaxDrawdown 触发为严格 >**: 回撤恰等于阈值→**不触发**(两种口径均如此, 专项边界测试打桩证实) | 我方反转为 ≥ (护栏保守默认), 须在单测中显式对照注释 | max_drawdown_protection.py#L96; test_protections.py#L866-912@2bd60670f4 |
| T2 | LowProfitPairs 触发为严格 <(利润和恰等于 required_profit 不触发); StoplossGuard 计数达 trade_limit(≥)即触发 | low_profit_pairs.py#L67; stoploss_guard.py#L73@2bd60670f4 |
| T3 | trade_limit 是"数据不足即放行"闸: 窗口内平仓数<limit→直接 None(**fail-open**); MaxDrawdown 无亏损时计算抛 ValueError 亦被吞为 None | test 注释明言"尚无亏损交易...会抛异常"→断言不锁。我方须区分"数据不足"(记录+告警)与"未触发" | max_drawdown_protection.py#L54-55, L93-94; test_protections.py#L708-710@2bd60670f4 |
| T4 | **双口径分歧实锤**: 同一交易序列(1盈2亏,大 starting_balance), ratios 锁 / equity 不锁——参数化对照测试即我方"双口径反例"的官方版本 | test_protections.py#L812-863@2bd60670f4 |
| T5 | equity 口径的窗口起点权益=starting_balance+窗口前累计绝对利润(dispatch 测试断言 1000+5=1005, 相对回撤模式), ratios 口径不传 starting_balance | max_drawdown_protection.py#L58-81; test_protections.py#L915-975@2bd60670f4 |
| T6 | 时长/回看双单位换算: *_candles 按 timeframe 折分钟(20根5m=100min; 1根1d=1440min), 与分钟参数互斥(fail-fast) | test_protections.py#L165-253, L108-162@2bd60670f4 |

### 2.5 触发与惰性求值

| # | 语义 | 核实结果 | 证据 |
|---|---|---|---|
| E1 | **锁是惰性物化的**: 平仓本身不产生锁; 只有保护被评估(live: 平仓事件→handle_protections; backtest: 平仓后 run_protections)时才落锁。测试中新标的平仓后, 评估前断言未锁、评估后断言已锁 | test_protections.py#L471-484@2bd60670f4 |
| E2 | live 每笔平仓附赠 "Auto lock"(锁到当根K线末)——独立于 CooldownPeriod、无法关闭; **backtest 无此装置**(回测循环顺序天然防同K线重进)。两栖对齐点, 移植时要么两侧都有要么都无 | freqtradebot.py#L2441-2442; backtesting.py#L1326-1329@2bd60670f4 |
| E3 | 回测入场闸以行时间戳直接查锁(锁 end 已上取整, 严格>保证"锁到K线 T 边界"在 T 行恰好放行); live 入场闸以 next(信号K线) 查锁——两者对 K线边界的语义一致 | backtesting.py#L1555; interface.py#L1179-1184@2bd60670f4 |

### 2.6 测试卫生反例 (不复现)

test 中多处以"对象/None ≠ 布尔"做断言(如 stop_per_pair 返回值 != only_per_side): Python 中 None≠False 与 对象≠True 恒真, 此类断言**恒通过, 是空转断言**; 该文件真正的裁决断言全部落在返回 bool 的 is_pair_locked/is_global_lock 上(用 ==)。教训入我方单测规范: 保护断言必须落在布尔判定函数上, 禁对象-布尔不等式。证据: test_protections.py#L391, L411, L433, L637, L653-654@2bd60670f4。

---

## §3 我方 strategy_locks 实施稿 (交易日历版)

定位(对照 system_inventory, 全部为**新建或明确升级差距**): 我方今天的"策略暂停/降级"散落在 candidate_state 字段与告警文本里, 无持久锁表、无显式恢复条件、无影子回放——本节为收敛设计稿。我方无自动下单(夜间选股→人工执行边界, 保持), 保护闸作用点是**发布管线**而非交易所。

### 3.1 strategy_locks 表 (SQLite, 建议独立于 recommendations.db 主表区)

| 字段 | 类型 | 语义 | 相对 freqtrade 的变化 |
|---|---|---|---|
| id | INTEGER PK | | 同 |
| scope | TEXT NOT NULL | 'strategy'/'symbol'/'global' | pair/'*' 的推广: 显式三层 |
| scope_key | TEXT NOT NULL | 策略名 / ts_code / '*' | 同 pair |
| side | TEXT NOT NULL DEFAULT 'entry' | 'entry'(禁入发布) / 'exit_blocked'(退出受阻状态, §4) / '*' | long/short 裁剪掉(A股无融券侧); 新增 exit_blocked |
| reason_code | TEXT NOT NULL | 机器可读枚举(见 3.4) | **升级**: freqtrade 只有自由文本 |
| reason_detail | TEXT | 人可读说明 | 对应 reason |
| lock_time | TEXT NOT NULL | ISO8601 **带显式时区**(统一 +08:00) | 修 tz-naive 坑(§1 pairlock 行) |
| unlock_condition_type | TEXT NOT NULL | 'trading_days' / 'at_phase' / 'condition' / 'manual' | stop_duration/unlock_at 的日历化+新增条件式 |
| unlock_after_days | INTEGER | 交易日数(trading_days 用) | 分钟→交易日 |
| unlock_at_phase | TEXT | 'next_open' / 'next_close' / 'after_review' | unlock_at HH:MM 的相位版 |
| unlock_condition | TEXT | 条件式 DSL, 如 'first_tradable_close'(§4) | **freqtrade 没有** |
| lock_end_date | TEXT | 物化解锁交易日(由 akshare 日历计算落库, 索引) | lock_end_time 的日历版; 派生字段进 run_manifest 对账 |
| active | INTEGER NOT NULL DEFAULT 1 | 软删除 | 同 |
| created_by | TEXT NOT NULL | 'protection:<名>' / 'manual' / 'pipeline' | 新增审计 |
| unlock_time / unlock_by | TEXT | 解锁事件审计(freqtrade 只翻 active 不留痕) | **升级** |

索引: (scope, scope_key, active), (lock_end_date, active)。

### 3.2 冻结边界语义 (直接移植 §2, 日历化)

1. **锁区间左闭右开**: is_locked(scope_key, as_of_day, phase) = active ∧ (lock_end_date > as_of_day ∨ (lock_end_date == as_of_day ∧ phase 先于 unlock_at_phase))。解锁日该相位起即可用 (S1)。
2. **解锁点只对齐交易日相位边界**(next_open/next_close), 无分钟粒度; 解锁日非交易日或标的停牌→顺延至下一可交易日 (S2+S5 的日历版, "K线缝隙"防护的日频等价物)。
3. **lookback 窗左开右闭**按交易日计: (signal_day−N, signal_day] (S3)。
4. **end 锚定触发事件日**(复盘日/事故日), 重评估不延长; 新触发产生更晚 end 才落新锁, 判定取最长 (S4+S6)。
5. **去重**: 同(scope_key, reason_code, lock_end_date, side)不重复落库 (O1); 提议锁被现有更长锁覆盖则吸收 (O2); global 锁抑制同义 symbol/strategy 锁 (O3)。
6. **全局锁=scope='global' 单记录**, is_locked 恒含全局判定 (G1)。
7. **触发阈值一律含等号(≥/≤)**——与 freqtrade 严格不等**显式相反**, 护栏保守默认; 单测中注明对照 (T1/T2 反转)。
8. **"数据不足"≠"未触发"**: 样本数<trade_limit 或计算失败时, 返回带 reason 的 INSUFFICIENT_DATA 事件落日志+计数器, 不静默放行为常态 (修 T3 fail-open)。
9. **惰性物化+事件驱动**: 锁只在保护评估点产生(20:45 收盘闭环 stage 之后追加 protections stage); 触发即发 Discord 类型化消息 {PROTECTION_TRIGGER, scope, reason_code, lock_end_date} (E1+告警同源)。
10. **配置 fail-fast**: unlock_condition_type 三选一互斥、相位枚举校验、非法即抛异常拒启动, 接 v2 合同硬校验路数 (§1 manager 行)。

### 3.3 接口形状 (我方记号伪代码)

```
LockStore(协议)                       # 修 freqtrade 类属性单例反模式
  lock(scope, key, side, reason_code, detail, unlock_spec, now) -> Lock   # 含去重/吸收
  is_locked(scope_key, as_of_day, phase, side) -> bool                    # 自身 ∨ global
  longest_lock(scope_key, as_of_day) -> Lock|None
  unlock_by_reason(reason_code, now, by) -> int                           # 运营批量解锁
  active_locks(as_of_day) -> list[Lock]
实现: DbLockStore(生产) / MemLockStore(影子回放), 构造注入, 无全局态

IProtectionRule(协议)                 # 检测层, 只读台账不碰锁
  scope_flags: {global: bool, per_strategy: bool, per_symbol: bool}
  evaluate(as_of_day, ledger, config) -> ProtectionVerdict|None
ProtectionVerdict = {lock: bool, unlock_spec, reason_code, detail, scope, scope_key}

ProtectionEngine                      # 执行层
  run(as_of_day): 按配置顺序 evaluate 全部规则 → LockStore.lock(带 O2/O3 抑制) → 告警
发布管线接入: stage(候选→发布) 前过滤 active_locks; 
validate_publication_contract 新增第 18 族检查: 发布清单 ∩ 活动 entry 锁 = ∅
```

### 3.4 首批保护器与 reason_code 枚举

| 规则(自研名) | 对应 freqtrade | scope | 触发(全部≥/≤含等) | 恢复 |
|---|---|---|---|---|
| ConsecutiveLossGuard | StoplossGuard | strategy | N 交易日内 mark-to-market 跌破止损线的推荐笔数 ≥ limit (不问是否"成交", §4) | trading_days |
| StrategyDrawdownBrake | MaxDrawdown | global | 复盘权益/累计超额曲线相对回撤 ≥ 阈值; **只有 equity 口径, 不实现 ratios** | at_phase('after_review') |
| LowEdgePause | LowProfitPairs | strategy | 滚动 M 日超额和 ≤ 阈值 且样本 ≥ limit | trading_days |
| PostFailureCooldown | CooldownPeriod | strategy/global | 管线级事故(0710 类)修复确认后 | trading_days(短) |
| ExitBlockedSentinel | **无对应, 自研** | symbol | §4 | condition |

reason_code: CONSECUTIVE_LOSS / DRAWDOWN_BRAKE / LOW_EDGE / POST_FAILURE_COOLDOWN / LIMIT_DOWN_SEALED / LIMIT_DOWN_INTRADAY / SUSPENDED / MARKET_STRESS / MANUAL / AUTO_DEDUP_DAILY(当日已发防重发, E2 的日频等价物)。

全局熔断动作分级(与我方诚实性规范一致): global entry 锁默认**降级为 research_only 发布**而非停发——客户向页面保留但加 honesty banner, 可回滚。

### 3.5 影子回放 (E 系语义移植)

复盘引擎加 shadow 模式: 用历史 candidate/复盘台账 + MemLockStore 重放 ProtectionEngine, 输出三列报告: (锁生效日, 被挡推荐, 被挡推荐的后验超额)。验收: 同一台账两次重放结果逐字节一致(确定性); 与生产零共享状态。这回答"如果当时有保护, 会挡掉什么"且**保护参数本身可被回测调参**——freqtrade shadow 的全部价值。

### 3.6 验收条件 (实施完成的定义)

- §5 单测清单全绿;
- 影子回放确定性验收通过;
- 发布合同第 18 族检查上线且 pre-push 守卫覆盖;
- 锁表往返: 人工 lock→当晚管线该策略缺席发布→unlock_by_reason→次日恢复, 全程 Discord 消息可见;
- 重启恢复: 杀掉管线进程重跑, 锁判定不变(状态在 DB 不在内存);
- run_manifest 记录当晚活动锁快照(lock_end_date 派生字段对账)。

成本: 表+引擎+发布闸接入 1-2 天; 影子回放 +1 天; 单测 +0.5-1 天。

---

## §4 A股"退出受阻"保护语义 (freqtrade 空白, 自研设计)

freqtrade 保护模型的隐含假设(卡 market_assumptions 已判): 7x24 随时可出, 锁只闸**入场**, "解锁即可交易"。A股头号风险恰是"出不去"——跌停封死/停牌。且 StoplossGuard 以"止损成交"计数, A股止损单跌停不成交, 触发信号本身失真。设计三件套:

### 4.1 ExitBlockedSentinel (检测)

每收盘对"活跃推荐持有窗内"标的判定(装载期预计算布尔契约, 参照 qlib tradability 思想; 涨跌停阈值**分板块**——主板10%/创业板科创板20%/北交所30%/ST±5%, 用规则表达式不用 0.095 一刀切, qlib 反例):

- `LIMIT_DOWN_SEALED`: 一字跌停(开=低=跌停价) 或 收盘封死跌停且尾盘无成交承接——当日实质不可卖出;
- `LIMIT_DOWN_INTRADAY`: 收盘跌停但盘中有非跌停成交——部分可成交, severity 低一档;
- `SUSPENDED`: 停牌(源: tushare/akshare 停复牌接口, 过可得性 spike 后接入; 新数据接口红线2)。

### 4.2 锁语义: side='exit_blocked' 是**状态标注**, 不是闸

我方不下单, "退出受阻"锁的产出为三联动:

1. **复盘诚实口径**(升级差距: 任务#38 已在回测器做了停牌/退市出场不丢弃, 本条推广到前瞻复盘): exit_blocked 活动期间禁止按当日收盘价假设可平(vnpy"停牌平bar恒可成交"反例); 名义出场日顺延至首个可成交日, 出场价取该日保守口径(收盘价与 VWAP 取劣)。复盘记录中显式标 blocked_days 字段。
2. **发布页持有警示**: 该标的在 stock-report 持有清单标注"退出受阻(跌停封死/停牌)+已持续天数", 属诚实性规范范围。
3. **入场闸联动**: 同标的 entry 自动锁(跌停/停牌中的票禁止再入任何策略推荐清单); 解锁跟随 exit_blocked 锁。

### 4.3 条件式恢复 (freqtrade 只有定时/定点, 此为新增类型)

unlock_condition='first_tradable_close': 每日收盘 protections stage 评估——标的当日复牌且非一字跌停且有有效成交→满足, 翻 active=0 并写 unlock_time/unlock_by='condition_met', 发解锁消息。**评估失败(数据缺失)当日不解锁**(fail-closed, 红线路线), 并计入 INSUFFICIENT_DATA 告警。

### 4.4 计数口径联动 (修 freqtrade 失真)

ConsecutiveLossGuard 的"止损"计数定义 = 持有期内任一收盘 mark-to-market 相对入场跌破止损线, **无论能否成交**; exit_blocked 期间的浮亏照计。另设全局应激熔断: 当日活跃持有中 exit_blocked 占比 ≥ 20%(参数, 用户拍板) → MARKET_STRESS 全局锁(只停买不涉卖, 与 FinRL turbulence 条目"只停买不强卖"结论一致——本条不宣称 alpha, 纯风控闸, 不触 necromap)。

成本: 检测器(复用已有涨跌停/停牌数据面)+条件解锁+复盘口径推广 1.5-2 天。

---

## §5 falsy-zero 与双口径反例 → 我方保护单测清单

freqtrade 两枚官方反例(卡已立案, 本轮从测试侧核实其**没有**对应负向测试——falsy-zero 行为在 test_protections.py 中无一条断言覆盖 close_profit==0.0 的交易, 反而 T4 双口径有对照测试):

- **falsy-zero**: 止损计数用 truthiness 连接 close_profit 判断→精确保本(0.0)交易被静默排除, required_profit>0 时按语义应计入 (stoploss_guard.py#L64-65@2bd60670f4); LowProfitPairs 求和同样以 truthiness 过滤 (low_profit_pairs.py#L63-65); 而 equity 口径用显式 None 归零处理——同仓三种 None/0 处理并存 (max_drawdown_protection.py#L63)。
- **双口径**: ratios 默认+equity 修正不设默认+阈值无校验 TODO (max_drawdown_protection.py#L25-26)。

### 我方保护实现单测清单 (验收=全绿; 标 † 为对 freqtrade 语义的显式反转)

**A. 边界 (S 系)**
1. 锁到 D 日 next_open: D-1 日任意相位 locked; D 日 next_open 及以后 unlocked (S1 左闭右开)。
2. 解锁日 D 为非交易日/标的停牌 → 顺延至下一可交易日 (S2/S5 日历版)。
3. lookback=N: 恰在窗左边界日平仓的复盘不计数, 窗内最后一日计数 (S3)。
4. 锁过期后重评估(台账仍在窗内): 不产生"仍活跃"的新锁, is_locked=False (S4 锚定语义)。
5. 同因同 end 同侧重复触发: 表内仅一条记录 (O1)。
6. 先有长锁再触发短锁: 吸收, 无新记录; 先短后长: 两条并存, longest_lock 取长 (O2+S6)。

**B. 作用域 (G 系)**
7. global 锁活动时, 任意 strategy/symbol 的 is_locked=True (G1/O3)。
8. scope_flags 硬边界: per_symbol=False 的规则对 symbol 评估恒 None (G2)。
9. entry 锁不影响 exit_blocked 判定, 反之 exit_blocked 联动产生 entry 锁 (G4 的我方版; side 维度语义表)。

**C. falsy-zero (专防)**
10. †保本推荐(超额恰=0.0)在 ConsecutiveLossGuard 计数中**计入**(阈值>0 场景); 断言实现里对 None 用显式 is-not-None 判别。
11. 复盘值为 None(数据缺失)的记录: 从计数排除**且**产生 INSUFFICIENT_DATA 事件, 不静默。
12. LowEdgePause 求和: 0.0 参与求和, None 排除+事件, 两者不混用一个 truthiness。

**D. 双口径 (专防)**
13. 移植 T4 反例: 构造仓位变化台账, 手工计算 equity 相对回撤, 断言我方 DrawdownBrake 数值==手工值; 断言系统内**不存在** ratios 模式开关(口径唯一性测试)。
14. †回撤恰等于阈值 → **触发** (与 freqtrade 严格>相反, 注释对照 T1)。
15. 阈值合法性: 配置回撤阈值∉(0,1] → 启动抛异常 (兑现 freqtrade 未兑现的 TODO)。
16. 无亏损样本: 不触发**且**留 NO_DRAWDOWN 事件, 区别于 INSUFFICIENT_DATA (修 T3 吞 ValueError)。

**E. A股退出受阻 (§4)**
17. 一字跌停日: 复盘不得按当日收盘价平仓, 出场顺延+blocked_days 累加。
18. 条件解锁: 复牌首个有效成交收盘→次相位解锁; 复牌当日又一字跌停→不解锁。
19. exit_blocked 活动 → 同标的 entry is_locked=True; 解除后同步解除。
20. exit_blocked 占比≥阈值 → MARKET_STRESS 全局 entry 锁, 不产生任何"强卖"副作用。

**F. 工程卫生**
21. 配置 fail-fast: 恢复条件互斥参数/非法相位/非法日期 → 启动异常 (T6)。
22. 重启恢复: 落锁→新进程→判定一致 (状态在 DB)。
23. 影子回放确定性: 同输入两次重放逐字节一致; 与 DbLockStore 零串态 (修类属性单例反模式)。
24. 断言卫生: 单测内禁"对象/None != 布尔"式断言(CI grep 该模式), 保护断言一律落在布尔判定函数 (§2.6 反例)。

---

## G条目闭合声明

- **G9 (保护边界语义未从测试核实) — 闭合**。G9 规定动作"自研 strategy_locks 前速读 test_protections.py 边界用例, 把断言翻译进我方单测"已超额完成: tests/plugins/test_protections.py 全文 1079 行逐断言核实, 产出 §2 语义表 19 条(起止边界 6/重叠 3/作用域 5/阈值 6 类)+§5 单测清单 24 条; 锁截止**左闭右开**、边界时刻**再推一根K线**、lookback **左开右闭**、end **锚定事件不锚定评估**、side='*' 查询**只命中双侧锁**、阈值**严格不等**(我方反转) 等歧义点全部裁定并绑定行号。附带发现: falsy-zero 行为在官方测试中无负向覆盖(临摹者若只抄测试仍会漏), 已单列 §5.C。G9 提及的 pairlist 姊妹体系**维持不读**(非 G9 规定动作; universe 级保护我方已有 S3 冻结人群+stock_list 双源, 按防重复建设硬门不新建, 见 confession)。

## coverage_confession

- **没读**: pairlist 过滤器体系(如上, 有意排除); exchange/ccxt 执行层; RPC/Telegram/webserver 实现(仅读 handle_protections 内的消息发送形状); DB migrations(pairlocks 表的 schema 演化史未核, 若曾有字段迁移坑不在本文); hyperopt 对保护的并行处理细节(仅知靠 fork 隔离, 卡二手结论); FreqAI; issue/PR 原文(#12835/#11726/#12717 采信 Phase1 卡, 本轮未重读); ~3.2 万 commits 演化史。
- **不确定**: ① K线边界"恰在边界推一整根"的裁定依据是测试行为反推(05:01 锁/05:05 解)+上取整函数委托给 ccxt 的 ROUND_UP, 未读 ccxt 源码本体——行为证据充分但实现细节是三方库; ② unlock_at 的时区语义(HH:MM 按 UTC 解释, 因锚点时间为 UTC)——对我方无影响(全用交易日历相位), 未再深查; ③ get_trades_proxy 的 DB 路径与内存路径行为一致性只核了 close_date 过滤条件本身, 未核 SQLAlchemy 时区转换在比较中的边角; ④ "回测无 Auto lock 但循环顺序天然防同K线重进"是我对 backtesting 主循环顺序(先入场后平仓)的推读, 未运行验证。全部行号基于冻结 SHA 2bd60670f4 静态阅读, 未运行任何 freqtrade 代码。

## Wave映射

| 产出 | 映射 | 粗成本 |
|---|---|---|
| §3 strategy_locks 表+ProtectionEngine+发布闸+合同第18族 | **Wave2 组合风险契约** | 1-2 天 |
| §3.5 影子回放(MemLockStore+复盘重放) | Wave2 (保护参数可回测) | +1 天 |
| §4 退出受阻三件套(检测/条件解锁/复盘口径推广) | **Wave2** (复盘口径部分兼属 **Wave1 数据可信**: 跌停/停牌布尔契约装载期预计算) | 1.5-2 天 |
| §5 单测清单 24 条 | Wave2 (验收基准) | 0.5-1 天 |
| MARKET_STRESS 全局应激闸(占比阈值, 用户拍板) | Wave2 | 0.5 天(含拍板项) |
| INSUFFICIENT_DATA/NO_DRAWDOWN 事件区分+告警 | Wave1 数据可信(fail-closed 路线复用) | 0.25 天 |
| AI 相关: PostFailureCooldown 把"AI 慢性失效"类事故纳入持久锁 | Wave3 AI 消融的运维前置(AI 永不改分数, 锁只作用于发布降级) | 含在 §3 内 |
| 上游迁移项目: 无直接映射(freqtrade 非我方上游) | — | 0 |
