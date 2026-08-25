// v4/render/market.js — 市场行情合并页（market-overview.html）。视觉：Brokerage Pro / 铜金（DESIGN-V4 第 3 节）。
//
// v4 只重做呈现层：抬升式指数卡网格 + divergingBars 行业发散条 + heatGrid 行业热力 + 动作表升级。
// 完全保留 v3 的数据接线、三 Tab 信息架构、薄壳别名映射、诚实性逻辑与 renderShell 签名。
//
// 页内三 Tab：大盘指数 | 行业热力 | 行业动作。
//   renderMarket(model, { initialTab }) — initialTab 取值与 views.js 注册表一致：
//     'indices'（默认）| 'heatmap' | 'strategyHeat'（行业热力 Tab 的别名，旧策略热力页）| 'actions'
//   旧 URL（market-industry-heatmap / industry-heatmap / industry-compare）经
//   marketHeatmap.js / strategyHeatmap.js / industryActions.js 薄包装进入本页并预选 Tab。
//
// 诚实性要点（DESIGN-V3 第 0 节，仍是铁律）：
//   - 指数快照只有单日数值（bar_count=0、无时序）时渲染抬升数值卡，不造假 sparkline；
//   - 指数取数失败（close=null）显示「数据源暂时不可用」，不外露 provider/source_kind/source_error；
//   - 晨判 AI 摘要标注生成时间与所依据的行情日；ai_summary 为空时显性提示，不用模板话术填充；
//   - 行业热力两组（全市场 / 策略聚焦）口径分开说明；策略组色块是推荐组合累计收益，
//     刚入选、尚无后续行情时如实显示 0%；
//   - 行业发散条只在有真实涨跌数据时绘制；session_snapshot / market_summary 缺值时
//     仍显示「数据源暂时不可用」/ emptySection，不造假；
//   - 文件缺失用 missingSection、数组为空用 emptySection，两种状态分开解释。

import {
  escapeHtml, safeText, formatNumber, formatPct, pctHtml,
  dateCn, friendlyTime, cleanAnalysisText, strategyLabel
} from './format.js';
import {
  badge, statCard, sectionHead, missingSection, emptySection,
  dataTable, tabsBar, tabPanel
} from './components.js';
import { sparkline, heatGrid, divergingBars } from './charts.js';
import { renderShell, renderHero } from './shell.js';

// ---------------------------------------------------------------------------
// Tab 定义与旧 URL 映射
// ---------------------------------------------------------------------------

const TABS = [
  { key: 'indices', label: '大盘指数' },
  { key: 'heatmap', label: '行业热力' },
  { key: 'actions', label: '行业动作' }
];

// 'strategyHeat'（旧策略热力页）与 'heatmap' 同属「行业热力」Tab（页内含两组热力图）。
const TAB_ALIASES = { strategyHeat: 'heatmap' };

// 旧 URL 进入时用对应 VIEW_META 标题（每页唯一标题，DESIGN-V3 第 5 节）。
const SHELL_KEY_BY_INITIAL_TAB = {
  heatmap: 'marketHeatmap',
  strategyHeat: 'strategyHeatmap',
  actions: 'industryActions'
};

const TAB_GROUP = 'market';

function normalizeTab(requested) {
  const key = TAB_ALIASES[requested] || requested;
  return TABS.some((tab) => tab.key === key) ? key : 'indices';
}

// model.isMissing / missingReason 的防御性读取（fixtures 直接构造 model 时也能渲染）。
function missingHelpers(model) {
  const isMissing = typeof model.isMissing === 'function' ? model.isMissing.bind(model) : () => false;
  const missingReason = typeof model.missingReason === 'function'
    ? model.missingReason.bind(model)
    : () => '数据缺失';
  return { isMissing, missingReason };
}

// 严格数值解析：null/undefined/'' → null（注意 Number(null)===0，会把缺数据伪装成 0）。
function finiteOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

// ---------------------------------------------------------------------------
// Tab 1：大盘指数（session_snapshot 八指数 + 行业涨跌面 + AI 晨间研判）
// ---------------------------------------------------------------------------

