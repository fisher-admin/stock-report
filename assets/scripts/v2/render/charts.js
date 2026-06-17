// v4/render/charts.js — SVG 字符串图表组件（纯函数，零第三方依赖，无 document/window，Node 可执行）。
//
// 设计：Brokerage Pro / 铜金（DESIGN-V4 第 2 节）。颜色全部走 CSS 变量，随深浅主题切换。
// 涨跌色：--up 红（涨）/ --down 绿（跌）/ --flat 灰；强调：--brand 铜金、--accent 冷蓝（命中率/折线）。
// 为兼容旧 app.css 令牌名，灰阶/表面类令牌用链式回退：var(--新, var(--旧, #硬回退))。
//
// 公开 API（全部返回 HTML/SVG 字符串；空数据返回带说明的占位元素，绝不返回空串、绝不编造数据）：
//
//   sparkline(values, opts?)
//     values : number[] 或 [{value}] / [{close}]，按顺序绘制走势小线
//     opts   : { width=140, height=40, tone='auto', label='', emptyText }
//              tone：'auto'（按首尾涨跌取红/绿）| 'up' | 'down' | 'flat' | 'brand' | 'accent'
//     输出：面积渐变 + 走势线 + 末点高亮圆点（涨红跌绿）。
//
//   equityCurve(points, opts?)
//     points : [{ date|label, value }] 或 number[]，净值序列（基期一般为 1.0）
//     opts   : { width=720, height=240, baseline=1, label='', emptyText }
//     输出：网格 + 基准虚线 + 走势线 + 水下回撤红色填充（峰下区段）+ 最低点标注 + 末值标签 + 首末日期。
//
//   comboBarLine(bars, line, opts?)              【新增 · 战绩用】
//     bars : [{ label, value }] 或 number[]，逐日收益（正 --up 红 / 负 --down 绿，零基线 + 网格）
//     line : number[] 或 [{value}]，与 bars 同长度的命中率序列（右轴蓝折线，缺省可传 [] / null 省略）
//     opts : { width=720, height=240, barUnit='%', barDigits=2, lineUnit='%', lineDigits=0,
//              lineMin, lineMax, label='', emptyText }
//
//   barSeries(items, opts?)
//     items  : [{ label, value }]，正负值双色柱（正 --up 红 / 负 --down 绿），
//              value 为 null 的槽位显示为空缺；每根柱带 <title> 悬浮提示。
//     opts   : { width=720, height=170, unit='%', digits=1, label='', emptyText }
//
//   divergingBars(items, opts?)                  【新增 · 行业强弱用】
//     items  : [{ name|label, pct|value, count?, note? }]，行业涨跌发散条
//     输出：中线分隔，正向右（--up 红）、负向左（--down 绿）；每行名称 + 数值 + 强度条。
//     opts   : { width=420, label='', maxAbs（不给则取数据绝对值最大）, digits=2, emptyText }
//
//   donut(segments, opts?)                        【新增 · 策略权重环】
//     segments : [{ label?, value, tone? }]，value 为权重（自动归一化为占比）
//     opts     : { size=120, thickness=14, centerLabel?, centerSub?, label='', emptyText }
//     tone：'brand' | 'accent' | 'up' | 'down' | 'warn' | 'flat'（缺省按顺序循环取色）。
//
//   weightBars(items, opts?)                       【新增 · donut 的横条替代】
//     items  : [{ label, value, tone? }]，权重横条（label + 百分比 + 比例条）
//     opts   : { width=320, label='', digits=1, emptyText }
//
//   scoreBar(value, opts?)                         【新增 · 候选评分横条】
//     value  : 0~max 的评分（null/非数 → 占位）
//     opts   : { width=200, height=14, max=100, tone='brand', label='', showValue=true }
//
//   heatGrid(rows, opts?)
//     rows   : [{ name|industry_name, pct|avg_pct_chg, count|stock_count, note }]
//     输出：HTML 网格（非 SVG），底色用 color-mix 以 --up/--down 按 |涨跌幅| 0~3% 映射浓度。
//     opts   : { limit=40, label='', emptyText }
//
//   gauge(score, opts?)
//     score  : 0~100 风险/情绪分（null/非数 → 占位）
//     opts   : { size=200, label='', regime='', max=100 }
//     输出：半圆渐变弧（绿→金→红）+ 灰底轨 + 指针 + 中心大数字 + 0/中/满刻度，色调随分值。
//
// 可访问性：所有 SVG 带 viewBox + role="img" + aria-label；heatGrid 容器同样带 aria-label。
// 转义纪律：一切进入标签文本/属性的外部字符串先过 escapeHtml。

import { escapeHtml, safeText, formatSignedPct } from './format.js';

let uidCounter = 0;
function nextUid(prefix) {
  uidCounter += 1;
  return `${prefix}-${uidCounter}`;
}

// 灰阶/表面令牌：v4 新名优先，回退旧名，再回退硬编码（保证脱离 app.css 也能看）。
const TRACK = 'var(--surface-2, var(--panel-2, #262019))'; // 轨道/底槽
const GRID = 'var(--line, #3a3225)'; // 网格线
const AXIS = 'var(--ink-3, var(--text-3, #8f8674))'; // 坐标轴文字
const BRAND = 'var(--brand, #c0883a)';
const ACCENT = 'var(--accent, #5b8def)';

function chartEmpty(text) {
  return `<div class="chart-empty" role="note">${escapeHtml(text)}</div>`;
}

