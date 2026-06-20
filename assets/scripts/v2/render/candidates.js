// v3/render/candidates.js — 个股推荐页（decision-candidates.html，data-view="candidates"）。
// DESIGN-V3 第 4 节 candidates 规范：策略 Tab（启动前夕 / O2C 日内 / T1 因子）+ 候选卡 + 共识分歧区。
//
// 诚实性要点：
//   - 每策略头部只放产品文案（写死的策略原理说明，零业绩数字）+ 来自数据的当日真实统计；
//   - T1：status=research_preview 必须打「研究预览」徽章；top20 因子分全为 0 时整组判定数据异常，
//     不照常列卡片；backtest_summary 仅在字段真实非空且非全零时展示，禁止任何兜底数字；
//   - O2C：不再渲染任何写死的门控/因子徽章，只展示数据中真实存在的字段；
//   - 空数组 ≠ 文件缺失：空名单渲染 emptySection 解释，文件缺失渲染 missingSection；
//   - 共识/分歧：全分歧（如 60/60）时显示口径说明，而非误导性的长名单。
//
// 导出（与 views.js 注册表一致）：renderCandidates(model)

import {
  escapeHtml, safeText, formatNumber, formatPct, formatSignedPct, dateCn
} from './format.js';
import {
  badge, chipList, statCard, sectionHead, missingSection, emptySection, tabsBar, tabPanel
} from './components.js';
import { renderShell, renderHero } from './shell.js';
import { renderStrategyCandidateCards } from './candidateCard.js';

// ---------------------------------------------------------------------------
// 数据读取小工具（只取数，不造数）
// ---------------------------------------------------------------------------

function executionsOf(model) {
  const list = (model.executionState || {}).executions;
  return Array.isArray(list) ? list : [];
}

function gfStocksOf(model) {
  const list = (model.greenfieldTop20 || {}).top20;
  return Array.isArray(list) ? list : [];
}

function t1RowsOf(model) {
  const recRows = (model.t1FactorRecommendations || {}).rows;
  if (Array.isArray(recRows) && recRows.length) return recRows;
  const stateRows = (model.researchStateT1 || {}).top20;
  return Array.isArray(stateRows) ? stateRows : [];
}

function isT1Preview(model) {
  // T1 任何「非实盘」状态都按研究预览呈现；并以 decision 的 t1_research_mode 作为兜底单一源，
  // 保证首页/个股页/系统说明页三处口径一致。
  const status = safeText((model.researchStateT1 || {}).status, '');
  if (status && status !== 'production' && status !== 'live') return true;
  return Boolean(((model.decisionState || {}).data_status || {}).t1_research_mode);
}

// 合同 v2：策略是否「研究观察」——优先读 decision_state.gates.strategy_gate.per_strategy（统一硬门槛结论），
// 回退 recommendation_state.strategies[sid].research_only，再回退 T1 旧判定。三套策略通用。
function isStrategyResearchOnly(model, sid) {
  const per = ((((model.decisionState || {}).gates || {}).strategy_gate || {}).per_strategy) || {};
  if (per[sid]) return per[sid] === 'research_only';
  const recStrat = ((model.recommendationState || {}).strategies || {})[sid];
  if (recStrat && typeof recStrat.research_only === 'boolean') return recStrat.research_only;
  return sid === 't1_factor_v1' ? isT1Preview(model) : false;
}

// 策略小节头：抬升式策略介绍卡 —— 产品文案（写死的原理说明，非业绩）+ 当日真实统计行 + 可选徽章。
// 版式升级（DESIGN-V4）：把标题/原理/统计收进铜金抬升卡，统计数字用 .strategy-stat 单独高亮，
// 文案与诚实性零改动（statsText 与徽章均来自上游真实数据）。
function strategyHead(title, blurb, statsText, badgesHtml = '') {
  return `<section class="strategy-intro elevated-card">
    <div class="strategy-intro-head">
      <h3 class="strategy-intro-title">${escapeHtml(title)}</h3>
      ${badgesHtml ? `<div class="candidate-tags">${badgesHtml}</div>` : ''}
    </div>
    <p class="strategy-intro-blurb">${escapeHtml(blurb)}</p>
    ${statsText ? `<p class="strategy-intro-stats help-text">${escapeHtml(statsText)}</p>` : ''}
  </section>`;
}

// ---------------------------------------------------------------------------
// 启动前夕（主力策略）
// ---------------------------------------------------------------------------

const PREBREAKOUT_BLURB = '主力策略：在股票放量启动前提前布局，综合趋势、均线、筹码分布与事件信号，每个交易日从全市场重新筛选候选名单。';

