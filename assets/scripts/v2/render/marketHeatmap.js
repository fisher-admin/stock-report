// v3/render/marketHeatmap.js — 旧 URL 薄包装（market-industry-heatmap.html）。
// v3 信息架构（DESIGN-V3 第 1 节）：本页合并进「市场行情」页，预选「行业热力」Tab。
// 与 views.js 注册表一致：marketHeatmap → renderMarket(model, { initialTab: 'heatmap' })。

import { renderMarket } from './market.js';

export function renderMarketHeatmap(model) {
  return renderMarket(model, { initialTab: 'heatmap' });
}
