# Spike-2 报告 · PIT 双时间 + revision + as_of (PEAD 业绩预告)

- 日期: 2026-07-11 · 建造者: spike2 subagent · 规格: PHASE2_TIERA_REPORT.md §Spike-2
- **结论: 通过 (7/7 PASS, 0 FAIL, 0 PENDING)** — 双时间字段表 + revision_seq + pit_asof 单入口在真实 akshare 数据上成立, "前视零记录"可机械断言(pandas+SQL 双断言), 全部毒样本/负向测试如期被拒。

## 0. 实施物清单 (全部 additive, 位于本目录内)

| 文件 | 内容 |
|---|---|
| `pit_store.py` | 双时间 parquet 表 schema(11 列) + `validate_store` 全表不变式 + `append_rows`(幂等/revision_seq 分配/拖带守卫/乱序回填守卫/原子写) + `pit_asof` 单入口 API(P/PRef/lag/双口径) |
| `ingest_yjyg_pit.py` | akshare `stock_yjyg_em` 真实拉取 → PIT 行; 28 列原始位置契约探针(直连东财端点); 本地缓存兜底(`--fallback-cache`, 只读) |
| `test_acceptance.py` | 七条验收逐条实测, 任一 FAIL 退出码 1 |
| `data/pit_yjyg.parquet` | 真实数据表: 21,488 行, 4,180 股, 期 20240331..20260630, 公告日 20240125..20260711 |
| `data/tmp/*.parquet` | 测试用临时店(a3/a5/a6/a7), 每次运行重建 |

Schema(qlib.md §1.2 字段表 + openbb.md §3.1 增量拼合): `symbol, field, period(int32 yyyymmdd), value(float64), announce_date(int32, 可见性唯一依据), available_at(datetime64[ns, Asia/Shanghai], 禁降精度), available_at_precision({'date','minute','second'}), is_preliminary(bool), revision_seq(int16, 0=首发), source, ingested_at(UTC)`。不采用 qlib `_next` 链表指针, `(period, announce_date, revision_seq)` 排序即等价修订链(规格原文决策)。

## 1. 数据接入实测

复跑: `/Users/fisher/.openclaw/venv/bin/python ingest_yjyg_pit.py --start 2024 --end 20260630`

```
契约探针: [{'period': '20260630', 'n_cols': 28, 'n_rows': 50, 'notice_date_all_midnight': True}]
append 结果: {'appended': 21488, 'skipped_duplicates': 0, 'total': 21488}
含自然修订链(revision_seq>0)的组数: 0
表: .../data/pit_yjyg.parquet  行数 21488  期 20240331..20260630  公告日 20240125..20260711  唯一股 4180
```

- akshare 拉取真实成功(10 期), 主表未启用兜底。兜底路径独立实测通过: `--fallback-cache --out data/tmp/pit_yjyg_cachebuild.parquet` → `appended: 20874`(离线, 读生产缓存只读, 行数少于真实拉取因生产缓存为 keep='first' 且止于 20260627)。
- **28 列位置契约断言落实**(akshare_audit.md §2.11): 直连原始端点验得 27 键 + index = 28 列; 列数≠28 即 `PITViolation` 拒纳。关键列非空率阈值(公告日期/预测指标 ≥99%)同步断言。
- 与生产 `ingest_pead_preannounce.py` 的差异: 生产 `keep='first'` 压扁修订链; 本 spike 保留全部公告建 revision 链, 生产"首次预告"口径由 `pit_asof(mode='as_first_reported')` 等价复现且带 `is_preliminary` 标记。
- 预测指标筛选改为**精确匹配**"归属于上市公司股东的净利润"(生产的 `contains("净利润")` 会同时吸入"扣除非经常性损益后的净利润", 见 §4 发现3)。

## 2. 七条验收逐条记录

复跑: `/Users/fisher/.openclaw/venv/bin/python test_acceptance.py` (确定性, 随机 as_of 种子=20260711; 退出码 0)

