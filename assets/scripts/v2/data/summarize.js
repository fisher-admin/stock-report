// v2/data/summarize.js — 热力文档“只取最新交易日”摘要逻辑（纯函数，无 DOM 依赖）。
// 首选服务端预生成的 *_latest.json（由 stock-report/generate_view_summaries.py 产出）；
// 当只有全量历史大文件可用时，这里在客户端做同样的裁剪，保证视图永远只处理最新一天。

import { safeText } from '../render/format.js';

// 与 legacy stock-data-hub.latestRows 相同的筛选 + 排序逻辑。
export function latestRows(doc, dateField, latestField, rankField) {
  const latest = safeText(doc?.[latestField], '');
  const rows = Array.isArray(doc?.rows) ? doc.rows : [];
  return rows
    .filter((row) => safeText(row?.[dateField], '') === latest)
    .sort((a, b) => Number(a?.[rankField] || 9999) - Number(b?.[rankField] || 9999));
}

// 把全量历史热力文档裁剪成“仅最新一天”的轻量文档（字段结构保持不变）。
export function summarizeHeatmapDoc(doc, { dateField, latestField, rankField }) {
  if (!doc || typeof doc !== 'object') return doc;
  const rows = latestRows(doc, dateField, latestField, rankField);
  return {
    ...doc,
    rows,
    summarized: true,
    summarized_from_rows: Array.isArray(doc.rows) ? doc.rows.length : 0
  };
}

export const MARKET_HEATMAP_FIELDS = {
  dateField: 'trade_date',
  latestField: 'latest_trade_date',
  rankField: 'market_heat_rank'
};

export const STRATEGY_HEATMAP_FIELDS = {
  dateField: 'recommend_date',
  latestField: 'latest_recommend_date',
  rankField: 'heat_rank'
};

// loader 在回退到大文件时按 key 调用，确保进入 model 的文档永远是轻量形态。
export function summarizeByKey(key, doc) {
  if (key === 'marketHeatmap') return summarizeHeatmapDoc(doc, MARKET_HEATMAP_FIELDS);
  if (key === 'strategyHeatmap') return summarizeHeatmapDoc(doc, STRATEGY_HEATMAP_FIELDS);
  return doc;
}
