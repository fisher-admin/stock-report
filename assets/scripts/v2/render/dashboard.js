// v4/render/dashboard.js — 今日操作（index.html）。纯函数：model → HTML 字符串，无 DOM 依赖。
//
// 版式：选股系统首页。区块顺序：
//   1. 决裁条：系统结论 + 主攻/观察/回避计数 + 诚实空态（今日无主攻标的）。
//   2. 生产观察名单：Top20 逐只往下铺开（行头 + 读卡），不点选、不折叠。
//   3. 市场行情摘要：上证/深证/创业板 + 涨跌面（完整八指数在市场页）。
//   4. 晨判 AI 摘要（aiStatusBadge 三态）。
//   5. 执行层分层（execution_state 权威；空则「今日没有执行建议」）。
//   6. 近期战绩：comboBarLine + KPI。
//
// 诚实性（DESIGN-V3 §0）：
//   - 本文件零硬编码业绩数字；一切数字来自 model，缺失即「—」或「暂无可验证数据」说明；
//   - Hero 不渲染买/观/避三计数对，执行数字只用 execution 层（adjusted_action）一套口径；
//   - 指数/晨判数据源不可用时如实显示降级文案，不外露 provider/报错等开发者信息；
//   - winner_rate 是「获利盘比例」（筹码口径），严禁伪装成「胜率」；AI 三态显性，不用模板话术冒充分析。

import {
  escapeHtml, safeText, formatNumber, formatPct, dateCn, pctHtml,
  strategyLabel, cleanAnalysisText, actionLabel, actionTone
} from './format.js';
import {
  badge, statCard, sectionHead, missingSection, emptySection, dataTable,
  capsule, aiStatusBadge
} from './components.js';
import { sparkline, comboBarLine } from './charts.js';
import { renderShell } from './shell.js';
import {
  resolveAction, stockAnchorId, renderCandidateAnalysis, executionFor
} from './candidateCard.js';

// ---------------------------------------------------------------------------
// 私有辅助
// ---------------------------------------------------------------------------

function hasText(value) {
  return typeof value === 'string' ? value.trim() !== '' : value !== null && value !== undefined && value !== '';
}

// 严格数值解析：null/undefined/'' 一律返回 null（注意 Number(null)===0，会把缺数据伪装成 0）。
function finiteOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

// 证券代码归一化：'601577.SH' / ' 601577 ' → '601577'，用于跨数据源 join 股名。
function normCode(code) {
  return safeText(code, '').trim().toUpperCase().replace(/\.(SH|SZ|BJ)$/i, '');
}

// 股名映射：execution_state 行若缺 stock_name，从 candidate_state / greenfield_top20 补齐。
function buildNameLookup(model) {
  const map = new Map();
  const add = (code, name) => {
    const key = normCode(code);
    if (key && hasText(name) && !map.has(key)) map.set(key, String(name).trim());
  };
  const candidates = (model.candidateState || {}).candidates || [];
  candidates.forEach((item) => add(item.code || item.normalized_code, item.name));
  const top20 = (model.greenfieldTop20 || {}).top20 || [];
  top20.forEach((item) => add(item.stock_code || item.ts_code, item.name));
  return map;
}

// 行首中文/英文字符，做股票头像字。
function avatarChar(name, code) {
  const text = safeText(name, '').trim();
  if (text) return text.slice(0, 1);
  const c = safeText(code, '').trim();
  return c ? c.slice(0, 1) : '·';
}

// 股票单元格：头像 + 股名 + 代码（规范要求执行清单必须有股票名称）。
function stockCellHtml(row, lookup) {
  const code = normCode(row.stock_code || row.code || row.ts_code);
  const name = safeText(row.stock_name || row.name, '').trim()
    || (code ? lookup.get(code) || '' : '');
  const av = avatarChar(name, code);
  const nameHtml = name
    ? `<span class="stk-nm">${escapeHtml(name)}</span>`
    : `<span class="stk-nm num">${escapeHtml(code || '未知代码')}</span>`;
  const codeHtml = name && code ? `<span class="stk-cd num">${escapeHtml(code)}</span>` : '';
  return `<span class="stk-cell"><span class="stk-av" aria-hidden="true">${escapeHtml(av)}</span><span class="stk-id">${nameHtml}${codeHtml}</span></span>`;
}

// 仓位档：用实心圆点表示（1~3 档），缺失则「—」。
function positionTierHtml(tier) {
  const num = finiteOrNull(tier);
  if (num === null) return '<span class="soft">—</span>';
  const total = 3;
  const on = Math.max(0, Math.min(total, Math.round(num)));
  const dots = [];
  for (let i = 0; i < total; i += 1) {
    dots.push(`<i${i < on ? ' class="on"' : ''}></i>`);
  }
  return `<span class="tier-dots" title="${escapeHtml(`${formatNumber(num)} 档`)}">${dots.join('')}</span>`;
}