| # | 验收条件 | 判定 | 实测证据(输出摘录) |
|---|---|---|---|
| 1 | 前视零记录(任意 as_of 重放, `announce_date > as_of` ==0, SQL 断言) | **PASS** | `31 个 as_of × 4 查询形态 = 124 次重放, 共 436913 行返回, announce_date>as_of 记录数 = 0 (pandas+SQL 双断言)` — SQL 断言用 sqlite in-memory `SELECT COUNT(*) WHERE announce_date > ?` 独立复核 |
| 2 | 毒样本(注入 announce_date=as_of+1, 必不返回) | **PASS** | `注入 announce_date=as_of+1 毒样本 2 条(当期+未来期), 4 查询形态下命中 0 次` — 且毒样本不得改变最新期选择(period=20260630 不出现)同测通过 |
| 3 | 修订一致性(seq 与 announce_date 排序严格一致; 重复 (period,announce_date) hard fail) | **PASS** | 真实表 21488 行 `validate_store` 通过; seq 与公告日反序 → `如期 raise PITViolation`; 重复键 → `如期 raise PITViolation`; 加测 append 层同键异值 → raise(历史行只读); 乱序回填(新行公告日≤组内存量最大) → raise |
| 4 | 未来期禁引(lag=-1 等价取未来期直接 raise) | **PASS** | `lag=-1/-5 如期 raise FuturePeriodReference`(qlib pit.py#L32-36 语义); `period+lag同用 raise ValueError`; 正向对照 lag=1 的期严格早于 lag=0 (6743/3088 行) |
| 5 | 双口径(as_first_reported 取预告带 is_preliminary; as_latest 取正式; 仅晚于参考日申报→置缺失) | **PASS** | 合成链 预告(seq0,100e6,prelim)→修正(seq1,95e6)→正式(seq2,98e6,非prelim) 三行并存不覆写; `as_latest@20260601→98e6/False`; `as_first_reported@20260601→100e6/True`; `as_first_reported@20260110(首发在0125)→0 行(缺失, 无更晚兜底)` — 收紧于 OpenBB fallback(openbb.md §3.1 机制二)落实; 真实表 as_first_reported 全带 prelim=True 且 seq=0 |
| 6 | 拖带守卫(期末后 500 天重述值被 450 天守卫拒) | **PASS** | `期末后500天(20260515)重述 如期 raise PITViolation(拖带守卫拒收)`; 边界: 恰 450 天放行(append=1), 451 天拒; 真实表滞后分布 min=-481 max=120 天全≤450 |
| 7 | 增量友好(append 带新 revision_seq, 重跑幂等第二遍新增=0) | **PASS** | 机械测: 全表 21488 行重放 `appended=0 skipped=21488 文件hash不变=True`; 新修订 append 得 seq=1(=存量max+1), 二遍重放 appended=0, append 后全表不变式通过。**端到端加测**: 真实 akshare 全量重拉第二遍 → `appended: 0, skipped_duplicates: 21488` |

无日历绑定/多日观察类验收条目 → 无 PENDING。

## 3. 关键发现

1. **东财 yjyg 源 surface 是 latest-only**: 原始载荷带 `IS_LATEST` 字段且抽样两期首页全为 `'T'`; 21,488 行 10 期内自然修订链(revision_seq>0 组)= 0。含义: (a) 修订/毒样本测试必须合成注入(规格预判正确); (b) **PIT 店的核心价值恰在于此**——上游会用新公告顶掉旧 vintage, 只有我方 append-only 店能保住 as_first_reported 原始 vintage, 修订链靠持续增量摄取随时间累积, 无法事后回补(规格"对新数据即刻生效, 不要求回填历史修订链"与此吻合)。
2. **负滞后(公告早于期末)真实存在且合法**: 661/21488 行 announce_date < period(预告本性); 极端样本 `920099 期20251231 公告20240906`(-481 天, 疑北交所 IPO 招股书盈利预测混入)与 `605090 期20241231 公告20240125`(-341 天)。PIT 上不构成前视(信息当日确实可得), 但对 growth 因子是异质口径。
3. **生产脚本两处口径注记**(只读观察, 未改动): ① `ingest_pead_preannounce.py` 的 `str.contains("净利润")` 会同时吸入"扣除非经常性损益后的净利润"行, `drop_duplicates keep='first'` 后保留哪条取决于源内行序(抽样中归母在前, 但无契约保证)——归母/扣非口径可能混杂; ② `keep='first'` 丢弃修订链(本 spike 的直接动机)。
4. **NOTICE_DATE 源头精度=日**(时间恒 00:00:00, 探针实测 `notice_date_all_midnight: True`): `available_at` 以 tz-aware 当日 00:00 构造并显式标 `available_at_precision='date'`——字段类型守住"禁降精度"(naive/date 型进店即拒), 源头精度诚实记录而非伪造分钟。

## 4. 规格修订建议 (不影响判定, 全部为增量收紧)

1. **增加公告滞后下界 sanity 守卫**: 现规格只有上界(450 天拖带); 建议对预告类加 `announce_date >= period - 400天` 类下界(可配置), 拦截发现2 的 -481 天类上游污染; 越界行为=显式告警+隔离列, 不静默丢。
2. **available_at 精度列入正式规格**: `available_at_precision` 列(本 spike 新增)建议并入 Wave1 统一 PIT 规格字段表——"禁降精度"验收原文只约束类型, 精度诚实性需要显式列承载(分钟级源接入时该列自动升档)。
3. **乱序回填的处置路径**: 规格未写"迟到的更早公告"(如换源补历史)如何处理; 本 spike 实现为 hard fail(历史行只读, seq 不可插入)。建议规格明文: 此场景唯一合法操作=整组重建入新表+双表对账, 禁止原地重排 seq。
4. **同日多公告**: `(symbol,field,period,announce_date)` 为唯一键, 同日两次异值公告(罕见但可能)会 hard fail——规格应写明处置(建议: 源头带盘中时刻时以 available_at 细分; 日精度源则人工裁决入库)。

## 5. 隔离铁律核查

`git -C /Users/fisher/.openclaw/workspace status --short -- factor_factory/research/2026-07-github-stock-systems/spikes/` 输出:

```
?? factor_factory/research/2026-07-github-stock-systems/spikes/
```

- 本 spike 全部产出为上述目录内**新增未跟踪文件**; 工作区其余 M/D 条目(95 M/71 D)与会话起始 git status 快照完全一致, 均为先前会话遗留, 本 spike 未触碰。
- 生产只读核验: `ingest_pead_preannounce.py` mtime=Jun 29 07:33, `pead_preannounce.parquet` mtime=Jun 29 07:35(仅兜底路径读取, 实际未启用兜底); 未触 panel_full/冻结 json/recommendations.db/backtest_cache/stock-report 克隆。
- tushare 未使用(本 spike 数据源=akshare+东财直连契约探针)。

## 6. 复跑命令汇总

```bash
V=/Users/fisher/.openclaw/venv/bin/python
D=/Users/fisher/.openclaw/workspace/factor_factory/research/2026-07-github-stock-systems/spikes/spike2_pit
cd $D
$V ingest_yjyg_pit.py --start 2024 --end 20260630   # 真实拉取+28列契约探针+append(重跑=幂等)
$V test_acceptance.py                               # 七条验收, 退出码0=全PASS
# 兜底路径(离线): $V ingest_yjyg_pit.py --fallback-cache --out data/pit_yjyg_cachebuild.parquet
```
