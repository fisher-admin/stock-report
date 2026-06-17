// v3/render/review.js — 历史战绩（recommendation-review.html，data-view=review）。
//
// 本页是 v3 重构最重要的页面：把系统过往全部推荐与真实结算结果如实摊开（DESIGN-V3 第 4 节）。
// 数据源：model.reviewTrack（loader 优先 data/latest/review_track_latest.json，
// 回退 review_state_unified.json，两者结构一致：strategies{} / daily_comparison[] / stock_rows[]）。
//
// 诚实性要点（DESIGN-V3 第 0 节，逐条执行）：
//   - 零硬编码业绩数字：本页所有 KPI 全部由 daily_comparison / stock_rows 现场计算；
//     strategies.*.performance 里的 0/null 占位字段一律不用。
//   - 累计收益照实显示：当前真实数据为负，按红涨绿跌规范负数显绿，不做任何粉饰。
//   - 重复推荐榜必须带平均累计收益列（不许只报次数、隐藏亏损事实）。
//   - O2C / T1 样本不足时如实写「样本不足以评估」，不留白也不粉饰。
//   - 口径说明（等权、按次日收盘、不含交易成本）+ 原始数据下载链接，方便逐条核验。
//
// 计算规范（与测试约定一致，函数均导出供 Node 验证）：
//   buildNavSeries(dailyComparison)
//     按 recommend_date 升序，净值 nav[i] = nav[i-1] * (1 + avg_next_day_return_pct/100)，基期 1.0；
//     最大回撤 = 净值序列（含基期 1.0）峰值回落的最大比例。
//   monthlyBreakdown(dailyComparison)
//     按 recommend_date 前 6 位聚合：平均日收益 / 平均命中率 / 交易日数 / 样本数。
//
// 交互约定（app.js 统一挂事件）：
//   - 全部推荐表默认展开最近 5 个交易日；更早批次预渲染为 hidden 节点，
//     按日期分组，配 [data-load-more="review-history"]（data-batch-size=5，单位是“日期组”）。
//   - 筛选：[data-filter-scope] + select[data-filter-field="strategy"|"date"] +
//     input[data-filter-field="keyword"]；行带 [data-filter-row] 与 data-strategy / data-date。

import {
  escapeHtml, safeText, formatNumber, formatPct, formatSignedPct,
  pctHtml, dateCn, friendlyTime, strategyLabel, strategyTone
} from './format.js';
import {
  badge, statCard, sectionHead, missingSection, emptySection, dataTable, elevatedCard
} from './components.js';
import { equityCurve, comboBarLine, sparkline } from './charts.js';
import { renderShell, renderHero } from './shell.js';

// ---------------------------------------------------------------------------
// 纯计算（导出供 tests/render.test.mjs 与 Node 抽查使用）
// ---------------------------------------------------------------------------

function compactDate(value) {
  return safeText(value, '').replace(/[^0-9]/g, '').slice(0, 8);
}

function finiteOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

// daily_comparison → 升序副本（按 recommend_date 数字串排序，丢弃无日期的脏行）。
export function sortDailyAscending(dailyComparison) {
  return (Array.isArray(dailyComparison) ? dailyComparison : [])
    .filter((row) => row && compactDate(row.recommend_date).length === 8)
    .slice()
    .sort((a, b) => (compactDate(a.recommend_date) < compactDate(b.recommend_date) ? -1 : 1));
}

// 净值曲线 + 累计收益 + 最大回撤（峰值回落）。基期 1.0；null 收益日跳过不复利。
export function buildNavSeries(dailyComparison) {
  const rows = sortDailyAscending(dailyComparison);
  const points = [];
  let nav = 1;
  let peak = 1;
  let maxDrawdown = 0;
  rows.forEach((row) => {
    const ret = finiteOrNull(row.avg_next_day_return_pct);
    if (ret === null) return;
    nav *= 1 + ret / 100;
    if (nav > peak) peak = nav;
    if (peak > 0) {
      const dd = (peak - nav) / peak;
      if (dd > maxDrawdown) maxDrawdown = dd;
    }
    points.push({ date: dateCn(row.recommend_date), value: nav });
  });
  const hasData = points.length > 0;
  return {
    rows,
    points,
    validDays: points.length,
    finalNav: hasData ? nav : null,
    cumulativeReturnPct: hasData ? (nav - 1) * 100 : null,
    maxDrawdownPct: hasData ? maxDrawdown * 100 : null
  };
}