function prebreakoutSection(model, executions) {
  if (model.isMissing('candidateState')) {
    return missingSection('启动前夕入选名单', model.missingReason('candidateState'));
  }
  const candidates = Array.isArray(model.candidates) ? model.candidates : [];
  if (!candidates.length) {
    return `${strategyHead('启动前夕（主力策略）', PREBREAKOUT_BLURB, '')}
    ${emptySection('今日启动前夕策略无入选标的', '策略每个交易日都会重新筛选，市况不满足条件时名单可能为空，属正常现象。')}`;
  }

  const counts = model.candidateRoleCounts || { main: 0, watch: 0, avoid: 0 };
  const dataDate = dateCn((model.candidateState || {}).latest_trade_date);
  const stats = `入选 ${formatNumber(candidates.length)} 只 · 策略分层：主攻 ${formatNumber(counts.main || 0)} / 观察 ${formatNumber(counts.watch || 0)} / 回避 ${formatNumber(counts.avoid || 0)} · 数据日期 ${dataDate}`;

  // AI 覆盖度如实说明（诚实性规范 0.2）：全无 AI 分析时明确告知，不假装有分析。
  const ai = model.aiCoverage || { total: 0, full: 0, stale: 0, none: 0 };
  let aiNote = '';
  if (ai.total > 0 && ai.none === ai.total) {
    aiNote = `本期 ${formatNumber(ai.total)} 只候选均无 AI 个股分析，卡片只展示量化信号读数。`;
  } else if (ai.none > 0) {
    aiNote = `本期 ${formatNumber(ai.full + ai.stale)} 只候选有 AI 分析，其余 ${formatNumber(ai.none)} 只仅有量化信号。`;
  }

  return `${strategyHead('启动前夕（主力策略）', PREBREAKOUT_BLURB, stats)}
  ${aiNote ? `<p class="help-text">${escapeHtml(aiNote)}</p>` : ''}
  <div class="candidate-grid">
    ${renderStrategyCandidateCards(candidates, 'prebreakout_v41', { executions, researchOnly: isStrategyResearchOnly(model, 'prebreakout_v41') })}
  </div>`;
}

// ---------------------------------------------------------------------------
// O2C 日内
// ---------------------------------------------------------------------------

const O2C_BLURB = '日内节奏策略：按「开盘附近买入、收盘前了结」的思路，用量价与筹码因子对全市场打分，取排名靠前的标的。';

// 因子池指标：仅当数据里真实存在对应字段时展示（当前数据为 null → 不渲染任何徽章/数字）。
function o2cFactorPoolHtml(gf) {
  const fp = gf && gf.factor_pool && typeof gf.factor_pool === 'object' ? gf.factor_pool : null;
  if (!fp) return '';
  const chips = [];
  if (fp.o2c_sharpe !== null && fp.o2c_sharpe !== undefined && Number.isFinite(Number(fp.o2c_sharpe))) {
    chips.push({ label: '回测风险调整后收益（夏普）', value: formatNumber(fp.o2c_sharpe, 2) });
  }
  if (fp.oos_sharpe !== null && fp.oos_sharpe !== undefined && Number.isFinite(Number(fp.oos_sharpe))) {
    chips.push({ label: '样本外夏普', value: formatNumber(fp.oos_sharpe, 2) });
  }
  if (fp.walkforward_pass_rate !== null && fp.walkforward_pass_rate !== undefined && fp.walkforward_pass_rate !== '') {
    chips.push({ label: '滚动验证通过率', value: safeText(fp.walkforward_pass_rate) });
  }
  if (!chips.length) return '';
  return `<div>
    ${chipList(chips)}
    <p class="help-text">以上为策略研发期的回测指标，并非实盘业绩，仅供了解策略背景。</p>
  </div>`;
}

function o2cSection(model, executions) {
  if (model.isMissing('greenfieldTop20')) {
    return missingSection('O2C 日内入选名单', model.missingReason('greenfieldTop20'));
  }
  const gf = model.greenfieldTop20 || {};
  const stocks = gfStocksOf(model);
  if (!stocks.length) {
    return `${strategyHead('O2C 日内', O2C_BLURB, '')}
    ${emptySection('今日 O2C 策略无入选标的', '该策略每个交易日重新打分，没有满足条件的股票时名单为空，属正常现象。')}`;
  }

  // 因子个数来自数据本身（不写死「6 因子」之类的徽章）。
  const factorCount = Object.keys(stocks[0] && stocks[0].factor_values && typeof stocks[0].factor_values === 'object' ? stocks[0].factor_values : {}).length;
  const dataDate = dateCn(gf.trade_date || gf.source_date);
  const stats = `入选 ${formatNumber(stocks.length)} 只 · 数据日期 ${dataDate}${factorCount ? ` · 本期使用 ${formatNumber(factorCount)} 个量价因子` : ''}`;

  return `${strategyHead('O2C 日内', O2C_BLURB, stats)}
  ${o2cFactorPoolHtml(gf)}
  <div class="candidate-grid">
    ${renderStrategyCandidateCards(stocks, 'greenfield_o2c_v1', { executions, researchOnly: isStrategyResearchOnly(model, 'greenfield_o2c_v1') })}
  </div>`;
}

