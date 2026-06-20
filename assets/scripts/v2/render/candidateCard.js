// v4/render/candidateCard.js — 唯一的候选股卡片组件（纯函数：item → HTML 字符串，无 DOM 依赖，Node 可执行）。
//
// v4 视觉（DESIGN-V4 第 1、3 节）：抬升式卡片 + 铜金强调 + charts.scoreBar 评分条 + 红绿涨跌 + AI 三态徽章。
// 仅升级呈现层（头部排名章、量化分评分条、涨跌方向强调）；数据接线与诚实性逻辑一字不改。
//
// 诚实性规范落实（DESIGN-V3 第 0 节，仍是铁律）：
//   0.2 AI 状态显性化：用 format.aiStatusOf 三态打徽章。ai-none 时绝不渲染任何模板话术，
//       只展示真实量化信号读数 —— v2 的 normalizeAiPoints 兜底文案逻辑已彻底删除。
//   0.4 单一动作权威：动作只读管线字段链 role_type → adjusted_action → final_candidate_action，
//       不再用 AI 文案的中文正则二次推断 —— v2 的 normalizeStrategyAction 已删除。
//   执行区四件套（买点/失效/仓位档/次日处理）仅当执行清单（execution_state）确实包含该股时渲染。
//
// 公开 API（与 v3 完全一致，签名不变）：
//   stockAnchorId(item)                                   — 卡片锚点 id（页内跳转用）
//   resolveAction(item)                                   — 动作字段链直读，返回 'main'|'watch'|'avoid'
//   executionFor(executions, item, strategyId)            — 在执行清单中按 代码+策略 匹配该股，无则 null
//   renderCandidateCard(item, opts)                       — 单张候选卡
//       opts = { strategyId = '', execution = null, index = 0 }
//   renderStrategyCandidateCards(items, strategyId, opts) — 候选卡列表（自动匹配执行清单）
//       opts = { executions = [], limit = 20 }
//
// 转义纪律：所有外部插值过 escapeHtml；唯一例外是 pctHtml / charts.* 产物（已是安全 HTML/SVG）。

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

