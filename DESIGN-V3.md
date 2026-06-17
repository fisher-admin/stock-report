# 前端 v3 设计规范（2026-06-13 面向客户完全重构）

本文件是 v3 重构的权威蓝图。诊断背景：71 项问题（详见会话诊断报告），核心结论——
v2 架构层（data/）可保留，render 层与视觉层按"付费散户客户"视角完全重写。

## 0. 不可妥协的诚实性规范（每个渲染器必须遵守）

1. **禁止一切硬编码业绩数字**。任何 Hit/Sharpe/回撤/胜率兜底字符串一律删除。
   数据为 null → 显示「暂无可验证数据」+ 原因。grep 验收：render/ 下不得出现写死的百分比业绩。
2. **AI 状态显性化**。候选卡必须区分三种状态并打徽章：
   - `ai-full`（ai_summary/ai_points 真实存在）→「AI 已分析」
   - `ai-stale`（ai_source_stale 或引用旧日分析）→「AI 分析（{date}）」
   - `ai-none`（ai 字段全空）→「无 AI 分析 · 仅量化信号」，**严禁**用模板话术填充分析区。
   删除 candidateCard.js 的 normalizeAiPoints 兜底文案逻辑。
3. **真实战绩照实呈现**。62 日累计收益为负就显示负数（红涨绿跌色规范下负数用绿色），
   配口径说明（等权、按次日收盘、不含交易成本）。重复推荐榜必须带累计收益列。
4. **单一动作权威**。买/观/避只信管线字段（execution_state.adjusted_action 与
   candidate role_type），删除 displayActionFromAdvice / normalizeStrategyAction 的中文正则
   二次推断。两套口径在 UI 上分开命名：「执行建议」（execution 层）与「策略分层」（candidate 层），
   首页 Hero 只显示执行层一套数字。
5. **数据过期必须警示**。model.js 计算 `staleness`：decision_trade_date 距今超过 1 个交易日
   （周末/常见节假日顺延：周六日不计）→ 全站顶栏黄色横幅「数据更新于 {date}，今日数据尚未生成」。
   非交易时段不再显示「盘中」类误导文案。
6. **空态不白屏**。数组为空 ≠ 文件缺失：空数组要渲染解释（「今日该策略无入选标的」），
   文件缺失沿用 missingSection。
7. **每页页脚免责声明**：「本站内容为量化模型自动生成的研究记录，不构成任何投资建议。
   股市有风险，入市需谨慎。历史表现不代表未来收益。」系统说明页放全文合规声明。
8. **开发者语言清零**。publish_ready/provider/akshare/原始因子键名/设计自评文案不得出现在
   客户界面；术语过 format.js 的 `glossary()` 转译（regime→市场状态，IC→因子有效性 等）。

## 1. 信息架构：10 页 → 5 个客户页面（旧文件名全部保留可访问）

导航（顺序即客户心智路径）：

| 导航名 | 文件 | data-view | 回答的问题 |
|---|---|---|---|
| 今日操作 | index.html | dashboard | 今天该不该动手、动多少仓位、买什么 |
| 个股推荐 | decision-candidates.html | candidates | 三条策略各推了哪些股、依据是什么 |
| 市场行情 | market-overview.html | market | 大盘/行业环境如何（页内 Tab） |
| 历史战绩 | recommendation-review.html | review | 系统过往全部推荐与真实收益 |
| 系统说明 | research-lab.html | research | 系统怎么工作、策略原理、数据状态、免责 |

旧页面 → 薄壳复用合并视图（保持 URL 兼容，渲染 market/research 页并预选 Tab）：
- market-industry-heatmap.html → data-view=marketHeatmap → market 页 tab=全市场热力
- industry-heatmap.html → data-view=strategyHeatmap → market 页 tab=策略热力
- industry-compare.html → data-view=industryActions → market 页 tab=行业动作
- strategy-vs-market.html → data-view=strategy → research 页 tab=策略中心
- sentiment.html：仍由 legacy 生成器维护（不进主导航；market 页脚链接）。

新增站点门面：404.html、robots.txt、assets/favicon.svg、每页 og:title/og:description、
meta description（中文，含「量化选股研究记录」字样，不用营销性表述）。

## 2. 视觉系统（assets/styles/app.css —— 全新文件，不改 stock-workbench.css 以保回滚）