// ---------------------------------------------------------------------------
// T1 因子（研究预览）
// ---------------------------------------------------------------------------

const T1_BLURB = '次日交易研究策略：用大规模量价因子库（Alpha191）为股票的次日表现打分。该方向仍在研究验证中，名单仅供观察，请勿直接据此交易。';

// 因子分全为 0 → 整组判定数据异常（诊断 P0），不照常列出个股卡片。
function t1ScoresAllZero(rows) {
  if (!rows.length) return false;
  return rows.every((row) => {
    const num = Number(row && row.score);
    return !Number.isFinite(num) || num === 0;
  });
}

function t1AnomalySection(rows) {
  const names = rows.slice(0, 20)
    .map((row) => `${safeText(row.name || row.stock_name, '未知')}（${safeText(row.code || row.stock_code, '—')}）`)
    .join('、');
  return `<section class="section-empty" role="note">
    <div class="empty-title">本期 T1 因子数据异常，名单不可用</div>
    <p>本期 ${formatNumber(rows.length)} 只入选股的综合因子分全部为 0，说明因子计算环节出现异常，这份名单不具备参考价值，已暂停展示个股卡片，待数据恢复正常后自动恢复。</p>
    <details>
      <summary>查看本期原始名单（仅作记录，不构成参考）</summary>
      <p class="help-text">${escapeHtml(names)}</p>
    </details>
  </section>`;
}

// 回测摘要：仅当字段真实非空且并非全零时显示；退化数据（全 0 / 全 null）如实说明，零兜底数字。
function t1BacktestHtml(t1State) {
  const bt = t1State && t1State.backtest_summary && typeof t1State.backtest_summary === 'object'
    ? t1State.backtest_summary
    : null;
  const entries = [];
  if (bt) {
    const push = (raw, label, render) => {
      if (raw === null || raw === undefined) return;
      const num = Number(raw);
      if (!Number.isFinite(num)) return;
      entries.push({ label, value: render(num), num });
    };
    push(bt.hit_rate, '次日上涨命中率', (num) => formatPct(num * 100, 1));
    push(bt.total_return, '区间累计收益', (num) => formatSignedPct(num * 100, 2));
    push(bt.sharpe, '风险调整后收益（夏普）', (num) => formatNumber(num, 2));
    push(bt.max_drawdown, '最大回撤', (num) => formatPct(num * 100, 2));
  }
  const meaningful = entries.length > 0 && entries.some((entry) => entry.num !== 0);
  if (!meaningful) {
    return '<p class="help-text">该策略暂无可验证的历史回测数据（回测字段为空或全为 0），因此不展示任何业绩数字。</p>';
  }
  return `<div>
    ${chipList(entries.map(({ label, value }) => ({ label, value })))}
    <p class="help-text">以上为研究回测结果（等权、不含交易成本），并非实盘业绩，不代表未来收益。</p>
  </div>`;
}

function t1Section(model, executions) {
  if (model.isMissing('t1FactorRecommendations') && model.isMissing('researchStateT1')) {
    return missingSection('T1 因子入选名单', model.missingReason('t1FactorRecommendations'));
  }
  const t1State = model.researchStateT1 || {};
  const rows = t1RowsOf(model);
  const previewBadge = isT1Preview(model) ? badge('研究预览（未实盘验证）', 'warn') : '';
  const dataDate = dateCn(
    (model.t1FactorRecommendations || {}).trade_date || t1State.latest_trade_date
  );

  const backtestBlock = `<section class="panel">
    <div class="panel-title">历史回测情况</div>
    ${t1BacktestHtml(t1State)}
  </section>`;

  if (!rows.length) {
    return `${strategyHead('T1 因子（研究中）', T1_BLURB, '', previewBadge)}
    ${emptySection('今日 T1 策略无入选标的', '该策略每个交易日重新打分，没有满足条件的股票时名单为空，属正常现象。')}
    ${backtestBlock}`;
  }

  const stats = `入选 ${formatNumber(rows.length)} 只 · 数据日期 ${dataDate}`;

  if (t1ScoresAllZero(rows)) {
    return `${strategyHead('T1 因子（研究中）', T1_BLURB, stats, previewBadge)}
    ${t1AnomalySection(rows)}
    ${backtestBlock}`;
  }

  return `${strategyHead('T1 因子（研究中）', T1_BLURB, stats, previewBadge)}
  ${backtestBlock}
  <div class="candidate-grid">
    ${renderStrategyCandidateCards(rows, 't1_factor_v1', { executions, researchOnly: isStrategyResearchOnly(model, 't1_factor_v1') })}
  </div>`;
}