// ---------------------------------------------------------------------------
// 1. Hero：最终结论 + 一句话 + 仓位/环境胶囊 +（裁决 vs 执行分歧时）纪律说明卡
// ---------------------------------------------------------------------------

function deskMasthead(model) {
  const verdict = model.verdict || {};
  const decision = model.decisionState || {};
  const title = safeText(verdict.label, '').trim()
    || safeText(decision.final_verdict, '').trim()
    || '当日结论暂缺';
  const subtitle = safeText(verdict.summary, '').trim()
    || (hasText(decision.final_verdict) ? `系统结论：${safeText(decision.final_verdict)}` : '')
    || '系统本期未给出结论说明，请结合历史战绩与系统说明页阅读。';
  const summary = model.executionSummary || { main: 0, watch: 0, avoid: 0 };
  const execMissing = model.isMissing('executionState');
  const main = finiteOrNull(summary.main) ?? 0;
  const noMainNote = !execMissing && main === 0
    ? '<p class="desk-note" role="status">今日无主攻标的 · 名单仅供观察，不自动下单。</p>'
    : '';
  const context = model.marketContext || {};
  const policy = safeText(context.policy, '').trim();
  const verdictExecutable = /execute|deploy|可执行|进攻/.test(
    `${safeText(verdict.action || verdict.label, '')}${safeText(verdict.label, '')}`.toLowerCase()
  );
  const downgraded = verdictExecutable && main === 0 && !execMissing;
  const disciplineNote = downgraded
    ? `<div class="divergence-note" role="note"><b>纪律说明：</b>策略闸门裁决为「${escapeHtml(safeText(verdict.label, '可执行'))}」，执行层未给出主攻——今日无主攻标的${policy ? `（${escapeHtml(policy)}）` : ''}。</div>`
    : '';
  const caps = [];
  if (hasText(context.position_limit)) {
    caps.push(capsule('仓位纪律', { value: safeText(context.position_limit), tone: 'warn' }));
  }
  const regime = safeText(context.regime || decision.market_regime, '').trim();
  if (regime) caps.push(capsule('市场环境', { value: regime, tone: 'info' }));
  return `<section class="desk-mast">
    <p class="desk-kicker">生产控制 · 启动前夕观察流 · 不自动下单</p>
    <div class="desk-mast-grid">
      <div>
        <h2 class="desk-verdict">${escapeHtml(title)}</h2>
        <p class="desk-sub">${escapeHtml(subtitle)}</p>
        ${noMainNote}
        ${disciplineNote}
        ${caps.length ? `<div class="hero-meta">${caps.join('')}</div>` : ''}
      </div>
      <div class="desk-counts" aria-label="席位分层">
        <div class="desk-count${main === 0 ? ' is-empty' : ''}"><span class="num">${escapeHtml(formatNumber(summary.main ?? 0))}</span><small>主攻</small></div>
        <div class="desk-count is-watch${(finiteOrNull(summary.watch) ?? 0) === 0 ? ' is-empty' : ''}"><span class="num">${escapeHtml(formatNumber(summary.watch ?? 0))}</span><small>观察</small></div>
        <div class="desk-count is-avoid${(finiteOrNull(summary.avoid) ?? 0) === 0 ? ' is-empty' : ''}"><span class="num">${escapeHtml(formatNumber(summary.avoid ?? 0))}</span><small>回避</small></div>
      </div>
    </div>
  </section>`;
}

