# A股推荐系统全面研判与整改优化升级方案

- 版本：v1.0（2026-06-12）
- 范围：`~/.openclaw/workspace` 全部股票系统组件（选股引擎、数据管线、AI 分析链、发布前端、因子工厂、编排调度）
- 既定决策：彻底脱钩上游开源项目；渐进式分阶段推进；**基本数据一律不动**
- 用途：本文档可直接作为后续整改执行的任务输入，逐条带验收标准

---

## 0. 一页结论

系统主链每日仍在运转（最新交易日 20260611 数据对齐），编排、健康闸门、降级机制的工程化水平明显高于一般个人量化系统——这是值得保留的底子。但研判结论是：**"在跑"与"在选好股"之间已严重脱节**，存在四个必须正视的事实：

1. **选股大脑已退化**。生产打分函数中动量=当日涨跌幅、量比=1.0、波动率=2.0 三个因子被写死，量价类因子实际失效，TOP20 退化为"低波筹码集中筛选器"——20260611 当日 20 只里 13 只是银行股。
2. **AI 增强链断裂一周以上**。venv 内 `jiter` 模块损坏导致 LiteLLM 全部调用失败，且未配置任何备选模型；20260605 起 AI 分析持续复用 20260604 旧结果（`ai_source_stale=1`），系统连续降级 `base_only`。
3. **系统已连续空转**。因 AI 缺位 + 执行闸门联动，候选层每日 20 只全部标记"回避"、主攻 0 只，最终裁决持续 `observe_only`——产出无决策价值，但管线每天照常消耗 API 与算力。
4. **验证闭环全面停摆**。T+1 验证止于 3/25、周复盘止于 3/06、因子评估止于 4/20、调参器为空——没有任何机制能发现上述退化，"银行股霸榜"无人报警即是后果。

另有两类高危问题：**安全**（GitHub PAT 明文嵌在 git remote URL；Tushare token 明文散布多处并经 HTTP 明文代理外发）与**可信度**（对外展示的回测指标 +15.89%/Sharpe 1.71 系硬编码的 2 月旧数，与实际回测结果及方法学缺陷均不符）。

整改路线：**阶段0 止血（1 周）→ 阶段1 后端重构（2-3 周）→ 阶段2 前端重构（2 周）→ 阶段3 闭环升级（持续）**。详见第 5 节路线图。

---

## 1. 系统全景

### 1.1 组件拓扑与数据流

```
                       OpenClaw cron（北京时间）
   08:00 晨判 │ 12:00 午盘 │ 19:30 主链 │ 20:00 AI分析 │ 15:40 T1(hermes调度)
                              │
  ┌───────────────────────────▼────────────────────────────────┐
  │ 编排器 skills/stock-system-orchestrator（run_pipeline.py）   │
  │ preflight → stage1数据 → 市场快照 → stage3策略套件           │
  │ → stage4 AI发布(非阻塞) → stage5入库 → validation → stage6发布│
  └──┬──────────────┬───────────────┬──────────────┬───────────┘
     │              │               │              │
 ┌───▼────┐   ┌─────▼─────┐   ┌─────▼─────┐  ┌─────▼─────────┐
 │数据层   │   │选股引擎    │   │AI分析引擎  │  │发布层          │
 │backtest_│   │skills/     │   │workspace/  │  │strategy_      │
 │cache/   │   │stock-      │   │daily_stock_│  │publication_   │
 │(Tushare │   │analyzer/   │   │analysis/   │  │layer.py       │
 │ 经HTTP  │   │pipeline.py │   │bridge_*.py │  │→ data/latest/*│
 │ 代理)   │   │backtest.py │   │(LiteLLM→   │  │→ strategy_    │
 │         │   │启动前夕v4.1│   │ mimo中转)  │  │  backtests.json│
 └─────────┘   │+O2C+T1     │   └────────────┘  └──────┬────────┘
               └────────────┘                          │
 ┌─────────────────────────┐              ┌────────────▼─────────────┐
 │factor_factory/           │              │stock-report/（发布仓）     │
 │O2C每日选股(盘前08:37)     │─────────────▶│GitHub Pages 9页 SPA       │
 │T1 Alpha191(15:40)        │              │fisher-admin.github.io     │
 │影子盘模拟、qlib旁路       │              │(工作仓: 03-working/        │
 └─────────────────────────┘              │ stock-report-repo)        │
                                          └──────────────────────────┘
```

