// v4/render/s3Watch.js — S3 分时形态 · top-20 观察名单（独立页 s3-watch.html，data-view="s3Watch"）。
//
// 导出：renderS3Watch(model) —— 纯函数（model → 整页 HTML 字符串），无 document/window，Node 可执行。
// 数据来源：model.s3Watchlist（data/latest/s3_watchlist.json）。
//   缺失 / 空 → 整页退占位（missingSection），绝不编造，其余站点不受影响。
//
// 结构（DESIGN-V3/V4 视觉体系 + 诚实性铁律，逐条落实）：
//   1. Hero：标题 + 醒目诚实横幅（honesty_banner 原文）+ 冻结签名前 8 位 + 60 笔进度条；
//   2. 今日名单表：rank / 代码 / 名称 / 格 / 尾盘强度 / 状态；
//      pending 清晰标「周一(trade_date)交易，收盘后自动结算」，不显示任何预估收益；
//      settled 显示 gap/o2c/net_med/net_p75（红涨绿跌，负值同等醒目）；miss 显示 miss_reason；
//   3. 每日表现：累计净值双线内联 SVG（cum_med 与 cum_p75，p75 大概率水下如实画）+ 逐日表；
//   4. 累计统计卡：n_days / hit_days_pct / cum_net(med 与 p75 并排) / max_drawdown / progress_60；
//   5. 回测背景折叠区：research / holdout_second_look / p75_warning，全部标「样本内 / 二次看，非前瞻」。
//
// 诚实红线（DESIGN-V3 第 0 节）：
//   - 无「可部署 / 买入」字样；负值与正值同等醒目（A股红涨绿跌，负数 pctHtml 显绿）；
//   - 所有回测数字标「样本内」；pending 不显示任何预估收益；
//   - 数据缺字段一律防御（?? / Number.isFinite），显示 '—' 或占位，不编造。

import {
  escapeHtml, safeText, formatNumber, formatPct, pctHtml, dateCn
} from './format.js';
import {
  badge, sectionHead, missingSection, emptySection, elevatedCard, statCard, dataTable
} from './components.js';
import { renderShell, renderHero } from './shell.js';

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

function finiteOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

// 冻结签名前 8 位（不足 8 位原样返回；缺失 → ''）。
function signaturePrefix(sig) {
  const text = safeText(sig, '');
  return text ? text.slice(0, 8) : '';
}

// 「1/60」→ { n, total, pct }，防御非法格式。
function parseProgress(raw) {
  const text = safeText(raw, '');
  const match = text.match(/^(\d+)\s*\/\s*(\d+)$/);
  if (!match) return { n: null, total: 60, pct: 0, text: text || '—' };
  const n = Number(match[1]);
  const total = Number(match[2]) > 0 ? Number(match[2]) : 60;
  const pct = Math.max(0, Math.min(100, Math.round((n / total) * 100)));
  return { n, total, pct, text: `${formatNumber(n)} / ${formatNumber(total)} 笔` };
}

// 状态徽章：pending 待结算 / settled 已结算 / miss 未成交。
function statusBadge(status) {
  if (status === 'settled') return badge('已结算', 'ok');
  if (status === 'miss') return badge('未成交', 'flat');
  return badge('待结算', 'warn');
}

// ---------------------------------------------------------------------------
// 60 笔进度条（复用剧本引擎 wts-progress 视觉；负值/进度都如实）
// ---------------------------------------------------------------------------