function toNumbers(values) {
  if (!Array.isArray(values)) return [];
  return values
    .map((item) => {
      if (item === null || item === undefined) return null;
      if (typeof item === 'object') {
        const raw = item.value ?? item.close ?? item.val;
        const num = Number(raw);
        return Number.isFinite(num) ? num : null;
      }
      const num = Number(item);
      return Number.isFinite(num) ? num : null;
    });
}

function fmt(num, digits = 2) {
  return Number(num).toFixed(digits);
}

function toneColor(tone, fallback) {
  switch (tone) {
    case 'up': return 'var(--up)';
    case 'down': return 'var(--down)';
    case 'flat': return 'var(--flat)';
    case 'brand': return BRAND;
    case 'brand-2': return 'var(--brand-2, #e6c178)';
    case 'accent': return ACCENT;
    case 'accent-2': return 'var(--accent-2, #7aa6ff)';
    case 'warn': return 'var(--warn)';
    case 'ok': return 'var(--ok)';
    case 'bad': return 'var(--bad)';
    default: return fallback || BRAND;
  }
}

// ---------------------------------------------------------------------------
// sparkline
// ---------------------------------------------------------------------------

export function sparkline(values, opts = {}) {
  const {
    width = 140, height = 40, tone = 'auto', label = '', emptyText = '暂无走势数据'
  } = opts;
  const nums = toNumbers(values).filter((v) => v !== null);
  if (nums.length < 2) return chartEmpty(emptyText);

  const pad = 3;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const stepX = (width - pad * 2) / (nums.length - 1);
  const y = (v) => pad + (height - pad * 2) * (1 - (v - min) / span);
  const pts = nums.map((v, i) => `${fmt(pad + i * stepX, 1)},${fmt(y(v), 1)}`);

  const diff = nums[nums.length - 1] - nums[0];
  const toneKey = tone === 'auto' ? (diff > 0 ? 'up' : diff < 0 ? 'down' : 'flat') : tone;
  const color = toneColor(toneKey, 'var(--flat)');

  const areaPts = [`${fmt(pad, 1)},${fmt(height - pad, 1)}`, ...pts, `${fmt(pad + (nums.length - 1) * stepX, 1)},${fmt(height - pad, 1)}`];
  const gradId = nextUid('spk');
  const last = pts[pts.length - 1].split(',');
  const aria = label
    ? `${label}走势小图，最新 ${fmt(nums[nums.length - 1])}`
    : `走势小图，区间 ${fmt(min)} 至 ${fmt(max)}，最新 ${fmt(nums[nums.length - 1])}`;

  return `<svg class="chart-sparkline" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.20"></stop>
        <stop offset="100%" stop-color="${color}" stop-opacity="0.01"></stop>
      </linearGradient>
    </defs>
    <polygon points="${areaPts.join(' ')}" fill="url(#${gradId})"></polygon>
    <polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1.9" stroke-linejoin="round" stroke-linecap="round"></polyline>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.6" fill="${color}"></circle>
  </svg>`;
}

// ---------------------------------------------------------------------------
// equityCurve — 净值曲线 + 水下回撤红色填充（参照 mockup-C 招牌图）
// ---------------------------------------------------------------------------

function maxDrawdownRange(values) {
  let peakIdx = 0;
  let best = { depth: 0, from: 0, to: 0 };
  for (let i = 1; i < values.length; i += 1) {
    if (values[i] > values[peakIdx]) {
      peakIdx = i;
    } else if (values[peakIdx] > 0) {
      const depth = (values[peakIdx] - values[i]) / values[peakIdx];
      if (depth > best.depth) best = { depth, from: peakIdx, to: i };
    }
  }
  return best;
}

// 全局最低点索引（用于标注「低点」）。
function troughIndex(values) {
  let idx = 0;
  for (let i = 1; i < values.length; i += 1) {
    if (values[i] < values[idx]) idx = i;
  }
  return idx;
}

