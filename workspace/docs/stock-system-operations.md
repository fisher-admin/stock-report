# A股选股系统运维说明

> 更新时点：基于 2026-04-14 本机现有健康文件、发布产物和代码配置整理。
> 目标：给后续维护提供一份“先看哪里、怎么判断、出问题查哪里”的固定入口。

## 1. 当前已核实状态

### 1.1 健康结论
当前系统主链路整体可用，已核实到的关键结果：

- `validation_report.json`：`ok=true`
- `validation_report.json`：`ai_complete=true`
- `validation_report.json`：`publish_ready=true`
- `ai_publish_readiness.json`：`ok=true`
- `ai_publish_readiness.json`：`ai_complete=true`
- `ai_publish_readiness.json`：`publish_mode=full`
- `ai_publish_readiness.json`：发布子阶段 `published=true`
- `recommendation_db_sync.json`：`ok=true`
- `stock-report/data/latest/run_manifest.json`：`trade_date=20260413`、`validation_ok=true`、`ai_complete=true`

### 1.2 当前需要注意的状态分歧
虽然主健康文件显示发布链通过，但 `run_manifest.json` 里当前仍显示：

- `publish_ready=false`
- `published=false`

这说明当前有**状态口径不一致**，后续判断系统是否“真发布完成”时，建议优先看：

1. `stock_data/03-working/health/validation_report.json`
2. `stock_data/03-working/health/ai_publish_readiness.json`
3. 再对照 `stock-report/data/latest/run_manifest.json`

如果这三者再次冲突，优先按“健康文件是否通过 + 发布目录实际产物是否已更新”来判断，不要只看 `run_manifest.json` 单点。

---

## 2. 目录与角色

### 2.1 主目录
- OpenClaw workspace：`~/.openclaw/workspace`
- Orchestrator 脚本目录：`~/.openclaw/workspace/skills/stock-system-orchestrator/scripts`
- 工作仓库（中间产物）：`~/.openclaw/workspace/stock_data/03-working/stock-report-repo`
- 发布仓库（对外页面/最终产物）：`~/.openclaw/workspace/stock-report`
- 健康状态目录：`~/.openclaw/workspace/stock_data/03-working/health`
- AI 外部分析器：`~/daily_stock_analysis/fisher_bridge.py`

### 2.2 两套仓库的职责

#### 工作仓库
用于编排、合并、校验、准备发布：
- `stock_data/03-working/stock-report-repo`

#### 发布仓库
用于最终页面和对外数据消费：
- `stock-report`

判断是否真的上线，不能只看工作仓库，必须看发布仓库产物是否同步完成。

---

## 3. 编排链路总览

### 3.1 Stage 1：数据准备
脚本：`stage1_data_prepare.py`

职责：
- 识别最新有效交易日
- 准备核心缓存：`daily / stk_factor / cyq_perf`
- 写出就绪状态文件

关键健康文件：
- `stock_data/03-working/health/data_preparation_run.json`
- `stock_data/03-working/health/data_ready_YYYYMMDD.json`

### 3.2 市场分析层
脚本：
- `generate_market_snapshot.py`
- `generate_market_morning_brief.py`
- `generate_market_industry_heatmap.py`

职责：
- 生成市场快照
- 生成晨判
- 生成行业热度映射

关键发布产物：
- `stock-report/data/recommendation_analytics/market_morning_brief_latest.json`
- `stock-report/data/recommendation_analytics/market_industry_heatmap.json`
- `stock-report/data/recommendation_analytics/industry_heatmap.json`

### 3.3 策略/候选层
脚本：
- `stage3_strategy_suite.py`
- `generate_unified_decision_payload.py`

职责：
- 运行保留策略
- 形成候选池
- 输出统一决策载荷

当前保留策略（来自 `validation_report.json`）：
- `baseline_v4`
- `prebreakout_v41`
- `alpha_short_v1`
- `alpha_long_v1`

关键产物：
- `stock-report/data/recommendation_analytics/prebreakout_recommendations.json`
- `stock-report/data/recommendation_analytics/unified_decision_payload.json`

### 3.4 午盘/状态层
脚本：
- `generate_midday_analysis.py`
- `generate_latest_states.py`
- `validate_run.py`

职责：
- 生成午盘分析
- 刷新 latest 状态页
- 统一健康校验

关键产物：
- `stock-report/data/recommendation_analytics/midday_analysis_latest.json`
- `stock-report/data/latest/run_manifest.json`
- `stock_data/03-working/health/validation_report.json`

