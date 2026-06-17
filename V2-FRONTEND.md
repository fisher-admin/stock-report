# 前端 v2 架构说明（2026-06-12 重构）

一页纸说明：架构、每个视图的数据清单、如何加视图、如何回滚。

## 1. 架构

9 个 HTML 页面仍是 15 行的"壳"（`<body data-view="...">`），唯一入口改为
`assets/scripts/v2/app.js`（ES Module，零构建，GitHub Pages 直接服务）。

```
assets/scripts/v2/
├── app.js                  # 唯一接触 DOM 的模块：读 data-view → 取数 → 渲染 → 挂事件（主题/Tab/筛选）
├── data/
│   ├── manifest.js         # 每视图数据清单：required[] + optional[]（见下表）
│   ├── loader.js           # fetch：run_manifest 用 no-store；其余带 ?v=<run_id> 走浏览器缓存；含 fallbackPath
│   ├── summarize.js        # 热力大文件"只取最新一天"裁剪（与 generate_view_summaries.py 同逻辑）
│   └── model.js            # JSON → 视图模型（纯函数，Node 可测）
└── render/                 # 全部纯函数（model → HTML 字符串），无 document/window
    ├── format.js           # 文本/数字格式化、tone 映射、escapeHtml（所有插值必须过它）
    ├── components.js       # pill/badge/bar/riskGauge/statCard/三策略卡/缺失占位/行业行/热力行
    ├── shell.js            # 侧边导航（含"深钻工具"组）/顶栏/Hero
    ├── notices.js          # 午盘陈旧提示、发布闭环提示、可选数据缺失提示
    ├── candidateCard.js    # 唯一候选股卡片（合并 legacy 两个重复渲染器）+ 策略候选归一化
    ├── views.js            # data-view → 渲染函数注册表
    └── dashboard|market|candidates|review|research|strategy|marketHeatmap|strategyHeatmap|industryActions.js
```

要点：
- **CSS 不变**：继续用 `assets/styles/stock-workbench.css`（仅末尾追加了 `accent-*` / `u-mt-*` / `.section-missing` 工具类，替代原 23 处 inline style）。
- **legacy 保留**：`assets/scripts/stock-workbench.js` 与 `stock-data-hub.js` 原样在仓库里，随时可回滚。
- **缺数据不白屏**：只有 `run_manifest` + `system_verdict` 是硬依赖；其余任何文件 404/解析失败时，
  顶部出现"部分可选数据源缺失"通知，对应分区渲染 `.section-missing` 占位与原因。
- **热力大文件**：`generate_view_summaries.py`（仓库根目录）预生成 `*_latest.json`（3.8MB→91KB、824KB→6KB）。
  流水线在更新热力全量文件后、git add 前调用：`python3 generate_view_summaries.py`。
  `_latest` 缺失时 loader 自动回退全量文件并在客户端裁剪，功能不受影响。

## 2. 每视图数据清单（manifest.js 即权威定义）

required（全部视图相同）：`run_manifest.json` + `system_verdict.json`（约 12KB）。
SHELL 公共 optional（Hero/顶栏来源行）：`market_state`(68K)、`candidate_state`(28K)、`review_state`(32K)、`midday_analysis_latest`(24K)。

| 视图 (data-view) | 页面 | 专属 optional | 估算载荷 | legacy 全量 |
|---|---|---|---|---|
| dashboard | index.html | execution_state(32K) | ~196K | ~6.6MB |
| market | market-overview.html | — | ~164K | ~6.6MB |
| candidates | decision-candidates.html | execution_state(32K)、greenfield_top20(32K)、t1_factor_recommendations(28K)、research_state_t1(20K) | ~276K | ~6.6MB |
| review | recommendation-review.html | execution_state(32K) | ~196K | ~6.6MB |
| research | research-lab.html | research_state(196K)、strategy_state(4K)、execution_state(32K) | ~396K | ~6.6MB |
| strategy | strategy-vs-market.html | strategy_state(4K) | ~168K | ~6.6MB |
| marketHeatmap | market-industry-heatmap.html | market_industry_heatmap_latest(91K，回退 3.8MB 全量) | ~255K | ~6.6MB |
| strategyHeatmap | industry-heatmap.html | industry_heatmap_latest(6K，回退 824K 全量) | ~170K | ~6.6MB |
| industryActions | industry-compare.html | unified_decision_payload(32K) | ~196K | ~6.6MB |

v2 已彻底不再加载（legacy 每页都拉但任何视图均未渲染）：
`prebreakout_recommendations.json`(1.4MB)、`combined_recommendation.json`(112K)、`market_morning_brief_latest.json`(8K)。
另外除 run_manifest 外全部改用 `?v=<run_id>` 缓存，跨页/重访为 304 或本地缓存命中。

## 3. 如何新增一个视图

1. 建页面壳 `my-view.html`（复制 index.html，改 `<title>` 与 `data-view="myView"`）。
2. `data/manifest.js`：在 `SOURCES` 登记新数据文件（如有），在 `VIEW_DEPS` 加 `myView: { required: ['runManifest','systemVerdict'], optional: [...] }`。
3. `render/myView.js`：导出 `renderMyView(model)` 纯函数（用 components/shell 组件拼装；所有插值过 `escapeHtml`/format 函数；核心数据缺失时返回 `missingSection(...)`）。
4. `render/views.js`：注册到 `RENDERERS`；`render/shell.js`：在 `VIEW_META` 加标题，必要时加导航项。
5. `tests/render.test.mjs` 会自动遍历 RENDERERS 做干净度断言；为新视图补一条内容抽查。
6. 验证：`node --check` 新模块 + `node tests/render.test.mjs`。

## 4. 测试与回滚

- 测试（纯 Node，无依赖）：`node tests/render.test.mjs` —— 用 `tests/fixtures/`（真实数据快照）渲染全部视图：
  断言交易日/裁决标签/候选股名出现，无 `undefined`/`NaN`/`[object Object]`/`分析过程出错`，降级模式有占位不白屏。
- 回滚（无需 git 知识）：`bash ~/.openclaw/workspace/ops/rollback_frontend_v2.sh`
  —— 把 9 个页面壳的脚本引用换回 `assets/scripts/stock-workbench.js`，两份克隆同时处理。
- 死资产归档（未删除，仅移出）：`~/.openclaw/workspace/trash/stock-report-cleanup-20260612/`
  （js/、css/、template.html、assets/stitch|visuals|visuals-png、design-system/、verification-*.png、
  3 个 CSV、data/ai_analysis/、analysis.md、deploy.sh、.github/workflows/deploy.yml）。
  注意：`data/history/` 被 stage4_ai_publish.py 读写，**保留未动**。

## 5. 其他随手修

- sentiment.html：移除未使用的 Vue3 CDN 与 Tailwind CDN（约 3MB 外链），等价样式内联；
  legacy 生成器 generate_github_pages.py 的模板同步修正，防止旧脚本回写 CDN 标签。
- 侧边栏新增"深钻工具"组：全市场行业热力、启动前夕行业热力、统一行业动作、情绪因子报告（原先 3 个孤儿页 + 情绪页首次入导航）。