// 八指数的展示顺序与客户名称（数据缺项时卡片仍占位，明确说明取不到）。
const INDEX_ORDER = [
  { key: 'shanghai', label: '上证指数', digits: 2 },
  { key: 'shenzhen', label: '深证成指', digits: 2 },
  { key: 'chinext', label: '创业板指', digits: 2 },
  { key: 'a50', label: '富时中国 A50', digits: 2 },
  { key: 'golden_dragon', label: '中国金龙指数', digits: 2 },
  { key: 'spx', label: '标普 500', digits: 2 },
  { key: 'nasdaq', label: '纳斯达克', digits: 2 },
  { key: 'cnh', label: '美元 / 离岸人民币', digits: 4 }
];

// 只认数据里真实存在的时序数组；不存在就返回 null（绝不构造假序列）。
function seriesOf(entry) {
  const candidates = [entry.bars, entry.closes, entry.series, entry.history, entry.trend];
  for (const arr of candidates) {
    if (Array.isArray(arr) && arr.length >= 2) return arr;
  }
  return null;
}

// 单个指数卡（抬升式 spark-card）：有时序（≥2 个点）才画 sparkline；只有单日数值就渲染数值卡。
// close 为 null（数据源失败）→ 「数据源暂时不可用」，不外露内部错误信息。
function indexCard(def, item) {
  const data = item && typeof item === 'object' ? item : {};
  const label = def.label;
  // 注意 Number(null) === 0：close 为 null/undefined/'' 必须先判空，否则失败源会被画成 0.00。
  const close = finiteOrNull(data.close);
  const change = finiteOrNull(data.change_pct);

  if (close === null) {
    return `<div class="spark-card index-card">
      <span class="spark-name">${escapeHtml(label)}</span>
      <span class="spark-value num pct-flat">—</span>
      <span class="help-text">数据源暂时不可用，本期未取到该项行情</span>
    </div>`;
  }

  const series = seriesOf(data);
  const sparkHtml = series ? sparkline(series, { width: 160, height: 36, label }) : '';
  const changeHtml = change !== null ? pctHtml(change, 2) : '<span class="num pct-flat">—</span>';

  return `<div class="spark-card index-card">
    <span class="spark-name">${escapeHtml(label)}</span>
    <span class="spark-value num">${escapeHtml(formatNumber(close, def.digits))}</span>
    <span class="index-change">较上一交易日 ${changeHtml}</span>
    ${sparkHtml ? `<div class="stat-spark">${sparkHtml}</div>` : ''}
  </div>`;
}

// 行业涨跌面：market_summary 真实统计（等权口径，与加权指数分开说明）。
function breadthSection(summary) {
  const sectorCount = finiteOrNull(summary.sector_count);
  if (sectorCount === null || sectorCount <= 0) return '';
  const ratio = finiteOrNull(summary.positive_sector_ratio);
  const cards = [
    statCard({
      title: '行业平均涨跌',
      valueHtml: pctHtml(summary.average_sector_change_pct, 2),
      note: `全市场 ${formatNumber(sectorCount)} 个行业的等权平均，口径与加权的大盘指数不同`,
      small: true
    }),
    statCard({
      title: '上涨行业占比',
      value: ratio !== null ? formatPct(ratio * 100, 1) : '—',
      note: `${formatNumber(summary.positive_sector_count)} / ${formatNumber(sectorCount)} 个行业收涨`,
      small: true
    })
  ];
  return `${sectionHead('行业涨跌面', '把全市场行业放在一起数一数，感受当日的整体氛围')}
  <div class="stat-grid breadth-grid">${cards.join('')}</div>`;
}

// 标签行：晨判的关注行业 / 关键因素（badge 内部转义）。
function badgeRow(label, items, tone) {
  const list = (Array.isArray(items) ? items : [])
    .map((item) => (typeof item === 'string' ? item : safeText(item && (item.label || item.name), '')))
    .map((text) => text.trim())
    .filter(Boolean);
  if (!list.length) return '';
  return `<div class="tag-row">
    <span class="tag-row-label">${escapeHtml(label)}</span>
    ${list.map((text) => badge(text, tone)).join('')}
  </div>`;
}