// 返回 { action, layer }：layer 标记动作出自哪一层口径——
//   'candidate'（role_type，策略分层） / 'execution'（adjusted_action / final_candidate_action，执行建议）
//   / 'none'（字段全缺，按观察处理但不打动作徽章，避免凭空编造）。
function resolveActionDetail(item = {}) {
  // 合同 v2：研究观察策略永不展示买入/主攻——硬门槛未达标的策略其个股一律按观察呈现。
  if (item.strategy_research_only === true || item.research_only === true) {
    return { action: 'watch', layer: 'research' };
  }
  const chain = [
    [item.final_action, 'execution'],          // 合同 v2：门槛后最终动作，最高优先
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
// 执行清单匹配：仅当 execution_state 确实包含该股（代码一致，策略可选约束）时返回条目。
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
// 量化分：取真实分值（不编造）；返回 { num, text }。
// 评分条仅在 0~100 量纲（如启动前夕 score）下渲染；小量纲（如 O2C 复合分 2.x）只显示数字，
// 不强行塞进 0~100 的条里造成误导。
// ---------------------------------------------------------------------------

function quantScore(item) {
  const num = firstFinite(item.score, item.composite_score);
  if (num === null) return { num: null, text: '—' };
  return { num, text: formatNumber(num, Math.abs(num) < 10 ? 2 : 1) };
}

// 指标行：现价 / 涨跌（pctHtml 红涨绿跌）/ 量化分 / 获利盘 / 量比 / 数据日。
// 缺什么显示 '—' 或直接省略可选项，不编数字。
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

// 量化评分条：仅当分值落在 0~100 量纲（启动前夕的综合分）时绘制 charts.scoreBar 横条；
// 其余量纲只在指标行里以数字呈现，避免把 2.x 的复合分误塞进百分制条里。
function scoreBarHtml(score, action) {
  if (score.num === null || !(score.num >= 0 && score.num <= 100)) return '';
  const tone = action === 'main' ? 'brand' : action === 'avoid' ? 'down' : 'brand-2';
  return `<div class="candidate-score">
    <span class="candidate-score-label">综合评分</span>
    ${scoreBar(score.num, { tone, max: 100, width: 240 })}
  </div>`;
}

// ---------------------------------------------------------------------------
// AI 区（仅 ai-full / ai-stale）：只渲染真实 AI 内容；清洗后无内容则退回量化信号区。
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
      // 未收录的内部键名不直出给客户：只展示值本身。
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

function aiBlockHtml(item, status) {
  const summary = cleanAnalysisText(safeText(item.ai_summary || item.ai_conclusion, ''));
  const pointsHtml = aiPointsHtml(item.ai_points);
  const risksHtml = aiRisksHtml(item);
  // AI 字段名义上有值、清洗后却无可展示内容 → 诚实退回量化信号区，不编话术。
  if (!summary && !pointsHtml && !risksHtml) return quantBlockHtml(item);

  const staleNote = status === 'ai-stale'
    ? `<p class="help-text">该 AI 分析生成于 ${escapeHtml(aiSourceDate(item) || '更早日期')}，不是最新交易日的判断，请注意时效。</p>`
    : '';
  return `<div class="ai-block">
    ${staleNote}
    ${summary ? `<p class="ai-summary">${escapeHtml(summary)}</p>` : ''}
    ${pointsHtml}
    ${risksHtml}
  </div>`;
}

// ---------------------------------------------------------------------------
// 量化信号区（ai-none）：只展示数据里真实存在的因子读数，白话标签。
// ---------------------------------------------------------------------------

function quantChips(item) {
  const chips = [];
  // T1 因子策略：无个股 AI/筹码读数，但有真实的合成因子分与 top20 内排名（研究证据，非交易信号）。
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

function quantBlockHtml(item) {
  const chips = quantChips(item);
  if (!chips.length) {
    return `<div class="ai-none-block">
      <p>本期没有 AI 个股分析，数据中也没有可展示的量化信号明细。</p>
    </div>`;
  }
  const hasChipLevels = (item.chip_support !== null && item.chip_support !== undefined)
    || (item.chip_resistance !== null && item.chip_resistance !== undefined);
  const footnote = hasChipLevels
    ? '<p class="help-text">筹码支撑/压力位为相对持仓成本中枢的比值，1.00 代表成本中枢价位。</p>'
    : '';
  return `<div class="ai-none-block">
    <p>本期没有 AI 个股分析，以下为量化模型的真实信号读数：</p>
    ${chipList(chips)}
    ${footnote}
  </div>`;
}

// ---------------------------------------------------------------------------
// 执行区四件套：买点 / 失效 / 仓位档 / 次日处理（仅执行清单含该股时渲染）。
// 执行层与候选层口径分开命名（诚实性规范 0.4）：此区标注为「盘前执行清单」口径。
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// 统一展开面板（三类策略同结构：最终动作 / AI 分析 / 策略证据 / 风险 / 执行）
// 无真实字段不编造；缺失显示「暂无」并说明是数据缺失；研究观察明显标注「不作为买入依据」。
// ---------------------------------------------------------------------------

function actionChainHtml(item) {
  const rows = [
    ['原始动作', actionLabel(safeText(item.raw_action, ''))],
    ['门槛后动作', actionLabel(safeText(item.gate_adjusted_action, ''))],
    ['最终动作', actionLabel(safeText(item.final_action, ''))],
    ['是否研究观察', item.research_only === true ? '是（不作为买入依据）' : (item.research_only === false ? '否' : '')]
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
      <p class="help-text"><strong>AI 未覆盖</strong>：当前仅展示该股的量化因子证据，尚未生成完整 AI 分析，不能作为完整 AI 结论。</p>
    </section>`;
  }
  const isResearch = type === 't1_template_note' || type === 't1_research_ai';
  const summary = cleanAnalysisText(safeText(item.ai_summary || item.ai_conclusion, ''));
  const advice = safeText(item.ai_advice, '');
  const researchNote = isResearch
    ? `<p class="help-text research-note"><strong>研究观察</strong>：以下为因子/模型研究解读${type === 't1_template_note' ? '（模板说明，非真实深度 AI 分析）' : ''}，仅供观察，<strong>不作为买入依据</strong>。</p>`
    : (status === 'ai-stale' ? `<p class="help-text">该 AI 分析生成于 ${escapeHtml(aiSourceDate(item) || '更早日期')}，非最新交易日判断，注意时效。</p>` : '');
  const body = summary || pointsToText(item.ai_points);
  return `<section class="analysis-section">
    <div class="panel-title">二、AI 分析结论</div>
    ${researchNote}
    ${summary ? `<p class="ai-summary">${escapeHtml(summary)}</p>` : ''}
    ${advice ? `<p class="help-text"><strong>建议</strong>：${escapeHtml(advice)}</p>` : ''}
    ${aiPointsHtml(item.ai_points)}
    ${!body ? '<p class="help-text">暂无（AI 文本字段为空，非伪造）。</p>' : ''}
  </section>`;
}

function pointsToText(points) {
  if (Array.isArray(points)) return points.filter(Boolean).join(' ');
  return typeof points === 'string' ? points : '';
}

function unifiedAnalysisPanel(item, execution) {
  const status = aiStatusOf(item);
  const evidence = quantBlockHtml(item);
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
// 候选卡
// ---------------------------------------------------------------------------

export function renderCandidateCard(item = {}, opts = {}) {
  const { execution = null, index = 0 } = opts;
  const { action, layer } = resolveActionDetail(item);
  const status = aiStatusOf(item);
  const code = codeOf(item);
  const industry = industryOf(item);
  const rank = rankOf(item, index);
  const score = quantScore(item);

  // 涨跌方向用于卡片整体的轻微强调（A 股红涨绿跌）。
  const changePct = firstFinite(item.current_change_pct, item.change_pct, item.pct_chg);
  const changeDir = changePct === null ? 'flat' : changePct > 0 ? 'up' : changePct < 0 ? 'down' : 'flat';

  // 动作徽章标注口径来源（诚实性规范 0.4：两套口径分开命名）；字段全缺时不编造动作徽章。
  const actionBadge = layer === 'candidate'
    ? badge(`策略分层 · ${actionLabel(action)}`, actionTone(action))
    : layer === 'execution'
      ? badge(`执行建议 · ${actionLabel(action)}`, actionTone(action))
      : '';
  // 稳定锚点/面板 id 来自发布合同（analysis_anchor_id 等），缺失时退回 stockAnchorId。
  const anchor = safeText(item.analysis_anchor_id, '') || stockAnchorId(item);
  const panelId = safeText(item.analysis_panel_id, '') || `${anchor}-analysis`;
  const href = safeText(item.analysis_link_href, '') || `#${anchor}`;
  const displayCode = safeText(item.display_code, '') || code || '—';

  // 个股 AI 分析默认折叠：点击「股票名称/代码超级链接」原地展开（手风琴：同时只展开一只，app.js 处理）。
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
    // 合同 v2：策略级研究观察兜底到每股（candidate_state.json 等旧文件可能缺 strategy_research_only），
    // 确保未达门槛策略的个股一律不显示买入。
    researchOnly ? { ...item, strategy_research_only: true } : item,
    {
      strategyId,
      index: idx,
      execution: executionFor(executions, item, strategyId)
    }
  )).join('');
}