### 令牌
```css
:root[data-theme="dark"] (默认) {
  --bg:#0a0f14; --panel:#101820; --panel-2:#16202a; --line:#1f2b37;
  --text:#e8edf2; --text-2:#9fb0c0; --text-3:#5f7183;
  --brand:#d9a441;           /* 金 · 品牌强调 */
  --up:#f0464d; --down:#26a96a; --flat:#8a98a6;   /* A股红涨绿跌 */
  --ok:#2f9e6e; --warn:#d9a441; --bad:#e05257; --info:#4f8fd0;  /* 状态色独立于涨跌色 */
}
:root[data-theme="light"] { 等价亮色板，badge/tag 文字色必须随主题（修复 v2 亮色破损） }
```
- 数字一律 `font-variant-numeric: tabular-nums`；涨跌幅必须经 `pctHtml()` 上色（红涨绿跌）。
- 字体：system-ui + "SF Pro SC"/"PingFang SC"/"Microsoft YaHei"。
- 移动优先：单列卡片流；≥768px 双栏；≥1100px 侧导航。窄屏下表格组件一律换卡片列表
  或带渐变提示的横滚容器（.scroll-x with mask 提示）。
- 触达：所有交互元素 min-height 44px；:focus-visible 有可见焦点环。

### 图表（assets/scripts/v2/render/charts.js —— 纯函数返回 SVG 字符串，零依赖）
- `sparkline(values, {width,height,tone})` — 指数走势小图
- `equityCurve(points, {})` — 净值曲线：面积渐变 + 0 轴基线 + 最大回撤区间着色 + 末值标签
- `barSeries(items, {})` — 逐日命中率/收益柱状（正负双色，hover title）
- `heatGrid(rows)` — 行业热力网格：CSS `color-mix` 以 --up/--down 按涨跌幅 0~3% 映射透明度
- `gauge(score)` — 风险刻度盘（0-100）
所有图表函数必须可在 Node 中执行（无 document/window），并对空数组返回占位字符串。

## 3. 数据接线（manifest.js 扩展；loader/summarize 不动）

新增 SOURCES：
```
decisionState:   data/latest/decision_state.json      （今日一句话裁决，首页 Hero）
marketContext:   data/latest/market_context.json      （仓位上限/纪律，首页 Hero）
reviewUnified:   { path:'data/latest/review_track_latest.json',
                   fallbackPath:'data/latest/review_state_unified.json' } （战绩页）
strategyRegistry:data/latest/strategy_registry.json   （系统说明页）
systemHealth:    data/latest/system_health.json       （系统说明页）
```
VIEW_DEPS 增量：dashboard+=decisionState,marketContext；review+=reviewUnified；
research+=strategyRegistry,systemHealth；market 系列+=两个 heatmap 与 unified（合并页按 Tab 取数：
为简化，market 视图 optional 同时声明 marketHeatmap/strategyHeatmap/unified）。
`generate_view_summaries.py` 扩展：从 review_state_unified.json 生成
review_track_latest.json（保留 daily_comparison 全量 + 各策略汇总 + stock_rows 最近 400 条），
并保持原有热力摘要逻辑。

## 4. 各视图内容规范

### dashboard（今日操作）
1. 过期横幅（如适用）
2. Hero：system_verdict.final_action.label 大字（「今日只观察」/「可执行」）+
   decision_state.final_verdict 一句话 + market_context.position_limit 仓位指引 +
   gauge(risk_score) + 数据日期。**不显示**买/观/避三计数对（v2 矛盾源）。
3. 今日执行清单（execution_state 权威）：主攻表（股名+代码！）/ 观察表（前 10 + 展开），
   空时显示「今日无主攻标的：{market_context.policy}」。
4. 市场一眼：上证/深成/创业板/A50 sparkline 卡（session_snapshot）。
5. 近期战绩条：最近 10 个交易日命中率 barSeries + 「查看完整战绩 →」。
6. 三策略状态行：每策略当日入选数 + 链接到个股推荐对应 Tab。

### candidates（个股推荐）
1. 策略 Tab：启动前夕（主力）/ O2C 日内 / T1 因子（标注「研究预览」当 status=research_preview）。
2. 每策略头部：策略一句话说明（写死的产品文案，非业绩数字）+ 当日真实统计
   （入选 N 只 / 分层计数，来自数据）。
3. 候选卡（candidateCard.js 重写）：
   - 头部：股名 + 代码 + 行业 + 排名徽章 + 动作徽章（role_type 直译）+ AI 状态徽章（规范 0.2）
   - 指标行：现价、涨跌（pctHtml 红绿）、量化分、获利盘 winner_rate、量比
   - AI 区：仅 ai-full/ai-stale 时渲染真实 ai_summary + 结构化要点；ai-none 时渲染
     「无 AI 分析」说明 + 真实量化因子小表（chip_conc/chip_support/chip_resistance 转白话标签）
   - 执行区：仅当 execution_state 含该股时显示 买点/失效/仓位档/次日处理 四件套
   - T1 卡：factor_details 经 factorShortLabel 转译；分值全为 0 时整组显示
     「因子值异常（全 0），本期数据不可用」而非照常列出（诊断 P0）。
