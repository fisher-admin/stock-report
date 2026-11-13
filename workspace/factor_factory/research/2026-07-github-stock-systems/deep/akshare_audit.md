# akshare 在用接口审计（G8 轨道 · Tier-A 深读报告）

- 日期: 2026-07-11
- 冻结 SHA: `fcdbf25aa864a218c54864c3f6ab6a2ed19cce28`（2026-05-27 最后一次 push, ≈v1.18.64 后 main）; 引证格式 `path#Lx-Ly@fcdbf25aa8`
- 我方代码扫描范围: `/Users/fisher/.openclaw/workspace/skills/` + `/Users/fisher/.openclaw/workspace/factor_factory/`（排除 venv/site-packages/outputs 历史备份）
- 方法: 本地 grep 定调用点 → 逐调用点读上下文 → `curl raw.githubusercontent.com@冻结SHA` 拉每个在用接口实现 → 与运行时安装版逐文件 diff → 输出契约表/脆弱点/对齐核查点
- 上游许可: akshare MIT, 无红线3限制; 本报告可含代码事实描述

---

## 0. 关键判决先行

**判决一（G8 盲区实测收窄）: 竞价/分钟/停复牌根本不走 akshare。**
- 集合竞价: `skills/stock-system-orchestrator/scripts/ingest_auction_data.py` L40-41 走 **Tushare** `stk_auction_o`/`stk_auction_c`（代理 http_url）。
- 分钟: `ingest_minute_data.py` L70-141 走 **Tushare** `stk_mins`（分页 ~8000 行/次, 1/2/4s 退避重试）。
- 停复牌: `stock_tfp_em` 全 workspace **零调用**（停牌处理走 spot close>0 过滤 + tushare 面板缺口逻辑）。
- 含义: PHASE1_GAPS G8 点名的"竞价/分钟盲区"在 akshare 侧不存在, 该风险归 **Tushare 代理契约**（Phase4 provider contract 需另立 tushare 契约条目, 本报告范围外, 见 Wave 映射末条）。

**判决二（版本对齐意外良好, 但属运气）**: 生产运行时 akshare=**1.18.21**（`/Users/fisher/Library/Python/3.9/lib/python/site-packages/akshare/`）, qlib_lab venv=1.18.60, 冻结 SHA≈1.18.64 后。对全部 10 个在用接口的实现文件逐一 diff: **byte-identical（diff=0）**——1.18.21→冻结 SHA 之间的 40+ 个 release 恰好没动我方在用文件。结论: 当前无契约漂移, 但这不是保证而是巧合; run_manifest 版本 pin + 升级前探针（卡建议）仍为必做。

**判决三（最高共性风险）**: 10 个在用接口中 **9 个无 timeout**（唯一例外: `stock_zh_a_spot_em` 经 `fetch_paginated_data`→`request_with_retry` timeout=15+3次退避）。我方调用侧防护不均: ingest 系有 SIGALRM 45s、cyq fallback 有 retry+max_runtime, 但**日历三调用点（pipeline/stage1/alert）完全裸奔**——夜间 cron 永久挂起风险集中在最基础的日历上。

---

## 1. 在用接口清单 + 判决表（逐文件）