### 1.2 活性地图

| 状态 | 组件 | 证据 |
|---|---|---|
| 🟢 活（每日） | backtest_cache、selection_history、ai_analysis、stock-report 发布、O2C/T1 日更、workspace 版 daily_stock_analysis | 数据/提交均到 20260611-12 |
| 🟡 带病运行 | AI 增强链（jiter 损坏，stale 复用）、T1 线（选出 0 因子、top20 全 0 分仍发布）、晨判 cron（最近一次 Message failed）、O2C 影子盘统计（累计 -100%/Sharpe -59.8 的病态口径） | health/、ACTIVE_TASKS、outputs |
| 🔴 停摆 | verify_history（3/25）、weekly_reviews（3/06）、alpha_mining（4/20）、long_term_alpha（3/27）、optimizer_results（空）、ops/task_watchdog（3/16） | 文件 mtime |
| ⚫ 死/遗留 | `~/daily_stock_analysis` 旧副本（5/18 休眠）、01-root/02-incremental 旧数据链（2-4 月冻结）、stock-report 内两代旧前端+9MB 死资产、scripts/stock_report_phase2~8、repos 实验、factor_factory 内 tmp/嵌套 bug 目录 | 详见 4.3 |

### 1.3 数据资产冻结清单（按"基本数据不动"原则）

以下资产为系统的历史事实，重构全程**只读、不迁移、不改写、不删除**：

| 资产 | 位置 | 规模 |
|---|---|---|
| 全市场历史 parquet | `stock_data/01-root/`（6128 只） | ~750MB |
| 日度增量 parquet | `stock_data/02-incremental/` | 15 天 |
| 现行数据缓存 | `stock_data/03-working/backtest_cache/`（3927 个 parquet） | 核心 |
| 选股历史快照 | `stock_data/03-working/selection_history/`（64 天） | — |
| 推荐入库 | `recommendation_warehouse/recommendations.db` | 1.46GB |
| AI 分析历史 | `stock_data/03-working/ai_analysis/`（113 份） | — |
| 验证/回测历史 | `verify_history/`、`backtest_result.json*` | — |
| 三个 SQLite | workspace 版 102MB / 旧副本 23MB / workspace 根 9.6MB | 见 4.2 |

新代码一律写**新目录 + schema 版本号**；如确需归档死数据，移入 `trash/` 而非删除（遵守工作区规则）。

---

## 2. 综合研判：问题清单（按严重度分级）

### P0 — 直接影响"推荐是否正确/可信/安全"

