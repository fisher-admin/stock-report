// v4/render/candidateCard.js — 唯一的候选股卡片组件（纯函数：item → HTML 字符串，无 DOM 依赖，Node 可执行）。
//
// 视觉升级：高密度金融表格列表项 + 平滑向下展开的深度 AI 分析抽屉。
// 默认不展开 AI 观点与因子大图，用户点击代码/名称或整行时平滑展开。

import {
  escapeHtml, safeText, formatNumber, formatPct, pctHtml, dateCn,
  aiStatusOf, aiSourceDate, cleanAnalysisText,
  actionLabel, actionTone, factorShortLabel
} from './format.js';
import { badge, aiStatusBadge, chipList } from './components.js';
import { scoreBar } from './charts.js';

// ---------------------------------------------------------------------------
// 基础字段读取（只做别名归一，不编造内容）
// ---------------------------------------------------------------------------

function codeOf(item = {}) {
  const direct = safeText(item.normalized_code || item.stock_code || item.code, '');
  if (direct) return direct;
  const tsCode = safeText(item.ts_code, '');
  return tsCode ? tsCode.replace(/\.\w+$/, '') : '';
}

function nameOf(item = {}) {
  return safeText(item.name || item.stock_name || item.security_name, '') || codeOf(item) || '未知股票';
}

function industryOf(item = {}) {
  return safeText(item.industry_name || item.industry || item.sector_name, '');
}

function rankOf(item = {}, index = 0) {
  const raw = item.rank ?? item.rank_no ?? item.displayRank ?? item.strategy_rank;
  const num = Number(raw);
  return Number.isFinite(num) && num > 0 ? num : index + 1;
}

function firstFinite(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === '') continue;
    const num = Number(value);
    if (Number.isFinite(num)) return num;
  }
  return null;
}

export function stockAnchorId(item = {}) {
  const key = codeOf(item) || nameOf(item);
  return `stock-${safeText(key, 'stock').replace(/[^a-zA-Z0-9_-]/g, '-')}`;
}

// ---------------------------------------------------------------------------
// 动作：只读管线字段链（诚实性规范 0.4），不做任何文案推断。
// ---------------------------------------------------------------------------

const ACTION_VALUES = new Set(['main', 'watch', 'avoid']);

function resolveActionDetail(item = {}) {
  if (item.strategy_research_only === true || item.research_only === true) {
    return { action: 'watch', layer: 'research' };
  }
  const chain = [
    [item.final_action, 'execution'],
    [item.role_type, 'candidate'],
    [item.roleType, 'candidate'],
    [item.gate_adjusted_action, 'execution'],
    [item.adjusted_action, 'execution'],
    [item.final_candidate_action, 'execution']
  ];
  for (const [raw, layer] of chain) {
    const value = safeText(raw, '').trim().toLowerCase();
    if (ACTION_VALUES.has(value)) return { action: value, layer };
  }
  return { action: 'watch', layer: 'none' };
}

export function resolveAction(item = {}) {
  return resolveActionDetail(item).action;
}

// ---------------------------------------------------------------------------
// 执行清单匹配：仅当 execution_state 确实包含该股时返回条目。
// ---------------------------------------------------------------------------

export function executionFor(executions, item, strategyId = '') {
  const list = Array.isArray(executions) ? executions : [];
  const code = codeOf(item);
  if (!code) return null;
  return list.find((entry) => {
    if (!entry || safeText(entry.stock_code, '') !== code) return false;
    if (strategyId && safeText(entry.strategy_source, '') !== strategyId) return false;
    return true;
  }) || null;
}

// ---------------------------------------------------------------------------
// 量化分：取真实分值
// ---------------------------------------------------------------------------

function quantScore(item) {
  const num = firstFinite(item.score, item.composite_score, item.quant_score);
  if (num === null) return { num: null, text: '—' };
  return { num, text: formatNumber(num, Math.abs(num) < 10 ? 2 : 1) };
}

