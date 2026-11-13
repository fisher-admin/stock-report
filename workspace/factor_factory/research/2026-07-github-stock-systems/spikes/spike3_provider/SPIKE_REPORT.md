# Spike-3 · provider contract 验收报告

- 日期: 2026-07-11 · 执行=spike 建造者(隔离 additive, 目录 `spikes/spike3_provider/`)
- 规格: PHASE2_TIERA_REPORT.md §Spike-3 (6 条验收)
- 输入: openbb.md §2.0/§2.1/§5 · akshare_audit.md §5/§3 · upstream_diff.md §3.3 · nautilus.md §1.2 R1
- 环境: python 3.14.3 (`/Users/fisher/.openclaw/venv/bin/python`), akshare 1.18.24, tushare 1.4.24(代理), pandas 3.0.0, pydantic 2.13.4; pytest 缺 → 裸 assert 脚本
- **结论: 通过 (6/6 验收 PASS; 1 个子项 PENDING=EM 通道对账落盘, 附可复跑命令与观察计划; 4 条规格修订建议)**

## 0. 交付物清单

| 文件 | 内容 |
|---|---|
| `contract/core.py` | StrictQuery/StrictRow(extra='forbid')/FailureRecord/FetchMeta(必填)/TET 三段基类/自检 harness(T 恒等取回+E 阶段隔离+T2 勾稽) |
| `contract/models.py` | DailyBarV1(含涨跌停例外表4类+BJ 30cm)/SecurityMetaV1/EarningsPreannounceV1 + MarketContext 跨表不变式 |
| `contract/fetchers.py` | 5 个 TET fetcher: daily×3(tushare 代理/akshare_em/akshare_sina)+stock_basic+code_name+yjyg(直连 datacenter, 镜像 akshare 1.18.24 请求) + implied-VWAP 单位自检(fail-closed) |
| `contract/lineage.py` | ProviderAttempt(含 freshness_lag_days)+LineageSink(写失败→WARN 不吞) |
| `contract/reconcile.py` | 日线跨源 1e-4 硬闸(ReconcileBreach)+名称/行业不一致清单 |
| `contract/pit.py` | pit_filter 单入口 + as-of 断言 |
| `contract/calendar_provider.py` | 日历(akshare 主/tushare 代理兜底)+尾部<90天护栏 |
| `contracts.yaml` | 6 接口冻结契约(akshare_audit §5 形状, 按本机运行时版本标定) |
| `test_acceptance.py` | 离线验收 34 断言(毒样本/阶段隔离/勾稽/例外表/PIT/血统WARN) |
| `run_spike.py` | 真实拉取驱动(20股×20交易日)+manifest 落盘 |
| `run_em_recon.py` | EM 通道(stock_zh_a_hist)对账补证脚本(独立 manifest) |
| `data/*.parquet` · `manifest/run_manifest_live.json` · `manifest/run_manifest_em_recon.json` | 数据与血统证据 |

复跑命令(全部机械可复跑):

```bash
V=/Users/fisher/.openclaw/venv/bin/python
D=/Users/fisher/.openclaw/workspace/factor_factory/research/2026-07-github-stock-systems/spikes/spike3_provider
cd $D && $V test_acceptance.py     # 离线 34 断言, exit 0=绿
cd $D && $V run_spike.py           # 真实拉取+harness+对账+PIT+血统 (约 60-90s)
cd $D && $V run_em_recon.py 4      # EM 通道补证(源间歇性, 见验收3)
```

## 1. 逐条验收记录

### 验收1 · 三接口自检 harness 全绿 — **PASS**

- **实测(live)**: `run_spike.py` → `HARNESS GREEN: 5/5`(daily_tushare 20/20键 400行 / daily_akshare_sina 20/20键 400行 / stock_basic 5530行 / code_name 5530行 / yjyg 563行), 全部 T/E/T2 三段绿。摘录:
  `harness_daily_tushare: {"all_green": true, "expected_keys": 20, "ok": 20, "failed": 0, "empty": 0, "rows": 400}`
