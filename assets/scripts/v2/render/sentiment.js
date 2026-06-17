// v4/render/sentiment.js — 情绪因子页（sentiment.html，替代 legacy 静态页）。
//
//   renderSentiment(model) — 纯函数：model → 整页 HTML 字符串（用 renderShell 包裹）。
//
// v4 视觉：Brokerage Pro / 铜金抬升式（DESIGN-V4 第 3 节）。版式：
//   分布抬升卡（四桶 statCard + weightBars 占比条）→ charts.gauge 情绪温度（约束宽度）
//   → charts.comboBarLine 情绪趋势（看多占比柱 + AI 评分均值折线）→ 逐日明细表 → 数据口径。
// 只改呈现/版式/图表注入：数据接线、诚实性、口径文案、空/缺态分流逻辑全部沿用 v3。
//
// 数据来源：model.sentimentState（data/latest/sentiment_state.json），由
// generate_view_summaries.py 从 review_state_unified.json 的 stock_rows 聚合而来。
// 字段：generated_at / trade_date / source / window_days / sample_count /
//       distribution{看多,中性偏多,中性,看空:{count,ratio}} / avg_ai_score /
//       daily_series[{date,bullish_ratio,avg_score,sample}]。
//
// 诚实性要点（DESIGN-V3 第 0 节，仍是铁律）：
//   - 口径必须显性标注：这是「推荐个股 AI 观点」的情绪聚合，不是全市场情绪指标；
//   - sentimentState 缺失（文件 404）→ missingSection；
//     sample_count=0（文件在但无可归类样本）→ emptySection；两态分开解释；
//   - 绝不编造任何情绪数字：分布、平均分、趋势全部来自数据，缺则显示「—」或空态说明；
//   - avg_ai_score 用 gauge 呈现「情绪温度」，并说明其为 AI 个股评分（0-100）的均值，
//     非市场涨跌；占比卡用真实计数与百分比。

import {
  escapeHtml, safeText, formatNumber, formatPct, dateCn, friendlyTime
} from './format.js';
import {
  badge, statCard, sectionHead, missingSection, emptySection, dataTable
} from './components.js';
import { comboBarLine, gauge, weightBars } from './charts.js';
import { renderShell, renderHero } from './shell.js';

// 四个情绪桶的展示顺序、色调与一句话说明（写死的口径文案，非业绩数字）。
const BUCKETS = [
  { key: '看多', tone: 'up', note: 'AI 首选买入 / 加仓' },
  { key: '中性偏多', tone: 'info', note: 'AI 倾向持有 / 持仓' },
  { key: '中性', tone: 'flat', note: 'AI 建议观望' },
  { key: '看空', tone: 'down', note: 'AI 建议卖出' }
];

// weightBars 的色调与四桶对齐（看多铜金高光 / 偏多冷蓝 / 中性灰 / 看空绿）。
const BUCKET_BAR_TONE = { 看多: 'up', 中性偏多: 'accent', 中性: 'flat', 看空: 'down' };

// model.isMissing / missingReason 的防御性读取（fixtures 直接构造 model 时也能渲染）。
function missingHelpers(model) {
  const isMissing = typeof model.isMissing === 'function' ? model.isMissing.bind(model) : () => false;
  const missingReason = typeof model.missingReason === 'function'
    ? model.missingReason.bind(model)
    : () => '数据缺失';
  return { isMissing, missingReason };
}

// 单个分布占比卡：真实 count 与 ratio，缺项显示 0（数据里确实为 0，不隐藏）。
function bucketCard(def, entry) {
  const data = entry && typeof entry === 'object' ? entry : {};
  const count = Number(data.count);
  const ratio = Number(data.ratio);
  const hasRatio = Number.isFinite(ratio);
  return statCard({
    title: def.key,
    valueHtml: `<span class="num">${hasRatio ? escapeHtml(formatPct(ratio * 100, 1)) : '—'}</span>`,
    note: `${def.note} · ${Number.isFinite(count) ? formatNumber(count) : '—'} 条`,
    tone: def.tone,
    small: true
  });
}