function observationDesk(model) {
  if (model.isMissing('candidateState')) {
    return `<section aria-label="生产观察名单">
      ${sectionHead('生产观察名单', '控制组 Top20 · 只观察')}
      ${missingSection('生产观察名单', model.missingReason('candidateState'))}
    </section>`;
  }
  const candidates = Array.isArray(model.candidates) ? model.candidates : [];
  if (!candidates.length) {
    return `<section aria-label="生产观察名单">
      ${sectionHead('生产观察名单', '')}
      ${emptySection('今日暂无候选', '系统本期没有产出可展示的候选个股，请等待下一个交易日的数据。')}
    </section>`;
  }
  const executions = Array.isArray((model.executionState || {}).executions)
    ? model.executionState.executions
    : [];
  const items = candidates.map((item, index) => {
    const action = resolveAction(item);
    const id = stockAnchorId(item);
    const rank = Number(item.rank || item.rank_no || index + 1);
    const code = normCode(item.normalized_code || item.code);
    const name = safeText(item.name, '').trim() || code || '未知代码';
    const change = finiteOrNull(item.current_change_pct ?? item.change_pct);
    const score = finiteOrNull(item.score);
    return `<article class="obs-item" id="${escapeHtml(id)}">
      <div class="obs-row" data-role="${escapeHtml(action)}">
        <span class="obs-rank num">${escapeHtml(formatNumber(rank))}</span>
        <span class="obs-id"><strong>${escapeHtml(name)}</strong><span class="num">${escapeHtml(code || '—')}</span></span>
        <span class="obs-chg">${pctHtml(change)}</span>
        <span class="obs-score num">${score === null ? '—' : escapeHtml(formatNumber(score, 1))}</span>
        <span class="obs-act">${badge(actionLabel(action), actionTone(action))}</span>
      </div>
      <div class="obs-reader">
        ${renderCandidateAnalysis(item, { execution: executionFor(executions, item, 'prebreakout_v41'), index })}
      </div>
    </article>`;
  }).join('');
  return `<section class="obs-desk-wrap" aria-label="生产观察名单">
    ${sectionHead('生产观察名单', `控制组 Top${formatNumber(candidates.length)} · 不是买入清单`, { href: './decision-candidates.html', label: '全部名单 →' })}
    <div class="obs-stack">${items}</div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 2. 市场行情：A 股三指数 + 涨跌面（完整八指数在市场页）
// ---------------------------------------------------------------------------

const INDEX_CARDS = [
  { key: 'shanghai', label: '上证指数', tag: '实采', closeDigits: 2 },
  { key: 'shenzhen', label: '深证成指', tag: '实采', closeDigits: 1 },
  { key: 'chinext', label: '创业板指', tag: '实采', closeDigits: 2 },
  { key: 'a50', label: '富时A50 代理', tag: '代理', closeDigits: 2 },
  { key: 'golden_dragon', label: '金龙指数 代理', tag: '代理', closeDigits: 2 },
  { key: 'spx', label: '标普 500', tag: '实采', closeDigits: 2 },
  { key: 'nasdaq', label: '纳斯达克', tag: '实采', closeDigits: 1 },
  { key: 'cnh', label: 'USD/CNH', tag: '汇率', closeDigits: 4 }
];

// 只认数据里真实存在的时序数组；不存在就返回 null（绝不构造假序列）。
function seriesOf(entry) {
  const candidates = [entry.bars, entry.series, entry.closes, entry.history, entry.trend];
  for (const arr of candidates) {
    if (Array.isArray(arr) && arr.length >= 2) return arr;
  }
  return null;
}

function indexCard(def, entry, model) {
  const data = entry || {};
  const change = finiteOrNull(data.change_pct);
  const close = finiteOrNull(data.close);
  const series = seriesOf(data);

  let valueHtml;
  let arrowHtml = '';
  let note;
  let toneClass = 'pct-flat';
  if (change !== null) {
    toneClass = change > 0 ? 'pct-up' : change < 0 ? 'pct-down' : 'pct-flat';
    const arrow = change > 0 ? '▲' : change < 0 ? '▼' : '—';
    arrowHtml = `<span class="idx-arrow" aria-hidden="true">${arrow}</span> `;
    valueHtml = pctHtml(change);
    note = close !== null ? `收盘 ${formatNumber(close, def.closeDigits)}` : '';
  } else if (def.key === 'a50') {
    // A50 快照缺失时退回市场环境数据里的隔夜外盘涨跌（真实数据，注明来源口径）。
    const external = finiteOrNull(((model.marketContext || {}).external_factors || {}).a50_change_pct);
    if (external !== null) {
      toneClass = external > 0 ? 'pct-up' : external < 0 ? 'pct-down' : 'pct-flat';
      const arrow = external > 0 ? '▲' : external < 0 ? '▼' : '—';
      arrowHtml = `<span class="idx-arrow" aria-hidden="true">${arrow}</span> `;
      valueHtml = pctHtml(external);
      note = '隔夜外盘数据';
    } else {
      valueHtml = '<span class="pct-flat num">—</span>';
      note = '数据源暂时不可用';
    }
  } else {
    valueHtml = '<span class="pct-flat num">—</span>';
    note = '数据源暂时不可用';
  }

  const closeLine = close !== null
    ? `<div class="idx-close num ${toneClass}">${formatNumber(close, def.closeDigits)}</div>`
    : '';

  return `<div class="idx-card">
    <div class="idx-top"><span class="idx-name">${escapeHtml(def.label)}</span><span class="idx-tag">${escapeHtml(def.tag)}</span></div>
    ${closeLine}
    <div class="idx-chg num">${arrowHtml}${valueHtml}</div>
    ${series ? sparkline(series, { width: 200, height: 34, label: def.label }) : ''}
    ${note ? `<span class="help-text num idx-note">${escapeHtml(note)}</span>` : ''}
  </div>`;
}

// 当日涨跌面（行业宽度口径）：positive_sector_count / sector_count + 平均涨跌幅。
function breadthCard(model) {
  const summary = (model.marketState || {}).market_summary || {};
  const total = finiteOrNull(summary.sector_count);
  const positive = finiteOrNull(summary.positive_sector_count);
  const avg = finiteOrNull(summary.average_sector_change_pct);
  const strong = finiteOrNull(summary.strong_signal_sector_count);
  const ratio = finiteOrNull(summary.positive_sector_ratio);

  if (total === null || total <= 0 || positive === null) {
    return `<section class="breadth-card">
      <h4 class="panel-title">当日涨跌面</h4>
      ${emptySection('暂无行业宽度数据', '本期数据没有行业涨跌统计，可前往市场行情页查看其他内容。')}
    </section>`;
  }

  const negative = Math.max(total - positive, 0);
  const upPct = total > 0 ? (positive / total) * 100 : 0;
  const downPct = 100 - upPct;
  const ratioPctText = ratio !== null ? formatPct(ratio * 100, 1) : formatPct(upPct, 1);

  const avgHtml = avg !== null
    ? `<span class="breadth-big num ${avg > 0 ? 'pct-up' : avg < 0 ? 'pct-down' : 'pct-flat'}">${pctHtmlText(avg)}</span>`
    : '<span class="breadth-big num pct-flat">—</span>';

  return `<section class="breadth-card">
    <h4 class="panel-title">当日涨跌面 · ${escapeHtml(formatNumber(total))} 个行业</h4>
    <div class="breadth-stat">
      ${avgHtml}<span class="breadth-lbl">行业平均涨跌</span>
    </div>
    <div class="breadth-bar" role="img" aria-label="${escapeHtml(`${formatNumber(positive)} 个行业上涨，${formatNumber(negative)} 个行业下跌`)}" title="${escapeHtml(`${formatNumber(positive)} 涨 / ${formatNumber(negative)} 跌`)}">
      <span class="b-up" style="width:${upPct.toFixed(1)}%"></span>
      <span class="b-down" style="width:${downPct.toFixed(1)}%"></span>
    </div>
    <div class="breadth-leg">
      <span class="pct-up">${escapeHtml(formatNumber(positive))} 行业上涨</span>
      <span class="pct-down">${escapeHtml(formatNumber(negative))} 行业下跌</span>
    </div>
    <p class="help-text breadth-note">${[
    strong !== null ? `强信号行业 <b>${escapeHtml(formatNumber(strong))}</b> 个` : '',
    `上涨行业占比 <b>${escapeHtml(ratioPctText)}</b>`,
    '口径为行业宽度（非个股家数）。'
  ].filter(Boolean).join(' · ')}</p>
  </section>`;
}

// 文本版上色百分比（用于 breadth 大数字内部，避免双重 span 包裹）。
function pctHtmlText(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(2)}%`;
}