- **T 段入参恒等取回**: harness 对每个 param `getattr(q,k)==v` 断言(core.py `run_harness`); 离线正反测 PASS。
- **E 段 raw 无 Row 实例**: 离线注入"pull_raw 偷带标准化 Row"的 LeakyFetcher → harness 必红:
  `[PASS] E: stage-isolation assertion catches Row instance inside raw`
- **T2 勾稽 len(keys)==成功+失败+显式空载荷**: live 实测多次成立(含 20260711 第二次运行 EM 间歇复活时 4 ok+16 failed+0 empty==20); 离线注入"悄悄丢 key"的 UnaccountedFetcher → 必红。
- 复跑: `$V test_acceptance.py`(A1 段 10 断言) + `$V run_spike.py`。

### 验收2 · 毒样本三件全拒 — **PASS**

| 毒样本 | 结果 | 摘录 |
|---|---|---|
| EM kline 假响应 13 列(f116 现身)与 11 列(掉列) | 双双拒, error_type=schema, 12 列对照组通过 | `[PASS] EM kline poison 13-col (f116 appears) rejected` |
| 未来日期/非开市日行(周日 20260712 + 日历外 20260803) | 拒, error_type=calendar | `[PASS] future/non-open date row rejected (calendar)` |
| yjyg 假响应 键数漂移(26 键缺列 / 28 键加列) | ContractViolation(expected_nkeys), 27 键对照组通过 | `[PASS] yjyg poison 26-key (missing col) rejected` |

**规格数字勘误(重要)**: 规格写"27/29 列 vs 28"系对照审计冻结 SHA 的 akshare **位置 28 列**表——其中第 1 列是 akshare 自己 `reset_index` 产的序号列, **服务端真实载荷是 27 个命名键**(本机 akshare 1.18.24 实测+直连 datacenter 抓包确认)。本 spike 冻结 27 键**集合等值断言**(命名契约>位置契约), 毒样本对 26/28 键——语义与规格等价(±1 列必拒), 数字按环境校正。见 §3 修订建议 2。

### 验收3 · 跨源对账 — **PASS**(EM 通道落盘子项 PENDING)

- **真实重叠段 ≤1e-4**: tushare 代理 vs akshare_sina(`stock_zh_a_daily`), 20 股×20 交易日, **400 根重叠 bar / 1600 个价格值 / max_rel_diff=0.0** → 通过:
  `"n_overlap_bars": 400, "n_compared_values": 1600, "max_rel_diff": 0.0, "tolerance": 0.0001, "passed": true`
  (成交量差信息性记录: median=0, max=0——两源股数级完全一致)
- **人工造 0.05% 差样本必拒+硬告警**: 离线注入 close×1.0005 → `ReconcileBreach: HARD ALERT: 1 price value(s) exceed |rel diff|<=0.0001` → PASS。
- **行业/名称双源不一致清单落 manifest(允许存在,必须显式)**: PASS——tushare stock_basic vs akshare stock_info_a_code_name, 5530 vs 5530 全对齐(only_a/only_b=0), **79 条名称不一致显式落 manifest**。实测分解: 21 条为全角/半角字母(万科A vs 万科Ａ, NFKC 可归一), 其余为交易所状态前缀(XD 除息/N 新股/-UW 类后缀: `XD招商银` vs `招商银行`)。行业为 tushare 单源(akshare 无行业列), 覆盖 5526/5530, 单源事实显式记录于 manifest `industry_note`。
- **EM 通道(规格点名的 stock_zh_a_hist)——子项 PENDING**: push2his.eastmoney.com 从本机**间歇性连接层拒绝**(直连/本地代理/NID 补丁/py3.9+py3.14 全试, 见 §4 发现 1)。三次 live 运行中一次成功(4/20 键, 80 行, 12 列冻结契约 live 通过, harness 6/6 绿), 该次证据被终版 manifest 覆盖; 补证脚本 `run_em_recon.py` 4 次重试均 0/20(失败链完整落 `manifest/run_manifest_em_recon.json`)。**观察计划**: 非交易时段每日跑一次 `$V run_em_recon.py 4`, 任一次成功即落 `data/daily_akshare_em.parquet`+`em_reconciliation`(脚本已备好, 成功即自动闭合); 若 5 个日历日仍 0 键, 判"本机对 push2 族不可用"并把 sina 通道正名为 akshare 日线对账主源(FetchMeta 源枚举本就含 akshare_sina, openbb.md §2.0)。