export function equityCurve(points, opts = {}) {
  const {
    width = 720, height = 240, baseline = 1, label = '净值曲线', emptyText = '暂无可绘制的净值数据'
  } = opts;
  const list = Array.isArray(points) ? points : [];
  const values = toNumbers(list);
  const clean = [];
  const labels = [];
  list.forEach((item, i) => {
    if (values[i] === null) return;
    clean.push(values[i]);
    labels.push(typeof item === 'object' && item !== null
      ? safeText(item.date || item.label || item.recommend_date, '')
      : '');
  });
  if (clean.length < 2) return chartEmpty(emptyText);

  const padL = 48; const padR = 66; const padT = 20; const padB = 28;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const min = Math.min(...clean, baseline);
  const max = Math.max(...clean, baseline);
  const span = (max - min) || 1;
  const x = (i) => padL + (innerW * i) / (clean.length - 1);
  const y = (v) => padT + innerH * (1 - (v - min) / span);

  const linePts = clean.map((v, i) => `${fmt(x(i), 1)},${fmt(y(v), 1)}`);

  // 水下回撤填充：净值低于「基准」的区域，自基准线向下用 --up 红半透明铺满。
  const baselineY = y(baseline);
  const underwaterId = nextUid('uw-clip');
  const areaDownId = nextUid('uw-grad');
  // 面积路径：沿曲线走，再沿基准线回到起点，闭合后用 clip 限制在基准线以下。
  const areaPath = `M ${fmt(x(0), 1)},${fmt(baselineY, 1)} L ${linePts.join(' L ')} L ${fmt(x(clean.length - 1), 1)},${fmt(baselineY, 1)} Z`;

  const final = clean[clean.length - 1];
  const finalTone = final > baseline ? 'var(--up)' : final < baseline ? 'var(--down)' : 'var(--flat)';
  const dd = maxDrawdownRange(clean);
  const ddText = dd.depth > 0.0001 ? `，期间最大回撤 ${fmt(dd.depth * 100, 1)}%` : '';

  // 最低点标注（仅当低点不是末点且确有起伏时）。
  const tIdx = troughIndex(clean);
  const showTrough = clean.length >= 4 && tIdx !== clean.length - 1 && span > 1e-9;
  const troughMark = showTrough
    ? `<circle cx="${fmt(x(tIdx), 1)}" cy="${fmt(y(clean[tIdx]), 1)}" r="3.4" fill="var(--up)"></circle>
    <text x="${fmt(x(tIdx), 1)}" y="${fmt(Math.min(y(clean[tIdx]) + 16, height - padB + 14), 1)}" text-anchor="middle" class="chart-trough-label" fill="var(--up)">${fmt(clean[tIdx], 3)} 低点</text>`
    : '';

  // 等距网格（3 条）。
  const grid = [0.25, 0.5, 0.75]
    .map((f) => {
      const gy = padT + innerH * f;
      return `<line x1="${padL}" y1="${fmt(gy, 1)}" x2="${width - padR}" y2="${fmt(gy, 1)}" stroke="${GRID}" stroke-width="1" opacity="0.5"></line>`;
    })
    .join('\n    ');

  const firstLabel = labels[0];
  const lastLabel = labels[labels.length - 1];
  const aria = `${label}：起点 ${fmt(clean[0], 3)}，最新 ${fmt(final, 3)}${ddText}，共 ${clean.length} 个交易日`;

  return `<svg class="chart-equity" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    <defs>
      <clipPath id="${underwaterId}"><rect x="${padL}" y="${fmt(baselineY, 1)}" width="${innerW}" height="${fmt(Math.max(padT + innerH - baselineY, 0), 1)}"></rect></clipPath>
      <linearGradient id="${areaDownId}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="var(--up)" stop-opacity="0.20"></stop>
        <stop offset="100%" stop-color="var(--up)" stop-opacity="0.05"></stop>
      </linearGradient>
    </defs>
    ${grid}
    <path d="${areaPath}" fill="url(#${areaDownId})" clip-path="url(#${underwaterId})"></path>
    <line x1="${padL}" y1="${fmt(baselineY, 1)}" x2="${width - padR}" y2="${fmt(baselineY, 1)}" stroke="${AXIS}" stroke-width="1" stroke-dasharray="4 4" opacity="0.8"></line>
    <text x="${padL - 6}" y="${fmt(baselineY + 3.5, 1)}" text-anchor="end" class="chart-axis-text">${fmt(baseline, 2)}</text>
    <text x="${padL - 6}" y="${padT + 4}" text-anchor="end" class="chart-axis-text">${fmt(max, 3)}</text>
    <text x="${padL - 6}" y="${padT + innerH + 4}" text-anchor="end" class="chart-axis-text">${fmt(min, 3)}</text>
    <polyline points="${linePts.join(' ')}" fill="none" stroke="${finalTone}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"></polyline>
    ${troughMark}
    <circle cx="${fmt(x(clean.length - 1), 1)}" cy="${fmt(y(final), 1)}" r="3.4" fill="${finalTone}" stroke="var(--card, var(--surface, #211c15))" stroke-width="1.5"></circle>
    <text x="${fmt(x(clean.length - 1) + 8, 1)}" y="${fmt(y(final) + 4, 1)}" class="chart-end-label" fill="${finalTone}">${fmt(final, 3)}</text>
    ${firstLabel ? `<text x="${padL}" y="${height - 8}" class="chart-axis-text">${escapeHtml(firstLabel)}</text>` : ''}
    ${lastLabel ? `<text x="${width - padR}" y="${height - 8}" text-anchor="end" class="chart-axis-text">${escapeHtml(lastLabel)}</text>` : ''}
  </svg>`;
}

// ---------------------------------------------------------------------------
// comboBarLine — 逐日收益柱（正红负绿）+ 命中率折线（右轴蓝）（参照 mockup-B 战绩图）
// ---------------------------------------------------------------------------