function metricRowHtml(item, score) {
  const price = firstFinite(item.current_price, item.price, item.close);
  const changePct = firstFinite(item.current_change_pct, item.change_pct, item.pct_chg);
  const priceDate = safeText(item.current_price_trade_date || item.recommend_date || item.trade_date, '');

  const cells = [
    `<div><span>现价</span><strong class="num">${escapeHtml(formatNumber(price, 2))}</strong></div>`,
    `<div><span>涨跌</span><strong>${pctHtml(changePct)}</strong></div>`,
    `<div><span>量化分</span><strong class="num">${escapeHtml(score.text)}</strong></div>`
  ];
  if (item.winner_rate !== null && item.winner_rate !== undefined) {
    cells.push(`<div><span>获利盘</span><strong class="num">${escapeHtml(formatPct(item.winner_rate, 1))}</strong></div>`);
  }
  if (item.volume_ratio !== null && item.volume_ratio !== undefined) {
    cells.push(`<div><span>量比</span><strong class="num">${escapeHtml(formatNumber(item.volume_ratio, 2))}</strong></div>`);
  }
  if (priceDate) {
    cells.push(`<div><span>数据日</span><strong class="num">${escapeHtml(dateCn(priceDate))}</strong></div>`);
  }
  return `<div class="metric-row">${cells.join('')}</div>`;
}

function scoreBarHtml(score, action) {
  if (score.num === null || !(score.num >= 0 && score.num <= 100)) return '';
  const tone = action === 'main' ? 'brand' : action === 'avoid' ? 'down' : 'brand-2';
  return `<div class="candidate-score">
    <span class="candidate-score-label">综合评分</span>
    ${scoreBar(score.num, { tone, max: 100, width: 240 })}
  </div>`;
}

// ---------------------------------------------------------------------------
// AI 区与量化证据区
// ---------------------------------------------------------------------------

const POINT_LABELS = {
  ideal_buy: '理想买点',
  secondary_buy: '次选买点',
  buy_zone: '买点参考',
  stop_loss: '止损位',
  take_profit: '目标位',
  trigger: '触发条件',
  invalidation: '失效条件',
  support: '支撑位',
  resistance: '压力位'
};

function aiPointsHtml(points) {
  const lines = [];
  if (typeof points === 'string') {
    cleanAnalysisText(points).split(/\n+/).map((line) => line.trim()).filter(Boolean)
      .forEach((line) => lines.push(escapeHtml(line)));
  } else if (Array.isArray(points)) {
    points.forEach((entry) => {
      const text = cleanAnalysisText(typeof entry === 'string' ? entry : safeText(entry, ''));
      if (text) lines.push(escapeHtml(text));
    });
  } else if (points && typeof points === 'object') {
    Object.entries(points).forEach(([key, value]) => {
      const text = cleanAnalysisText(safeText(value, ''));
      if (!text) return;
      const label = POINT_LABELS[key];
      lines.push(label ? `<strong>${escapeHtml(label)}</strong>：${escapeHtml(text)}` : escapeHtml(text));
    });
  }
  if (!lines.length) return '';
  return `<ul>${lines.map((line) => `<li>${line}</li>`).join('')}</ul>`;
}

function aiRisksHtml(item) {
  const raw = [
    ...(Array.isArray(item.ai_risks) ? item.ai_risks : []),
    item.ai_risk_warning
  ];
  const seen = new Set();
  const risks = [];
  raw.forEach((entry) => {
    const text = cleanAnalysisText(safeText(entry, ''));
    if (text && !seen.has(text)) {
      seen.add(text);
      risks.push(text);
    }
  });
  if (!risks.length) return '';
  return `<p class="help-text"><strong>风险提示</strong>：${escapeHtml(risks.slice(0, 3).join('；'))}</p>`;
}

