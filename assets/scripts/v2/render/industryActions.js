// v3/render/industryActions.js — 旧 URL 薄包装（industry-compare.html）。
// v3 信息架构（DESIGN-V3 第 1 节）：本页合并进「市场行情」页，预选「行业动作」Tab。
// 与 views.js 注册表一致：industryActions → renderMarket(model, { initialTab: 'actions' })。

import { renderMarket } from './market.js';

export function renderIndustryActions(model) {
  return renderMarket(model, { initialTab: 'actions' });
}