// 进阶绩效指标（用于策略评价/回测对照），全部从逐日净值序列真实计算。
// 口径：等权组合、按次日收盘、不含成本、无风险利率取 0、252 日年化。
export function computePerformanceMetrics(nav) {
  const rets = (nav.rows || [])
    .map((row) => finiteOrNull(row.avg_next_day_return_pct))
    .filter((value) => value !== null)
    .map((value) => value / 100); // 小数日收益
  const n = rets.length;
  if (!n) return null;
  const ANN = 252;
  const mean = rets.reduce((sum, value) => sum + value, 0) / n;
  const variance = n > 1 ? rets.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (n - 1) : 0;
  const sd = Math.sqrt(variance);
  const finalNav = nav.finalNav != null ? nav.finalNav : rets.reduce((acc, value) => acc * (1 + value), 1);
  const annRetDec = Math.pow(finalNav, ANN / n) - 1;
  const maxDdDec = nav.maxDrawdownPct != null ? nav.maxDrawdownPct / 100 : null;
  const wins = rets.filter((value) => value > 0);
  const losses = rets.filter((value) => value < 0);
  const grossWin = wins.reduce((sum, value) => sum + value, 0);
  const grossLoss = Math.abs(losses.reduce((sum, value) => sum + value, 0));
  let maxStreak = 0;
  let cur = 0;
  rets.forEach((value) => {
    if (value < 0) { cur += 1; if (cur > maxStreak) maxStreak = cur; } else { cur = 0; }
  });
  return {
    n,
    annualizedReturnPct: annRetDec * 100,
    annualizedVolPct: sd * Math.sqrt(ANN) * 100,
    sharpe: sd > 0 ? (mean / sd) * Math.sqrt(ANN) : null,
    calmar: maxDdDec && maxDdDec > 0 ? annRetDec / maxDdDec : null,
    winRatePct: (wins.length / n) * 100,
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : null,
    avgWinPct: wins.length ? (grossWin / wins.length) * 100 : null,
    avgLossPct: losses.length ? (losses.reduce((sum, value) => sum + value, 0) / losses.length) * 100 : null,
    maxConsecLossDays: maxStreak
  };
}

// 月度汇总：[{ month:'2026-02', tradingDays, avgReturnPct, avgHitRatePct, sampleCount }]，按月升序。
export function monthlyBreakdown(dailyComparison) {
  const rows = sortDailyAscending(dailyComparison);
  const buckets = new Map();
  rows.forEach((row) => {
    const key = compactDate(row.recommend_date).slice(0, 6);
    if (!buckets.has(key)) {
      buckets.set(key, { retSum: 0, retDays: 0, hitSum: 0, hitDays: 0, sampleCount: 0 });
    }
    const bucket = buckets.get(key);
    const ret = finiteOrNull(row.avg_next_day_return_pct);
    if (ret !== null) {
      bucket.retSum += ret;
      bucket.retDays += 1;
    }
    const hit = finiteOrNull(row.next_day_hit_rate_pct);
    if (hit !== null) {
      bucket.hitSum += hit;
      bucket.hitDays += 1;
    }
    bucket.sampleCount += finiteOrNull(row.sample_count) || 0;
  });
  return Array.from(buckets.keys()).sort().map((key) => {
    const bucket = buckets.get(key);
    return {
      month: `${key.slice(0, 4)}-${key.slice(4, 6)}`,
      tradingDays: Math.max(bucket.retDays, bucket.hitDays),
      avgReturnPct: bucket.retDays ? bucket.retSum / bucket.retDays : null,
      avgHitRatePct: bucket.hitDays ? bucket.hitSum / bucket.hitDays : null,
      sampleCount: bucket.sampleCount
    };
  });
}

// 平均次日命中率：各交易日命中率的简单平均（不用 performance 里的 0/null 占位字段）。
export function averageHitRate(dailyComparison) {
  const hits = sortDailyAscending(dailyComparison)
    .map((row) => finiteOrNull(row.next_day_hit_rate_pct))
    .filter((value) => value !== null);
  if (!hits.length) return null;
  return hits.reduce((sum, value) => sum + value, 0) / hits.length;
}

// 单个策略的样本概况（O2C / T1 小样本如实呈现用）。
export function strategySampleSummary(track, strategyId) {
  const stockRows = (track.stockRows || []).filter((row) => row && row.strategy_id === strategyId);
  const dailyRows = sortDailyAscending(track.dailyComparison).filter((row) => row.strategy_id === strategyId);
  const dates = new Set(stockRows.map((row) => compactDate(row.recommend_date)).filter(Boolean));
  dailyRows.forEach((row) => dates.add(compactDate(row.recommend_date)));
  const settled = stockRows
    .map((row) => finiteOrNull(row.next_day_return_pct))
    .filter((value) => value !== null);
  const block = (track.strategies || {})[strategyId] || {};
  return {
    strategyId,
    block,
    rowCount: stockRows.length || finiteOrNull(block.row_count) || 0,
    dayCount: dates.size,
    settledCount: settled.length,
    settledAvgReturnPct: settled.length
      ? settled.reduce((sum, value) => sum + value, 0) / settled.length
      : null
  };
}