function progressBar(progressRaw, { threshold = 60 } = {}) {
  const p = parseProgress(progressRaw);
  const total = Number.isFinite(threshold) && threshold > 0 ? threshold : p.total;
  return `<div class="wts-progress s3-progress" role="img" aria-label="前瞻审判进度 ${escapeHtml(p.text)}，审判门槛 ${escapeHtml(String(total))} 笔">
    <div class="wts-progress-top">
      <span class="wts-progress-label">60 笔审判进度</span>
      <span class="wts-progress-val num">${escapeHtml(p.text)}</span>
    </div>
    <div class="wts-progress-track">
      <div class="wts-progress-fill" style="width:${p.pct}%"></div>
      <span class="wts-progress-goal" aria-hidden="true"></span>
    </div>
    <div class="wts-progress-scale" aria-hidden="true">
      <span>0</span><span>${escapeHtml(String(total))} · 可复议门槛</span>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// 累计净值双线图（内联 SVG，无外链；双主题走 CSS 变量；负值同等醒目）
// cum_med 与 cum_p75 两条线；p75 大概率在水下（负值），如实画在零轴以下。
// 值语义：累计净收益百分比（daily_series 逐日 cum_med_pct / cum_p75_pct，单位 %）。
// ---------------------------------------------------------------------------

const CHART_GRID = 'var(--line, #3a3225)';
const CHART_AXIS = 'var(--ink-3, var(--text-3, #8f8674))';
const MED_COLOR = 'var(--accent, #5b8def)'; // 中位口径：冷蓝主线（与命中率折线同族，中性不诱导）
const P75_COLOR = 'var(--brand, #c0883a)'; // 保守 p75 口径：铜金，醒目区别于中位线

function fmt(num, digits = 2) {
  return Number(num).toFixed(digits);
}

function cumulativeDualLine(series, opts = {}) {
  const { width = 720, height = 260, emptyText = '暂无可绘制的累计净值数据' } = opts;
  const rows = Array.isArray(series) ? series : [];
  // 逐日抽取 (label, med, p75)，任一为 null 的点跳过绘制但保留其它口径。
  const points = rows.map((row) => ({
    label: safeText(row && row.signal_date, ''),
    med: finiteOrNull(row && row.cum_med_pct),
    p75: finiteOrNull(row && row.cum_p75_pct)
  }));
  const medVals = points.map((p) => p.med).filter((v) => v !== null);
  const p75Vals = points.map((p) => p.p75).filter((v) => v !== null);
  const allVals = medVals.concat(p75Vals);
  if (points.length < 1 || allVals.length < 1) {
    return `<div class="chart-empty" role="note">${escapeHtml(emptyText)}</div>`;
  }

  const padL = 46; const padR = 58; const padT = 20; const padB = 30;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  // 值域始终包含 0（零轴），确保水下部分如实显示；单点时给一点余量避免除零。
  let min = Math.min(0, ...allVals);
  let max = Math.max(0, ...allVals);
  if (max - min < 1e-9) { min -= 1; max += 1; }
  const span = (max - min) || 1;
  // 单点时 x 固定居中；多点均分。
  const denom = points.length > 1 ? points.length - 1 : 1;
  const x = (i) => (points.length > 1 ? padL + (innerW * i) / denom : padL + innerW / 2);
  const y = (v) => padT + innerH * (1 - (v - min) / span);
  const zeroY = y(0);

  // 单口径折线 + 末点标注（值为 null 的点断开）。单点退化为一个圆点。
  function lineFor(getVal, color, name) {
    const segs = [];
    let cur = [];
    points.forEach((p, i) => {
      const v = getVal(p);
      if (v === null) { if (cur.length) { segs.push(cur); cur = []; } return; }
      cur.push(`${fmt(x(i), 1)},${fmt(y(v), 1)}`);
    });
    if (cur.length) segs.push(cur);
    if (!segs.length) return '';
    const polylines = segs
      .map((seg) => seg.length > 1
        ? `<polyline points="${seg.join(' ')}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"></polyline>`
        : `<circle cx="${seg[0].split(',')[0]}" cy="${seg[0].split(',')[1]}" r="3.2" fill="${color}"></circle>`)
      .join('\n    ');
    // 末点标注：找最后一个有效点。
    let lastIdx = -1; let lastVal = null;
    for (let i = points.length - 1; i >= 0; i -= 1) {
      const v = getVal(points[i]);
      if (v !== null) { lastIdx = i; lastVal = v; break; }
    }
    const endMark = lastIdx >= 0
      ? `<circle cx="${fmt(x(lastIdx), 1)}" cy="${fmt(y(lastVal), 1)}" r="3.4" fill="${color}" stroke="var(--card, var(--surface, #211c15))" stroke-width="1.4"></circle>
    <text x="${fmt(Math.min(x(lastIdx) + 8, width - 4), 1)}" y="${fmt(y(lastVal) + 4, 1)}" class="chart-end-label" fill="${color}">${fmt(lastVal, 2)}%</text>`
      : '';
    return `<g role="presentation" aria-label="${escapeHtml(name)}">${polylines}\n    ${endMark}</g>`;
  }

  // 网格（3 条）。
  const grid = [0.25, 0.5, 0.75]
    .map((f) => {
      const gy = padT + innerH * f;
      return `<line x1="${padL}" y1="${fmt(gy, 1)}" x2="${width - padR}" y2="${fmt(gy, 1)}" stroke="${CHART_GRID}" stroke-width="1" opacity="0.4"></line>`;
    })
    .join('\n    ');

  const medLast = medVals.length ? medVals[medVals.length - 1] : null;
  const p75Last = p75Vals.length ? p75Vals[p75Vals.length - 1] : null;
  const aria = `累计净值双线图，中位口径最新 ${medLast === null ? '暂无' : `${fmt(medLast, 2)}%`}，`
    + `保守p75口径最新 ${p75Last === null ? '暂无' : `${fmt(p75Last, 2)}%`}，共 ${points.length} 个信号日。`;

  const firstLabel = points[0] ? dateCn(points[0].label) : '';
  const lastLabel = points[points.length - 1] ? dateCn(points[points.length - 1].label) : '';

  return `<svg class="chart-equity s3-dualline" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    ${grid}
    <line x1="${padL}" y1="${fmt(zeroY, 1)}" x2="${width - padR}" y2="${fmt(zeroY, 1)}" stroke="${CHART_AXIS}" stroke-width="1" stroke-dasharray="4 4" opacity="0.85"></line>
    <text x="${padL - 6}" y="${fmt(zeroY + 3.5, 1)}" text-anchor="end" class="chart-axis-text">0%</text>
    <text x="${padL - 6}" y="${padT + 4}" text-anchor="end" class="chart-axis-text">${fmt(max, 2)}%</text>
    <text x="${padL - 6}" y="${padT + innerH + 4}" text-anchor="end" class="chart-axis-text">${fmt(min, 2)}%</text>
    ${lineFor((p) => p.med, MED_COLOR, '中位口径累计净值')}
    ${lineFor((p) => p.p75, P75_COLOR, '保守p75口径累计净值')}
    ${firstLabel ? `<text x="${padL}" y="${height - 8}" class="chart-axis-text">${escapeHtml(firstLabel)}</text>` : ''}
    ${(lastLabel && lastLabel !== firstLabel) ? `<text x="${width - padR}" y="${height - 8}" text-anchor="end" class="chart-axis-text">${escapeHtml(lastLabel)}</text>` : ''}
  </svg>`;
}

// 图例（双主题走 CSS 变量；med 冷蓝 / p75 铜金）。
function dualLineLegend() {
  return `<div class="s3-legend" aria-hidden="true">
    <span class="s3-legend-item"><span class="s3-legend-swatch" style="background:${MED_COLOR}"></span>中位滑点口径（cum_med）</span>
    <span class="s3-legend-item"><span class="s3-legend-swatch" style="background:${P75_COLOR}"></span>保守 p75 成本口径（cum_p75）</span>
  </div>`;
}

// ---------------------------------------------------------------------------
// 1. Hero
// ---------------------------------------------------------------------------

// Hero 左主区：醒目诚实横幅（原文）+ 标签行（非买入 / 60 笔审判制）。
function heroBody(data) {
  const banner = safeText(data.honesty_banner, '');
  const bannerHtml = banner
    ? `<div class="s3-honesty-banner" role="note">
        <span class="s3-honesty-icon" aria-hidden="true">!</span>
        <p>${escapeHtml(banner)}</p>
      </div>`
    : '';
  const metaRow = `<div class="s3-hero-meta">
      ${badge('研究观察 · 非买入建议', 'warn')}
      ${badge('60 笔审判制', 'flat')}
    </div>`;
  return `${bannerHtml}${metaRow}`;
}

// Hero 右侧焦点面板：冻结签名前 8 位 + 60 笔审判进度条（填满 aside 列，避免空栏）。
function heroAside(data) {
  const sig = signaturePrefix(data.frozen_cell_signature);
  const cum = (data && typeof data.cumulative === 'object' && data.cumulative) || {};
  return `<div class="s3-hero-panel">
    <div class="s3-hero-panel-title">前瞻审判席</div>
    ${sig ? `<div class="s3-sig">冻结签名 <code class="num">${escapeHtml(sig)}</code></div>` : ''}
    ${progressBar(cum.progress_60)}
    <p class="s3-hero-panel-note">满 60 笔样本外记录后触发正式复议；未满前只观察、不下结论。</p>
  </div>`;
}

// ---------------------------------------------------------------------------
// 2. 今日名单表
// ---------------------------------------------------------------------------

function todayListSection(data) {
  const list = Array.isArray(data.latest_top20) ? data.latest_top20 : [];
  const signalDate = dateCn(data.latest_signal_date);
  const head = sectionHead(
    '今日名单 · top-20',
    `信号日 ${signalDate}。按尾盘强度（late_strength）升序取前 20，最超卖优先——研究观察，非买入建议。`
  );
  if (!list.length) {
    return `${head}${emptySection('今日名单', '本期未读取到 top-20 观察名单内容。')}`;
  }

  const rows = list.map((item) => {
    const status = safeText(item && item.status, 'pending');
    const rank = finiteOrNull(item && item.rank);
    const code = safeText(item && item.ts_code, '—');
    const name = safeText(item && item.name, '') || '（未获取名称）';
    const cell = safeText(item && item.cell, '—');
    const late = finiteOrNull(item && item.late_strength_pct);

    // 状态列：pending / settled / miss 三分支，pending 绝不显示任何预估收益。
    let statusCellHtml;
    if (status === 'settled') {
      const gap = pctHtml(item && item.gap_pct, 2);
      const o2c = pctHtml(item && item.o2c_pct, 2);
      const netMed = pctHtml(item && item.net_med_pct, 2);
      const netP75 = pctHtml(item && item.net_p75_pct, 2);
      statusCellHtml = `${statusBadge('settled')}
        <div class="s3-settle-grid">
          <span>跳空 ${gap}</span>
          <span>开收 ${o2c}</span>
          <span>净·中位 ${netMed}</span>
          <span>净·p75 ${netP75}</span>
        </div>`;
    } else if (status === 'miss') {
      const reason = safeText(item && item.miss_reason, '未提供原因');
      statusCellHtml = `${statusBadge('miss')}<div class="s3-miss-reason soft">${escapeHtml(reason)}</div>`;
    } else {
      const td = dateCn(item && item.trade_date);
      statusCellHtml = `${statusBadge('pending')}<div class="s3-pending-note soft">周一（${escapeHtml(td)}）交易，收盘后自动结算</div>`;
    }

    return [
      { html: `<span class="num s3-rank">${rank === null ? '—' : escapeHtml(String(rank))}</span>` },
      { html: `<span class="num">${escapeHtml(code)}</span>` },
      { html: `<span class="s3-name">${escapeHtml(name)}</span>` },
      { html: `<span class="s3-cell">${escapeHtml(cell)}</span>` },
      { html: `<span class="num">${pctHtml(late, 2)}</span>`, align: 'right' },
      { html: statusCellHtml }
    ];
  });

  return `${head}
    ${dataTable({
      columns: [
        { label: '#', align: 'right' },
        '代码',
        '名称',
        '格',
        { label: '尾盘强度', align: 'right' },
        '状态'
      ],
      rows,
      emptyText: '今日无观察标的',
      tableClass: 's3-today-table'
    })}
    <p class="help-text">「待结算」为下一交易日开盘执行、收盘后自动回填——尚未成交，不显示任何预估收益。尾盘强度越低（越超卖）排名越前。</p>`;
}

// ---------------------------------------------------------------------------
// 3. 每日表现（双线图 + 逐日表）
// ---------------------------------------------------------------------------

function dailyPerformanceSection(data) {
  const series = Array.isArray(data.daily_series) ? data.daily_series : [];
  const head = sectionHead(
    '每日表现',
    '逐日结算后的累计净值。中位滑点口径与保守 p75 成本口径并列——p75 大概率在水下，如实展示。'
  );
  if (!series.length) {
    return `${head}${emptySection('每日表现', '尚无已结算的信号日，累计净值待首批结算后生成。')}`;
  }

  const chart = cumulativeDualLine(series);

  // 逐日表：signal_date / n / 当日净·中位 / 当日净·p75 / 累计·中位 / 累计·p75。
  const rows = series.map((row) => [
    { html: `<span class="num">${escapeHtml(dateCn(row && row.signal_date))}</span>` },
    { html: `<span class="num">${escapeHtml(formatNumber(finiteOrNull(row && row.n) ?? '—'))}</span>`, align: 'right' },
    { html: pctHtml(row && row.day_net_med_pct, 3), align: 'right' },
    { html: pctHtml(row && row.day_net_p75_pct, 3), align: 'right' },
    { html: pctHtml(row && row.cum_med_pct, 3), align: 'right' },
    { html: pctHtml(row && row.cum_p75_pct, 3), align: 'right' }
  ]);

  return `${head}
    ${elevatedCard(`<div class="s3-chart-wrap">${chart}${dualLineLegend()}</div>`, { className: 's3-chart-card' })}
    ${dataTable({
      columns: [
        '信号日',
        { label: '样本 n', align: 'right' },
        { label: '当日净·中位', align: 'right' },
        { label: '当日净·p75', align: 'right' },
        { label: '累计·中位', align: 'right' },
        { label: '累计·p75', align: 'right' }
      ],
      rows,
      emptyText: '暂无逐日记录',
      tableClass: 's3-daily-table'
    })}`;
}

// ---------------------------------------------------------------------------
// 4. 累计统计卡
// ---------------------------------------------------------------------------

function cumulativeStatsSection(data) {
  const cum = (data && typeof data.cumulative === 'object' && data.cumulative) || {};
  const head = sectionHead('累计统计', '截至最近一个已结算信号日，全部为样本外前瞻记录。');

  const nDays = finiteOrNull(cum.n_days);
  const hitPct = finiteOrNull(cum.hit_days_pct);
  const cumMed = finiteOrNull(cum.cum_net_med_pct);
  const cumP75 = finiteOrNull(cum.cum_net_p75_pct);
  const maxDd = finiteOrNull(cum.max_drawdown_med_pct);
  const progress = parseProgress(cum.progress_60);

  const cards = [
    statCard({
      title: '已结算信号日',
      value: nDays === null ? '—' : `${formatNumber(nDays)} 天`,
      note: '进入结算的样本外交易日数'
    }),
    statCard({
      title: '盈利日占比',
      value: hitPct === null ? '—' : formatPct(hitPct, 1),
      note: '当日净·中位为正的信号日占比'
    }),
    // 累计净：中位与 p75 并排在同一卡内，负值同等醒目（pctHtml 红涨绿跌）。
    statCard({
      title: '累计净收益',
      valueHtml: `<span class="s3-cum-pair">${pctHtml(cumMed, 2)}<small class="s3-cum-tag">中位</small></span>`,
      noteHtml: `<span class="s3-cum-pair">${pctHtml(cumP75, 2)}<small class="s3-cum-tag">保守 p75</small></span>`
    }),
    statCard({
      title: '最大回撤（中位）',
      valueHtml: pctHtml(maxDd === null ? null : -Math.abs(maxDd), 2),
      note: '累计净值峰值回落幅度'
    }),
    statCard({
      title: '审判进度',
      value: progress.text,
      note: '满 60 笔触发正式复议'
    })
  ].join('\n');

  return `${head}<div class="stat-grid s3-stat-grid">${cards}</div>`;
}

// ---------------------------------------------------------------------------
// 5. 回测背景（折叠区）—— 全部标「样本内 / 二次看，非前瞻」
// ---------------------------------------------------------------------------

function backtestContextSection(data) {
  const ctx = (data && typeof data.backtest_context === 'object' && data.backtest_context) || null;
  if (!ctx) return '';

  const research = (ctx.research && typeof ctx.research === 'object') ? ctx.research : {};
  const holdout = (ctx.holdout_second_look && typeof ctx.holdout_second_look === 'object') ? ctx.holdout_second_look : {};
  const baseline = (research.basket_baseline && typeof research.basket_baseline === 'object') ? research.basket_baseline : {};

  const num = (v, digits = 3, suffix = '') => {
    const n = finiteOrNull(v);
    return n === null ? '—' : `${formatNumber(n, digits)}${suffix}`;
  };

  const rows = [
    ['研究（样本内）· 日净·中位', num(research.day_net_med_pct, 3, '%'), `t=${num(research.t, 2)}`],
    ['研究（样本内）· 年化', num(research.ann_pct, 1, '%'), `安慰剂 z=${num(research.placebo_z, 1)}`],
    ['研究（样本内）· 篮子基线日净', num(baseline.day_net, 3, '%'), `t=${num(baseline.t, 2)}`],
    ['holdout 二次看 · 日净·中位', num(holdout.day_net_med_pct, 3, '%'), `t=${num(holdout.t, 2)}`]
  ].map((r) => [
    { html: `<span>${escapeHtml(r[0])}</span>` },
    { html: `<span class="num">${escapeHtml(r[1])}</span>`, align: 'right' },
    { html: `<span class="num soft">${escapeHtml(r[2])}</span>`, align: 'right' }
  ]);

  const p75Warn = safeText(ctx.p75_warning, '');
  const holdoutCaveat = safeText(holdout.caveat, '');

  const inner = `<div class="s3-backtest">
    <p class="help-text"><strong>口径提醒：</strong>以下均为<b>样本内 / holdout 二次看</b>结果，<b>非前瞻</b>——不代表未来收益，仅作背景参考。</p>
    ${dataTable({
      columns: ['指标', { label: '数值', align: 'right' }, { label: '统计量', align: 'right' }],
      rows,
      emptyText: '暂无回测背景',
      tableClass: 's3-backtest-table'
    })}
    ${holdoutCaveat ? `<p class="help-text"><strong>holdout 说明：</strong>${escapeHtml(holdoutCaveat)}</p>` : ''}
    ${p75Warn ? `<p class="help-text s3-p75-warn"><strong>p75 执行敏感警告：</strong>${escapeHtml(p75Warn)}</p>` : ''}
  </div>`;

  return `<details class="s3-backtest-details">
    <summary><span class="s3-summary-title">回测背景（样本内 / 二次看，非前瞻）</span></summary>
    ${inner}
  </details>`;
}

// ---------------------------------------------------------------------------
// 页面入口
// ---------------------------------------------------------------------------

export function renderS3Watch(model) {
  const data = (model && typeof model.s3Watchlist === 'object' && model.s3Watchlist) || {};
  const missing = model && typeof model.isMissing === 'function' && model.isMissing('s3Watchlist');
  const hasBody = data && (
    (Array.isArray(data.latest_top20) && data.latest_top20.length)
    || (Array.isArray(data.daily_series) && data.daily_series.length)
    || (data.cumulative && typeof data.cumulative === 'object')
  );

  const title = safeText(data.title, '') || 'S3 分时形态 · top-20 观察名单';
  const subtitle = 'S3 分时形态日频化剧本的前瞻观察名单——研究观察，非买入建议。';

  // 文件缺失 / 空 → 整页退占位（Hero 仍在，主体退占位），绝不编造，其余站点不受影响。
  if (missing || !hasBody) {
    const hero = renderHero(model, title, subtitle, {
      eyebrow: 'S3 前瞻观察 · 剧本引擎',
      bodyHtml: '',
      asideHtml: ''
    });
    const body = `${hero}
      <div class="s3-watch">
        ${missingSection('S3 观察名单', missing
          ? '观察名单文件暂未生成或读取失败'
          : '观察名单暂无内容')}
      </div>`;
    return renderShell('s3Watch', model, body);
  }

  const hero = renderHero(model, title, subtitle, {
    eyebrow: `信号日 ${dateCn(data.latest_signal_date)} · S3 前瞻观察`,
    bodyHtml: heroBody(data),
    // 右侧焦点面板：冻结签名 + 60 笔进度条（不用默认风险盘/来源时间，那套与本页无关）。
    asideHtml: heroAside(data)
  });

  const body = `${hero}
    <div class="s3-watch">
      <section class="s3-section">${todayListSection(data)}</section>
      <section class="s3-section">${cumulativeStatsSection(data)}</section>
      <section class="s3-section">${dailyPerformanceSection(data)}</section>
      <section class="s3-section">${backtestContextSection(data)}</section>
    </div>`;

  return renderShell('s3Watch', model, body);
}