### 验收4 · PIT as-of 断言 — **PASS**

- 三表 6 组 as-of 重放, `available_at > as_of` 可见记录数全为 0:

| 重放 | total | visible | hidden | violations |
|---|---|---|---|---|
| daily@窗口中点(20260625 16:00 CN) | 800 | 440 | 360 | 0 |
| daily@now | 800 | 800 | 0 | 0 |
| names@now / names@20260101 | 11060 | 11060 / 0 | 0 / 11060 | 0 |
| yjyg@20260501 / yjyg@now | 563 | 553 / 563 | 10 / 0 | 0 |

- 毒样本: 注入 available_at=as_of+1 日的行 → pit_filter 必不返回(离线 PASS); naive(无时区) as_of → 直接 raise。
- available_at 口径: 日线=trade_date 15:00 CN; 名称=fetched_at(快照语义); 公告=NOTICE_DATE(源精度为日, 未降精度——源本身只给 00:00:00)。

### 验收5 · 涨跌停例外表正反 — **PASS**

- **正**: 科创板 688001.SH 上市第 3 交易日 +35% → 通过且 `limit_exempt="star_gem_first5"`(上市日+板块+日历纯推导)。
- **反**: 主板 600000.SH +25% → 必 failure, error_type=limit_band。
- 加测: 主板上市首日 +44% → `ipo_day1` 豁免 ✓; 北交所 +25% → 30cm 白名单内不豁免不告警 ✓; 科创板第 6 日 +35% → 豁免窗外必拒 ✓; **豁免不豁 OHLC 一致性**(high<close 的"豁免行"仍被 pydantic 拒) ✓。
- live 侧: 20 股窗口内无新股, `limit_exempt_rows=0`, 400/400 行过不变式(与预期一致)。

### 验收6 · 血统尝试链落 manifest — **PASS**

- `manifest/run_manifest_live.json` attempts 附表(终版运行摘录):

```
calendar      akshare_sina         success=True  158-484ms  8797 rec
name          tushare_proxy        success=True  ~400ms     5530 rec
name          akshare_exchange     success=True  ~6s        5530 rec
daily         tushare_proxy        success=True  ~200ms     400 rec  fresh_lag=0
daily         akshare_em           success=False ~8s  err=exception  fallback_to=akshare_sina   ← 真实失败尝试在链
daily         akshare_sina         success=True  ~23s       400 rec  fresh_lag=0
announcement  eastmoney_datacenter success=True  ~600ms     563 rec
```

含失败尝试(akshare_em, 带 error_type/fallback_to/latency), freshness_lag_days=0(数据最大日=期望有效交易日 20260710)。`run_manifest_em_recon.json` 另有 4 条失败尝试+failure_sample。
- **血统写入失败本身产生 WARN**: 离线把 sink 指向不可写路径 → `record()` 返回 False + logger WARN `LINEAGE_WRITE_FAILED` + 记入 sink.warnings, 进程不中断(观测断了要响, 但不阻塞取数主链) → PASS。
- 熔断器未实现(规格输入仅要求 ProviderAttempt 字段表挂同一 FetchMeta; circuit_open 枚举已保留, 熔断实现属 Wave1 W1-7)。

## 2. 验收计数

**PASS 6/6 · FAIL 0 · PENDING 1(子项)**: 验收 3 的 EM 通道对账数据落盘(源间歇不可达, 非契约层缺陷; 契约层对该源的 12 列断言在一次间歇成功中已 live 通过, 失败链全程显式)。离线断言 34/34 PASS。