// 分布抬升卡：四桶占比卡 + 右侧 weightBars 占比条（视觉抬升，数字仍来自真实分布）。
function distributionSection(doc) {
  const distribution = doc.distribution && typeof doc.distribution === 'object' ? doc.distribution : {};
  const distCards = BUCKETS.map((def) => bucketCard(def, distribution[def.key])).join('');

  // weightBars 用真实 count 作权重（自动归一化为占比）；全 0 时组件自降级为占位。
  const barItems = BUCKETS.map((def) => {
    const entry = distribution[def.key];
    const count = Number(entry && typeof entry === 'object' ? entry.count : NaN);
    return {
      label: def.key,
      value: Number.isFinite(count) ? count : 0,
      tone: BUCKET_BAR_TONE[def.key] || 'flat'
    };
  });

  const windowDays = Number(doc.window_days);
  const sampleCount = Number(doc.sample_count);
  const sub = Number.isFinite(windowDays) && Number.isFinite(sampleCount)
    ? `近 ${formatNumber(windowDays)} 个推荐日、${formatNumber(sampleCount)} 条 AI 个股观点的归一化分布`
    : 'AI 对推荐个股观点的归一化分布';

  return `${sectionHead('情绪分布', sub)}
  <section class="panel sentiment-dist-panel">
    <div class="stat-grid sentiment-dist-grid">${distCards}</div>
    <div class="sentiment-dist-bars">
      <div class="panel-title">观点占比一览</div>
      ${weightBars(barItems, { label: 'AI 个股观点分布', digits: 1 })}
    </div>
  </section>`;
}

// 情绪温度盘：avg_ai_score 为 AI 个股评分均值（0-100），不是市场涨跌幅。
function temperatureSection(doc) {
  const score = doc.avg_ai_score;
  const hasScore = score !== null && score !== undefined && Number.isFinite(Number(score));
  const head = sectionHead(
    '情绪温度',
    'AI 对推荐个股给出的评分（0-100）的样本均值，分数越高代表 AI 整体越偏积极。这是观点强度，不是市场涨跌幅。'
  );
  if (!hasScore) {
    return head + emptySection('情绪温度', '窗口内没有可用的 AI 评分，暂无法计算情绪温度。');
  }
  return `${head}
  <section class="panel sentiment-gauge-panel">
    <div class="sentiment-gauge">
      ${gauge(score, { regime: '情绪温度' })}
      <p class="gauge-caption soft">AI 个股评分均值（0–100）· 50 为中性基准</p>
    </div>
  </section>`;
}

// 逐推荐日趋势：看多占比柱（正红）叠加 AI 评分均值折线（右轴蓝），charts.comboBarLine。
function trendSection(doc) {
  const series = Array.isArray(doc.daily_series) ? doc.daily_series : [];
  const head = sectionHead(
    '情绪趋势',
    '每个推荐日的「看多」占比（柱）与当日 AI 评分均值（蓝线，右轴），观察情绪随时间的变化。'
  );
  if (!series.length) {
    return head + emptySection('情绪趋势', '窗口内没有逐日样本，暂无法绘制趋势。');
  }

  // 柱：当日看多占比（%）。折线：当日 AI 评分均值（右轴，0–100 量纲，缺则跳过该点）。
  const bars = series.map((point) => {
    const ratio = Number(point && point.bullish_ratio);
    return {
      label: dateCn(point && point.date),
      value: Number.isFinite(ratio) ? ratio * 100 : null
    };
  });
  const scoreLine = series.map((point) => {
    const score = Number(point && point.avg_score);
    return Number.isFinite(score) ? score : null;
  });

  return `${head}
  <section class="panel sentiment-trend-panel">
    <div class="chart-block">
      ${comboBarLine(bars, scoreLine, {
        label: '逐日看多占比与 AI 评分均值',
        barUnit: '%', barDigits: 1,
        lineUnit: '', lineDigits: 1, lineMin: 0, lineMax: 100
      })}
    </div>
    <p class="chart-footnote">红柱＝当日 AI「看多」观点占当日有效样本的比例；蓝线＝当日 AI 评分均值（右轴 0–100，50 为中性）。悬停可看具体数值。</p>
  </section>`;
}

// 逐推荐日明细表（看多占比 / 平均分 / 样本数），趋势图的数字底稿。
function dailyTableSection(doc) {
  const series = Array.isArray(doc.daily_series) ? doc.daily_series : [];
  if (!series.length) return '';
  // 倒序展示（最新在上），与战绩页明细一致的阅读习惯。
  const rows = series.slice().reverse().map((point) => {
    const ratio = Number(point && point.bullish_ratio);
    const score = point && point.avg_score;
    const sample = Number(point && point.sample);
    return [
      dateCn(point && point.date),
      { text: Number.isFinite(ratio) ? formatPct(ratio * 100, 1) : '—', align: 'right' },
      {
        text: score === null || score === undefined || !Number.isFinite(Number(score))
          ? '—' : formatNumber(score, 2),
        align: 'right'
      },
      { text: Number.isFinite(sample) ? formatNumber(sample) : '—', align: 'right' }
    ];
  });
  return `${sectionHead('逐日明细', '趋势图背后的逐日数字，供核对')}
  ${dataTable({
    columns: [
      '推荐日',
      { label: 'AI 看多占比', align: 'right' },
      { label: 'AI 平均评分', align: 'right' },
      { label: '样本数', align: 'right' }
    ],
    rows,
    emptyText: '窗口内暂无逐日样本',
    tableClass: 'sentiment-daily-table'
  })}`;
}