// ---------------------------------------------------------------------------
// 私有：数据归一化与小工具
// ---------------------------------------------------------------------------

function trackOf(model) {
  if (model && model.reviewTrack) return model.reviewTrack;
  const unified = (model && model.reviewUnified) || {};
  return {
    generatedAt: safeText(unified.generated_at, ''),
    tradeDate: safeText(unified.trade_date, ''),
    strategies: unified.strategies || {},
    dailyComparison: unified.daily_comparison || [],
    stockRows: unified.stock_rows || []
  };
}

function missingOf(model, key) {
  if (model && typeof model.isMissing === 'function' && model.isMissing(key)) {
    return safeText(typeof model.missingReason === 'function' ? model.missingReason(key) : '', '数据缺失');
  }
  return '';
}

function stockNameOf(row) {
  return safeText(row.stock_name || row.name, '未知股票');
}

function stockCodeOf(row) {
  const code = safeText(row.stock_code || row.code, '');
  if (code) return code;
  const ts = safeText(row.ts_code, '');
  return ts ? ts.split('.')[0] : '—';
}

function industryOf(row) {
  return safeText(row.sector_name || row.industry, '未标注');
}

// ---------------------------------------------------------------------------
// 区块 1：KPI 横幅
// ---------------------------------------------------------------------------

function kpiSection(track, nav) {
  const pre = (track.strategies || {}).prebreakout_v41 || {};
  if (!nav.validDays) {
    return `<section class="panel" id="kpi">
      ${sectionHead('核心战绩指标', '主策略「启动前夕」每日推荐组合的真实结算汇总')}
      ${emptySection('暂无可验证数据', '历史战绩需要至少一个已结算的交易日（推荐次日收盘后）才能计算，当前还没有可结算的记录。')}
    </section>`;
  }

  const range = pre.date_range || {};
  const fromDate = dateCn(range.from || (nav.rows[0] || {}).recommend_date);
  const toDate = dateCn(range.to || (nav.rows[nav.rows.length - 1] || {}).recommend_date);
  const totalRows = finiteOrNull(pre.total_rows);
  const sampleSum = nav.rows.reduce((sum, row) => sum + (finiteOrNull(row.sample_count) || 0), 0);
  const uniqueStocks = finiteOrNull(pre.unique_stock_count);
  const avgHit = averageHitRate(nav.rows);

  // 招牌横幅：六张大数字卡。三张测试锁定的卡（累计净值收益 / 最大回撤 / 覆盖交易日）
  // 必须保持 statCard 结构（回归测试按 stat-title/stat-value 取值），其余同构呈现。
  // 累计收益按真实涨跌着色（A 股红涨绿跌，负数显绿——pctHtml 已处理，并给卡片 tone 边条）。
  const cumTone = nav.cumulativeReturnPct > 0 ? 'up' : nav.cumulativeReturnPct < 0 ? 'down' : 'flat';

  const cards = [
    statCard({
      title: '累计净值收益',
      valueHtml: pctHtml(nav.cumulativeReturnPct, 2),
      note: `基期 1.0 复利累乘，期末净值 ${formatNumber(nav.finalNav, 3)}`,
      tone: cumTone
    }),
    statCard({
      title: '最大回撤',
      value: formatPct(nav.maxDrawdownPct, 1),
      note: '净值从阶段高点回落的最大幅度',
      tone: 'warn'
    }),
    statCard({
      title: '覆盖交易日',
      value: `${formatNumber(nav.validDays)} 天`,
      note: `${fromDate} 至 ${toDate}`,
      tone: 'brand'
    }),
    statCard({
      title: '推荐总条数',
      value: totalRows !== null ? `${formatNumber(totalRows)} 条` : `${formatNumber(sampleSum)} 条`,
      note: totalRows !== null ? '策略历史库累计发布' : '按每日已结算样本累计'
    }),
    statCard({
      title: '独立个股数',
      value: uniqueStocks !== null ? `${formatNumber(uniqueStocks)} 只` : '—',
      note: '去重后被推荐过的股票数量'
    }),
    statCard({
      title: '平均次日命中率',
      value: avgHit === null ? '—' : formatPct(avgHit, 1),
      note: '各交易日「次日上涨比例」的简单平均',
      tone: 'info'
    })
  ];

  return `<section class="panel panel-feature" id="kpi">
    ${sectionHead('核心战绩指标', '主策略「启动前夕」每日推荐组合的真实结算汇总，涨跌如实呈现（O2C / T1 样本见下方）')}
    <div class="stat-grid kpi-banner">${cards.join('')}</div>
  </section>`;
}