## 3. 规格修订建议

1. **DailyBarV1.pre_close"必填且来自源"需配源能力表**: akshare sina 日线(`stock_zh_a_daily`)无 pre_close/涨跌额列——本实现放宽为 `float|None`, None 仅当源不供给, 落 FetchMeta.notes+涨跌幅不变式仅在 pre_close 在场时评估。EM 通道 pre_close 取同行 `收盘-涨跌额`(源载 delta, 非跨行自算), 建议规格明示该推导合法性边界。
2. **yjyg 列数断言改"27 命名键集合等值"**: 位置 28 列契约把 akshare 自产 index 列计入合同, 且 `columns=ALL` 下位置契约天然脆; 命名键集合断言同时覆盖"加列/减列/换列"三种漂移且数字不随 akshare 小版本变。毒样本数字相应为 26/28(等价于规格 27/29 的 ±1 语义)。
3. **"源健康度"与"契约合规"分离度量**: harness 绿(勾稽成立)≠源活(可 0 ok+20 failed)。规格的"自检全绿"建议明确为契约合规口径, 另设源健康阈值(如 ok 率<50% 触发降级告警)——本 spike driver 以"0 键成功即 RuntimeError+血统失败记录"实现了最小版。
4. **名称对账需归一化规约**: 79/5530 原始不一致中 21 条 NFKC 可归一(全半角), 其余为交易所状态前缀(XD/N/DR/-UW)。建议 Wave1 名称对账 = NFKC 归一+状态前缀剥离后比对, 原始差异清单仍全量落 manifest。

## 4. 关键发现(超出验收但影响 Wave1/生产)

1. **push2his/push2(东财行情族)从本机间歇性连接层拒绝**: 直连与本地代理(127.0.0.1:7897)均 RemoteDisconnected/ProxyError; NID 补丁(daily_stock_analysis/patch/eastmoney_patch.py)拿到 nid 后仍拒——非 NID 层, 疑 IP/TLS 指纹级间歇封锁。**datacenter 族(yjyg/gdhs)与新浪族完全正常**——与审计"三族故障模式不同"判决一致且更狠: 同一供应商两族一个死一个活。生产 `enrich_review_quotes.py`/`fetch_market_snapshot_fallback.py` 的 EM 兜底路径可能正在静默退化(已另旗标移交)。
2. **akshare 版本漂移实证**: 本 venv 1.18.24 的 `stock_yjyg_em` 返回 11 列(28 位置名→11 列子集), 与审计冻结 SHA(≈1.18.64)的 28 列表述不同——版本 pin+契约按运行时标定的必要性被本 spike 直接踩实。
3. **tushare 代理与新浪日线股数级完全一致**(1600 值 max_rel_diff=0.0, 成交量/成交额亦全等), 双源对账 1e-4 容差在 A 股日线上有充足余量。
4. 新浪日历(`tool_trade_date_hist_sina`)返回 8797 天至 20261231, 尾部 173 天>90 天护栏 — 但已进入"日历尽头一年内", 2026Q4 起护栏将开始报警, 与审计预言一致。

## 5. 隔离核查(红线)

`git -C /Users/fisher/.openclaw/workspace status --short` 关键输出:

```
?? factor_factory/research/2026-07-github-stock-systems/spikes/    ← 本 spike 全部产出(untracked, additive)
```

被修改的 166 个 tracked 文件全部与开工前快照一致(AGENTS.md/skills/*/stock_data/* 等系他人/前序会话遗留, 本 spike 未触碰); 未动 skills/ 生产脚本、panel_full、冻结 json、recommendations.db、backtest_cache、stock-report 克隆。生产数据零写入——本 spike 全部写入均在 `spikes/spike3_provider/{data,manifest}/` 内。tushare 一律走代理(`.secrets/tushare_api_url`), 拉取量 20 股×20 日+全表快照×2+yjyg 单期, 符合限流控制。