// 口径说明块：明确数据范围与局限，避免被误读为全市场情绪。
function methodologySection(doc) {
  const windowDays = Number(doc.window_days);
  const sampleCount = Number(doc.sample_count);
  const generatedAt = safeText(doc.generated_at, '');
  const lines = [
    `统计口径：${safeText(doc.source, '基于近期 AI 对推荐个股的观点聚合，非全市场情绪')}。`,
    Number.isFinite(windowDays) && Number.isFinite(sampleCount)
      ? `样本范围：最近 ${formatNumber(windowDays)} 个推荐日、共 ${formatNumber(sampleCount)} 条有效 AI 观点。`
      : '',
    'ai_view 为中文观点（可能是复合值，如「持有/加仓」），按首关键字归一到看多 / 中性偏多 / 中性 / 看空四类。',
    '这是系统对自己「推荐过的个股」的观点温度，覆盖面有限，不能代表整个市场的情绪。',
    generatedAt ? `数据生成时间：${friendlyTime(generatedAt)}。` : ''
  ].filter(Boolean);
  return `${sectionHead('数据口径与局限', '看数前先看这里')}
  <section class="panel methodology-panel">
    <ul class="methodology-list">
      ${lines.map((line) => `<li>${escapeHtml(line)}</li>`).join('')}
    </ul>
  </section>`;
}

function heroSubtitle() {
  return '基于 AI 对推荐个股观点的市场情绪温度计';
}

// Hero 副信息：口径徽章（一眼看清这不是全市场情绪）。
function heroAside(doc) {
  const sampleCount = Number(doc.sample_count);
  const windowDays = Number(doc.window_days);
  return `<div class="sentiment-hero-aside">
    ${badge('口径：推荐个股 AI 观点', 'warn')}
    <p class="sentiment-hero-note">${escapeHtml(
      Number.isFinite(windowDays) && Number.isFinite(sampleCount)
        ? `近 ${formatNumber(windowDays)} 个推荐日 · ${formatNumber(sampleCount)} 条有效观点`
        : '推荐个股 AI 观点聚合，非全市场情绪'
    )}</p>
  </div>`;
}

export function renderSentiment(model) {
  const safeModel = model || {};
  const { isMissing, missingReason } = missingHelpers(safeModel);
  const doc = safeModel.sentimentState || {};

  let body;
  if (isMissing('sentimentState') || !doc || typeof doc !== 'object' || !Object.keys(doc).length) {
    body = `
      ${renderHero(safeModel, '情绪因子', heroSubtitle())}
      ${sectionHead('情绪因子', '基于 AI 对推荐个股观点的市场情绪聚合')}
      ${missingSection('情绪因子', isMissing('sentimentState') ? missingReason('sentimentState') : '情绪数据文件暂未生成')}
    `;
    return renderShell('sentiment', safeModel, body);
  }

  const sampleCount = Number(doc.sample_count);
  const hasSamples = Number.isFinite(sampleCount) && sampleCount > 0;

  if (!hasSamples) {
    body = `
      ${renderHero(safeModel, '情绪因子', heroSubtitle(), { asideHtml: heroAside(doc) })}
      ${methodologySection(doc)}
      ${sectionHead('情绪分布', 'AI 对推荐个股的观点分布')}
      ${emptySection('情绪分布', '窗口内没有可归类的 AI 个股观点，暂无法统计情绪分布。')}
    `;
    return renderShell('sentiment', safeModel, body);
  }

  body = `
    ${renderHero(safeModel, '情绪因子', heroSubtitle(), { asideHtml: heroAside(doc) })}
    ${distributionSection(doc)}
    ${temperatureSection(doc)}
    ${trendSection(doc)}
    ${dailyTableSection(doc)}
    ${methodologySection(doc)}
  `;

  return renderShell('sentiment', safeModel, body);
}
