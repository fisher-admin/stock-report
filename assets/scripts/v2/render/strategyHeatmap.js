// v3/render/strategyHeatmap.js — 旧 URL 薄包装（industry-heatmap.html）。
// v3 信息架构（DESIGN-V3 第 1 节）：本页合并进「市场行情」页，预选「行业热力」Tab
// （'strategyHeat' 在 market.js 内是 'heatmap' Tab 的别名，页内含「策略聚焦行业」分组）。
// 与 views.js 注册表一致：strategyHeatmap → renderMarket(model, { initialTab: 'strategyHeat' })。

import { renderMarket } from './market.js';

export function renderStrategyHeatmap(model) {
  return renderMarket(model, { initialTab: 'strategyHeat' });
}