| # | akshare 接口 | 我方调用点 | 用途 | 我方现有防护 | 判决 |
|---|---|---|---|---|---|
| 1 | `tool_trade_date_hist_sina` | `skills/stock-analyzer/pipeline.py:252`; `skills/stock-system-orchestrator/scripts/stage1_data_prepare.py:80`; `stock_pipeline_alert.py:65,169` | 交易日历（主用, 已去 Tushare SPOF） | 进程内缓存(仅 pipeline.py); 异常返回 None fail-soft | **需加固**: 无 timeout/重试/落盘快照; 4 处重复实现 |
| 2 | `stock_zh_a_hist` | `skills/stock-analyzer/stock_analyzer.py:422`(兜底分支); `skills/stock-system-orchestrator/scripts/enrich_review_quotes.py:65` | 个股日线（复盘回填/兜底） | enrich: 3 workers+sqlite 缓存+异常计数; 两处均 adjust=""不复权✓ | **合格**（不复权符合红线; timeout=None 但有外层容错） |
| 3 | `stock_zh_a_spot_em` | `skills/stock-analyzer/fetch_market_snapshot_fallback.py:232` | 全市场快照兜底(第3优先) | 真实交易日 fail-closed 校验+close>0 过滤+量纲自检 | **合格**（防护最全的调用点） |
| 4 | `stock_zh_a_spot` (新浪) | `fetch_market_snapshot_fallback.py:225` | 全市场快照兜底(第1优先) | 同上 | **合格但注意封 IP**（源 docstring 自认重复运行封 IP） |
| 5 | `stock_info_a_code_name` | `stock_analyzer.py:621`(akshare 分支) | 股票列表/名称映射 | 无 | **兜底可容忍+警告**: 仅在市股(幸存者名单)+`@lru_cache`进程内永不刷新, 禁用作历史 universe |
| 6 | `stock_cyq_em` | `skills/stock-analyzer/fetch_akshare_cyq_fallback.py:127` | 筹码分布兜底(主源 tushare cyq_perf) | retry+max_runtime+按 trade_date 精确匹配+`fallback_schema=approx_90_band` 诚实标注 | **合格**（近似语义已标注） |
| 7 | `stock_zh_index_spot_sina` | `skills/stock-system-orchestrator/scripts/generate_midday_analysis.py:199` | 国内指数盘中快照 | source_kind='intraday_spot' 诚实标注 | **合格+1 缺陷**: 见 §2.7, `最新行情时间`列不存在→as_of 恒 None |
| 8 | `index_global_spot_em` | `generate_midday_analysis.py:226` | 全球指数快照 | -999 哨兵过滤 | **需加固**: /100 硬编码缩放, 无量级断言（见 §2.9） |
| 9 | `stock_zh_index_daily` | `skills/stock-system-orchestrator/scripts/generate_o2c_review.py:199` | 沪深300 真基准(开/收) | 异常返回空 dict fail-soft | **需加固**: 基准静默缺失=复盘超额全 None, 建议与 tushare index_daily 双源断言 |
| 10 | `stock_zh_a_gdhs` | `skills/stock-system-orchestrator/scripts/ingest_holder_count.py:51` | 股东户数(研究因子) | SIGALRM 45s+逐期容错+`公告日期`为 PIT 锚✓ | **合格**（PIT 锚正确） |
| 11 | `stock_yjyg_em` | `skills/stock-system-orchestrator/scripts/ingest_pead_preannounce.py:54` | 业绩预告(PEAD 研究) | SIGALRM 45s+首次预告 keep='first'✓+growth 缺失派生 | **合格**（PIT 锚正确） |

非 akshare 澄清: `factor_factory/scripts/greenfield_multifactor_lib.py:257` 的 `ak.replace(` 系 `rolling_peak.replace` 换行误匹配, 非 akshare。`outputs/evolution/backup_*` 为历史快照死代码, 不计。

---

## 2. 每接口冻结契约（单位与字段契约表 + 脆弱点）

### 2.1 `tool_trade_date_hist_sina` — 交易日历（新浪）
- 实现: `akshare/tool/trade_date_hist.py#L19-L39@fcdbf25aa8`
- 源 URL: `https://finance.sina.com.cn/realstock/company/klc_td_sh.txt`（新浪混淆 JS 数据）
- 机制: `requests.get(url)` **无 timeout** → `py_mini_racer` 执行 `hk_js_decode`（`akshare/stock/cons.py#L239` 起的解码 JS）→ `r.text.split("=")[1].split(";")[0]` 结构依赖 → 手工补 1992-05-04（L36）
- 返回契约: 单列 `trade_date`(datetime.date), 升序, 覆盖 1990-12-19→未来（实测含未来已排期交易日; 未来边界未实测断言, 见 confession）
- 日期边界: 无参数, 全量返回; 我方 4 处调用全部转 `yyyymmdd` 字符串排序后二分
- 异常行为: 任何网络/解码失败直接抛; 我方 4 处均 try/except 返 None（fail-soft, 日历失败→当日按缓存跑——0710 事故已在 stage1 修为 akshare 兜底解析真实交易日）
- 脆弱点: ①新浪 JS 混淆格式变更即全断（mini_racer 解码链）; ②`split("=")[1]` 对响应结构强依赖; ③无 timeout=夜间挂起点; ④与静态 `akshare/file_fold/calendar.json`(至 20261231, 8797 天)双轨, 我方走 live 版✓
- **对齐核查点**: 4 处调用重复实现同一逻辑且各自拉一次新浪（封 IP 面聚集）; 无落盘冻结快照——日历是全系统单点, 建议统一 provider+每日落盘+`日历尾部<90天`硬告警（卡已提, 落 Wave1）