// AI 晨间研判：标注生成时间与所依据的行情日；内容为空时显性说明（不编造话术）。
function morningSection(model) {
  const morning = (model.marketState || {}).morning || {};
  const generatedAt = safeText(morning.generated_at, '');
  const basisDate = morning.market_data_trade_date || morning.trade_date || (model.marketState || {}).latest_trade_date;
  const head = sectionHead(
    'AI 晨间研判',
    generatedAt
      ? `生成于 ${friendlyTime(generatedAt)} · 依据 ${dateCn(basisDate)} 收盘行情`
      : 'AI 在开盘前对市场环境的文字研判'
  );

  const summaryText = cleanAnalysisText(morning.ai_summary);
  const adviceSegments = safeText(morning.ai_action_advice, '')
    .split('｜')
    .map((seg) => seg.trim())
    .filter(Boolean);
  // 高价值字段：盘前开盘执行计划（此前全前端未消费）。
  const playbook = (Array.isArray(morning.opening_playbook) ? morning.opening_playbook : [])
    .map((step) => cleanAnalysisText(step)).filter(Boolean);

  if (!summaryText && !adviceSegments.length && !playbook.length) {
    return head + emptySection('AI 晨间研判', '本期没有生成晨间研判内容，请以上方的量化指标为准。');
  }

  return `${head}
  <section class="panel morning-panel">
    ${summaryText ? `<p class="morning-summary">${escapeHtml(summaryText)}</p>` : ''}
    ${adviceSegments.length ? `<ul class="morning-advice">${adviceSegments.map((seg) => `<li>${escapeHtml(seg)}</li>`).join('')}</ul>` : ''}
    ${playbook.length ? `<div class="morning-playbook"><div class="panel-title">今日开盘计划</div><ol class="playbook-list">${playbook.map((step) => `<li>${escapeHtml(step)}</li>`).join('')}</ol></div>` : ''}
    ${badgeRow('今晨关键因素', morning.key_drivers, 'info')}
    ${badgeRow('今日关注行业', morning.focus_sectors, 'brand')}
  </section>`;
}

function renderIndicesTab(model) {
  const { isMissing, missingReason } = missingHelpers(model);
  if (isMissing('marketState')) {
    return missingSection('大盘指数与晨间研判', missingReason('marketState'));
  }

  const marketState = model.marketState || {};
  const snapshot = marketState.session_snapshot || {};
  const parts = [];

  parts.push(sectionHead(
    '大盘指数',
    `${dateCn(marketState.latest_trade_date)} 收盘快照，涨跌为较上一交易日的变化`
  ));
  if (!Object.keys(snapshot).length) {
    parts.push(emptySection('大盘指数', '本期没有生成指数快照数据。'));
  } else {
    parts.push(`<div class="spark-grid index-grid">${INDEX_ORDER.map((def) => indexCard(def, snapshot[def.key])).join('')}</div>`);
  }

  parts.push(breadthSection(marketState.market_summary || {}));
  parts.push(morningSection(model));
  return parts.filter(Boolean).join('\n');
}

// ---------------------------------------------------------------------------
// Tab 2：行业热力（全市场 + 策略聚焦两组，原两个热力页合并）
// ---------------------------------------------------------------------------

const HEAT_LIMIT = 40;
const DIVERGE_TOP = 8; // 发散条上下各取多少个行业

// 热力图例（红涨绿跌的色阶说明，全 CSS 变量随主题）。
function heatLegend() {
  return `<div class="heat-legend" aria-hidden="true">
    <span class="heat-legend-item"><i class="heat-swatch heat-swatch-up"></i>上涨</span>
    <span class="heat-legend-scale" title="颜色越深，幅度越大">浅 → 深 = 幅度由小到大</span>
    <span class="heat-legend-item"><i class="heat-swatch heat-swatch-down"></i>下跌</span>
  </div>`;
}

// 从全市场热力行里挑出涨幅最强 / 跌幅最深各 DIVERGE_TOP 个，拼成发散条数据（真实数据，缺值跳过）。
function divergeItemsFromRows(rows) {
  const valued = rows
    .map((row) => {
      const pct = finiteOrNull(row.avg_pct_chg ?? row.pct ?? row.change_pct);
      return pct === null ? null : { name: safeText(row.industry_name || row.name || row.industry, '未标注'), pct };
    })
    .filter(Boolean);
  if (valued.length < 2) return [];
  const sorted = valued.slice().sort((a, b) => b.pct - a.pct);
  const tops = sorted.slice(0, DIVERGE_TOP);
  const bottoms = sorted.slice(-DIVERGE_TOP).filter((b) => !tops.includes(b));
  return [...tops, ...bottoms.reverse()];
}