4. 共识/分歧区：execution_state.divergence 仅在有意义时显示（60/60 全分歧时显示口径说明）。

### review（历史战绩 —— 本次重构最重要的新页面）
数据：reviewUnified（详 3 节）。
1. KPI 横幅：累计净值收益（复利累乘 daily_comparison.avg_next_day_return_pct）、
   覆盖交易日数、推荐总条数、独立个股数、平均次日命中率、最大单日回撤。负数照实显示。
2. equityCurve：净值曲线（基期 1.0），下方 barSeries 逐日命中率。
3. 月度汇总表：按月聚合收益/命中率。
4. 全部历史推荐表（stock_rows）：列=日期/股名/代码/行业/推荐价/次日收益%/AI观点；
   筛选=策略+日期+关键字；默认显示最近 5 个交易日，「加载更多」分批渲染。
5. 重复推荐榜：次数 + **累计收益**（诊断 P1：现版隐藏了 27 次推荐 -9.16% 的事实）。
6. 口径说明块 + 「原始数据可下载验证」（链接 data/latest/review_state_unified.json）。
7. O2C/T1 战绩：样本不足时如实显示「样本仅 N 天，不足以评估」。

### market（市场行情，页内 Tab：大盘指数 | 行业热力 | 行业动作）
- 大盘指数：session_snapshot 八指数 sparkline 卡 + 晨判 ai_summary（标注生成时间）+
  morning.key_drivers/focus_sectors（白话标签）。
- 行业热力：heatGrid（全市场 + 策略聚焦两组，原两个热力页数据）。
- 行业动作：industry_actions 表（行动建议中文化）。
- marketHeatmap/strategyHeatmap/industryActions 三个 data-view 渲染同一页面预选对应 Tab。

### research（系统说明 + 策略中心，Tab：系统如何工作 | 策略中心 | 数据状态）
- 系统如何工作：五步链路图（静态产品文案）+ 四级闸门当日状态（gates 转白话）。
- 策略中心（strategy 视图预选此 Tab）：三策略卡（原理白话说明 + 当日状态 + 真实历史表现，
  无数据则「待积累」）。
- 数据状态：sources 八时间戳转「数据新鲜度」表 + system_health.checks 转白话 +
  完整免责声明全文。

## 5. 工程规约

- 所有新渲染器：`render/*.js` 纯函数（model→HTML 字符串），禁止 document/window。
- **转义纪律统一**：components.js 所有组件一律内部转义（修复 v2 的 statCard/sectionHead 混合约定）；
  唯一例外是明确以 `Html` 结尾命名的参数（如 pctHtml 产物）。
- views.js 注册表 + VIEW_META 每页唯一标题（修复三页同名）。
- app.js：通用 Tab 挂载（data-tabs 约定，hash 同步 #tab=heatmap）、筛选、主题、
  「加载更多」事件、noscript 提示。
- 测试：tests/render.test.mjs 重写——固定 fixtures 渲染 9 视图 + 战绩页数字正确性抽查
  （净值末值与 fixtures 手算一致）+ 诚实性断言（AI 空数据时禁止出现模板话术关键词、
  禁止出现「60%」「4.44」等已删除的硬编码）+ 转义/无 undefined/NaN。
- 性能预算：每页首屏 JSON ≤ 300KB（review 页 ≤ 600KB），无外链 CDN。

## 6. 管线接触面（本次仅三处小改，其余只报告不动）

1. `generate_view_summaries.py`：增加 review_track_latest.json 生成（见 3 节）。
2. `stage6_deploy_and_notify.py`：git add 之前调用 generate_view_summaries.py
   （非致命 try/except）——修复诊断 P1「_latest 摘要从下个交易日起永久过期」。
3. `generate_github_pages.py`：sentiment 模板加生成日期 + 免责声明行（不改布局）。

不动：data/latest/ 与 data/recommendation_analytics/ 所有路径字段、data.json/combined.json、
data/history/、各策略 push 链路。回滚：ops/rollback_frontend_v2.sh 依然有效（壳页引用不变，
仍是 v2/app.js 入口；v3 是 render 层与 CSS 的替换，旧 CSS 文件保留在仓库）。