export function comboBarLine(bars, line, opts = {}) {
  const {
    width = 720, height = 240, barUnit = '%', barDigits = 2,
    lineUnit = '%', lineDigits = 0, lineMin, lineMax,
    label = '逐日收益与命中率', emptyText = '暂无逐日战绩数据'
  } = opts;

  const barList = Array.isArray(bars) ? bars.filter((b) => b !== null && b !== undefined) : [];
  if (!barList.length) return chartEmpty(emptyText);
  const barVals = barList.map((item) => {
    const num = Number(typeof item === 'object' ? item.value : item);
    return Number.isFinite(num) ? num : null;
  });
  const finiteBars = barVals.filter((v) => v !== null);
  if (!finiteBars.length) return chartEmpty(emptyText);

  const lineVals = toNumbers(line);

  const padL = 10; const padR = 44; const padT = 16; const padB = 26;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const maxAbs = Math.max(...finiteBars.map((v) => Math.abs(v))) || 1;
  const zeroY = padT + innerH / 2;
  const scale = (innerH / 2) / maxAbs;
  const slot = innerW / barVals.length;
  const barW = Math.max(Math.min(slot * 0.6, 34), 2);

  // 网格 + 零基线。
  const grid = `<line x1="${padL}" y1="${fmt(padT, 1)}" x2="${width - padR}" y2="${fmt(padT, 1)}" stroke="${GRID}" stroke-width="1" opacity="0.5"></line>
    <line x1="${padL}" y1="${fmt(padT + innerH, 1)}" x2="${width - padR}" y2="${fmt(padT + innerH, 1)}" stroke="${GRID}" stroke-width="1" opacity="0.5"></line>`;
  const zeroLine = `<line x1="${padL}" y1="${fmt(zeroY, 1)}" x2="${width - padR}" y2="${fmt(zeroY, 1)}" stroke="${GRID}" stroke-width="1.4" stroke-dasharray="3 4"></line>`;

  const bars2 = barVals.map((v, i) => {
    const cx = padL + slot * i + slot / 2;
    const itemLabel = typeof barList[i] === 'object' ? safeText(barList[i].label || barList[i].date, `第 ${i + 1} 项`) : `第 ${i + 1} 项`;
    if (v === null) {
      return `<g><title>${escapeHtml(`${itemLabel}：暂无数据`)}</title><rect x="${fmt(cx - barW / 2, 1)}" y="${fmt(zeroY - 1, 1)}" width="${fmt(barW, 1)}" height="2" fill="var(--flat)" opacity="0.35"></rect></g>`;
    }
    const h = Math.max(Math.abs(v) * scale, v === 0 ? 1 : 1.5);
    const yPos = v >= 0 ? zeroY - h : zeroY;
    const color = v > 0 ? 'var(--up)' : v < 0 ? 'var(--down)' : 'var(--flat)';
    return `<g><title>${escapeHtml(`${itemLabel}：${formatSignedPct(v, barDigits).replace('%', barUnit)}`)}</title><rect x="${fmt(cx - barW / 2, 1)}" y="${fmt(yPos, 1)}" width="${fmt(barW, 1)}" height="${fmt(h, 1)}" rx="2.5" fill="${color}" opacity="0.92"></rect></g>`;
  }).join('\n    ');

  // 命中率折线（右轴）：把 [lo, hi] 映射进绘图区上 ~78% 的纵向带。
  let lineLayer = '';
  let lineAxis = '';
  const finiteLine = lineVals.filter((v) => v !== null);
  if (finiteLine.length >= 1) {
    const lo = Number.isFinite(lineMin) ? lineMin : Math.min(...finiteLine);
    const hi = Number.isFinite(lineMax) ? lineMax : Math.max(...finiteLine);
    const lineSpan = (hi - lo) || 1;
    const bandTop = padT + innerH * 0.06;
    const bandBot = padT + innerH * 0.92;
    const ly = (v) => bandBot - (bandBot - bandTop) * ((v - lo) / lineSpan);
    const lx = (i) => padL + slot * i + slot / 2;
    const pts = [];
    const dots = [];
    lineVals.forEach((v, i) => {
      if (v === null) return;
      const px = fmt(lx(i), 1); const py = fmt(ly(v), 1);
      pts.push(`${px},${py}`);
      dots.push(`<circle cx="${px}" cy="${py}" r="2.6"><title>${escapeHtml(`${typeof barList[i] === 'object' ? safeText(barList[i].label || barList[i].date, `第 ${i + 1} 项`) : `第 ${i + 1} 项`}：命中率 ${fmt(v, lineDigits)}${lineUnit}`)}</title></circle>`);
    });
    if (pts.length >= 2) {
      lineLayer = `<polyline points="${pts.join(' ')}" fill="none" stroke="${ACCENT}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" opacity="0.95"></polyline>
    <g fill="${ACCENT}">${dots.join('')}</g>`;
    } else if (pts.length === 1) {
      lineLayer = `<g fill="${ACCENT}">${dots.join('')}</g>`;
    }
    // 右轴刻度（hi / lo）。
    lineAxis = `<text x="${width - padR + 6}" y="${fmt(ly(hi) + 3, 1)}" class="chart-axis-text" fill="${ACCENT}">${fmt(hi, lineDigits)}${lineUnit}</text>
    <text x="${width - padR + 6}" y="${fmt(ly(lo) + 3, 1)}" class="chart-axis-text" fill="${ACCENT}">${fmt(lo, lineDigits)}${lineUnit}</text>`;
  }

  const firstLabel = typeof barList[0] === 'object' ? safeText(barList[0].label || barList[0].date, '') : '';
  const lastItem = barList[barList.length - 1];
  const lastLabel = typeof lastItem === 'object' ? safeText(lastItem.label || lastItem.date, '') : '';
  const aria = `${label}，共 ${barVals.length} 个交易日，柱为逐日收益（最大绝对值 ${fmt(maxAbs, barDigits)}${barUnit}），蓝色折线为命中率`;

  return `<svg class="chart-combo" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    ${grid}
    ${zeroLine}
    ${bars2}
    ${lineLayer}
    ${lineAxis}
    ${firstLabel ? `<text x="${padL}" y="${height - 7}" class="chart-axis-text">${escapeHtml(firstLabel)}</text>` : ''}
    ${lastLabel ? `<text x="${width - padR}" y="${height - 7}" text-anchor="end" class="chart-axis-text">${escapeHtml(lastLabel)}</text>` : ''}
  </svg>`;
}

// ---------------------------------------------------------------------------
// barSeries
// ---------------------------------------------------------------------------