### 2.2 `stock_zh_a_hist` — 个股日线（东财）
- 实现: `akshare/stock_feature/stock_hist_em.py#L952-L1039@fcdbf25aa8`
- 源 URL: `https://push2his.eastmoney.com/api/qt/stock/kline/get`; 参数 `klt=101`(daily)/`fqt={"":"0","qfq":"1","hfq":"2"}`/`secid={market}.{symbol}`, `market=1 if symbol.startswith("6") else 0`（北交所 4/8 开头落 market=0, 全库同模式, 未实测正确性）
- 翻页: 无（beg/end 一次拉全, **双闭区间**）
- 请求: `requests.get(..., timeout=timeout)` 默认 **timeout=None 裸奔无重试**（L958/L992, 卡已证）
- 空数据: `data.klines` 空→返回**空 DataFrame**（L994-995, 不抛错）——调用方必须 len 检查（我方两处都有✓）
- 字段契约（位置重命名, L998-1011）:

| 列 | dtype | 单位 |
|---|---|---|
| 日期 | date | 自然日 |
| 股票代码 | str | 6位(入参回填) |
| 开盘/收盘/最高/最低 | float | 元（fqt=0 为不复权真实价） |
| 成交量 | float | **手**（1手=100股） |
| 成交额 | float | **元** |
| 振幅/涨跌幅/换手率 | float | **百分点** |
| 涨跌额 | float | 元 |

- 隐藏脆弱点: `fields2` 请求了 12 个字段含 `f116`, 但服务端 klines 实际只回 11 个值; 位置重命名 12 列（11 数据列+回填股票代码）恰好对齐——**若东财某天开始返回 f116, 列数变 13 vs 12 名即 ValueError 崩溃**（fail-loud, 尚可接受）; 但若东财调整 klines 内部顺序则**静默错位**（#7328 同类风险）
- 复权: qfq/hfq 为**服务端请求时点口径**, 不吐复权因子（PIT 敌对, 卡已证）; **我方两个调用点均 adjust=""不复权✓**（enrich_review_quotes 显式, stock_analyzer 走默认""), 符合红线"缓存禁 qfq"
- **对齐核查点**: enrich_review_quotes 把 开盘/收盘 写入 `quote_cache`(sqlite) 供复盘——与主链路 tushare daily(不复权)同口径✓; 若窗口内有除权, 两源不复权价一致, 无漂移

### 2.3 `stock_zh_a_spot_em` — 全市场快照（东财）
- 实现: `stock_feature/stock_hist_em.py#L15-L121@fcdbf25aa8` + `akshare/utils/func.py#L18-L60@fcdbf25aa8`
- 源 URL: `https://82.push2.eastmoney.com/api/qt/clist/get`（**硬编码 82 分片子域**）; `fs=m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048`（沪深主板+创业板+科创板+北交所）
- 翻页: `fetch_paginated_data`: pz=100/页, 首页取 total 算页数, 页间 `sleep(uniform(0.5,1.5))`（全市场 ~54 页→**约1-2分钟延迟**）, 经 `request_with_retry`(timeout=15, 指数退避+抖动, 每页新建 Session 禁连接池)——**在用接口中唯一有完整 HTTP 护栏的**
- 返回顺序: 按 f3(涨跌幅)降序重排（L57）, **非代码稳定序**——我方按代码 join, 不受影响✓
- 字段契约: 31 列位置重命名（L37-70）; 关键单位: 最新价/今开/昨收/最高/最低=元, 成交量=**手**, 成交额=**元**, 总市值/流通市值=**元**, 涨跌幅/换手率=百分点; 全列 `pd.to_numeric(errors="coerce")`——格式漂移静默 NaN
- 停牌/未成交: 最新价为 NaN 或 0 → 我方 `close>0` 过滤✓（20260702 零价稀释布林带事故的修复）
- 脆弱点: ①`ut=bd1d9ddb...` 硬编码 token; ②`data.diff` 为 null 时 TypeError; ③首页失败=全失败; ④东财 nid 反爬——我方经 `daily_stock_analysis/patch/eastmoney_patch.py` 打 NID 授权补丁（**跨项目依赖**, 见 §3 对齐点5）