function marketSection(model) {
  const head = (sub) => sectionHead('市场行情', sub, { href: './market-overview.html', label: '完整市场 →' });

  if (model.isMissing('marketState')) {
    return `<section aria-label="市场行情">
      ${head('')}
      ${missingSection('市场行情', model.missingReason('marketState'))}
    </section>`;
  }

  const marketState = model.marketState || {};
  const snapshot = marketState.session_snapshot || {};
  if (!Object.keys(snapshot).length) {
    return `<section aria-label="市场行情">
      ${head('')}
      ${emptySection('市场行情', '本期数据中没有指数行情快照，可前往市场行情页查看其他内容。')}
    </section>`;
  }

  const snapDate = dateCn(marketState.latest_trade_date || (model.runManifest || {}).trade_date);
  const cards = INDEX_CARDS.slice(0, 3).map((def) => indexCard(def, snapshot[def.key], model)).join('\n');

  return `<section aria-label="市场行情">
    ${head(`上证 / 深证 / 创业板 · 交易日 ${snapDate} 收盘`)}
    <div class="idx-grid">${cards}</div>
    <div class="breadth-grid">${breadthCard(model)}</div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 3. 晨判 · AI 市场摘要卡（marketState.morning；AI 三态显性）
// ---------------------------------------------------------------------------

function morningSection(model) {
  const morning = (model.marketState || {}).morning || {};
  const summaryRaw = cleanAnalysisText(morning.ai_summary);
  const advice = cleanAnalysisText(morning.ai_action_advice);
  const focus = Array.isArray(morning.focus_sectors) ? morning.focus_sectors.filter(hasText) : [];

  // 没有任何真实晨判内容时，不编造——给出诚实占位。
  if (!hasText(summaryRaw) && !hasText(advice) && !focus.length) {
    return `<section aria-label="晨判 AI 市场摘要">
      ${sectionHead('晨判 · AI 市场摘要', '开盘前的一句话市场判断')}
      ${emptySection('今日暂无 AI 晨判', '本期没有生成可展示的 AI 晨判内容，请以市场行情与系统说明页为准。')}
    </section>`;
  }

  // AI 三态徽章：morning 自身字段判定（ai_summary 真实 → ai-full；标陈旧 → ai-stale）。
  const aiItem = {
    ai_summary: morning.ai_summary,
    ai_source_kind: morning.ai_source_kind,
    ai_source_name: morning.ai_source_name,
    ai_source_stale: morning.ai_source_stale,
    ai_source_date: morning.ai_source_date,
    trade_date: morning.market_data_trade_date || morning.trade_date
  };

  const sectorChips = focus.length
    ? `<div class="sectors">${focus.slice(0, 6).map((s, i) => `<span class="sector-chip${i < 3 ? ' hot' : ''}">${escapeHtml(safeText(s))}</span>`).join('')}</div>`
    : '';

  const summaryHtml = hasText(summaryRaw)
    ? `<p class="morning-summary">${escapeHtml(summaryRaw)}</p>`
    : '';
  const adviceHtml = hasText(advice)
    ? `<p class="morning-advice-text">${escapeHtml(advice)}</p>`
    : '';

  return `<section aria-label="晨判 AI 市场摘要">
    ${sectionHead('晨判 · AI 市场摘要', `开盘前的一句话市场判断 · 基于 ${dateCn(morning.market_data_trade_date || morning.trade_date)} 收盘`)}
    <section class="elevated-card morning-card">
      <div class="morning-head">
        <span class="ai-dot" aria-hidden="true"></span>
        <h4 class="panel-title">今晨市场判断</h4>
        ${aiStatusBadge(aiItem)}
      </div>
      ${summaryHtml}
      ${adviceHtml}
      ${focus.length ? `<div class="morning-focus"><span class="morning-focus-label">关注主线</span>${sectorChips}</div>` : ''}
    </section>
  </section>`;
}

// ---------------------------------------------------------------------------
// 4. 今日执行清单（execution_state 权威；纪律 banner + 主攻 / 观察分表）
// ---------------------------------------------------------------------------

const EXEC_COLUMNS = [
  '标的',
  '动作',
  { label: '仓位档', align: 'left' },
  '买点 / 触发位',
  '失效条件',
  { label: '来源', align: 'right' }
];

function actionBadgeHtml(row) {
  const action = safeText(row.adjusted_action || row.raw_action, 'watch');
  if (action === 'main') return badge('主攻', 'ok');
  if (action === 'avoid') return badge('回避', 'bad');
  return badge('观察', 'info');
}

function executionRow(row, lookup) {
  return [
    { html: stockCellHtml(row, lookup) },
    { html: actionBadgeHtml(row), align: 'left' },
    { html: positionTierHtml(row.position_tier), align: 'left' },
    { html: `<span class="buyzone">${escapeHtml(safeText(row.buy_zone, '—'))}</span>` },
    { html: `<span class="invalid">${escapeHtml(safeText(row.invalidation, '—'))}</span>` },
    { html: `<span class="src-chip">${escapeHtml(strategyLabel(row.strategy_source))}</span>`, align: 'right' }
  ];
}

function executionTable(rows, lookup) {
  return dataTable({
    columns: EXEC_COLUMNS,
    rows: rows.map((row) => executionRow(row, lookup)),
    emptyText: '暂无记录',
    tableClass: 'exec-table'
  });
}

function miniStat(value, label, tone) {
  return `<div class="mini-stat ${escapeHtml(tone)}"><div class="mini-v num">${escapeHtml(value)}</div><div class="mini-l">${escapeHtml(label)}</div></div>`;
}

function executionSection(model) {
  const head = (sub) => sectionHead('执行层分层', sub, { href: './decision-candidates.html', label: '个股推荐 →' });

  if (model.isMissing('executionState')) {
    return `<section aria-label="今日执行清单">
      ${head('')}
      ${missingSection('今日执行清单', model.missingReason('executionState'))}
    </section>`;
  }

  const exec = model.executionState || {};
  const summary = model.executionSummary || { total: 0, main: 0, watch: 0, avoid: 0 };
  const rows = Array.isArray(exec.executions) ? exec.executions : [];
  const policy = safeText((model.marketContext || {}).policy, '').trim();

  if (!rows.length) {
    return `<section aria-label="今日执行清单">
      ${head('')}
      ${emptySection('今日没有执行建议', policy
        ? `系统本期未生成任何执行建议。${policy}`
        : '系统本期未生成任何执行建议，请等待下一个交易日的数据。')}
    </section>`;
  }

  const lookup = buildNameLookup(model);
  const mains = rows.filter((row) => row.adjusted_action === 'main');
  const watches = rows.filter((row) => row.adjusted_action === 'watch');
  const avoids = rows.filter((row) => row.adjusted_action === 'avoid');

  const sub = `主攻 ${formatNumber(summary.main)} · 观察 ${formatNumber(summary.watch)}${summary.avoid ? ` · 回避 ${formatNumber(summary.avoid)}` : ''}`;

  // 纪律 banner：主攻为空（全部转观察）时，如实显性说明「今日无主攻标的」（诚实性断言）。
  const bannerTitle = mains.length
    ? `今日 ${formatNumber(mains.length)} 只主攻 · ${formatNumber(watches.length)} 只观察`
    : '今日无主攻标的 · 全部转「观察」';
  const bannerBody = mains.length
    ? (policy ? `系统纪律：${policy}` : '主攻为可执行买入标的，观察标的需等待确认信号再动作。')
    : (policy
      ? `策略层已产出候选，但执行层在「${policy}」纪律下，将全部标的统一降为观察。纪律优先于名单：等待盘中放量突破关键位再轻仓试探，不主动追高。`
      : '当前市场环境下，系统没有把任何股票升级为可买入的主攻标的，全部转为观察。');

  const banner = `<div class="exec-banner">
    <div class="exec-banner-ico" aria-hidden="true">${mains.length ? '◎' : '⏸'}</div>
    <div class="exec-banner-text">
      <h4 class="panel-title">${escapeHtml(bannerTitle)}</h4>
      <p class="help-text">${escapeHtml(bannerBody)}</p>
    </div>
    <div class="exec-counts">
      ${miniStat(formatNumber(summary.main), '主攻', 'main')}
      ${miniStat(formatNumber(summary.watch), '观察', 'watch')}
    </div>
  </div>`;

  const mainHtml = mains.length
    ? `<h5 class="panel-title u-mt-2">主攻（可执行买入，${formatNumber(mains.length)} 只）</h5>
      ${executionTable(mains, lookup)}`
    : '';

  let watchHtml = '';
  if (watches.length) {
    const visible = watches.slice(0, 10);
    const rest = watches.slice(10);
    watchHtml = `<h5 class="panel-title u-mt-2">观察（等待确认信号，${formatNumber(watches.length)} 只，先列前 ${formatNumber(visible.length)} 只）</h5>
      ${executionTable(visible, lookup)}
      ${rest.length ? `<details class="u-mt-1">
        <summary class="text-link">展开其余 ${formatNumber(rest.length)} 只观察标的</summary>
        ${executionTable(rest, lookup)}
      </details>` : ''}`;
  }

  const avoidNote = avoids.length
    ? `<p class="help-text u-mt-1">另有 ${formatNumber(avoids.length)} 条回避建议（不建议买入），明细见个股推荐页。</p>`
    : '';

  return `<section aria-label="今日执行清单">
    ${head(sub)}
    <section class="elevated-card exec-wrap">
      ${banner}
      ${mainHtml}
      ${watchHtml}
      ${avoidNote}
    </section>
  </section>`;
}

// ---------------------------------------------------------------------------
// 5. 近期战绩：comboBarLine（逐日次日收益柱 + 命中率折线）+ 最近一期 KPI
// ---------------------------------------------------------------------------

function dateKey(entry) {
  return safeText(entry.recommend_date, '').replace(/-/g, '');
}

function performanceSection(model) {
  const head = (sub) => sectionHead('近期战绩', sub, { href: './recommendation-review.html', label: '历史战绩 →' });

  if (model.isMissing('reviewState')) {
    return `<section aria-label="近期战绩">
      ${head('')}
      ${missingSection('近期战绩', model.missingReason('reviewState'))}
    </section>`;
  }

  const stats = Array.isArray((model.reviewState || {}).date_stats) ? model.reviewState.date_stats : [];
  if (!stats.length) {
    return `<section aria-label="近期战绩">
      ${head('')}
      ${emptySection('暂无可验证数据', '系统还没有积累出可评估的推荐记录（推荐需要等到下一个交易日收盘后才能验证）。')}
    </section>`;
  }

  // date_stats 多为倒序，统一排序后取最近 14 期，按时间从左到右绘制。
  const recent = stats
    .slice()
    .sort((a, b) => (dateKey(a) < dateKey(b) ? -1 : dateKey(a) > dateKey(b) ? 1 : 0))
    .slice(-14);

  // comboBarLine：柱=逐日平均次日收益（正红负绿），折线=命中率（右轴蓝）。
  const bars = recent.map((entry) => ({
    label: dateCn(entry.next_trade_date || entry.recommend_date),
    value: finiteOrNull(entry.avg_next_day_return_pct)
  }));
  const line = recent.map((entry) => finiteOrNull(entry.next_day_hit_rate_pct));

  const chart = comboBarLine(bars, line, {
    barDigits: 2, lineDigits: 0,
    label: '逐日次日收益与命中率',
    emptyText: '暂无逐日结算数据'
  });

  // 汇总 KPI：14 期均次日收益、平均命中率、最佳单日（全部来自真实数据，负值照实）。
  const retVals = bars.map((b) => b.value).filter((v) => v !== null);
  const hitVals = line.filter((v) => v !== null);
  const avgRet = retVals.length ? retVals.reduce((s, v) => s + v, 0) / retVals.length : null;
  const avgHit = hitVals.length ? hitVals.reduce((s, v) => s + v, 0) / hitVals.length : null;
  let best = null;
  recent.forEach((entry) => {
    const v = finiteOrNull(entry.avg_next_day_return_pct);
    if (v === null) return;
    if (best === null || v > best.value) best = { value: v, date: dateCn(entry.next_trade_date || entry.recommend_date) };
  });

  const kpis = `<div class="stat-grid perf-kpi-grid">
    ${statCard({
    title: `近 ${formatNumber(recent.length)} 期均次日收益`,
    valueHtml: avgRet === null ? '—' : pctHtml(avgRet),
    note: '逐日推荐组合等权平均收益的均值'
  })}
    ${statCard({
    title: '平均命中率',
    value: avgHit === null ? '—' : formatPct(avgHit, 0),
    note: '各交易日「次日上涨比例」的简单平均',
    tone: 'info'
  })}
    ${statCard({
    title: '最佳单日',
    valueHtml: best ? pctHtml(best.value) : '—',
    note: best ? `次日 ${best.date} 收盘验证` : '—'
  })}
  </div>`;

  return `<section aria-label="近期战绩">
    ${head(`启动前夕 · 最近 ${formatNumber(recent.length)} 个可评估交易日 · 每日 20 只`)}
    <section class="elevated-card perf-card">
      <div class="chart-block">
        <div class="combo-legend">
          <span><i class="lg-up"></i>次日收益 正(红)</span>
          <span><i class="lg-down"></i>次日收益 负(绿)</span>
          <span><i class="lg-line"></i>命中率(右轴)</span>
        </div>
        ${chart}
      </div>
      ${kpis}
    </section>
    <p class="help-text">命中率 = 当日推荐股票中、次日收盘上涨的比例；收益为等权平均、按次日收盘价计算，不含交易成本。</p>
  </section>`;
}

// ---------------------------------------------------------------------------
// 6. 三策略权重条：decision.strategy_weights 权重 + 今日入选数（执行清单口径）
// ---------------------------------------------------------------------------

const STRATEGY_ROWS = [
  { id: 'prebreakout_v41', tabKey: 'prebreakout', tone: 'brand', tag: '主策略', factoryHref: './prebreakout-shadow.html' },
  { id: 'greenfield_o2c_v1', tabKey: 'o2c', tone: 'accent', tag: '日内' },
  { id: 't1_factor_v1', tabKey: 't1', tone: 'warn', tag: '因子' }
];

function strategySection(model) {
  const decision = model.decisionState || {};
  const weights = decision.strategy_weights || {};
  const counts = (model.executionState || {}).strategy_counts || {};
  const execMissing = model.isMissing('executionState');
  const t1Research = Boolean(((decision.data_status) || {}).t1_research_mode);

  const hasWeights = STRATEGY_ROWS.some((def) => finiteOrNull(weights[def.id]) !== null);

  const rows = STRATEGY_ROWS.map((def, idx) => {
    const weight = finiteOrNull(weights[def.id]);
    const weightPct = weight === null ? null : weight * 100;
    const count = finiteOrNull(counts[def.id]);
    const isResearch = def.id === 't1_factor_v1' && t1Research;
    const countText = count === null ? '—' : formatNumber(count);
    const href = `./decision-candidates.html#tab=${encodeURIComponent(def.tabKey)}`;
    // 权重条宽度：以最高权重为满格更直观；此处直接用权重百分比映射（0~100）。
    const barW = weightPct === null ? 0 : Math.max(0, Math.min(100, weightPct));
    const tagBadge = isResearch ? badge('研究态', 'warn') : badge(def.tag, def.tone);
    const goHtml = isResearch
      ? '<span class="strat-go soft">研究中</span>'
      : `<a class="strat-go text-link" href="${escapeHtml(href)}">进入 →</a>`;
    const factoryHtml = def.factoryHref
      ? `<div class="strat-factory"><a class="text-link" href="${escapeHtml(def.factoryHref)}">双轨观察 →</a></div>`
      : '';

    return `<div class="strat-row">
      <div class="strat-rank${isResearch ? ' dim' : ''}">${idx + 1}</div>
      <div class="strat-info">
        <div class="strat-n">${escapeHtml(strategyLabel(def.id))} ${tagBadge}</div>
        <div class="strat-w num">权重 ${weightPct === null ? '—' : formatPct(weightPct, 1)}</div>
        <div class="wbar"><i style="width:${barW.toFixed(1)}%"></i></div>
        ${factoryHtml}
      </div>
      <div class="strat-cnt">
        <div class="strat-c num${isResearch ? ' dim' : ''}">${escapeHtml(countText)}</div>
        <div class="strat-u">只入选</div>
        ${goHtml}
      </div>
    </div>`;
  }).join('\n');

  const note = !hasWeights
    ? '<p class="help-text u-mt-1">本期决策层未提供策略权重，仅显示入选数量。</p>'
    : '';
  const execNote = execMissing
    ? '<p class="help-text u-mt-1">执行清单数据暂未加载，入选数量无法显示。</p>'
    : '';
  const factoryNote = '<p class="help-text u-mt-1">启动前夕另有三组固定影子组合与公告事件轨（均只观察）：<a class="text-link" href="./prebreakout-shadow.html">打开双轨验证页 →</a></p>';

  return `<section aria-label="三策略权重与入选">
    ${sectionHead('三策略 · 今日入选', '权重源自决策层 · 入选数为执行清单口径')}
    <section class="elevated-card strat-card">
      ${rows}
      ${note}
      ${execNote}
      ${factoryNote}
    </section>
  </section>`;
}