### 3.5 AI 深度分析与发布层
脚本：
- `stage4_ai_publish.py`
- `stage4_ai_backfill_loop.py`

外部桥接：
- `~/daily_stock_analysis/fisher_bridge.py`

职责：
- 为候选 top20 生成 AI 字段
- 回填同日 AI 分析
- 把结果合并进最终发布链

当前已核实结果：
- `target_strategy=prebreakout_v41`
- `target_top_n=20`
- `ready=20`
- `failed=0`
- `ai_complete=true`

### 3.6 推荐数据库同步层
脚本：
- `stage5_recommendation_db_sync.py`
- `recommendation_warehouse.py`

职责：
- 持久化推荐结果
- 导出 CSV / JSON / summary
- 为后续统计与追踪提供仓储底座

关键健康文件：
- `stock_data/03-working/health/recommendation_db_sync.json`

当前已核实结果：
- `ok=true`
- `latest_recommend_date=20260413`
- `latest_date_row_count=20`

---

## 4. 关键配置文件

### 4.1 OpenClaw 主配置
文件：`~/.openclaw/openclaw.json`

主要承载：
- gateway / channel 配置
- agent 配置
- Brave 相关配置
- OpenClaw 全局运行参数

### 4.2 daily_stock_analysis 环境配置
文件：`~/daily_stock_analysis/.env`

当前已核实存在的关键变量项：
- `OPENAI_BASE_URL`
- `LITELLM_MODEL`
- `LITELLM_FALLBACK_MODELS`
- `BOCHA_API_KEYS`
- `TAVILY_API_KEYS`
- `BRAVE_API_KEYS`
- `TUSHARE_TOKEN`
- `TUSHARE_API_URL`

注意：
- 文档里只记录“变量名和位置”，不要把真实 key/token 写进文档。
- 这份 `.env` 既影响 AI 分析，也影响搜索增强和数据源调用。

---

## 5. 交易日口径规则

### 5.1 统一原则
现在应统一遵循：

1. **优先用最新有效交易日，不用系统自然日硬推。**
2. 所有分析产物应围绕同一个 `trade_date` 收口。
3. 晨判、午盘、策略、AI、发布都应跟随这个交易日。

### 5.2 关键 helper
文件：`orchestrator_common.py`

关键方法：
- `extract_yyyymmdd()`
- `resolve_effective_trade_date()`

当前代码行为：
- `extract_yyyymmdd()`：从任意日期文本里提取前 8 位数字日期
- `resolve_effective_trade_date()`：按候选顺序取第一个有效交易日；只有显式允许时才 fallback 到当前自然日

### 5.3 当前建议的强校验点
至少对齐这几项：
- `market_morning_brief_latest.json -> trade_date`
- `market_morning_brief_latest.json -> market_data_trade_date`
- `midday_analysis_latest.json -> source_dates.*`
- `run_manifest.json -> trade_date`
- `validation_report.json -> checks.latest_common_trade_date`

当前已核实：
- 晨判 `trade_date=20260413`
- 晨判 `market_data_trade_date=20260413`
- 午盘源日期全部指向 `20260413`
- `run_manifest trade_date=20260413`

---

## 6. 搜索增强链说明

### 6.1 当前配置结构
`daily_stock_analysis/src/search_service.py` 当前初始化顺序是：

1. `Bocha`
2. `Tavily`
3. `Brave`
4. `SerpAPI`（代码支持，当前是否启用以环境变量为准）

### 6.2 当前已核实的环境项
在 `~/daily_stock_analysis/.env` 已找到：
- `BOCHA_API_KEYS`
- `TAVILY_API_KEYS`
- `BRAVE_API_KEYS`

因此当前实际主搜索链至少包括：
- `Bocha -> Tavily -> Brave`

### 6.3 Brave 降级规则
`search_service.py` 中，Brave 命中以下错误会触发 provider 级冷却：
- `HTTP 402`
- `Payment Required`
- `Usage limit exceeded`

当前代码行为：
- 命中额度/套餐类错误后会临时禁用该 provider
- Brave 的对应冷却时长为 **6 小时**
- 这样可以避免 Brave 持续报错刷屏，主链自动回退给 Bocha / Tavily

### 6.4 运维判断原则
- 正常情况下优先看 Bocha 是否可用
- Bocha 不可用时看 Tavily 是否接管
- Brave 不再适合作为主力，只适合末级备用

---

## 7. 日常巡检最短路径

如果只想用最少时间判断系统是否正常，先看这 5 个文件：