### 2.4 `stock_zh_a_spot` — 全市场快照（新浪）
- 实现: `akshare/stock/stock_zh_a_sina.py#L45-L124@fcdbf25aa8`; URL 常量 `akshare/stock/cons.py#L40-L52@fcdbf25aa8`
- 源 URL: `http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData`（注意 **http 非 https**）; 页数来自 `getHQNodeStockCount?node=hs_a`
- 翻页: 80/页, `demjson.decode`（宽松 JSON 解析）; 页数计算有量子 bug: `int(...)/80` 恒为 float→`isinstance(page_count,int)` 恒 False→`int(page_count)+1`（多拉一页空页, 无害但反映质量）
- 请求: 裸 `requests.get` **无 timeout 无重试**; docstring 自认"重复运行会被新浪暂时封 IP"
- 字段契约: `astype(float)` **硬转换**（非 coerce, 格式漂移直接抛错=fail-loud, 反而好）; 单位: **成交量=股**（新浪原生, 与东财"手"差 100 倍!）, 成交额=元; `时间戳`列=ticktime(HH:MM:SS)
- **对齐核查点（已解决的100倍雷）**: 我方 `reconcile_vol_to_hand`（fetch_market_snapshot_fallback.py L46-88）不猜源, 用 `成交额÷价格` 反推隐含手数, 中位比落 {1,100,0.01} 档才校正, 落档外告警不擅改——此护栏经 0702 事故打磨, **审计判定为正确姿势, 建议原样纳入 provider contract 规格**

### 2.5 `stock_info_a_code_name` — 股票列表
- 实现: `akshare/stock/stock_info.py#L439-L468@fcdbf25aa8`
- 拼接四路官方源: 上交所 `query.sse.com.cn/sseQuery/commonQuery.do`(JSON, `COMPANY_STATUS=2,4,5,7,8` **仅在市**, 无 timeout) + 深交所 `szse.cn/api/report/ShowReport`(**xlsx 下载**+pd.read_excel, timeout=15) + 科创板 + 北交所
- 返回契约: `code`(6位str)/`name` 两列
- 脆弱点: ①`@lru_cache()` **进程级缓存永不刷新**——长驻进程会用旧名单; ②xlsx 结构依赖(深交所改报表即断); ③**天然幸存者名单**: 退市股不在内
- **对齐核查点**: 我方主用 `stock_list.csv`(4925)+panel_full 双源, akshare 此接口仅 stock_analyzer 兜底分支当 universe——**禁止**把它引入任何历史研究 universe（S3 冻结人群契约不受影响, 其排除逻辑在我方侧）

### 2.6 `stock_cyq_em` — 筹码分布（东财）
- 实现: `akshare/stock_feature/stock_cyq_em.py#L16-L308@fcdbf25aa8`
- 机制: 拉东财 kline `lmt=210` 根日 K（`end=今天`, 裸 requests 无 timeout）→ **py_mini_racer 本地执行东财网页同款 CYQCalculator JS** 逐日重算筹码分布 → 只返回**最近 90 行**（L306 `iloc[-90:]`）
- 字段契约: 日期(date), 获利比例(**0-1小数**), 平均成本/90成本-低/高/70成本-低/高(元), 90/70集中度(小数)
- 口径警告: JS 中 `this.range` 未定义→`start=0`→**用全部 210 根 K 线累计**, 而东财网页版 range=120——与网页显示的筹码分布**可能不同口径**（未实测差异量级）
- PIT 警告: `end=now.date()` → **盘中调用会把当日未收盘 bar 计入分布**; 我方 fallback 按 `日期==trade_date` 精确匹配且 19:30 后跑, 已规避✓
- **对齐核查点**: 我方 `map_row`(fetch_akshare_cyq_fallback.py L67-86) 用 90 带近似 5/15/85/95 分位并标注 `fallback_schema=approx_90_band`——**验收条件**: 下游消费 cyq_perf 的代码必须能区分 `fallback_source`, 不得把近似分位当真分位做阈值判断（待查点, 列 Wave2）