// 进阶绩效指标区：年化/波动/夏普/卡玛/胜率/盈亏比/平均盈亏/最大连亏（策略评价工具箱）。
function metricsSection(metrics) {
  if (!metrics || metrics.n < 2) {
    return `<section class="panel" id="metrics">
      ${sectionHead('进阶绩效指标（用于策略评价）', '从逐日净值序列计算，供回测对照')}
      ${emptySection('样本不足', '可结算交易日不足，暂无法计算年化、夏普等进阶指标。')}
    </section>`;
  }
  const tone = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
  const cards = [
    statCard({ title: '年化收益', valueHtml: pctHtml(metrics.annualizedReturnPct, 1), note: `按 ${formatNumber(metrics.n)} 个交易日、252 日年化`, tone: tone(metrics.annualizedReturnPct) }),
    statCard({ title: '年化波动', value: formatPct(metrics.annualizedVolPct, 1), note: '日收益标准差年化', tone: 'warn' }),
    statCard({ title: '夏普比率', value: metrics.sharpe == null ? '—' : formatNumber(metrics.sharpe, 2), note: '无风险利率取 0，越高越好', tone: 'info' }),
    statCard({ title: '卡玛比率', value: metrics.calmar == null ? '—' : formatNumber(metrics.calmar, 2), note: '年化收益 ÷ 最大回撤' }),
    statCard({ title: '日胜率', value: formatPct(metrics.winRatePct, 0), note: '组合当日收益为正的天数占比', tone: 'info' }),
    statCard({ title: '盈亏比', value: metrics.profitFactor == null ? '—' : formatNumber(metrics.profitFactor, 2), note: '盈利日收益之和 ÷ 亏损日' }),
    statCard({ title: '平均盈/亏日', valueHtml: `${metrics.avgWinPct == null ? '—' : pctHtml(metrics.avgWinPct, 2)} <span class="soft">/</span> ${metrics.avgLossPct == null ? '—' : pctHtml(metrics.avgLossPct, 2)}`, note: '盈利日均涨 / 亏损日均跌' }),
    statCard({ title: '最大连亏', value: `${formatNumber(metrics.maxConsecLossDays)} 天`, note: '净值连续回撤的最长天数', tone: 'warn' })
  ];
  return `<section class="panel panel-feature" id="metrics">
    ${sectionHead('进阶绩效指标（用于策略评价）', '从逐日净值序列计算，供回测对照 · 口径：等权组合 / 按次日收盘 / 不含成本 / 无风险利率取 0 / 252 日年化')}
    <div class="stat-grid kpi-banner">${cards.join('')}</div>
  </section>`;
}

// 分层归因区：行业 / AI 观点 / 评分段 三个维度的真实战绩（数据已在 strategies 沉淀，找出策略在哪些条件下更靠谱）。
function attributionSection(track) {
  const pre = (track.strategies || {}).prebreakout_v41 || {};
  const sectors = Array.isArray(pre.sector_stats) ? pre.sector_stats : [];
  const aiViews = Array.isArray(pre.ai_view_stats) ? pre.ai_view_stats : [];
  const buckets = Array.isArray(pre.score_bucket_stats) ? pre.score_bucket_stats : [];
  if (!sectors.length && !aiViews.length && !buckets.length) return '';

  const retCell = (value) => ({ html: finiteOrNull(value) === null ? '<span class="soft num">—</span>' : pctHtml(value, 2), align: 'right' });
  const numCell = (value, digits = 0) => ({ text: finiteOrNull(value) === null ? '—' : formatNumber(value, digits), align: 'right' });

  const sectorRows = sectors
    .slice()
    .sort((a, b) => (finiteOrNull(b.avg_next_day_return_pct) ?? -Infinity) - (finiteOrNull(a.avg_next_day_return_pct) ?? -Infinity))
    .map((s) => [
      safeText(s.sector_name, '未标注'),
      numCell(s.recommendation_count),
      numCell(s.unique_stock_count),
      retCell(s.avg_next_day_return_pct),
      retCell(s.avg_cumulative_return_pct),
      numCell(s.avg_ai_score, 1)
    ]);
  const aiRows = aiViews.map((a) => [
    safeText(a.ai_view, '—'),
    numCell(a.recommendation_count),
    retCell(a.avg_next_day_return_pct),
    retCell(a.avg_cumulative_return_pct),
    { text: finiteOrNull(a.hit_rate) === null ? '—' : formatPct(a.hit_rate, 1), align: 'right' },
    numCell(a.avg_ai_score, 1)
  ]);
  const bucketRows = buckets.map((b) => [
    safeText(b.bucket, '—'),
    numCell(b.recommendation_count),
    retCell(b.avg_next_day_return_pct),
    retCell(b.avg_cumulative_return_pct)
  ]);

  const block = (title, sub, html) => `<div class="attr-block">
    <h4 class="chart-title">${escapeHtml(title)}</h4>
    <p class="chart-sub soft">${escapeHtml(sub)}</p>
    ${html}
  </div>`;

  return `<section class="panel" id="attribution">
    ${sectionHead('分层归因（哪些条件下更靠谱）', '把历史推荐按行业 / AI 观点 / 量化评分段拆开看真实收益与命中率，为策略优化与回测提供依据')}
    ${sectors.length ? block('按行业', '仅含推荐数 ≥ 5 的行业（小样本略去），按平均次日收益从高到低排序', dataTable({
      columns: ['行业', { label: '推荐数', align: 'right' }, { label: '独立股', align: 'right' }, { label: '平均次日收益', align: 'right' }, { label: '平均累计', align: 'right' }, { label: '平均AI分', align: 'right' }],
      rows: sectorRows, emptyText: '暂无行业样本'
    })) : ''}
    ${aiViews.length ? block('按 AI 观点', 'AI 当时给「买入 / 持有 / 观望」的标的，事后真实表现如何', dataTable({
      columns: ['AI 观点', { label: '推荐数', align: 'right' }, { label: '平均次日收益', align: 'right' }, { label: '平均累计', align: 'right' }, { label: '命中率', align: 'right' }, { label: '平均AI分', align: 'right' }],
      rows: aiRows, emptyText: '暂无 AI 观点样本'
    })) : ''}
    ${buckets.length ? block('按量化评分段', '量化综合分越高的标的，事后是否真的更强', dataTable({
      columns: ['评分段', { label: '推荐数', align: 'right' }, { label: '平均次日收益', align: 'right' }, { label: '平均累计', align: 'right' }],
      rows: bucketRows, emptyText: '暂无评分段样本'
    })) : ''}
  </section>`;
}

