// v4/render/strategy.js — 旧 URL 薄包装（strategy-vs-market.html，data-view="strategy"）。
//
// DESIGN-V3 第 1 节（IA 不变）：旧页面保持 URL 可访问，但内容合并进「系统说明」页并预选「策略中心」Tab。
// views.js 注册表已直接调用 renderResearch(model, { initialTab: 'strategies' })；
// 本文件保留同义导出，供任何旧引用继续工作。除此之外不渲染任何独立内容
// （v2 的设计自评文案与硬编码业绩数字已随旧实现一并删除；v4 重做仅改 research 页呈现层，本壳不变）。

import { renderResearch } from './research.js';

export function renderStrategy(model) {
  return renderResearch(model, { initialTab: 'strategies' });
}