### 2.7 `stock_zh_index_spot_sina` — 国内指数快照（新浪）
- 实现: `akshare/index/index_stock_zh.py#L58-L126@fcdbf25aa8`; URL `akshare/index/cons.py#L70-L85@fcdbf25aa8` `Market_Center.getHQNodeDataSimple?node=hs_s`(http)
- 翻页: 80/页 demjson, 裸 requests 无 timeout, 同款页数 float bug
- 字段契约: 代码/名称/最新价/涨跌额/涨跌幅/昨收/今开/最高/最低/成交量/成交额——**没有`最新行情时间`列**
- **发现的实际缺陷**: `generate_midday_analysis.py:204` 读 `row.get('最新行情时间')` 在此接口**恒为 None**→midday 国内指数快照 as_of 恒空。诚实降级(不误标)但丢失时效信息; 修法: 改用同文件 `__stock_zh_main_spot_em`(东财, 私有)或接受 None+在 payload 注明。1 行级修复, 列 Wave1 小件
- 我方仅取 最新价/昨收/涨跌幅（已标 `intraday_spot` 诚实口径✓）, 成交量单位争议不影响

### 2.8 `stock_zh_index_daily` — 指数日线（新浪, 真基准源）
- 实现: `akshare/index/index_stock_zh.py#L293-L316@fcdbf25aa8`
- 源 URL: `https://finance.sina.com.cn/realstock/company/{symbol}/hisdata/klc_kl.js`; 与日历同款 mini_racer+`split("=")[1]` 解码链, **无 timeout**; `params={"d":"2020_2_4"}` 为化石参数, 实际返回全历史
- 字段契约: date(date)/open/close/high/low(点位)/volume（我方只用 open/close✓）
- **对齐核查点（复盘基准单点）**: `generate_o2c_review.load_index_prices` 失败返回**空 dict**→下游超额收益全 None——复盘会静默缺基准。且同系统 midday 用 tushare `index_daily` 取同一指数 exact_close, **两源并存无对账**。验收条件: 同交易日 `abs(ak_close - ts_close) < 0.01` 断言+基准缺失硬告警, 列 Wave1

### 2.9 `index_global_spot_em` — 全球指数快照（东财）
- 实现: `akshare/index/index_global_em.py#L15-L92@fcdbf25aa8`
- 源 URL: `https://push2.eastmoney.com/api/qt/clist/get`, `fltt=1`（服务端回**原始整数**）, 55 个指数硬编码列表, `np=2`(dict-of-dicts→`.T` 解析)
- 字段契约: 最新价/涨跌额/开盘价/最高价/最低价/昨收价 = 原始整数 **÷100**（L84-90 硬编码缩放）; 涨跌幅/振幅 亦 ÷100 后为百分点; `最新行情时间`=f124 unix 秒→Asia/Shanghai 字符串
- **脆弱点（本接口最危险）**: /100 是对 `fltt=1` 两位小数定点数的硬解码——上游若改精度(fltt 语义/dect 参数)即**静默 100 倍错**, 且 errors="coerce" 不会报错。我方 midday 用它给全球指数 source_kind='exact'
- 验收条件（Phase3 必加）: 每指数量级 sanity 区间断言（如 上证综指∈[1500,8000], SPX∈[2000,10000], 恒生∈[10000,50000]）, 破区间即拒发+告警——1 小时工作量, 性价比最高的单项加固

### 2.10 `stock_zh_a_gdhs` — 股东户数（东财数据中心）
- 实现: `akshare/stock_feature/stock_gdhs.py#L15-L127@fcdbf25aa8`
- 源 URL: `https://datacenter-web.eastmoney.com/api/data/v1/get`, `reportName=RPT_HOLDERNUM_DET`, `filter=(END_DATE='YYYY-MM-DD')`
- 翻页: 500/页, 页数取自首响应 `result.pages`, **页间无 sleep 无重试无 timeout**（datacenter 接口限流风险; 我方 SIGALRM 45s 兜底）
- 字段契约: 16 列位置重命名; 关键: `公告日期`=HOLD_NOTICE_DATE(date)——**真公告日, PIT 可用**; `股东户数-增减比例`=百分点; `股东户数统计截止日-本次/上次`=报告期末
- 我方 PIT 用法核验: `ann_date=公告日期`✓, `end_date=报告期`分离✓, 同股同期 keep 最早公告✓——**符合红线2, 无需改动**（防重复建设: 不要再提"新建 PIT 处理"）