function marketHeatBlock(model) {
  const { isMissing, missingReason } = missingHelpers(model);
  const doc = model.marketHeatmap || {};
  let rows = Array.isArray(model.marketHeatmapLatestRows) ? model.marketHeatmapLatestRows : [];
  const head = sectionHead(
    '全市场行业热力',
    `${dateCn(doc.latest_trade_date || (model.marketState || {}).latest_trade_date)} 收盘统计 · 色块为行业当日平均涨跌幅，红涨绿跌、颜色越深幅度越大`
  );

  // 主热力文件缺失时，退回 market_state 的 top/bottom 行业（同口径 avg_pct_chg），如实标注来源。
  let usedFallback = false;
  if (isMissing('marketHeatmap') || !rows.length) {
    const top = Array.isArray((model.marketState || {}).top_market_sectors) ? model.marketState.top_market_sectors : [];
    const bottom = Array.isArray((model.marketState || {}).bottom_market_sectors) ? model.marketState.bottom_market_sectors : [];
    const merged = [...top, ...bottom].map((s) => ({
      industry_name: s.industry,
      avg_pct_chg: s.avg_pct_chg,
      stock_count: s.stock_count,
      trend_signal: s.trend_signal
    }));
    if (merged.length) {
      rows = merged;
      usedFallback = true;
    } else if (isMissing('marketHeatmap')) {
      return head + missingSection('全市场行业热力', missingReason('marketHeatmap'));
    } else {
      return head + emptySection('全市场行业热力', '热力数据文件已生成，但最新交易日没有可展示的行业行情。');
    }
  }

  const cells = rows.map((row) => ({
    name: row.industry_name,
    pct: row.avg_pct_chg,
    count: row.stock_count,
    note: row.trend_signal ? `趋势：${safeText(row.trend_signal)}` : ''
  }));

  // 发散条：领涨 / 领跌行业一眼对照（强弱对比，红右绿左）。
  const divergeItems = divergeItemsFromRows(rows);
  const divergeBlock = divergeItems.length
    ? `<div class="chart-block diverge-block">
        <div class="block-caption">领涨与领跌行业（红向右、绿向左，长度代表幅度）</div>
        ${divergingBars(divergeItems, { label: '全市场领涨领跌行业' })}
      </div>`
    : '';

  const overflowNote = rows.length > HEAT_LIMIT
    ? `<p class="chart-footnote">共 ${formatNumber(rows.length)} 个行业，按热度排名展示前 ${HEAT_LIMIT} 个；鼠标悬停色块可看具体涨跌与样本数。</p>`
    : '';
  const fallbackNote = usedFallback
    ? '<p class="chart-footnote">行业热力主数据暂未读取到，以上为当日市场状态文件中的领涨/领跌行业（同口径平均涨跌幅）。</p>'
    : '';

  return `<section class="panel" aria-label="全市场行业热力">
    ${head}
    ${divergeBlock}
    <div class="chart-block">
      ${heatLegend()}
      ${heatGrid(cells, { limit: HEAT_LIMIT, label: '全市场行业热力图' })}
    </div>
    ${overflowNote}
    ${fallbackNote}
    <p class="chart-footnote">个股级市场云图（含候选股金色高亮）：<a href="http://localhost:3710/" target="_blank" rel="noopener">打开 A 股热力图</a>（本地 Next.js 服务，候选股描边由 candidates.json 驱动）。</p>
  </section>`;
}