1. `~/.openclaw/workspace/stock_data/03-working/health/validation_report.json`
2. `~/.openclaw/workspace/stock_data/03-working/health/ai_publish_readiness.json`
3. `~/.openclaw/workspace/stock_data/03-working/health/orchestrator_run.json`
4. `~/.openclaw/workspace/stock_data/03-working/health/recommendation_db_sync.json`
5. `~/.openclaw/workspace/stock-report/data/latest/run_manifest.json`

### 7.1 最短判断口径

#### A. 主健康是否通过
看 `validation_report.json`：
- `ok=true`
- `ai_complete=true`
- `publish_ready=true`

#### B. AI 是否补满
看 `ai_publish_readiness.json`：
- `ok=true`
- `ai_complete=true`
- `ready=20`
- `failed=0`
- 发布子阶段 `published=true`

#### C. 前端/latest 是否跟上
看 `run_manifest.json`：
- `trade_date` 是否正确
- `validation_ok` 是否为 `true`
- `ai_complete` 是否为 `true`
- 若 `published/publish_ready` 为 `false`，需要结合健康文件和发布目录真实产物交叉判断

---

## 8. 常见问题排查入口

### 8.1 日期错乱 / 今天与结果日期不一致
优先检查：
- `stock-report/data/recommendation_analytics/market_morning_brief_latest.json`
- `stock-report/data/recommendation_analytics/midday_analysis_latest.json`
- `stock-report/data/latest/run_manifest.json`
- `stock_data/03-working/health/validation_report.json`
- `orchestrator_common.py` 中交易日 helper 是否被正确复用

### 8.2 AI 没补满 / top20 缺字段
优先检查：
- `stock_data/03-working/health/ai_publish_readiness.json`
- `stage4_ai_publish.py`
- `stage4_ai_backfill_loop.py`
- `~/daily_stock_analysis/fisher_bridge.py`

重点看：
- `required_ai_field_counts`
- `ai_status_counts`
- `missing_source_codes`
- `unexpected_ai_provenance`

### 8.3 发布口径冲突
优先检查：
- `validation_report.json`
- `ai_publish_readiness.json`
- `run_manifest.json`
- 发布仓库 `stock-report/data/...` 实际文件时间戳和内容

如果健康文件显示通过，但 latest 清单没跟上，通常是：
- latest 状态刷新没同步
- 发布后状态回写滞后
- 某个阶段只更新了工作仓库，没有完整收口到发布仓库

### 8.4 搜索增强不稳定
优先检查：
- `~/daily_stock_analysis/.env` 中搜索 key 是否仍在
- `daily_stock_analysis/src/search_service.py`
- 是否为 Brave 402 / 额度问题

判断原则：
- Bocha 正常优先 Bocha
- Tavily 负责接管
- Brave 报 402 时按预期应进入冷却，不应长期污染主流程

---

## 9. 当前已知风险

1. **发布口径存在文件间分歧**
   - 健康文件显示通过
   - `run_manifest.json` 仍显示 `published=false`
   - 后续需要统一这套状态表达

2. **搜索层仍依赖外部服务**
   - Bocha / Tavily / Brave 都是外部服务
   - 即便主备已分层，仍要关注额度、网络、供应商波动

3. **Brave 只适合作为末级备用**
   - 当前策略是“出错就冷却”，不是主力承载

4. **判断上线不能只看单文件**
   - 至少同时看健康文件、latest 文件、发布目录产物三层

---

## 10. 维护建议

1. **以 `validation_report.json` 作为主判断入口**
   - 它现在是最完整的总校验汇总

2. **交易日问题优先查 helper 与产物对齐**
   - 先看日期一致性，再看逻辑解释

3. **AI 链改动先做 smoke test，再全量跑**
   - 不要一上来就全量重跑

4. **搜索层默认主力应是 Bocha + Tavily**
   - Brave 作为末级备用保留即可

5. **后续最好补一轮状态统一**
   - 把 `validation_report / ai_publish_readiness / run_manifest` 的发布字段收口到同一语义

---

## 11. 一句话版

这套系统当前已经具备：
- 数据准备
- 市场分析
- 策略候选
- 午盘分析
- AI 补全
- 推荐入库
- latest 页面产物
- 搜索主备降级

但仍有一个需要后续收口的问题：
**发布健康状态与 `run_manifest` 的 published/publish_ready 字段存在分歧。**

后续如果只想快速判断系统健康，先看：
`validation_report.json -> ai_publish_readiness.json -> run_manifest.json`。