// ---------------------------------------------------------------------------
// 7. 个股一瞥卡网格：候选 Top（股名/代码/涨跌/综合分/AI分/获利盘比例）
// ---------------------------------------------------------------------------

function glanceCard(item) {
  const code = normCode(item.normalized_code || item.code);
  const name = safeText(item.name, '').trim();
  const industry = safeText(item.industry_name, '').trim();
  const change = finiteOrNull(item.current_change_pct ?? item.change_pct);
  const score = finiteOrNull(item.score);
  const aiScore = finiteOrNull(item.ai_score);
  const winnerRate = finiteOrNull(item.winner_rate);

  const nameHtml = name
    ? `<div class="gl-nm">${escapeHtml(name)}</div>`
    : `<div class="gl-nm num">${escapeHtml(code || '未知代码')}</div>`;

  const meta = [];
  if (score !== null) meta.push(`<span>综合分 <b class="num">${escapeHtml(formatNumber(score, 1))}</b></span>`);
  if (aiScore !== null) meta.push(`<span>AI <b class="num">${escapeHtml(formatNumber(aiScore))}</b></span>`);
  if (winnerRate !== null) meta.push(`<span>获利盘 <b class="num">${escapeHtml(formatPct(winnerRate, 1))}</b></span>`);

  return `<div class="gl-card">
    <div class="gl-top">
      <div>${nameHtml}${name && code ? `<div class="gl-cd num">${escapeHtml(code)}</div>` : ''}</div>
      <div class="gl-chg num">${pctHtml(change)}</div>
    </div>
    ${industry ? `<div><span class="gl-ind">${escapeHtml(industry)}</span></div>` : ''}
    ${meta.length ? `<div class="gl-meta">${meta.join('')}</div>` : '<div class="gl-meta soft">暂无评分数据</div>'}
  </div>`;
}