| # | 问题 | 证据 | 后果 |
|---|---|---|---|
| P0-1 | **生产打分因子退化**：`score_from_bulk()` 中 `momentum=当日pct_chg`、`volume_ratio=1.0`、`volatility=2.0` 写死，并非配置的 20 日动量/真实量比/真实波动 | `skills/stock-analyzer/backtest.py:817-826` | TOP20 行业集中（13/20 银行），"启动前夕"名存实亡；回测与实盘同源退化，互相验证形成假闭环 |
| P0-2 | **AI 链路断裂且无备份**：venv `jiter` 模块损坏 → LiteLLM 全挂；`LITELLM_FALLBACK_MODELS` 为空；单一 mimo 中转单点 | ACTIVE_TASKS（`No module named 'jiter.jiter'`）；`daily_stock_analysis/.env` | 6/05 起 AI 字段全 stale，`publish_mode=base_only`，pending 20/20 |
| P0-3 | **系统空转**：AI 缺位联动执行闸门 → 候选全部 `avoid`、主攻 0、裁决 `observe_only` 持续一周+ | `stock-report/data/latest/system_verdict.json`（candidate_gate: avoid:20） | 每日产出零决策价值，但成本照常发生 |
| P0-4 | **对外指标失真**：data.json 携带硬编码旧回测数（+15.89%/年化62.56%/Sharpe1.71，系 2/28 备份数据）；回测方法无费用/滑点/开盘可达性/涨停约束，用次日 close-to-close | `skills/stock-analyzer/pipeline.py:929-936`；`run_strategy_suite.py:225-285` | 公开页面展示不可信指标 |
| P0-5 | **凭证泄露面**：GitHub PAT 明文嵌在发布仓 remote URL；Tushare token 明文散布（`pipeline.py:23`、TOOLS.md、多个旧脚本）且经 HTTP 明文代理（jiaoch.site，旧地址 <REDACTED_IP> 残留）外发 | `git remote -v`；`skills/stock-analyzer/pipeline.py:23-24` | token 可被嗅探/滥用；PAT 入 git 配置随备份扩散 |
| P0-6 | **失败结果污染数据**：LLM 全失败仍返回 score=50/"持有"入库推送（历史库中 score=50 占 33.75%）；LLM 报错原文被烘进公开 data.json；解析失败走关键词打分仍标 `success=True` | `daily_stock_analysis/src/analyzer.py:773-786,1228-1241`；`stock-report/data.json:69` | 历史数据与回测统计被稀释，公开页暴露内部报错 |
| P0-7 | **0 分策略照常发布**：T1 线 `selected_factors=[]`、top20 全部 score=0.0（按代码顺序排列）仍每日发布 | `factor_factory/outputs/t1_alpha191/portfolio_spec.json`；strategy_backtests.json | 公开页面展示无意义名单 |

### P1 — 闭环与可靠性

| # | 问题 | 证据 |
|---|---|---|
| P1-1 | 验证/复盘/调参闭环全部停摆（见 1.2 活性地图 🔴 行），退化无人报警 | mtime |
| P1-2 | 配置三处漂移：展示权重（run_strategy_suite.py:100-116 旧 v4.1）≠ 实际打分权重（pipeline.py v4.3）≠ 文档（SKILL.md 还在写 5 因子/17:00/旧代理） | 三文件对照 |
| P1-3 | 晨判 cron error（Message failed）未修复；O2C 影子盘统计口径病态（-100%/win_rate 0.0008）持续输出 | cron snapshot；`outputs/research_preview/cumulative_stats.json` |
| P1-4 | AI 引擎（daily_stock_analysis）数据质量缺陷：多源复权口径不一致（qfq/不复权/auto_adjust 混用）、ETF 用净值伪造 OHLC、量比口径与交易软件不符、UA 轮换形同虚设 | `data_provider/*`（efinance_fetcher.py:433-442 等） |
| P1-5 | 搜索新闻原文直接注入 LLM prompt，存在 prompt-injection 面；旧副本 .env 搜索 key 单复数写错导致情报静默为空（现役副本需核查） | `src/search_service.py`、`src/config.py:428-438` |
| P1-6 | 监控只有"健康文件"，无主动告警推送——stale/fail 不会主动通知到人 | health/ 仅落盘 |
| P1-7 | 1.46GB recommendations.db 无瘦身/备份机制；SQLite 并发写无 WAL | storage 层 |

### P2 — 工程质量（重构主要对象）