function strategyHeatBlock(model) {
  const { isMissing, missingReason } = missingHelpers(model);
  const doc = model.strategyHeatmap || {};
  let rows = Array.isArray(model.strategyHeatmapLatestRows) ? model.strategyHeatmapLatestRows : [];
  const stratName = strategyLabel(safeText(doc.strategy_id, 'prebreakout_v41'));
  const head = sectionHead(
    '策略聚焦行业',
    `「${stratName}」策略 ${dateCn(doc.latest_recommend_date || (model.marketState || {}).latest_trade_date)} 入选个股的行业分布 · 色块为各行业推荐组合的平均累计收益，刚入选、尚无后续行情时如实显示 0%`
  );

  // 策略热力文件缺失时退回 market_state.top_strategy_sectors（同口径累计收益），如实标注来源。
  let usedFallback = false;
  if (isMissing('strategyHeatmap') || !rows.length) {
    const top = Array.isArray((model.marketState || {}).top_strategy_sectors) ? model.marketState.top_strategy_sectors : [];
    const merged = top.map((s) => ({
      sector_name: s.industry,
      avg_cumulative_return_pct: s.avg_cumulative_return_pct,
      recommendation_count: s.recommendation_count,
      trend_signal: s.trend_signal
    }));
    if (merged.length) {
      rows = merged;
      usedFallback = true;
    } else if (isMissing('strategyHeatmap')) {
      return `<section class="panel" aria-label="策略聚焦行业">${head}${missingSection('策略聚焦行业', missingReason('strategyHeatmap'))}</section>`;
    } else {
      return `<section class="panel" aria-label="策略聚焦行业">${head}${emptySection('策略聚焦行业', '最近一个推荐日策略没有形成行业聚焦。')}</section>`;
    }
  }

  const cells = rows.map((row) => ({
    name: row.sector_name,
    pct: row.avg_cumulative_return_pct,
    count: row.recommendation_count,
    note: row.trend_signal ? `趋势：${safeText(row.trend_signal)}` : ''
  }));

  const fallbackNote = usedFallback
    ? '<p class="chart-footnote">策略热力主数据暂未读取到，以上为当日市场状态文件中的策略聚焦行业（同口径平均累计收益）。</p>'
    : '';

  return `<section class="panel" aria-label="策略聚焦行业">
    ${head}
    <div class="chart-block">${heatGrid(cells, { limit: HEAT_LIMIT, label: '策略聚焦行业热力图' })}</div>
    ${fallbackNote}
  </section>`;
}

function renderHeatmapTab(model) {
  return [marketHeatBlock(model), strategyHeatBlock(model)].join('\n');
}

// ---------------------------------------------------------------------------
// Tab 3：行业动作（unified_decision_payload.industry_actions，动作中文化）
// ---------------------------------------------------------------------------

const KIND_LABELS = {
  market_only: '市场热度入选',
  strategy_only: '策略推荐入选',
  overlap: '市场与策略共振'
};

function kindLabel(kind) {
  const key = safeText(kind, '').trim();
  return KIND_LABELS[key] || (key ? '其他来源' : '—');
}

// 动作归一化：数据已是中文（增配/观察/回避），同时兼容英文枚举；空值显示 '—'，不替客户编动作。
function actionInfo(raw) {
  const text = safeText(raw, '').trim();
  if (text === '增配' || /^(add|increase|overweight)$/i.test(text)) return { label: '增配', tone: 'ok' };
  if (text === '回避' || /^(avoid|reduce|underweight)$/i.test(text)) return { label: '回避', tone: 'bad' };
  if (text === '观察' || /^(watch|observe|hold)$/i.test(text)) return { label: '观察', tone: 'info' };
  if (!text) return { label: '—', tone: 'flat' };
  return { label: text, tone: 'info' };
}

function rankText(value) {
  const num = finiteOrNull(value);
  return num === null ? '—' : `第 ${Math.round(num)}`;
}

// 证券代码归一化（去交易所后缀），用于动作行内代表股展示。
function normCode(code) {
  return safeText(code, '').trim().toUpperCase().replace(/\.(SH|SZ|BJ)$/i, '');
}