### 2.11 `stock_yjyg_em` — 业绩预告（东财数据中心）
- 实现: `akshare/stock_feature/stock_yjyg_em.py#L135-L220@fcdbf25aa8`
- 源 URL: `https://datacenter.eastmoney.com/securities/api/data/v1/get`（注意与 gdhs 用的 `datacenter-web` **不同子域**）, `reportName=RPT_PUBLIC_OP_NEWPREDICT`, `filter=(REPORT_DATE='YYYY-MM-DD')`, `columns=ALL`
- 翻页: 同 gdhs 模式, 无 sleep 无 timeout
- 字段契约: **28 列位置重命名**（L171-200, `columns=ALL` 回什么全凭上游——**位置契约最脆的一个**: 上游 schema 加列即全表错位且 coerce 吞成 NaN）; 关键列: 公告日期(date, PIT 锚)/预测指标/预测数值/上年同期值/业绩变动幅度(百分点)/预告类型
- 我方 PIT 用法核验: ann_date=公告日期✓; 净利润筛选✓; growth 缺失时 `(预测数值-上年同期)/|上年同期|` 派生（负基数防翻转✓）; 首次预告 keep='first'✓——PEAD 研究口径合格
- 验收条件: 因 `columns=ALL`+28 列位置契约, 此接口的冻结契约断言必须含**列数==28**+关键列非空率阈值（列数变即上游改 schema, 直接拒纳）

---

## 3. 与我方缓存层的口径对齐核查点（checklist）

| # | 对齐点 | 现状 | 验收条件（Phase3 spike 直接可用） |
|---|---|---|---|
| 1 | **daily 缓存单位契约**: tushare 口径 vol=手/amount=千元 | akshare 东财 成交额=元→`/1000`✓, sina 同✓; vol 走 implied-hand 自检✓（fetch_market_snapshot_fallback L46-88, L188） | 契约文件明文: `vol:手, amount:千元, price:元, pct:百分点`; 自检"落档外"从告警升级为可选 fail-closed |
| 2 | **日历**: 4 处重复实现, 无落盘快照 | 各自拉新浪, 失败 fail-soft 返 None | 统一 calendar provider: 每日成功后落盘冻结快照(含 fetched_at), 失败读昨快照+告警; `日历尾部<90天`硬告警; timeout=30+重试2 |
| 3 | **指数基准双源**: o2c_review 用 akshare sh000300, midday 用 tushare index_daily | 无对账 | 同日 close 双源差异<0.01 断言; 基准缺失=复盘拒绝生成而非超额=None |
| 4 | **cyq fallback 语义**: approx_90_band 近似 5/95 分位 | 已标注 fallback_source/fallback_schema | 核查 cyq_perf 全部消费点是否读 fallback_schema 区分处理（半天排查, Wave2） |
| 5 | **跨项目依赖**: em 系调用依赖 `daily_stock_analysis/patch/eastmoney_patch.py`(NID 反爬补丁) | fetch_market_snapshot_fallback 走 `STOCK_SYSTEM_DAILY_ANALYSIS` env/`external/daily_stock_analysis` 路径, patch 失败静默不打(em provider 会裸奔被限流); cyq fallback 则 import 失败直接崩 | 上游迁移项目切片清单加一条: eastmoney_patch 属"被我方生产依赖的上游文件", backport 时冻结其接口; patch 加载失败改为显式告警 |
| 6 | **spot 快照盖当日缓存的 PIT 卫生** | 已有: 真实交易日 fail-closed 校验+close>0 过滤+交易日收盘前禁裸跑护栏 | 保持; 增加行覆盖率断言(≥95% universe)——对照 myhhub 反例#3(spot 盖历史日期戳), 我方已系统性规避 |
| 7 | **universe 卫生**: stock_info_a_code_name 仅在市股+lru_cache 不刷新 | 仅兜底分支使用 | 契约注记"禁止用作历史 universe/幸存者名单"; 长驻进程禁用(缓存不失效) |
| 8 | **版本 pin**: 运行时 1.18.21 vs venv 1.18.60 vs 冻结 fcdbf25 | 三环境版本各异但在用文件恰好同源(本报告实测 diff=0) | run_manifest 记录 `akshare.__version__`+10 个在用文件的 sha256; 升级前跑接口探针(复用 Phase B.0 基建)对照本报告字段表 |

---

## 4. 脆弱点汇总（横向判决表）