// ---------------------------------------------------------------------------
// 区块 2：净值曲线 + 逐日柱状
// ---------------------------------------------------------------------------

// 招牌净值图：单独抬升卡片，突出净值曲线 + 水下回撤红填充。
function equityCard(nav) {
  return elevatedCard(`
    <div class="feature-chart-head">
      <h4 class="chart-title">推荐组合净值曲线</h4>
      <p class="chart-sub soft">每日组合平均次日收益按复利连乘，基期 1.0（横向虚线）；红色底纹是净值从峰值回落最深的一段——回撤如实标注，不做平滑。</p>
    </div>
    <div class="chart-block chart-block-tall">
      ${equityCurve(nav.points, { label: '推荐组合净值曲线' })}
    </div>
    <p class="chart-footnote">曲线末端的圆点为期末净值 ${escapeHtml(formatNumber(nav.finalNav, 3))}；当前累计为${nav.cumulativeReturnPct < 0 ? '负' : '正'}，照实呈现。</p>
  `, { className: 'feature-chart', tone: 'brand' });
}

function curveSection(nav) {
  if (!nav.validDays) {
    return `<section class="panel" id="curve">
      ${sectionHead('净值曲线', '每日平均次日收益按复利连乘，起点 1.0')}
      ${emptySection('暂无可绘制的净值数据', '等第一个交易日的推荐结算后，这里会出现真实的净值曲线。')}
    </section>`;
  }

  // 逐日收益柱（正红负绿）+ 命中率折线（右轴蓝）合成一图，逐日 label 走悬浮提示。
  const bars = nav.rows.map((row) => ({
    label: dateCn(row.recommend_date),
    value: finiteOrNull(row.avg_next_day_return_pct)
  }));
  const hitLine = nav.rows.map((row) => finiteOrNull(row.next_day_hit_rate_pct));

  return `<section class="panel" id="curve">
    ${sectionHead('净值曲线与逐日表现', '先看长期净值走势（招牌图），再看每个交易日的收益与命中率')}
    ${equityCard(nav)}
    <div class="chart-block chart-block-tall u-mt-2">
      <h4 class="chart-title">逐日收益与命中率</h4>
      <p class="chart-sub soft">柱子是每天推荐组合等权平均、按次日收盘结算的涨跌幅（红涨绿跌、零基线居中）；蓝色折线是当日推荐里次日上涨的比例（命中率，右轴）。悬停可看每个交易日的具体数值。</p>
      ${comboBarLine(bars, hitLine, {
        label: '逐日次日收益与命中率',
        barDigits: 2,
        lineDigits: 0,
        lineMin: 0,
        lineMax: 100
      })}
    </div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 区块 3：月度汇总
// ---------------------------------------------------------------------------

function monthlySection(nav) {
  const months = monthlyBreakdown(nav.rows);
  const table = dataTable({
    columns: [
      '月份',
      { label: '交易日数', align: 'right' },
      { label: '平均日收益', align: 'right' },
      { label: '平均命中率', align: 'right' },
      { label: '推荐条数', align: 'right' }
    ],
    rows: months.map((month) => [
      month.month,
      formatNumber(month.tradingDays),
      { html: pctHtml(month.avgReturnPct, 3), align: 'right' },
      month.avgHitRatePct === null ? '—' : formatPct(month.avgHitRatePct, 1),
      formatNumber(month.sampleCount)
    ]),
    emptyText: '暂无可汇总的月度数据'
  });
  return `<section class="panel" id="monthly">
    ${sectionHead('月度汇总', '按推荐日期所在月份聚合，平均日收益为该月各交易日收益的简单平均')}
    ${table}
  </section>`;
}

// ---------------------------------------------------------------------------
// 区块 4：全部推荐记录（按日期分组 + 筛选 + 加载更多）
// ---------------------------------------------------------------------------

const HISTORY_VISIBLE_DAYS = 5;

function historyRowHtml(row) {
  const strategyId = safeText(row.strategy_id, '');
  const date = compactDate(row.recommend_date);
  const price = finiteOrNull(row.recommend_price !== undefined && row.recommend_price !== null
    ? row.recommend_price
    : row.close);
  const nextReturn = finiteOrNull(row.next_day_return_pct);
  const nextReturnHtml = nextReturn === null
    ? '<span class="soft">待结算</span>'
    : pctHtml(nextReturn, 2);
  const cumulative = finiteOrNull(row.cumulative_return_pct);
  const aiView = safeText(row.ai_view, '—');

  return `<tr data-filter-row data-strategy="${escapeHtml(strategyId)}" data-date="${escapeHtml(date)}">
      <td><strong>${escapeHtml(stockNameOf(row))}</strong> <span class="soft num">${escapeHtml(stockCodeOf(row))}</span></td>
      <td>${escapeHtml(industryOf(row))}</td>
      <td>${badge(strategyLabel(strategyId), strategyTone(strategyId))}</td>
      <td class="num ta-r">${price === null ? '—' : escapeHtml(formatNumber(price, 2))}</td>
      <td class="num ta-r">${nextReturnHtml}</td>
      <td class="num ta-r">${cumulative === null ? '—' : pctHtml(cumulative, 2)}</td>
      <td>${escapeHtml(aiView)}</td>
    </tr>`;
}

function historyDayGroupHtml(date, rows, hidden) {
  const table = `<div class="scroll-x">
      <table class="data-table review-history-table">
        <thead><tr>
          <th scope="col">股票</th>
          <th scope="col">行业</th>
          <th scope="col">策略</th>
          <th scope="col" class="ta-r">推荐价</th>
          <th scope="col" class="ta-r">次日收益</th>
          <th scope="col" class="ta-r">至今累计</th>
          <th scope="col">AI 观点</th>
        </tr></thead>
        <tbody>${rows.map((row) => historyRowHtml(row)).join('\n')}</tbody>
      </table>
    </div>`;
  const attrs = hidden ? ' data-load-more-item="review-history" hidden' : '';
  return `<section class="review-day" data-filter-group${attrs}>
      <h4 class="review-day-head"><span class="num">${escapeHtml(dateCn(date))}</span><span class="soft">${formatNumber(rows.length)} 条推荐</span></h4>
      ${table}
    </section>`;
}

function historySection(track) {
  const stockRows = Array.isArray(track.stockRows)
    ? track.stockRows.filter((row) => row && compactDate(row.recommend_date).length === 8)
    : [];
  if (!stockRows.length) {
    return `<section class="panel" id="all-history">
      ${sectionHead('全部推荐记录', '系统每天发布的推荐逐条留档，按日期分组')}
      ${emptySection('暂无推荐明细', '推荐明细尚未生成，生成后会按日期逐条展示在这里。')}
    </section>`;
  }

  // 按日期分组（降序：最近的在最上面）。
  const groups = new Map();
  stockRows.forEach((row) => {
    const date = compactDate(row.recommend_date);
    if (!groups.has(date)) groups.set(date, []);
    groups.get(date).push(row);
  });
  const dates = Array.from(groups.keys()).sort().reverse();

  const strategyIds = Array.from(new Set(stockRows.map((row) => safeText(row.strategy_id, '')).filter(Boolean)));
  const strategyOptions = ['<option value="all">全部策略</option>']
    .concat(strategyIds.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(strategyLabel(id))}</option>`))
    .join('');
  const dateOptions = ['<option value="all">全部日期</option>']
    .concat(dates.map((date) => `<option value="${escapeHtml(date)}">${escapeHtml(dateCn(date))}</option>`))
    .join('');

  const groupsHtml = dates
    .map((date, idx) => historyDayGroupHtml(date, groups.get(date), idx >= HISTORY_VISIBLE_DAYS))
    .join('\n');
  const hiddenDayCount = Math.max(dates.length - HISTORY_VISIBLE_DAYS, 0);
  const loadMoreHtml = hiddenDayCount > 0
    ? `<button type="button" class="btn-load-more" data-load-more="review-history" data-batch-size="5">加载更早的推荐记录（还有 ${formatNumber(hiddenDayCount)} 个交易日）</button>`
    : '';

  const sub = `本页含最近 ${formatNumber(stockRows.length)} 条记录（${formatNumber(dates.length)} 个交易日），默认展开最近 ${formatNumber(Math.min(HISTORY_VISIBLE_DAYS, dates.length))} 天；最新一天的推荐要等下一个交易日收盘后才能结算收益。更早期的完整记录可在页底下载原始数据核验。`;

  return `<section class="panel" id="all-history">
    ${sectionHead('全部推荐记录', sub)}
    <div data-filter-scope>
      <div class="filter-bar" role="search">
        <label class="filter-item">策略
          <select data-filter-field="strategy" aria-label="按策略筛选">${strategyOptions}</select>
        </label>
        <label class="filter-item">日期
          <select data-filter-field="date" aria-label="按日期筛选">${dateOptions}</select>
        </label>
        <label class="filter-item">搜索
          <input type="search" data-filter-field="keyword" placeholder="股票名称 / 代码 / 行业" aria-label="按关键字筛选">
        </label>
      </div>
      <p class="filter-hint soft">筛选只作用于已展开的日期；要查更早的记录，请先点「加载更早的推荐记录」。O2C 策略的推荐价为推荐日收盘价。</p>
      ${groupsHtml}
      ${loadMoreHtml}
    </div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 区块 5：重复推荐榜（必须带累计收益列）
// ---------------------------------------------------------------------------

function repeatSection(track) {
  const pre = (track.strategies || {}).prebreakout_v41 || {};
  const repeats = Array.isArray(pre.top_repeat_recommendations) ? pre.top_repeat_recommendations : [];
  const inner = repeats.length
    ? dataTable({
      columns: [
        { label: '排名', align: 'right' },
        '股票',
        { label: '被推荐次数', align: 'right' },
        { label: '平均累计收益', align: 'right' }
      ],
      rows: repeats.slice(0, 10).map((item, idx) => [
        { html: `<span class="rank-badge${idx < 3 ? ' rank-top' : ''}">${escapeHtml(formatNumber(idx + 1))}</span>`, align: 'right' },
        { html: `<strong>${escapeHtml(safeText(item.stock_name, '未知股票'))}</strong> <span class="soft num">${escapeHtml(safeText(item.stock_code, ''))}</span>` },
        formatNumber(finiteOrNull(item.recommend_count) || 0),
        { html: pctHtml(item.avg_cumulative_return_pct, 2), align: 'right' }
      ]),
      emptyText: '暂无重复推荐统计',
      tableClass: 'repeat-table'
    })
    : emptySection('暂无重复推荐统计', '重复推荐榜需要积累一段时间的推荐记录后才会出现。');

  return `<section class="panel" id="repeat">
    ${sectionHead('重复推荐榜', '被系统反复选中的股票最能反映策略偏好；平均累计收益为该股各次推荐持有至最新价的平均涨跌幅，盈亏都如实列出')}
    ${inner}
  </section>`;
}

// ---------------------------------------------------------------------------
// 区块 6：O2C / T1 战绩（小样本如实说明）
// ---------------------------------------------------------------------------

function smallSampleCard(summary) {
  const label = strategyLabel(summary.strategyId);
  const tone = strategyTone(summary.strategyId);
  if (!summary.rowCount && !summary.dayCount) {
    return `<article class="strategy-sample-card">
      <header>${badge(label, tone)}</header>
      <p class="sample-status">暂无推荐记录</p>
      <p class="soft">该策略还没有产生过推荐，开始记录后会在这里逐日累积。</p>
    </article>`;
  }
  const settledLine = summary.settledCount > 0
    ? `已结算 ${formatNumber(summary.settledCount)} 条，平均次日收益 ${formatSignedPct(summary.settledAvgReturnPct, 2)}。`
    : '尚无已结算的次日收益（最新推荐要等下一个交易日收盘）。';
  return `<article class="strategy-sample-card">
    <header>${badge(label, tone)}</header>
    <p class="sample-status">已记录 ${formatNumber(summary.dayCount)} 个交易日、${formatNumber(summary.rowCount)} 条推荐</p>
    <p>${escapeHtml(settledLine)}</p>
    <p class="soft">样本仅 ${formatNumber(summary.dayCount)} 天，不足以评估策略有效性——先如实记录，攒够样本再下结论。</p>
  </article>`;
}

function otherStrategiesSection(track) {
  const o2c = strategySampleSummary(track, 'greenfield_o2c_v1');
  const t1 = strategySampleSummary(track, 't1_factor_v1');
  return `<section class="panel" id="other-strategies">
    ${sectionHead('O2C 与 T1 策略战绩', '这两条策略上线时间很短，样本不足以评估，这里只如实记录现状')}
    <div class="sample-card-grid">
      ${smallSampleCard(o2c)}
      ${smallSampleCard(t1)}
    </div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 区块 7：口径说明 + 原始数据下载