// 行内「代表个股」小标签：取动作里的市场热门股 / 策略候选股前若干，带涨跌色（真实数据，缺则不渲染）。
function hotStockChips(item) {
  const list = Array.isArray(item.market_hot_stocks) && item.market_hot_stocks.length
    ? item.market_hot_stocks
    : (Array.isArray(item.strategy_candidates) ? item.strategy_candidates : []);
  const chips = list.slice(0, 3).map((s) => {
    const code = normCode(s.ts_code || s.code || s.stock_code);
    const name = safeText(s.name || s.stock_name, '').trim();
    const pct = finiteOrNull(s.pct_chg);
    const labelText = name || code || '—';
    const pctPart = pct !== null ? ` <span class="num ${pct > 0 ? 'pct-up' : pct < 0 ? 'pct-down' : 'pct-flat'}">${pct > 0 ? '+' : ''}${pct.toFixed(1)}%</span>` : '';
    return `<span class="mini-stock">${escapeHtml(labelText)}${pctPart}</span>`;
  });
  if (!chips.length) return '—';
  return `<div class="action-stocks">${chips.join('')}</div>`;
}

function renderActionsTab(model) {
  const { isMissing, missingReason } = missingHelpers(model);
  const unified = model.unified || {};
  let items = Array.isArray(unified.industry_actions) ? unified.industry_actions : [];
  let usedFallback = false;

  // unified 载荷缺失时回退 market_state.industry_actions（字段结构一致），并如实标注来源。
  if (!items.length && isMissing('unified')) {
    const fallback = Array.isArray(model.industryActions) ? model.industryActions : [];
    if (fallback.length) {
      items = fallback;
      usedFallback = true;
    } else {
      return missingSection('行业动作', missingReason('unified'));
    }
  }

  const head = sectionHead(
    '行业动作参考',
    `把全市场行业热度与策略推荐两个视角放在一起对照后，给出的行业层面参考（共 ${formatNumber(items.length)} 个行业）。这是行业观察，不是个股买卖指令。`
  );

  if (!items.length) {
    return head + emptySection('行业动作参考', '本期没有生成行业动作建议。');
  }

  const table = dataTable({
    columns: [
      '行业',
      '入选原因',
      '参考动作',
      { label: '市场热度名次', align: 'right' },
      { label: '策略热度名次', align: 'right' },
      '趋势',
      '代表个股',
      '一句话说明'
    ],
    rows: items.map((item) => {
      const action = actionInfo(item.action);
      return [
        safeText(item.industry, '—'),
        kindLabel(item.kind),
        { html: badge(action.label, action.tone) },
        rankText(item.market_rank),
        rankText(item.strategy_rank),
        safeText(item.trend_signal, '—'),
        { html: hotStockChips(item), align: 'left' },
        safeText(item.action_summary || item.reason, '—')
      ];
    }),
    emptyText: '本期没有生成行业动作建议',
    tableClass: 'industry-actions-table'
  });

  const fallbackNote = usedFallback
    ? '<p class="chart-footnote">行业动作主数据暂未读取到，以上内容来自当日市场状态文件中的同口径备份。</p>'
    : '';
  return `<section class="panel" aria-label="行业动作参考">${head}${table}${fallbackNote}</section>`;
}

// ---------------------------------------------------------------------------
// 页面入口
// ---------------------------------------------------------------------------

const TAB_RENDERERS = {
  indices: renderIndicesTab,
  heatmap: renderHeatmapTab,
  actions: renderActionsTab
};

function heroSubtitle(model) {
  const morning = (model.marketState || {}).morning || {};
  const lead = safeText(morning.ai_action_advice, '').split('｜')[0].trim();
  return lead || '按「大盘指数 → 行业热力 → 行业动作」三个角度查看当前市场环境。';
}

export function renderMarket(model, opts = {}) {
  const requested = safeText(opts.initialTab, '');
  const activeTab = normalizeTab(requested);
  const shellKey = SHELL_KEY_BY_INITIAL_TAB[requested] || 'market';

  const panels = TABS.map((tab) => tabPanel(
    tab.key,
    TAB_RENDERERS[tab.key](model),
    { active: tab.key === activeTab, groupId: TAB_GROUP }
  )).join('\n');

  const body = `
    ${renderHero(model, '大盘与行业环境', heroSubtitle(model), { showGauge: true })}
    ${tabsBar(TABS, activeTab, { groupId: TAB_GROUP })}
    ${panels}
    <section class="related-links">
      ${sectionHead('延伸阅读：情绪因子报告', '独立生成的市场情绪参考，不参与本页的行业判断', { href: './sentiment.html', label: '打开情绪因子报告' })}
    </section>
  `;

  return renderShell(shellKey, model, body);
}