export function barSeries(items, opts = {}) {
  const {
    width = 720, height = 170, unit = '%', digits = 1, label = '逐日柱状图', emptyText = '暂无逐日数据'
  } = opts;
  const list = Array.isArray(items) ? items.filter((item) => item !== null && item !== undefined) : [];
  if (!list.length) return chartEmpty(emptyText);

  const values = list.map((item) => {
    const num = Number(typeof item === 'object' ? item.value : item);
    return Number.isFinite(num) ? num : null;
  });
  const finite = values.filter((v) => v !== null);
  if (!finite.length) return chartEmpty(emptyText);

  const padL = 8; const padR = 8; const padT = 14; const padB = 24;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const maxAbs = Math.max(...finite.map((v) => Math.abs(v))) || 1;
  const hasNegative = finite.some((v) => v < 0);
  const zeroY = hasNegative ? padT + innerH / 2 : padT + innerH;
  const scale = hasNegative ? (innerH / 2) / maxAbs : innerH / maxAbs;
  const slot = innerW / values.length;
  const barW = Math.max(Math.min(slot * 0.64, 36), 2);

  const grid = hasNegative
    ? `<line x1="${padL}" y1="${fmt(padT, 1)}" x2="${width - padR}" y2="${fmt(padT, 1)}" stroke="${GRID}" stroke-width="1" opacity="0.4"></line>
    <line x1="${padL}" y1="${fmt(padT + innerH, 1)}" x2="${width - padR}" y2="${fmt(padT + innerH, 1)}" stroke="${GRID}" stroke-width="1" opacity="0.4"></line>`
    : '';

  const bars = values.map((v, i) => {
    const cx = padL + slot * i + slot / 2;
    const itemLabel = typeof list[i] === 'object' ? safeText(list[i].label || list[i].date, `第 ${i + 1} 项`) : `第 ${i + 1} 项`;
    if (v === null) {
      return `<g><title>${escapeHtml(`${itemLabel}：暂无数据`)}</title><rect x="${fmt(cx - barW / 2, 1)}" y="${fmt(zeroY - 1, 1)}" width="${fmt(barW, 1)}" height="2" fill="var(--flat)" opacity="0.35"></rect></g>`;
    }
    const h = Math.max(Math.abs(v) * scale, v === 0 ? 1 : 1.5);
    const yPos = v >= 0 ? zeroY - h : zeroY;
    const color = v > 0 ? 'var(--up)' : v < 0 ? 'var(--down)' : 'var(--flat)';
    return `<g><title>${escapeHtml(`${itemLabel}：${formatSignedPct(v, digits).replace('%', unit)}`)}</title><rect x="${fmt(cx - barW / 2, 1)}" y="${fmt(yPos, 1)}" width="${fmt(barW, 1)}" height="${fmt(h, 1)}" rx="2" fill="${color}" opacity="0.9"></rect></g>`;
  }).join('\n    ');

  const firstLabel = typeof list[0] === 'object' ? safeText(list[0].label || list[0].date, '') : '';
  const lastItem = list[list.length - 1];
  const lastLabel = typeof lastItem === 'object' ? safeText(lastItem.label || lastItem.date, '') : '';
  const aria = `${label}，共 ${values.length} 项，最大绝对值 ${fmt(maxAbs, digits)}${unit}`;

  return `<svg class="chart-bars" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    ${grid}
    <line x1="${padL}" y1="${fmt(zeroY, 1)}" x2="${width - padR}" y2="${fmt(zeroY, 1)}" stroke="${GRID}" stroke-width="1.2"></line>
    ${bars}
    ${firstLabel ? `<text x="${padL}" y="${height - 7}" class="chart-axis-text">${escapeHtml(firstLabel)}</text>` : ''}
    ${lastLabel ? `<text x="${width - padR}" y="${height - 7}" text-anchor="end" class="chart-axis-text">${escapeHtml(lastLabel)}</text>` : ''}
  </svg>`;
}

// ---------------------------------------------------------------------------
// divergingBars — 行业涨跌发散条（正右红 / 负左绿 / 中线）（参照 mockup-B 行业强弱）
// ---------------------------------------------------------------------------

export function divergingBars(items, opts = {}) {
  const {
    width = 420, label = '行业涨跌发散条', maxAbs: maxAbsOpt, digits = 2, emptyText = '暂无行业涨跌数据'
  } = opts;
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  const rows = list
    .map((item) => {
      const name = safeText(item.name || item.label || item.industry_name, '未标注');
      const raw = item.pct ?? item.value ?? item.avg_pct_chg ?? item.change_pct;
      const num = Number(raw);
      return Number.isFinite(num) ? { name, value: num } : null;
    })
    .filter(Boolean);
  if (!rows.length) return chartEmpty(emptyText);

  const maxAbs = Number.isFinite(maxAbsOpt) && maxAbsOpt > 0
    ? maxAbsOpt
    : Math.max(...rows.map((r) => Math.abs(r.value))) || 1;

  const rowH = 28;
  const gap = 8;
  const nameW = 86;
  const valW = 64;
  const trackL = nameW + 6;
  const trackR = width - valW - 6;
  const mid = (trackL + trackR) / 2;
  const halfW = (trackR - trackL) / 2;
  const height = rows.length * (rowH + gap) - gap + 8;

  const body = rows.map((row, i) => {
    const cy = 4 + i * (rowH + gap);
    const barY = cy + (rowH - 14) / 2;
    const w = Math.max((Math.abs(row.value) / maxAbs) * halfW, row.value === 0 ? 0 : 1.5);
    const isUp = row.value > 0;
    const isFlat = row.value === 0;
    const color = isFlat ? 'var(--flat)' : isUp ? 'var(--up)' : 'var(--down)';
    const barX = isUp ? mid : mid - w;
    const valClass = isFlat ? 'pct-flat' : isUp ? 'pct-up' : 'pct-down';
    const valText = formatSignedPct(row.value, digits);
    const bar = isFlat
      ? ''
      : `<rect x="${fmt(barX, 1)}" y="${fmt(barY, 1)}" width="${fmt(w, 1)}" height="14" rx="3" fill="${color}" opacity="0.92"><title>${escapeHtml(`${row.name}：${valText}`)}</title></rect>`;
    return `<g>
      <text x="${nameW}" y="${fmt(cy + rowH / 2 + 4, 1)}" text-anchor="end" class="diverge-name">${escapeHtml(row.name)}</text>
      <rect x="${fmt(trackL, 1)}" y="${fmt(barY, 1)}" width="${fmt(trackR - trackL, 1)}" height="14" rx="3" fill="${TRACK}" opacity="0.5"></rect>
      ${bar}
      <text x="${width - 4}" y="${fmt(cy + rowH / 2 + 4, 1)}" text-anchor="end" class="diverge-val num ${valClass}" fill="${color}">${escapeHtml(valText)}</text>
    </g>`;
  }).join('\n    ');

  const aria = `${label}，共 ${rows.length} 个行业，正值向右（红/上涨）、负值向左（绿/下跌），最大幅度 ${fmt(maxAbs, digits)}%`;

  return `<svg class="chart-diverge" viewBox="0 0 ${width} ${fmt(height, 0)}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    <line x1="${fmt(mid, 1)}" y1="2" x2="${fmt(mid, 1)}" y2="${fmt(height - 2, 1)}" stroke="${GRID}" stroke-width="1.4"></line>
    ${body}
  </svg>`;
}