function quantChips(item) {
  const chips = [];
  const isT1 = String(item.strategy_id || '') === 't1_factor_v1' || item.ai_analysis_type === 't1_template_note';
  if (isT1) {
    const fscore = firstFinite(item.score, item.composite_score, item.factor_score);
    if (fscore !== null && fscore !== undefined && Number.isFinite(Number(fscore))) {
      chips.push({ label: '合成因子分', value: formatNumber(fscore, 3) });
    }
    const frank = Number(item.factor_rank_in_top20 ?? item.rank_no ?? item.rank);
    if (Number.isFinite(frank) && frank > 0) {
      chips.push({ label: 'top20内排名', value: `第 ${frank}/20` });
    }
  }
  if (item.winner_rate !== null && item.winner_rate !== undefined) {
    chips.push({ label: '获利盘比例', value: formatPct(item.winner_rate, 1) });
  }
  if (item.chip_conc !== null && item.chip_conc !== undefined) {
    chips.push({ label: '筹码集中度', value: formatNumber(item.chip_conc, 3) });
  }
  if (item.chip_support !== null && item.chip_support !== undefined) {
    chips.push({ label: '筹码支撑位', value: formatNumber(item.chip_support, 2) });
  }
  if (item.chip_resistance !== null && item.chip_resistance !== undefined) {
    chips.push({ label: '筹码压力位', value: formatNumber(item.chip_resistance, 2) });
  }
  if (item.factor_details && typeof item.factor_details === 'object' && !Array.isArray(item.factor_details)) {
    Object.entries(item.factor_details).slice(0, 6).forEach(([key, detail]) => {
      const value = detail && typeof detail === 'object' ? detail.value : detail;
      chips.push({
        label: factorShortLabel(key),
        value: value !== null && value !== undefined ? formatNumber(value, 3) : '—'
      });
    });
  } else if (item.factor_values && typeof item.factor_values === 'object' && !Array.isArray(item.factor_values)) {
    Object.entries(item.factor_values).slice(0, 6).forEach(([key, value]) => {
      chips.push({ label: factorShortLabel(key), value: formatNumber(value, 3) });
    });
  }
  return chips;
}

function quantBlockHtml(item, status = 'ai-none') {
  const chips = quantChips(item);
  const hasAiAnalysis = status !== 'ai-none';
  if (!chips.length) {
    return `<div class="ai-none-block">
      <p>${hasAiAnalysis
        ? 'AI 分析已展示；本期没有可公开的量化信号明细。'
        : '本期没有 AI 个股分析，数据中也没有可展示的量化信号明细。'}</p>
    </div>`;
  }
  const hasChipLevels = (item.chip_support !== null && item.chip_support !== undefined)
    || (item.chip_resistance !== null && item.chip_resistance !== undefined);
  const footnote = hasChipLevels
    ? '<p class="help-text">筹码支撑/压力位为相对持仓成本中枢的比值，1.00 代表成本中枢价位。</p>'
    : '';
  return `<div class="ai-none-block">
    <p>${hasAiAnalysis
      ? '以下为量化模型的原始信号读数，用于与上方 AI 分析分开核对：'
      : '本期没有 AI 个股分析，以下为量化模型的真实信号读数：'}</p>
    ${chipList(chips)}
    ${footnote}
  </div>`;
}

function execBlockHtml(execution) {
  if (!execution || typeof execution !== 'object') return '';
  const tier = Number(execution.position_tier);
  const entries = [
    ['执行建议', actionLabel(resolveAction(execution))],
    ['买点参考', safeText(execution.buy_zone, '')],
    ['失效条件', safeText(execution.invalidation, '')],
    ['仓位档位', Number.isFinite(tier) ? `第 ${formatNumber(tier)} 档` : ''],
    ['次日处理', safeText(execution.next_day_handling, '')]
  ].filter(([, value]) => value);
  if (!entries.length) return '';
  return `<div class="exec-block">
    <div class="panel-title">执行参考（盘前执行清单口径）</div>
    <div class="exec-grid">${entries.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('')}</div>
  </div>`;
}