// ---------------------------------------------------------------------------
// 多策略共识 / 分歧（execution_state 口径）
// ---------------------------------------------------------------------------

function consensusSection(model, executions) {
  if (model.isMissing('executionState')) {
    return missingSection('多策略交叉验证', model.missingReason('executionState'));
  }
  const exec = model.executionState || {};
  const total = executions.length;
  const consensus = Number(exec.consensus_in_execution) || 0;
  const divergence = Array.isArray(exec.divergence_stocks) ? exec.divergence_stocks : [];

  const head = sectionHead(
    '多策略交叉验证',
    '同一只股票被多条策略同时选中（共识）通常确认度更高；只被单一策略选中则记为分歧。'
  );

  if (!total) {
    return `${head}
    ${emptySection('今日执行清单为空', '没有可比较的共识与分歧记录。')}`;
  }

  // 全分歧（例如 60/60）：列 60 只“分歧股”没有信息量，改为口径说明（DESIGN-V3 4.4）。
  if (consensus === 0 && divergence.length >= total) {
    return `${head}
    <section class="panel">
      <p>今日 ${formatNumber(total)} 只入选股全部只被单一策略选中，没有出现多策略共识股。</p>
      <p class="help-text">三条策略的选股逻辑与股票池各不相同，名单完全不重叠是常见情况，不代表数据有误。出现共识股时会在此列出。</p>
    </section>`;
  }

  const nameByCode = new Map(
    executions.map((entry) => [safeText(entry.stock_code, ''), safeText(entry.stock_name, '')])
  );
  const divergenceRows = divergence.slice(0, 20).map((code, idx) => {
    const codeText = safeText(code, '—');
    const name = nameByCode.get(codeText) || '';
    return `<div class="list-row">
      <div class="rank-dot">${formatNumber(idx + 1)}</div>
      <div>
        <h5>${escapeHtml(name || codeText)}${name ? ` <span class="soft num">${escapeHtml(codeText)}</span>` : ''}</h5>
        <p class="help-text">仅被单一策略选中，建议结合其他信息交叉确认</p>
      </div>
    </div>`;
  }).join('');

  return `${head}
  <div class="stat-grid">
    ${statCard({ title: '共识股', value: formatNumber(consensus), note: '被两条以上策略同时选中' })}
    ${statCard({ title: '分歧股', value: formatNumber(divergence.length), note: '只被单一策略选中' })}
  </div>
  ${divergence.length ? `<section class="panel">${divergenceRows}${divergence.length > 20 ? `<p class="help-text">仅显示前 20 只，共 ${formatNumber(divergence.length)} 只分歧股。</p>` : ''}</section>` : ''}`;
}

// ---------------------------------------------------------------------------
// 页面入口
// ---------------------------------------------------------------------------

export function renderCandidates(model) {
  const executions = executionsOf(model);
  const prebreakoutCount = Array.isArray(model.candidates) ? model.candidates.length : 0;
  const o2cCount = gfStocksOf(model).length;
  const t1Count = t1RowsOf(model).length;

  const tabs = [
    { key: 'prebreakout', label: '启动前夕', note: `主力 · ${prebreakoutCount} 只` },
    { key: 'o2c', label: 'O2C 日内', note: `${o2cCount} 只` },
    { key: 't1', label: 'T1 因子', note: isT1Preview(model) ? `研究预览 · ${t1Count} 只` : `${t1Count} 只` }
  ];

  const body = `
    ${renderHero(model, '个股推荐', '三条策略各自给出当日入选名单：选了什么、依据是什么、如何跟踪。')}
    ${tabsBar(tabs, 'prebreakout', { groupId: 'strategies' })}
    ${tabPanel('prebreakout', prebreakoutSection(model, executions), { active: true, groupId: 'strategies' })}
    ${tabPanel('o2c', o2cSection(model, executions), { groupId: 'strategies' })}
    ${tabPanel('t1', t1Section(model, executions), { groupId: 'strategies' })}
    ${consensusSection(model, executions)}
  `;
  return renderShell('candidates', model, body);
}