巨型单文件：`pipeline.py` 1505 行、`recommendation_warehouse.py` 75KB、`strategy_publication_layer.py` 65KB、`stage4_ai_publish.py` 43KB、前端 `stock-workbench.js` 1576 行。全局变量原地改 `FACTOR_CONFIG` 切换策略（非线程安全）。bare `except: continue` 吞错遍布打分热路径。双副本并存（`~/daily_stock_analysis` 休眠 vs workspace 现役，git 均为脏状态、定制层 untracked，从未收口提交）。三个同名 SQLite 各自为政。四套数据 schema 并行（data.json=combined.json、data/data.json、data/latest.json、data/latest/*）。测试：编排器/选股引擎/前端 0 测试；AI 引擎仓有 252 个测试但 `network` marker 形同虚设，现役 venv 连 pytest 都没装。

### P3 — 死代码与体积

stock-report 仓 273MB 其中 .git 占 249MB（日度 MB 级 JSON 全量重写历史）；两代旧前端约 2000+ 行 + 9MB 无引用资产（js/app.js、css/style.css、chart.min.js、observatory-governance.css、template.html、stitch/visuals 图、过期验收截图 verification-*.png）；僵尸数据文件（top50*.csv 止 3/24、data/history 止 3/10）；孤儿页 2 张；sentiment.html 加载 Vue 却不用；deploy.sh/deploy.yml 已失效；scripts/stock_report_phase2~8 一次性补丁脚本；factor_factory 内 `Users/` 嵌套 bug 目录、tmp_stage6x、空 modules/ 脚手架、空根 mlruns/。

### 值得保留的资产（重构基石）

1. **`data/latest/*.json` 状态契约**：run_manifest/system_verdict/四级闸门/各层 state 的分层设计本身是对的，前后端唯一事实源已成形。
2. **健康合同与 fail-closed 纪律**：stage1 数据质量闸"宁可失败不用昨日数据冒充"、降级来源标注 `fallback_source`、run_id 全链一致性校验。
3. **编排分级**：非阻塞 stage4/5 + 自愈重试 + A/B/C/D 降级，结构成熟。
4. **启动前夕的因子设计思想**：硬过滤"选蓄势不追涨"、连续函数打分避免同分——设计是好的，坏在生产路径喂错了特征。
5. 前端 token/双主题体系、`escapeHtml` 纪律、零构建 GitHub Pages 部署模式。
6. factor_factory 的晋级门（promotion gate）与 shadow 车道思想——应推广为全系统策略治理标准。

---

## 3. 整改方案 — 阶段0 止血（目标 1 周内）

> 原则：只修不改架构；每项独立可回滚；完成一项验证一项。

### 3.1 安全整改（第 1 天）

| 动作 | 具体步骤 | 验收 |
|---|---|---|
| 撤销并轮换 GitHub PAT | GitHub 后台 revoke 现 token → 改用 SSH key 或 credential helper；`git remote set-url` 移除内嵌凭证（发布仓+工作仓） | `git remote -v` 无任何凭证字样 |
| 轮换 Tushare token | tushare.pro 重置 token → 新 token 只存 `.env`；删除 `pipeline.py:23`、TOOLS.md、旧脚本中的明文 | 全 workspace `grep` 旧 token 零命中 |
| 数据代理去明文 | jiaoch.site 代理改 HTTPS（自建则上证书），或直连官方 `api.tushare.pro`；删除 <REDACTED_IP> 全部残留（stock_analyzer.py:143,589、fix_missing.py:10、SKILL.md:59、旧仓 3 处） | 抓包/代码审查无 HTTP 明文 token 外发 |
| AI 引擎 API 面收口 | 确认 daily_stock_analysis WebUI 仅监听 127.0.0.1 或不启动；如需启动则开 `ADMIN_AUTH_ENABLED=true` | 端口扫描确认 |

### 3.2 AI 链路复活（第 1-2 天）

1. 重建 workspace 版 `daily_stock_analysis/venv`（固定 Python 3.11），`pip install -r requirements.txt` 后单测 `litellm.completion` 修复 `jiter` 损坏。
2. `.env` 配置 `LITELLM_FALLBACK_MODELS` 至少 2 个异源模型（不同中转/厂商），消除单点。
3. 修复 LLM 失败兜底逻辑：失败不再产出 score=50 假结果，改为 `status=failed` 落库、不入推送、不进回测（`src/analyzer.py:773-786`）；解析降级结果打 `degraded` 标记。
4. 跑通当日全量 20 只 → 确认 `ai_complete=true`、`ai_source_stale=0`。

验收：连续 3 个交易日 `ai_publish_readiness.publish_mode=full`；candidate 层不再因 AI 缺位全量 avoid。

### 3.3 选股因子热修（第 2-4 天）

1. `backtest.py score_from_bulk()`：从 backtest_cache 历史窗口计算**真实 20 日动量、5 日量比、20 日波动率**，替换三个写死值（保持函数签名不变，最小侵入）。
2. 增加**行业暴露上限**：TOP20 单一行业 ≤ 5 只（可配置），超限顺位替补。
3. 权重单源化热修：run_strategy_suite 直接 import `PREBREAKOUT_CONFIG`，消除展示权重≠实际权重。
4. 用修复后逻辑重跑近 65 日横截面对照（修复前 vs 修复后 TOP20 次日收益、行业分布），结果落 `strategy_research/`。

验收：TOP20 行业 HHI 显著下降（银行 ≤5 只）；对照报告产出且修复后不劣于修复前。

### 3.4 止噪与诚实化（第 4-5 天）

1. 删除 `pipeline.py:929-936` 硬编码回测数，data.json 改为引用 `backtest_result.json` 实算值，并在前端标注回测口径与局限。
2. T1 线加 fail-closed：`selected_factors` 为空或全 0 分时**不发布**，状态页显示"研究线无有效输出"。
3. O2C 影子盘统计：修复口径或暂停 cumulative_stats 输出，避免 -100% 病态数据误导。
4. 修晨判 cron 投递错误；LLM 报错原文从公开 data.json 清除（发布层过滤）。

---

## 4. 优化方案 — 阶段1 后端重构（2-3 周）

> 原则：渐进、可灰度——新模块与旧链路并跑对照，验证一致后切换；脱钩上游，按自身需求裁剪。

### 4.1 目标架构：收敛为单一项目 `fisher-quant`

```
fisher-quant/
├── config/            # 唯一配置中心（pydantic-settings + .env）：因子权重、模型链、调度、阈值
├── datahub/           # 统一数据访问层（只读封装 backtest_cache/01-root/各DB；统一复权口径标注）
├── strategies/        # 策略包：prebreakout/ o2c/ t1/ …（每策略= 配置+打分+硬过滤+元数据）
│   └── registry.py    # 策略注册表：research → shadow → active → retired 生命周期与晋级门槛
├── ai_engine/         # 从 daily_stock_analysis 抽取的单股分析核心（LLM调用/搜索/prompt/解析）
├── backtest/          # 回测引擎 v2（见 5.2）
├── publish/           # 发布层：生成 data/latest/* 契约（schema 版本化），git 推送
├── verify/            # 验证闭环：T+1/T+5 验证、周报、因子 IC 体检（复活 verify_history）
├── ops/               # 调度入口、健康合同、告警推送（Discord）
└── tests/
```

落地方式（渐进）：在 workspace 内新建 `fisher-quant/`，按依赖顺序逐模块迁移——datahub → strategies（含 3.3 热修成果）→ publish → ai_engine → verify。每迁移一个模块，编排器改调新模块，旧脚本保留两周观察期后归档 `trash/`。

### 4.2 关键重构项

| 项 | 现状 → 目标 | 验收 |
|---|---|---|
| 配置中心 | 权重/阈值/模型散布 3+ 处 → `config/` 单源，启动时校验 schema，权重变更留审计记录 | 全仓 grep 无第二处权重定义 |
| 拆巨型文件 | pipeline.py 1505 行等 4 个巨石 → 按"数据/打分/过滤/持久化/发布"拆为 ≤300 行模块；消灭全局变量切策略，改为策略实例传参 | 单文件 ≤500 行；策略并行计算安全 |
| AI 引擎脱钩瘦身 | daily_stock_analysis 全仓（含 webui/bot/api/桌面端，实际只用 `analyzer_service.analyze_stock` 一条链）→ 只抽取 pipeline 单股分析 + LiteLLM 适配 + 搜索服务 + 解析器入 `ai_engine/`；淘汰双副本 | 旧两副本归档；bridge 改调 ai_engine；功能等价对照通过 |
| AI 引擎修缺陷 | 顺带修复：复权口径统一为 qfq 并在 datahub 标注来源；ETF 伪 OHLC 数据剔除量价分析；搜索 key 配置兼容单/复数；新闻文本用分隔符+指令隔离降低注入面；移除 aihubmix 推广 header | 专项用例通过 |
| 数据库收敛 | 三个 stock_analysis.db → 现役 102MB 为唯一库（开 WAL+busy_timeout），其余两个只读归档；recommendations.db 建月度 VACUUM+导出归档机制 | 单一写入点；备份脚本入 cron |
| 测试与门禁 | 0 测试核心 → 打分/硬过滤/解析/回填/发布契约的纯逻辑单测 ≥80% 行覆盖；pre-push 跑 ruff+pytest | CI gate 绿 |
| 告警 | health 文件只落盘 → ops 每日汇总推 Discord：stale/fail/行业集中度/分数分布漂移超阈值即时告警 | 人为注入故障 10 分钟内收到告警 |
| 调度收口 | OpenClaw cron + hermes 调度 T1 双头 → 统一入口清单（cron 仅调 `fisher-quant/ops/run_*.py`），cron_jobs_snapshot 与实际一致 | cron 审计脚本通过 |

### 4.3 清理清单（归档 `trash/`，不删除）

`~/daily_stock_analysis` 整仓（休眠副本）；workspace 副本中 webui/bot/api 未用部分（待 ai_engine 抽取后）；`scripts/stock_report_phase2~8.py`；`stock_ai_failed_fallback_fill.py`（硬编码日期的一次性脚本）；factor_factory 的 `Users/` 嵌套目录、`tmp_*.py`、空 `modules/`、空根 `mlruns/`、`outputs/evolution/`（5/3 停）；`repos/autoresearch*`；根目录 2-3 月海报 PNG、`tmp_check_yfinance.py`；各处 `.bak/.backup` 文件。

---

## 5. 升级方案 — 阶段2 前端重构 + 阶段3 闭环升级

### 5.1 阶段2：前端重构（stock-report，2 周）

定位决策：**stock-report 为唯一前端**。dsa-web（daily_stock_analysis 内 7450 行 React 管理台）随 AI 引擎瘦身一并淘汰——它与发布站功能重叠、自身存在 Tailwind v4 配置断裂（高频类未生成）、死 store、0 测试等问题，继续维护两个前端不经济。

保留（资产）：`data/latest/*` 契约；CSS token + 双主题；"空壳页 + data-view"零构建模式；escapeHtml 纪律；数据新鲜度降级提示 UX。

重构内容：

| 项 | 现状 → 目标 |
|---|---|
| 视图层 | 1576 行单文件字符串拼 HTML、详情卡两份拷贝、死函数混居 → 组件化重写（推荐 Preact+HTM 或 Lit，保持零构建直发 Pages；如接受构建步骤则 Vite+预渲染）。组件：Shell/Gauge/CandidateCard/ReviewTable/Heatmap 等 ≤150 行/个 |
| 数据加载 | 每页全量拉 18 个 JSON（6-7MB、含 3.9MB 热力史）+ 显式禁缓存 → 按页声明依赖清单；发布层为大文件生成 `*_summary.json`（仅最新交易日）；文件名带 run_id 版本号实现长缓存；13 个硬依赖全部降级为"缺失则该区块显示占位+原因" |
| 信息架构 | 孤儿热力页 2 张、sentiment.html 无入口 → 收敛为 6 页主导航或并入子 Tab；删除无用 Vue CDN |
| 死资产清理 | 两代旧前端 + 9MB 资产 + 僵尸 CSV/history + 过期验收截图 → 归档后从仓库移除 |
| Git 瘦身 | .git 249MB → `git filter-repo` 重建历史（保留近 90 天），配合 PAT 轮换一次完成；后续日度数据改"覆盖式小文件 + 月度归档"控制增长 |
| 质量基建 | 0 测试 → 契约快照测试（每个 data-view 用 fixture JSON 渲染断言）+ Playwright 冒烟（6 页可开、关键字段可见）；Lighthouse 预算：首屏 payload ≤500KB、LCP ≤2.5s |

验收：6 页在桌面+手机可用；断网任一 JSON 页面不白屏；Pages 部署链路（stage4/stage6 推送）不变。

### 5.2 阶段3：闭环升级（持续，4-8 周）

1. **回测引擎 v2**（替换 run_strategy_suite 简易口径）：次日**开盘价**成交、一字涨停/停牌不可买剔除、双边费用+滑点（默认 0.13%+0.1%）、持有期矩阵（T+1/3/5/10）、基准对照（中证全指等权 + 剔除银行对照组）、滚动 walk-forward；指标一律由引擎写入结果文件，**代码内禁止出现手写收益数字**（加 CI 检查）。
2. **验证闭环复活**：每日 T+1/T+5 推荐命中验证（续 verify_history）；每周自动复盘报告（续 weekly_reviews）；每月因子 IC/IR 体检（续 alpha_mining）→ 全部进入告警体系：方向胜率连续 2 周 <45%、行业 HHI 超阈、分数分布漂移即报警。
3. **策略治理**：把 factor_factory 的晋级门推广为统一标准——任何策略必须经 shadow ≥20 交易日且通过晋级条件才能上公开页；不达标自动降级/下线（T1 当前状态即应自动退回 research）。
4. **AI 升级**：批量分析模式（一次请求多股，降本 ~10x）；JSON Schema 强约束输出（litellm response_format）；AI 结论与量化信号在前端分栏展示并标注模型版本与生成时间；AI 点位与现价做程序合理性校验（止损<现价<目标）。
5. **可选增强**：盘中风险提示推送、移动端 PWA、factor_factory 正信号因子（O2C）通过晋级门后接入主策略池。

---

## 6. 路线图与里程碑

| 阶段 | 周期 | 关键产出 | 核心验收 |
|---|---|---|---|
| 0 止血 | 第 1 周 | 安全整改、AI 复活、因子热修、止噪 | PAT/token 轮换完成；连续 3 日 `publish_mode=full`；TOP20 银行 ≤5 只；公开页无硬编码指标 |
| 1 后端重构 | 第 2-4 周 | fisher-quant 骨架、配置中心、ai_engine 脱钩、DB 收敛、测试+告警 | 新旧并跑 5 日输出一致；CI 绿；告警演练通过 |
| 2 前端重构 | 第 5-6 周 | 组件化视图层、按页加载、死资产清理、git 瘦身 | 6 页可用；首屏 ≤500KB；任一数据缺失不白屏 |
| 3 闭环升级 | 第 7 周起 | 回测 v2、验证闭环、策略治理、AI 批量化 | 周复盘自动产出；策略晋级门生效；回测含成本口径上线 |

依赖关系：0 → 1 → 2 可流水并行尾接；3 依赖 1 的 datahub/registry。每阶段结束打 git tag，可整体回滚。

---

## 7. 执行原则与风险

1. **基本数据不动**：1.3 冻结清单只读；清理一律 `trash/` 归档，不 `rm`。
2. **新旧并跑**：选股/发布的任何重构必须与旧链路并跑对照 ≥5 个交易日，diff 报告通过才切换。
3. **单一事实源**：配置、权重、回测指标、数据 schema 各自只允许一个权威出处。
4. **fail-closed 延续**：宁可当日不发布，不发布错误/陈旧/0 分数据。
5. **诚实展示**：公开页所有指标可追溯到结果文件；回测口径与局限必须标注。
6. 主要风险：数据源（Tushare 中转/反爬接口）脆弱——datahub 保留多源降级；LLM 中转单点——多模型 fallback + 月度账单监控；重构期间退化——靠并跑对照与告警兜底。

---

## 附录：关键证据索引

- 因子退化：`skills/stock-analyzer/backtest.py:817-826`；权重漂移：`run_strategy_suite.py:100-116` vs `pipeline.py:76-97`
- 硬编码指标：`skills/stock-analyzer/pipeline.py:929-936`；回测口径：`run_strategy_suite.py:225-285`
- AI 断链：`memory/core/ACTIVE_TASKS.md`（jiter 异常）；`.env` 无 fallback；`health/ai_publish_readiness.json`
- 空转裁决：`stock-report/data/latest/system_verdict.json`（avoid:20 / observe_only）
- 凭证：发布仓 `git remote -v`；`pipeline.py:23-24`；`TOOLS.md:44`
- 兜底污染：`daily_stock_analysis/src/analyzer.py:773-786`；历史库 score=50 占 33.75%
- 停摆闭环与死资产：见 1.2 / 2 / 4.3 各表