// ---------------------------------------------------------------------------
// donut — 策略权重环（参照 mockup-C donut）
// ---------------------------------------------------------------------------

const DONUT_TONE_CYCLE = ['brand', 'accent', 'warn', 'up', 'down', 'flat'];

export function donut(segments, opts = {}) {
  const {
    size = 120, thickness = 14, centerLabel = '', centerSub = '', label = '权重环', emptyText = '暂无权重数据'
  } = opts;
  const list = Array.isArray(segments) ? segments.filter(Boolean) : [];
  const parsed = list
    .map((seg, i) => {
      const num = Number(typeof seg === 'object' ? seg.value : seg);
      if (!Number.isFinite(num) || num < 0) return null;
      return {
        value: num,
        tone: (typeof seg === 'object' && seg.tone) || DONUT_TONE_CYCLE[i % DONUT_TONE_CYCLE.length],
        label: typeof seg === 'object' ? safeText(seg.label, '') : ''
      };
    })
    .filter(Boolean);
  const total = parsed.reduce((sum, s) => sum + s.value, 0);
  if (!parsed.length || total <= 0) return chartEmpty(emptyText);

  const cx = size / 2;
  const cy = size / 2;
  const r = (size - thickness) / 2;
  const circ = 2 * Math.PI * r;

  let offset = 0;
  const arcs = parsed.map((seg) => {
    const frac = seg.value / total;
    const len = circ * frac;
    const color = toneColor(seg.tone, BRAND);
    const dash = `${fmt(Math.max(len - 1.5, 0), 2)} ${fmt(circ - Math.max(len - 1.5, 0), 2)}`;
    const dashOffset = fmt(-offset, 2);
    offset += len;
    const pctText = `${(frac * 100).toFixed(1)}%`;
    return `<circle cx="${cx}" cy="${cy}" r="${fmt(r, 2)}" fill="none" stroke="${color}" stroke-width="${thickness}" stroke-dasharray="${dash}" stroke-dashoffset="${dashOffset}" transform="rotate(-90 ${cx} ${cy})" stroke-linecap="butt"><title>${escapeHtml(`${seg.label || '分段'}：${pctText}`)}</title></circle>`;
  }).join('\n    ');

  const center = centerLabel
    ? `<text x="${cx}" y="${fmt(cy + (centerSub ? -1 : 4), 1)}" text-anchor="middle" class="donut-center num">${escapeHtml(centerLabel)}</text>${centerSub ? `<text x="${cx}" y="${fmt(cy + 13, 1)}" text-anchor="middle" class="donut-sub">${escapeHtml(centerSub)}</text>` : ''}`
    : '';

  const parts = parsed.map((s) => `${s.label || '分段'} ${((s.value / total) * 100).toFixed(1)}%`).join('，');
  const aria = `${label}：${parts}`;

  return `<svg class="chart-donut" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg">
    <circle cx="${cx}" cy="${cy}" r="${fmt(r, 2)}" fill="none" stroke="${TRACK}" stroke-width="${thickness}"></circle>
    ${arcs}
    ${center}
  </svg>`;
}

// ---------------------------------------------------------------------------
// weightBars — donut 的横条替代（label + 百分比 + 比例条）
// ---------------------------------------------------------------------------