function actionChainHtml(item) {
  const rows = [
    ['原始动作', actionLabel(safeText(item.raw_action, ''))],
    ['门槛后动作', actionLabel(safeText(item.gate_adjusted_action, ''))],
    ['最终动作', actionLabel(safeText(item.final_action, ''))],
    ['是否研究观察', item.research_only === true ? '是（仅供观察）' : (item.research_only === false ? '否' : '')]
  ].filter(([, v]) => v);
  if (!rows.length) return '';
  const reasons = (Array.isArray(item.adjustment_reasons) ? item.adjustment_reasons : []).filter((r) => typeof r === 'string' && r.trim());
  return `<section class="analysis-section">
    <div class="panel-title">一、最终动作</div>
    <div class="exec-grid">${rows.map(([l, v]) => `<div><span>${escapeHtml(l)}</span><strong>${escapeHtml(v)}</strong></div>`).join('')}</div>
    ${reasons.length ? `<p class="help-text"><strong>调整原因</strong>：${escapeHtml(reasons.join('；'))}</p>` : ''}
  </section>`;
}

function aiConclusionHtml(item, status) {
  const type = item.ai_analysis_type || (status === 'ai-none' ? 'none' : 'trading_ai');
  if (type === 'none') {
    return `<section class="analysis-section ai-uncovered">
      <div class="panel-title">二、AI 分析</div>
      <p class="help-text"><strong>AI 未覆盖</strong>：当前仅展示该股的量化因子证据，尚未生成完整 AI 分析。</p>
    </section>`;
  }
  const isResearch = type === 't1_template_note' || type === 't1_research_ai';
  const summary = cleanAnalysisText(safeText(item.ai_summary || item.ai_conclusion, ''));
  const advice = safeText(item.ai_advice, '');
  const researchNote = isResearch
    ? `<p class="help-text research-note"><strong>研究观察</strong>：以下为因子/模型研究解读${type === 't1_template_note' ? '（模板说明，非真实深度 AI 分析）' : ''}，仅供观察参考。</p>`
    : (status === 'ai-stale' ? `<p class="help-text">该 AI 分析生成于 ${escapeHtml(aiSourceDate(item) || '更早日期')}，非最新交易日判断，注意时效。</p>` : '');
  const body = summary || pointsToText(item.ai_points);
  return `<section class="analysis-section">
    <div class="panel-title">二、AI 分析结论</div>
    ${researchNote}
    ${summary ? `<p class="ai-summary">${escapeHtml(summary)}</p>` : ''}
    ${advice ? `<p class="help-text"><strong>建议</strong>：${escapeHtml(advice)}</p>` : ''}
    ${aiPointsHtml(item.ai_points)}
    ${!body ? '<p class="help-text">暂无（AI 文本字段为空）。</p>' : ''}
  </section>`;
}

function pointsToText(points) {
  if (Array.isArray(points)) return points.filter(Boolean).join(' ');
  return typeof points === 'string' ? points : '';
}

function unifiedAnalysisPanel(item, execution) {
  const status = aiStatusOf(item);
  const evidence = quantBlockHtml(item, status);
  const risks = aiRisksHtml(item);
  return `${actionChainHtml(item)}
    ${aiConclusionHtml(item, status)}
    <section class="analysis-section">
      <div class="panel-title">三、策略证据</div>
      ${evidence || '<p class="help-text">暂无可读因子证据。</p>'}
    </section>
    ${risks ? `<section class="analysis-section"><div class="panel-title">四、风险提示</div>${risks}</section>` : ''}
    ${execBlockHtml(execution)}`;
}

// ---------------------------------------------------------------------------
// 导出：单卡与多卡
// ---------------------------------------------------------------------------