function glanceSection(model) {
  const head = (sub) => sectionHead('个股一瞥 · 候选', sub, { href: './decision-candidates.html', label: '全部候选 →' });

  if (model.isMissing('candidateState')) {
    return `<section aria-label="个股一瞥">
      ${head('')}
      ${missingSection('个股一瞥', model.missingReason('candidateState'))}
    </section>`;
  }

  const candidates = Array.isArray(model.candidates) ? model.candidates : [];
  if (!candidates.length) {
    return `<section aria-label="个股一瞥">
      ${head('')}
      ${emptySection('今日暂无候选', '系统本期没有产出可展示的候选个股，请等待下一个交易日的数据。')}
    </section>`;
  }

  const cards = candidates.slice(0, 8).map(glanceCard).join('\n');
  const strategyName = safeText((candidates[0] || {}).strategy_name, '').trim() || strategyLabel('prebreakout_v41');

  return `<section aria-label="个股一瞥">
    ${head(`${strategyName} Top${formatNumber(Math.min(candidates.length, 8))} · 综合分排序`)}
    <div class="glance-grid">${cards}</div>
  </section>`;
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

export function renderDashboard(model) {
  const body = [
    deskMasthead(model),
    observationDesk(model),
    marketSection(model),
    morningSection(model),
    executionSection(model),
    performanceSection(model)
  ].join('\n');
  return renderShell('dashboard', model, body);
}
