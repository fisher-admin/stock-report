// v2/render/views.js — 视图注册表：data-view 值 → 渲染函数（全部为纯函数）。
//
// v3 信息架构（DESIGN-V3.md 第 1 节）：5 个客户页面 + 4 个旧 URL 薄壳。
// 约定导出名（视图模块由视图层实现，签名如下）：
//   dashboard.js  → renderDashboard(model)
//   market.js     → renderMarket(model, { initialTab })   // tab: 'indices' | 'heatmap' | 'strategyHeat' | 'actions'
//   candidates.js → renderCandidates(model)
//   review.js     → renderReview(model)
//   research.js   → renderResearch(model, { initialTab }) // tab: 'how' | 'strategies' | 'dataStatus'
// 旧 URL → 合并页预选 Tab（URL 兼容，内容同页）：
//   strategy(strategy-vs-market.html)            → research 页 tab='strategies'
//   marketHeatmap(market-industry-heatmap.html)  → market 页 tab='heatmap'
//   strategyHeatmap(industry-heatmap.html)       → market 页 tab='strategyHeat'
//   industryActions(industry-compare.html)       → market 页 tab='actions'
// 旧 URL 独立页（不进主导航，market 页脚链接进入）：
//   sentiment(sentiment.html)                    → renderSentiment(model)（情绪因子页）

import { renderDashboard } from './dashboard.js';
import { renderMarket } from './market.js';
import { renderCandidates } from './candidates.js';
import { renderReview } from './review.js';
import { renderResearch } from './research.js';
import { renderSentiment } from './sentiment.js';

export const RENDERERS = {
  dashboard: (model) => renderDashboard(model),
  market: (model) => renderMarket(model, {}),
  candidates: (model) => renderCandidates(model),
  review: (model) => renderReview(model),
  research: (model) => renderResearch(model, {}),
  strategy: (model) => renderResearch(model, { initialTab: 'strategies' }),
  marketHeatmap: (model) => renderMarket(model, { initialTab: 'heatmap' }),
  strategyHeatmap: (model) => renderMarket(model, { initialTab: 'strategyHeat' }),
  industryActions: (model) => renderMarket(model, { initialTab: 'actions' }),
  // 情绪因子页（sentiment.html，旧 URL 入口，不进主导航）。
  sentiment: (model) => renderSentiment(model)
};

export function rendererFor(viewKey) {
  return RENDERERS[viewKey] || RENDERERS.dashboard;
}