// ---------------------------------------------------------------------------

function methodologySection(track) {
  const generated = friendlyTime(track.generatedAt);
  const metaLine = generated !== '—'
    ? `<p class="method-meta soft">统计数据生成时间：${escapeHtml(generated)}；数据交易日：${escapeHtml(dateCn(track.tradeDate))}。</p>`
    : '';
  return `<section class="panel" id="methodology">
    ${sectionHead('统计口径与原始数据', '本页所有数字按同一口径自动计算，不做人工挑选；欢迎下载原始数据逐条核验')}
    <ul class="method-list">
      <li><strong>等权组合：</strong>每天把当日全部推荐按相同权重平均，不放大任何一只的影响。</li>
      <li><strong>按次日收盘结算：</strong>收益 = 推荐日下一个交易日的收盘价相对推荐价的涨跌幅。</li>
      <li><strong>不含交易成本：</strong>未扣除佣金、印花税与买卖滑点，实际成交的结果通常比表中数字更差。</li>
      <li><strong>净值曲线：</strong>把每天的组合平均收益按复利连乘，起点为 1.0。</li>
      <li><strong>命中率：</strong>当日推荐的股票里，次日收盘上涨的占比。</li>
    </ul>
    ${metaLine}
    <div class="download-row">
      <a class="download-link" href="./data/latest/recommendation_history.csv" download>下载历史荐股全记录（CSV 明细），可直接用于回测</a>
      <a class="download-link" href="./data/latest/strategy_evaluation.json" download>下载策略评价小结（JSON 指标）</a>
      <a class="download-link" href="./data/latest/review_state_unified.json" download>下载原始数据（JSON）</a>
    </div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 页面装配
// ---------------------------------------------------------------------------

function heroFor(model, nav) {
  let subtitle;
  if (nav.validDays) {
    const fromDate = (nav.rows[0] || {}).recommend_date;
    const toDate = (nav.rows[nav.rows.length - 1] || {}).recommend_date;
    subtitle = `系统每天发布的推荐都在这里留档，并按次日收盘真实结算：${dateCn(fromDate)} 至 ${dateCn(toDate)} 共 ${formatNumber(nav.validDays)} 个可结算交易日，累计净值收益 ${formatSignedPct(nav.cumulativeReturnPct, 2)}。涨跌都如实展示，不挑样本。`;
  } else {
    subtitle = '系统每天发布的推荐都会在这里留档，并按次日收盘真实结算。目前还没有可结算的记录，暂无可验证数据。';
  }

  const asideHtml = nav.validDays
    ? `<div class="hero-mini-chart">
        ${sparkline(nav.points.map((point) => point.value), { width: 220, height: 64, tone: 'auto', label: '净值走势' })}
        <small class="soft">${escapeHtml(`${formatNumber(nav.validDays)} 个交易日净值走势，期末 ${formatNumber(nav.finalNav, 3)}`)}</small>
      </div>`
    : '<div class="hero-mini-chart"><small class="soft">净值曲线将在第一个交易日结算后出现。</small></div>';

  return renderHero(model, '推荐全记录，按真实收盘结算', subtitle, { asideHtml });
}

export function renderReview(model) {
  const safeModel = model || {};
  const missingReason = missingOf(safeModel, 'reviewUnified');
  if (missingReason) {
    const body = [
      missingSection('历史战绩明细', missingReason),
      `<section class="panel">${sectionHead('这一页是做什么的', '')}
        <p>历史战绩页用来逐条核验系统过往推荐与真实收益。明细数据当前未能读取，等数据恢复后这里会展示净值曲线、月度汇总与全部推荐记录。</p>
      </section>`
    ].join('\n');
    return renderShell('review', safeModel, body);
  }

  const track = trackOf(safeModel);
  const nav = buildNavSeries(track.dailyComparison);
  const metrics = computePerformanceMetrics(nav);

  const body = [
    heroFor(safeModel, nav),
    kpiSection(track, nav),
    metricsSection(metrics),
    curveSection(nav),
    attributionSection(track),
    monthlySection(nav),
    historySection(track),
    repeatSection(track),
    otherStrategiesSection(track),
    methodologySection(track)
  ].join('\n');

  return renderShell('review', safeModel, body);
}