export function weightBars(items, opts = {}) {
  const {
    width = 320, label = '权重分布', digits = 1, emptyText = '暂无权重数据'
  } = opts;
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  const parsed = list
    .map((seg, i) => {
      const num = Number(typeof seg === 'object' ? seg.value : seg);
      if (!Number.isFinite(num) || num < 0) return null;
      return {
        value: num,
        tone: (typeof seg === 'object' && seg.tone) || DONUT_TONE_CYCLE[i % DONUT_TONE_CYCLE.length],
        label: typeof seg === 'object' ? safeText(seg.label, `分段 ${i + 1}`) : `分段 ${i + 1}`
      };
    })
    .filter(Boolean);
  const total = parsed.reduce((sum, s) => sum + s.value, 0);
  if (!parsed.length || total <= 0) return chartEmpty(emptyText);

  const rowH = 34;
  const height = parsed.length * rowH;
  const padX = 4;
  const trackTop = 18;
  const trackH = 8;
  const innerW = width - padX * 2;

  const body = parsed.map((seg, i) => {
    const top = i * rowH;
    const frac = seg.value / total;
    const w = Math.max(frac * innerW, 2);
    const color = toneColor(seg.tone, BRAND);
    const pctText = `${(frac * 100).toFixed(digits)}%`;
    return `<g>
      <text x="${padX}" y="${fmt(top + 12, 1)}" class="weight-name">${escapeHtml(seg.label)}</text>
      <text x="${width - padX}" y="${fmt(top + 12, 1)}" text-anchor="end" class="weight-pct num">${escapeHtml(pctText)}</text>
      <rect x="${padX}" y="${fmt(top + trackTop, 1)}" width="${fmt(innerW, 1)}" height="${trackH}" rx="4" fill="${TRACK}" opacity="0.6"></rect>
      <rect x="${padX}" y="${fmt(top + trackTop, 1)}" width="${fmt(w, 1)}" height="${trackH}" rx="4" fill="${color}"><title>${escapeHtml(`${seg.label}：${pctText}`)}</title></rect>
    </g>`;
  }).join('\n    ');

  const parts = parsed.map((s) => `${s.label} ${((s.value / total) * 100).toFixed(digits)}%`).join('，');
  const aria = `${label}：${parts}`;

  return `<svg class="chart-weightbars" viewBox="0 0 ${width} ${fmt(height, 0)}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    ${body}
  </svg>`;
}

// ---------------------------------------------------------------------------
// scoreBar — 候选评分横条
// ---------------------------------------------------------------------------

export function scoreBar(value, opts = {}) {
  const {
    width = 200, height = 14, max = 100, tone = 'brand', label = '', showValue = true
  } = opts;
  if (value === null || value === undefined || value === '') return chartEmpty('暂无评分');
  const num = Number(value);
  if (!Number.isFinite(num)) return chartEmpty('暂无评分');

  const clamped = Math.max(0, Math.min(max, num));
  const frac = max > 0 ? clamped / max : 0;
  const padR = showValue ? 38 : 4;
  const trackW = width - padR - 2;
  const fillW = Math.max(frac * trackW, num > 0 ? 2 : 0);
  const trackY = (height - 8) / 2;
  const color = toneColor(tone, BRAND);
  const aria = `${label ? `${label}：` : '评分 '}${fmt(clamped, num % 1 === 0 ? 0 : 1)} / ${max}`;
  const valText = showValue
    ? `<text x="${width}" y="${fmt(height / 2 + 4, 1)}" text-anchor="end" class="scorebar-val num" fill="${color}">${fmt(clamped, num % 1 === 0 ? 0 : 1)}</text>`
    : '';

  return `<svg class="chart-scorebar" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    <rect x="1" y="${fmt(trackY, 1)}" width="${fmt(trackW, 1)}" height="8" rx="4" fill="${TRACK}" opacity="0.7"></rect>
    <rect x="1" y="${fmt(trackY, 1)}" width="${fmt(fillW, 1)}" height="8" rx="4" fill="${color}"></rect>
    ${valText}
  </svg>`;
}

// ---------------------------------------------------------------------------
// heatGrid（HTML 网格，底色 color-mix 映射涨跌幅浓度；0~3% 线性映射）
// ---------------------------------------------------------------------------

export function heatGrid(rows, opts = {}) {
  const { limit = 40, label = '行业热力图', emptyText = '暂无行业热力数据' } = opts;
  const list = Array.isArray(rows) ? rows.filter(Boolean).slice(0, limit) : [];
  if (!list.length) return chartEmpty(emptyText);

  const cells = list.map((row) => {
    const name = safeText(row.name || row.industry_name || row.sector_name, '未标注');
    const rawPct = row.pct ?? row.avg_pct_chg ?? row.change_pct ?? row.value;
    const num = Number(rawPct);
    const hasPct = Number.isFinite(num);
    const weight = hasPct ? Math.min(Math.abs(num) / 3, 1) : 0;
    const mix = Math.round(8 + weight * 64);
    const baseColor = !hasPct || num === 0 ? 'var(--flat)' : num > 0 ? 'var(--up)' : 'var(--down)';
    const count = row.count ?? row.stock_count ?? row.recommendation_count;
    const countText = Number.isFinite(Number(count)) ? `（${Number(count)} 只）` : '';
    const pctText = hasPct ? formatSignedPct(num, 2) : '—';
    const title = `${name}：${pctText}${countText}${row.note ? `，${safeText(row.note, '')}` : ''}`;
    const pctClass = !hasPct || num === 0 ? 'pct-flat' : num > 0 ? 'pct-up' : 'pct-down';
    return `<div class="heat-cell" title="${escapeHtml(title)}" style="background:color-mix(in srgb, ${baseColor} ${mix}%, transparent);">
      <span class="heat-name">${escapeHtml(name)}</span>
      <span class="heat-pct num ${pctClass}">${escapeHtml(pctText)}</span>
    </div>`;
  }).join('\n  ');

  const aria = `${label}，共 ${list.length} 个行业，红色代表上涨、绿色代表下跌，颜色越深幅度越大`;
  return `<div class="heat-grid" role="img" aria-label="${escapeHtml(aria)}">
  ${cells}
</div>`;
}