| 接口 | 上游结构依赖 | 限流/封IP | timeout | 库内重试 | 列契约类型 | 静默错误面 |
|---|---|---|---|---|---|---|
| tool_trade_date_hist_sina | **JS混淆+split结构** | 低 | 无 | 无 | 单列 | 低(全断型) |
| stock_zh_a_hist | kline 逗号串位置 | 中(逐股高频) | None默认 | 无 | 位置12列(f116悬雷) | 中(空df/错位) |
| stock_zh_a_spot_em | data.diff JSON+硬编码82分片+ut token | 中(NID反爬) | **15s** | **有(退避+抖动)** | 位置31列 | 中(coerce吞NaN) |
| stock_zh_a_spot | demjson宽松解析 | **高(自认封IP)** | 无 | 无 | astype硬转 | 低(fail-loud) |
| stock_info_a_code_name | SSE JSON+SZSE xlsx | 低 | 部分15s | 无 | 名称重命名 | 中(lru_cache陈旧) |
| stock_cyq_em | kline+**本地JS重算** | 中(逐股) | 无 | 无 | 位置11列 | 中(盘中bar/口径210vs120) |
| stock_zh_index_spot_sina | demjson | 中 | 无 | 无 | 位置15列 | 低 |
| stock_zh_index_daily | **JS混淆+split结构** | 中(自认封IP) | 无 | 无 | 键名 | 低(全断型) |
| index_global_spot_em | JSON np=2转置 | 低 | 无 | 无 | 键名+**÷100硬缩放** | **高(100倍静默错)** |
| stock_zh_a_gdhs | datacenter JSON | 中(无sleep翻页) | 无 | 无 | 位置16列 | 中 |
| stock_yjyg_em | datacenter JSON | 中(无sleep翻页) | 无 | 无 | **位置28列+columns=ALL** | **高(schema加列全错位)** |