export function renderCandidateCard(item = {}, opts = {}) {
  const { execution = null, index = 0 } = opts;
  const { action, layer } = resolveActionDetail(item);
  const status = aiStatusOf(item);
  const code = codeOf(item);
  const industry = industryOf(item);
  const rank = rankOf(item, index);
  const score = quantScore(item);

  const changePct = firstFinite(item.current_change_pct, item.change_pct, item.pct_chg);
  const changeDir = changePct === null ? 'flat' : changePct > 0 ? 'up' : changePct < 0 ? 'down' : 'flat';
  const price = firstFinite(item.current_price, item.price, item.close);

  const actionBadge = layer === 'candidate'
    ? badge(`策略分层 · ${actionLabel(action)}`, actionTone(action))
    : layer === 'execution'
      ? badge(`执行建议 · ${actionLabel(action)}`, actionTone(action))
      : '';

  const anchor = safeText(item.analysis_anchor_id, '') || stockAnchorId(item);
  const panelId = safeText(item.analysis_panel_id, '') || `${anchor}-analysis`;
  const href = safeText(item.analysis_link_href, '') || `#${anchor}`;
  const displayCode = safeText(item.display_code, '') || code || '—';

  return `<article id="${escapeHtml(anchor)}" class="candidate-card" data-role="${escapeHtml(action)}" data-change="${escapeHtml(changeDir)}">
    <header class="candidate-head">
      <div class="candidate-rank" aria-label="策略排名第 ${escapeHtml(formatNumber(rank))} 名">
        <span class="candidate-rank-no num">${escapeHtml(formatNumber(rank))}</span>
        <span class="candidate-rank-cap">名</span>
      </div>
      <div class="candidate-title">
        <a class="stock-analysis-link" href="${escapeHtml(href)}" data-stock-analysis-toggle data-ai-toggle
           aria-expanded="false" aria-controls="${escapeHtml(panelId)}"
           aria-label="展开/收起 ${escapeHtml(nameOf(item))} 的分析">
          <span class="stock-name">${escapeHtml(nameOf(item))}</span>
          <span class="stock-code num">${escapeHtml(displayCode)}</span>
          <span class="stock-link-caret" aria-hidden="true">▾</span>
        </a>
        <p class="candidate-sub">${escapeHtml(industry || '行业未标注')}</p>
      </div>
      <div class="candidate-tags">
        ${actionBadge}
        ${aiStatusBadge(item)}
      </div>
    </header>
    ${metricRowHtml(item, score)}
    ${scoreBarHtml(score, action)}
    <div id="${escapeHtml(panelId)}" class="ai-analysis-wrap" data-ai-wrap hidden>
      ${unifiedAnalysisPanel(item, execution)}
    </div>
  </article>`;
}

export function renderStrategyCandidateCards(items, strategyId = '', opts = {}) {
  const { executions = [], limit = 20, researchOnly = false } = opts;
  const list = Array.isArray(items) ? items.slice(0, limit) : [];
  return list.map((item, idx) => renderCandidateCard(
    researchOnly ? { ...item, strategy_research_only: true } : item,
    {
      strategyId,
      index: idx,
      execution: executionFor(executions, item, strategyId)
    }
  )).join('');
}

export function renderCandidateAnalysis(item = {}, opts = {}) {
  const { execution = null } = opts;
  const action = resolveAction(item);
  const score = quantScore(item);
  return `<div class="obs-reader-inner">
    <header class="obs-reader-head">
      <div class="obs-reader-name">${escapeHtml(nameOf(item))}</div>
      <p class="obs-reader-meta">
        <span class="num">${escapeHtml(codeOf(item) || '—')}</span>
        ${industryOf(item) ? `<span>${escapeHtml(industryOf(item))}</span>` : ''}
        ${badge(actionLabel(action), actionTone(action))}
        ${aiStatusBadge(item)}
      </p>
    </header>
    ${metricRowHtml(item, score)}
    ${scoreBarHtml(score, action)}
    ${unifiedAnalysisPanel(item, execution)}
  </div>`;
}