// ---------------------------------------------------------------------------
// gauge（半圆风险/情绪刻度盘，0-100；渐变弧 绿→金→红 + 指针 + 刻度，参照 mockup-B/C）
// ---------------------------------------------------------------------------

function polar(cx, cy, r, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy - r * Math.sin(rad) };
}

function arcPath(cx, cy, r, fromDeg, toDeg) {
  const start = polar(cx, cy, r, fromDeg);
  const end = polar(cx, cy, r, toDeg);
  const largeArc = Math.abs(fromDeg - toDeg) > 180 ? 1 : 0;
  // sweep=1：从左(180°)向右(0°)绘制半圆的【上半弧】（polar 用 y=cy-r·sin，端点水平对齐时
  // sweep=0 会画成朝下的下半圆——刻度盘必须用 sweep=1）。
  return `M ${fmt(start.x, 2)} ${fmt(start.y, 2)} A ${fmt(r, 2)} ${fmt(r, 2)} 0 ${largeArc} 1 ${fmt(end.x, 2)} ${fmt(end.y, 2)}`;
}

export function gauge(score, opts = {}) {
  const {
    size = 200, label = '', regime = '', max = 100
  } = opts;
  if (score === null || score === undefined || score === '') return chartEmpty('暂无风险评分数据');
  const num = Number(score);
  if (!Number.isFinite(num)) return chartEmpty('暂无风险评分数据');

  const value = Math.max(0, Math.min(max, num));
  const frac = max > 0 ? value / max : 0;
  const pct100 = frac * 100;
  const tone = pct100 >= 70 ? 'var(--bad)' : pct100 >= 50 ? 'var(--warn)' : 'var(--ok)';

  const cx = size / 2;
  const r = size * 0.4;
  const strokeW = Math.max(size * 0.09, 12);
  const cy = size * 0.62; // 圆心下沉，留出半圆 + 底部文字空间
  const height = size * 0.78;
  const valueAngle = 180 - frac * 180; // 0→180°（左），max→0°（右）

  const gradId = nextUid('gauge-arc');

  // 值标记：弧线上的端点（替代穿过中心的指针，避免压住中心数字造成重叠）。
  const valuePoint = polar(cx, cy, r, valueAngle);
  // 刻度端点。
  const tick0 = polar(cx, cy, r, 180);
  const tickMid = polar(cx, cy, r, 90);
  const tickMax = polar(cx, cy, r, 0);

  const valDigits = value % 1 === 0 ? 0 : 1;
  const aria = `${label || '评分'} ${fmt(value, valDigits)} / ${max}${regime ? `，${regime}` : ''}`;

  return `<svg class="chart-gauge" viewBox="0 0 ${size} ${fmt(height, 0)}" role="img" aria-label="${escapeHtml(aria)}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
    <defs>
      <linearGradient id="${gradId}" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="var(--ok, #1fbf7a)"></stop>
        <stop offset="50%" stop-color="${BRAND}"></stop>
        <stop offset="100%" stop-color="var(--bad, #f5455c)"></stop>
      </linearGradient>
    </defs>
    <path d="${arcPath(cx, cy, r, 180, 0)}" fill="none" stroke="${TRACK}" stroke-width="${fmt(strokeW, 1)}" stroke-linecap="round"></path>
    ${value > 0 ? `<path d="${arcPath(cx, cy, r, 180, valueAngle)}" fill="none" stroke="url(#${gradId})" stroke-width="${fmt(strokeW, 1)}" stroke-linecap="round"></path>` : ''}
    <g stroke="${GRID}" stroke-width="1.6">
      <line x1="${fmt(tick0.x, 1)}" y1="${fmt(tick0.y, 1)}" x2="${fmt(tick0.x + 8, 1)}" y2="${fmt(tick0.y, 1)}"></line>
      <line x1="${fmt(tickMid.x, 1)}" y1="${fmt(tickMid.y, 1)}" x2="${fmt(tickMid.x, 1)}" y2="${fmt(tickMid.y + 9, 1)}"></line>
      <line x1="${fmt(tickMax.x, 1)}" y1="${fmt(tickMax.y, 1)}" x2="${fmt(tickMax.x - 8, 1)}" y2="${fmt(tickMax.y, 1)}"></line>
    </g>
    <circle cx="${fmt(valuePoint.x, 1)}" cy="${fmt(valuePoint.y, 1)}" r="${fmt(strokeW * 0.5, 1)}" fill="var(--card, #211c15)" stroke="${tone}" stroke-width="3"></circle>
    <text x="${fmt(cx, 1)}" y="${fmt(cy - size * 0.06, 1)}" text-anchor="middle" class="gauge-score num" fill="${tone}">${fmt(value, valDigits)}</text>
    <text x="${fmt(cx, 1)}" y="${fmt(cy + size * 0.04, 1)}" text-anchor="middle" class="gauge-sub">满分 ${max}</text>
    <text x="${fmt(tick0.x + 2, 1)}" y="${fmt(tick0.y + 16, 1)}" text-anchor="start" class="gauge-tick">0</text>
    <text x="${fmt(tickMax.x - 2, 1)}" y="${fmt(tickMax.y + 16, 1)}" text-anchor="end" class="gauge-tick">${max}</text>
    ${regime ? `<text x="${fmt(cx, 1)}" y="${fmt(height - 4, 1)}" text-anchor="middle" class="gauge-regime" fill="${tone}">${escapeHtml(regime)}</text>` : (label ? `<text x="${fmt(cx, 1)}" y="${fmt(height - 4, 1)}" text-anchor="middle" class="gauge-label">${escapeHtml(label)}</text>` : '')}
  </svg>`;
}