通判: 上游分三族——**新浪族**(JS 混淆解码, 全断型故障, 封 IP)/**东财行情族**(push2 JSON, 位置契约, 静默错位型)/**东财数据中心族**(datacenter JSON, reportName+filter, schema 漂移型)。三族故障模式不同, provider contract 的断言设计应按族区分: 新浪族重可用性探针, 行情族重单位/量级断言, 数据中心族重列数/schema 哈希断言。

---

## 5. Phase3 provider contract spike 供料（接口形状设计稿）

```
# 我方 AkshareProvider 包装层（伪代码, 自有记号）
契约文件: config/akshare_contracts.yaml  (每接口一条冻结契约)
  interface_id: stock_zh_a_hist
  akshare_func: stock_zh_a_hist
  family: eastmoney_push2        # sina_js | eastmoney_push2 | eastmoney_datacenter
  frozen_columns: [日期, 股票代码, 开盘, ...]     # 列名+顺序
  units: {成交量: 手, 成交额: 元, 涨跌幅: pct_point}
  sanity: {收盘: [0.01, 10000], 换手率: [0, 100]}   # 量级断言
  expected_ncols: 12              # datacenter 族必填
  http: {timeout: 30, retries: 3, backoff: exp_jitter}   # 统一包装, 不信库内行为
  pit: {as_of_col: 公告日期 | null, ban_adjust: [qfq, hfq]}

调用形状: fetch(interface_id, **params) -> (df, meta)
  meta = {source_url, fetched_at, akshare_version, impl_file_sha256,
          row_count, schema_ok, unit_check, sanity_check}
  任一 check 失败 → fail-closed 抛 ContractBroken + Discord 硬告警(不 coerce 不静默)
```

验收条件（spike 完成定义）:
1. 10 个在用接口契约文件全落盘（列表即本报告 §2, 字段表可直接抄）
2. 探针命令 `python3 probe_akshare.py --all` 逐接口: schema 断言+单位断言+量级断言+行数下限, 复用 Phase B.0 探针基建（防重复建设: 是升级不是新建）
3. run_manifest 增记 akshare 版本+在用实现文件哈希; 升级 akshare 前探针必须绿
4. 日历/基准两个单点完成 §3.2/§3.3 加固
5. 负向测试: 喂 12 列 kline 假响应给 stock_zh_a_hist 契约→必须拒; 喂 fltt 精度改变的 index_global 假响应→sanity 必须拦（对照综合报告"守卫必须配毒样本测试"教训）

---

## G 条目闭合声明

- **G8（我方在用竞价/分钟接口未审计 + 在用接口清单未逐个覆盖）: 闭合**。
  ①在用接口清单已落成文件（本报告 §1, 11 个调用点/10 个接口, 逐文件行号）;
  ②每个在用接口已读冻结 SHA 下实现全文, 给出数据源 URL/字段/单位/翻页/日期边界/异常处理契约（§2）;
  ③G8 点名的竞价/分钟盲区实测结论: **不在 akshare 侧**（Tushare stk_auction_o/c + stk_mins）, `stock_tfp_em` 零调用——原怀疑面收窄为"tushare 代理契约未审计", 已作为范围移交注记写入 Wave 映射（本轨道不越界展开）;
  ④运行时/冻结 SHA 版本对齐已实测（10 文件 diff=0）。
  残留（不影响闭合判定, 见 confession）: 东财服务端字段语义未抓包复核、北交所 market_code 未实测。

## coverage_confession

没读/没做:
- 东财 push2/datacenter 的**服务端**字段官方语义（f2/f3/fltt/dect 等）未抓包独立复核, 单位结论来自 akshare 映射代码+我方 implied-hand 自检实测中位比, 未取东财官方文档;
- 新浪 hq 的 volume=股 凭 akshare 不换算行为+我方 reconcile 实测档位反推, 未直接引证新浪文档;
- `stock_zh_a_hist` 对北交所代码(4/8开头→market=0)的正确性未实测;
- `stock_cyq_em` 的 range=undefined→全窗210根 与东财网页(120)的数值差异未实测复算;
- 新浪日历返回的未来边界(排期到何时)未做当日快照断言;
- em spot 对停牌股返回 NaN 还是昨收未实测(我方 close>0 过滤两况皆防);
- `daily_stock_analysis` 克隆内部的 akshare 调用未 grep（不在任务指定两目录, 属上游迁移轨道; 但其 eastmoney_patch 被我方两个 fallback 依赖的事实已在 §3.5 记录）;
- `factor_factory/outputs/evolution/` 历史备份脚本按死代码跳过, 未逐个核;
- 除系统 python3.9 user-site 与 qlib_lab venv 外, 未穷举机器上其它 python 环境的 akshare 安装。

不确定:
- 运行时 1.18.21 与冻结 SHA 的 diff=0 结论仅覆盖 10 个在用实现文件+utils/func.py, 不覆盖其 import 的全部间接依赖（utils/request.py 等三套 HTTP 层卡已审, demjson/tqdm 未 diff）。

## Wave 映射

| 产出 | 映射 | 粗成本 |
|---|---|---|
| 10 接口冻结契约文件+探针断言（§2 字段表直接抄, 复用 B.0 探针基建） | **Wave1 数据可信** | 2-3 天（比卡估的一周低, 因字段表已由本报告产出） |
| 日历 provider 统一(4处收敛)+落盘快照+尾部<90天告警+timeout | **Wave1 数据可信** | 1 天 |
| index_global /100 量级 sanity 断言+midday as_of 修复 | **Wave1 数据可信** | 2 小时 |
| 指数基准双源对账断言+基准缺失 fail-closed | **Wave1 数据可信**（复盘度量诚实） | 半天 |
| run_manifest 记 akshare 版本+实现文件哈希, 升级前探针门 | **Wave1 数据可信** | 半天 |
| yjyg/gdhs 列数+schema 断言(datacenter 族专用) | **Wave1 数据可信** | 2 小时 |
| cyq fallback_schema 下游消费点排查 | **Wave2 组合风险契约**（近似数据不得触发真阈值决策） | 2 小时 |
| eastmoney_patch 列为上游迁移保护文件+加载失败显式告警 | **上游迁移项目** | 1 小时（并入 Phase2 上游 diff 轨道切片清单） |
| 范围移交注记: 竞价(stk_auction_o/c)/分钟(stk_mins)契约属 tushare 代理, 建议 Phase4 另立 tushare contract 条目（含 stk_mins 8000行翻页边界/竞价字段单位） | **Wave1 数据可信**（新条目, 非本轨道） | 立项 1 小时, 审计另计 |
| Wave3 AI 消融: 本轨道无直接供料（akshare 数据层与 AI 环节无耦合点） | — | — |
