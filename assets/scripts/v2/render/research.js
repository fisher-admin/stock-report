// v4/render/research.js — 系统说明页（research-lab.html，data-view="research"）。
//
// 导出：renderResearch(model, { initialTab }) —— 纯函数（model → HTML 字符串），无 document/window。
// 三个页内 Tab（DESIGN-V3 第 4 节，IA 不变）：
//   how        系统如何工作：五步链路卡 + 当日四道安全检查（gates 白话直读）
//   strategies 策略中心：三策略当日权重环（donut）+ 三策略卡（白话原理 + 当日真实计数 + 真实历史表现）
//   dataStatus 数据状态：数据新鲜度（run_manifest.sources）+ 数据完整性（system_health.checks）+ 免责全文
// 旧 URL strategy-vs-market.html 由 views.js 以 initialTab='strategies' 复用本页（见 strategy.js 薄包装）。
//
// v4 视觉重做（DESIGN-V4 第 3 节）：仅改呈现/版式/图表注入，数据接线与诚实性逻辑完全不动。
//   - 五步链路升级为编号链路卡（pipeline-flow / pipeline-step，复用基础层样式）；
//   - 四闸门升级为状态卡网格（gate-grid / gate-card，tone-* 左描边）；
//   - 策略中心顶部新增「三策略当日权重环」：charts.donut（真实当日入选数归一化）+ 图例，
//     无任一策略可读名单时整段退占位，绝不编造权重；
//   - 数据新鲜度白话表、数据完整性 system_health、免责全文沿用 v3 结构。
//
// 诚实性（DESIGN-V3 第 0 节）逐条落实（与 v3 完全一致，未放松）：
//   - 零硬编码业绩数字：历史表现全部由 review_state.date_stats 真实逐日数据聚合而来（performanceFromReview）；
//     无可验证数据时只显示「待积累」与原因，绝不显示编造的胜率/收益。
//   - 权重环只用当日真实入选计数（strategyDayCount），不掺任何业绩/收益数字；都读不到则整段退占位。
//   - 负收益照实显示（pctHtml 红涨绿跌）；统计配口径说明（等权、按次日收盘、不含交易成本）。
//   - gates 只透出 summary（已是中文白话），不透出 hard_blocking / downstream_allowed 等内部字段。
//   - 开发者术语清零：检查项 key 全部转白话；未收录的 key 折叠进「更多技术检查项」，不直接出现在正文。

import {
  escapeHtml, safeText, formatNumber, formatPct, pctHtml,
  dateCn, friendlyTime
} from './format.js';
import {
  badge, chipList, statCard, sectionHead, missingSection, emptySection,
  tabsBar, tabPanel, dataTable, elevatedCard, DISCLAIMER_TEXT
} from './components.js';
import { donut } from './charts.js';
import { renderShell, renderHero } from './shell.js';

const TAB_GROUP = 'research';

const TABS = [
  { key: 'how', label: '系统如何工作' },
  { key: 'strategies', label: '策略中心' },
  { key: 'dataStatus', label: '数据状态' }
];

// ---------------------------------------------------------------------------
// 静态产品文案（只描述机制，不含任何业绩数字）
// ---------------------------------------------------------------------------

// 五步链路：市场闸门 → 策略选择 → 个股筛选 → AI 复核 → 每日复盘。
const PIPELINE_STEPS = [
  {
    title: '市场闸门',
    text: '每天先看大环境：行情数据是否新鲜、大盘风险高不高。环境不合格时，系统会收紧建议，甚至当天不给任何买入指引。'
  },
  {
    title: '策略选择',
    text: '在市场允许的前提下，决定当天启用哪条策略：主策略「启动前夕」负责实盘推荐，另外两条策略提供印证与研究参考。'
  },
  {
    title: '个股筛选',
    text: '被启用的策略对全市场个股逐一打分，选出当天得分最高的候选名单（一般每条策略 20 只），并做分层标注。'
  },
  {
    title: 'AI 复核',
    text: 'AI 助手逐只复核候选股的技术面与筹码结构，用白话给出点评。当天没有 AI 复核结果时，页面会如实标注「无 AI 分析」，不会用模板话术冒充。'
  },
  {
    title: '每日复盘',
    text: '收盘后回头核对此前推荐的真实表现，把次日收益和命中率记进「历史战绩」页。赚是赚、亏是亏，照实展示。'
  }
];

// 四道安全检查的固定说明（与 model.workflow 的 id 对应；当日状态另由 gates.summary 给出）。
const GATE_DESCRIPTIONS = {
  freshness: '确认当天的行情、推荐、复盘都基于同一个最新交易日，杜绝拿旧数据冒充新数据。',
  market: '评估大盘风险与市场状态，环境恶化时收紧甚至暂停买入建议。',
  strategy: '确认主策略当天正常运行，并产出了完整的候选名单。',
  candidate: '检查候选名单的质量与分层，决定是否给出可执行的主攻建议。'
};

// 三策略产品文案：白话原理（机制描述，零业绩数字）。tone 用于权重环/卡片配色（与 format.strategyTone 一致）。
const STRATEGY_DEFS = [
  {
    id: 'prebreakout_v41',
    fallbackName: '启动前夕',
    fallbackRole: '主策略',
    tone: 'brand',
    oneLiner: '在个股启动之前提前蹲点',
    principle: '在股价大涨之前提前布局：筛选筹码集中（多数持仓人的成本互相靠近）、量价收敛（成交温和、波动收窄）的个股。'
      + '这种形态常见于主力吸筹接近完成、还没开始拉升的阶段。每个交易日选出候选名单，并分为主攻、观察、回避三层。',
    emptyPerfNote: '该策略暂无可验证的历史收益记录。等真实收盘数据积累后，这里会展示覆盖天数、累计收益与命中率。'
  },
  {
    id: 'greenfield_o2c_v1',
    fallbackName: 'O2C 日内',
    fallbackRole: '辅助策略',
    tone: 'accent',
    oneLiner: '研究从隔夜跳空到收盘的日内规律',
    principle: '只关心每天从开盘到收盘这一段：隔夜跳空怎么开盘、盘中量价怎么配合、尾盘强弱如何。'
      + '用 6 个日内因子给全市场打分，挑出日内走势结构最健康的个股，作为主策略之外的参考信号。',
    emptyPerfNote: '该策略上线时间较短，还没有积累可验证的收盘表现数据。数据积累后这里会展示与「历史战绩」页同口径的真实统计。'
  },
  {
    id: 't1_factor_v1',
    fallbackName: 'T1 因子',
    fallbackRole: '研究线',
    tone: 'warn',
    oneLiner: 'Alpha191 多因子打分（研究预览）',
    principle: '用一套 191 个经典量化因子（Alpha191 因子库）对个股综合打分，目标是提高“买入后次日上涨”的概率。'
      + '目前处于研究预览阶段：结果只用于观察因子有效性，不进入实盘推荐。',
    emptyPerfNote: '研究预览阶段还没有可验证的实盘收盘数据；为避免与实盘表现混淆，回测过程数据不在此展示。'
  }
];

// 策略分层标签白话（tier 字段 → 客户语言；未收录时退回各策略的固定角色文案）。
const TIER_LABELS = {
  primary: '主策略',
  secondary: '辅助策略',
  research: '研究线',
  experimental: '研究线'
};

// run_manifest.sources 八时间戳 → 「数据新鲜度」白话行（key、环节名、这一步做什么）。
const SOURCE_STEPS = [
  ['market_generated_at', '晨判分析', '开盘前的大盘研判与当天关注方向'],
  ['midday_generated_at', '午盘快照', '交易日中午的行情快照，作盘中参考'],
  ['orchestrator_generated_at', '盘后选股', '收盘后运行三条策略，生成当日候选名单'],
  ['recommendation_db_generated_at', '推荐归档', '把当日推荐写入历史档案，供日后复盘核对'],
  ['ai_publish_generated_at', '盘后推荐发布', 'AI 复核完成后，发布当日推荐内容'],
  ['validation_report_generated_at', '数据自检', '对当日生成的数据做完整性检查'],
  ['review_generated_at', '推荐复盘', '统计过往推荐的真实次日表现'],
  ['research_generated_at', '研究汇总', '汇总研究线状态，供「系统说明」页展示']
];

// system_health.checks → 「数据完整性」白话行；未收录的 key 折叠进技术信息，不在正文直出。
const HEALTH_CHECK_LABELS = {
  shared_source_database_exists: '行情源数据',
  prebreakout_strategy_db_exists: '启动前夕策略数据',
  o2c_strategy_db_exists: 'O2C 策略数据',
  t1_strategy_db_exists: 'T1 策略数据',
  publication_aggregates_three_strategies: '发布内容覆盖三条策略',
  canonical_strategy_count: '在册策略数量',
  o2c_top20_count: 'O2C 当日候选数量',
  t1_top20_count: 'T1 当日候选数量'
};

// ---------------------------------------------------------------------------
// 私有辅助
// ---------------------------------------------------------------------------

// 把可能为 null/undefined/'' 的值转有限数字；非法返回 null（避免 Number(null)===0 的坑）。
function finiteNum(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function gateBadgeText(tone) {
  if (tone === 'ok') return '通过';
  if (tone === 'warn') return '有提醒';
  return '未通过';
}

// ---------------------------------------------------------------------------
// Tab 1：系统如何工作
// ---------------------------------------------------------------------------

function pipelineHtml() {
  const steps = PIPELINE_STEPS.map((step, idx) => `<section class="pipeline-step">
      <span class="step-index num" aria-hidden="true">${idx + 1}</span>
      <h4>${escapeHtml(step.title)}</h4>
      <p>${escapeHtml(step.text)}</p>
    </section>`).join('\n');
  return `${sectionHead('五步链路', '每个交易日，系统都按同一条流水线走完这五步。')}
    <div class="pipeline-flow">${steps}</div>`;
}

function gatesHtml(model) {
  const items = Array.isArray(model.workflow) ? model.workflow : [];
  const tradeDate = dateCn((model.runManifest || {}).trade_date);
  if (!items.length) {
    return `${sectionHead('当日安全检查')}
      ${emptySection('当日安全检查', '本期数据中没有安全检查结果，可能是当日流程尚未运行完成。')}`;
  }

  // 四闸门概览徽章带（一眼看清几道通过 / 几道有提醒）。
  const tally = items.reduce((acc, item) => {
    const key = item.tone === 'ok' ? 'ok' : item.tone === 'warn' ? 'warn' : 'bad';
    acc[key] += 1;
    return acc;
  }, { ok: 0, warn: 0, bad: 0 });
  const tallyChips = [
    tally.ok ? badge(`${tally.ok} 道通过`, 'ok') : '',
    tally.warn ? badge(`${tally.warn} 道有提醒`, 'warn') : '',
    tally.bad ? badge(`${tally.bad} 道未通过`, 'bad') : ''
  ].filter(Boolean).join('');

  const cards = items.map((item) => {
    const desc = GATE_DESCRIPTIONS[item.id] || '';
    return `<section class="gate-card tone-${escapeHtml(safeText(item.tone, 'info'))}">
      <div class="gate-head">
        <h4>${escapeHtml(item.label)}</h4>
        ${badge(gateBadgeText(item.tone), item.tone)}
      </div>
      <p class="gate-summary">${escapeHtml(item.summary)}</p>
      ${desc ? `<p class="gate-desc">${escapeHtml(desc)}</p>` : ''}
    </section>`;
  }).join('\n');

  const verdict = model.verdict || {};
  const verdictHtml = verdict.label ? elevatedCard(`<div class="verdict-line">
        ${badge(safeText(verdict.label), model.verdictTone)}
        <p>${escapeHtml(safeText(verdict.summary, ''))}</p>
      </div>`, { className: 'verdict-note' }) : '';

  return `${sectionHead('当日安全检查', `四道检查在 ${tradeDate} 数据上的实际结果。`)}
    ${tallyChips ? `<div class="gate-tally">${tallyChips}</div>` : ''}
    <div class="gate-grid">${cards}</div>
    ${verdictHtml}`;
}

function howTabHtml(model) {
  return `${pipelineHtml()}
    ${gatesHtml(model)}`;
}

// ---------------------------------------------------------------------------
// Tab 2：策略中心
// ---------------------------------------------------------------------------

// 当日入选数：优先各策略专属文件，其次执行清单计数，最后健康检查计数；都没有返回 null。
function strategyDayCount(model, strategyId) {
  if (strategyId === 'prebreakout_v41') {
    const summaries = (model.candidateState || {}).strategy_summaries || [];
    const summary = summaries.find((row) => row && row.strategy_id === strategyId);
    const fromSummary = finiteNum(summary && summary.top20_count);
    if (fromSummary !== null) return fromSummary;
    const candidates = (model.candidateState || {}).candidates;
    if (Array.isArray(candidates) && candidates.length) {
      return candidates.filter((row) => row && (!row.strategy_id || row.strategy_id === strategyId)).length;
    }
  }
  if (strategyId === 'greenfield_o2c_v1') {
    const top = (model.greenfieldTop20 || {}).top20;
    if (Array.isArray(top)) return top.length;
    const fromCount = finiteNum((model.greenfieldTop20 || {}).count);
    if (fromCount !== null) return fromCount;
  }
  if (strategyId === 't1_factor_v1') {
    const top = (model.researchStateT1 || {}).top20;
    if (Array.isArray(top)) return top.length;
  }
  const execCounts = (model.executionState || {}).strategy_counts || {};
  const fromExec = finiteNum(execCounts[strategyId]);
  if (fromExec !== null) return fromExec;
  const checks = (model.systemHealth || {}).checks || {};
  const healthKey = strategyId === 'greenfield_o2c_v1' ? 'o2c_top20_count'
    : strategyId === 't1_factor_v1' ? 't1_top20_count' : null;
  if (healthKey) {
    const fromHealth = finiteNum(checks[healthKey]);
    if (fromHealth !== null) return fromHealth;
  }
  return null;
}

// 策略当日名称（registry 优先，退回固定名）。
function strategyName(model, def) {
  const registryRow = ((model.strategyRegistry || {}).strategies || []).find((row) => row && row.strategy_id === def.id) || {};
  return safeText(registryRow.strategy_name, def.fallbackName);
}

// 三策略当日权重环（charts.donut）：仅用真实「当日入选数」归一化为占比，零业绩数字。
// 任一策略读不到名单计数（全 null）→ 整段退占位，绝不编造权重。
function weightDonutHtml(model) {
  const segments = STRATEGY_DEFS.map((def) => {
    const count = strategyDayCount(model, def.id);
    return count === null ? null : {
      def,
      count,
      label: strategyName(model, def),
      value: count,
      tone: def.tone
    };
  });
  const usable = segments.filter(Boolean);
  const total = usable.reduce((sum, seg) => sum + seg.count, 0);

  const head = sectionHead(
    '当日候选权重',
    '按三条策略当日各自的入选数量占比展示（同一天各自独立选股，名单可重叠）。这是数量占比，不是仓位建议，也不含任何收益数字。'
  );

  if (!usable.length || total <= 0) {
    return `${head}
      ${emptySection('当日候选权重', '本页未能读取到任一策略的当日入选名单数量，暂无法绘制权重环。可到「个股推荐」页查看各策略名单。')}`;
  }

  const donutSvg = donut(usable, {
    size: 168,
    thickness: 22,
    centerLabel: `${formatNumber(total)}`,
    centerSub: '当日入选合计',
    label: '三策略当日候选数量占比'
  });

  const legend = usable.map((seg) => {
    const pct = total > 0 ? (seg.count / total) * 100 : 0;
    return `<li class="weight-legend-item">
        <span class="weight-legend-dot tone-${escapeHtml(seg.tone)}" aria-hidden="true"></span>
        <span class="weight-legend-name">${escapeHtml(seg.label)}</span>
        <span class="weight-legend-val num">${escapeHtml(formatNumber(seg.count))} 只 · ${escapeHtml(formatPct(pct, 0))}</span>
      </li>`;
  }).join('\n');

  // 读不到名单的策略如实列出（不参与权重环，但要说明为什么缺席）。
  const missingDefs = STRATEGY_DEFS.filter((def) => strategyDayCount(model, def.id) === null);
  const missingNote = missingDefs.length
    ? `<p class="weight-missing soft">${escapeHtml(
        `${missingDefs.map((def) => strategyName(model, def)).join('、')} 的当日名单数量暂未读取到，未计入上方占比。`
      )}</p>`
    : '';

  return `${head}
    ${elevatedCard(`<div class="weight-donut-row">
      <div class="weight-donut-figure">${donutSvg}</div>
      <ul class="weight-legend">${legend}</ul>
    </div>
    ${missingNote}`, { className: 'weight-donut-card' })}`;
}

// tier / 激活状态白话徽章（数据有就直读，没有退回固定角色文案，不显示原始英文字段值）。
function strategyBadgesHtml(model, def) {
  const badges = [];
  const stateRow = ((model.strategyState || {}).strategies || []).find((row) => row && row.strategy_id === def.id);
  const summaryRow = ((model.candidateState || {}).strategy_summaries || []).find((row) => row && row.strategy_id === def.id);
  const tier = safeText((stateRow && stateRow.tier) || (summaryRow && summaryRow.strategy_tier), '');
  badges.push(badge(TIER_LABELS[tier] || def.fallbackRole, def.tone === 'brand' ? 'brand' : 'info'));

  const activation = safeText(stateRow && stateRow.activation, '');
  if (activation === 'active') {
    badges.push(badge('运行中', 'ok'));
  } else if (activation) {
    badges.push(badge('未激活', 'warn'));
  }

  if (def.id === 't1_factor_v1') {
    // 研究预览是该策略的当前定位；有数据时直读 status，没有时按产品定位标注。
    const status = safeText((model.researchStateT1 || {}).status, '') || 'research_preview';
    if (status === 'research_preview') {
      badges.push(badge('研究预览 · 未实盘验证', 'warn'));
    }
  }
  return badges.join('');
}

// 真实历史表现：从 review_state.date_stats 逐日真实数据聚合（等权累乘 + 平均命中率）。
// 复盘文件归属由 strategy_id 判定；对不上号或没有逐日数据时返回 null（由调用方显示「待积累」）。
function performanceFromReview(reviewState, strategyId) {
  if (!reviewState || typeof reviewState !== 'object') return null;
  const sid = safeText(reviewState.strategy_id, '');
  if (sid ? sid !== strategyId : strategyId !== 'prebreakout_v41') return null;
  const stats = Array.isArray(reviewState.date_stats) ? reviewState.date_stats : [];
  const returnRows = stats.filter((row) => row && finiteNum(row.avg_next_day_return_pct) !== null);
  if (!returnRows.length) return null;

  let equity = 1;
  returnRows.forEach((row) => {
    equity *= 1 + finiteNum(row.avg_next_day_return_pct) / 100;
  });
  const hitValues = stats
    .map((row) => row && finiteNum(row.next_day_hit_rate_pct))
    .filter((value) => value !== null && value !== undefined);
  const range = reviewState.date_range || {};
  return {
    days: returnRows.length,
    cumulativePct: (equity - 1) * 100,
    avgHitPct: hitValues.length ? hitValues.reduce((acc, value) => acc + value, 0) / hitValues.length : null,
    from: safeText(range.from, ''),
    to: safeText(range.to, '')
  };
}

function performanceHtml(model, def) {
  if (def.id === 'prebreakout_v41' && model.isMissing('reviewState')) {
    return missingSection('历史表现', model.missingReason('reviewState'));
  }
  const perf = performanceFromReview(model.reviewState, def.id);
  if (!perf) {
    return `<div class="perf-empty">
      ${badge('待积累', 'flat')}
      <p>${escapeHtml(def.emptyPerfNote)}</p>
    </div>`;
  }
  const rangeText = perf.from && perf.to ? `${dateCn(perf.from)} ~ ${dateCn(perf.to)}` : '';
  return `<div class="perf-grid">
      ${statCard({
        title: '覆盖交易日',
        value: `${formatNumber(perf.days)} 天`,
        note: rangeText || '按有次日收盘数据的交易日统计',
        small: true
      })}
      ${statCard({
        title: '等权累计收益',
        valueHtml: pctHtml(perf.cumulativePct, 2),
        note: '每天等权买入当日全部推荐、次日收盘卖出，逐日连乘；不含交易成本与滑点',
        small: true
      })}
      ${statCard({
        title: '平均次日命中率',
        value: perf.avgHitPct === null ? '暂无可验证数据' : formatPct(perf.avgHitPct, 1),
        note: perf.avgHitPct === null ? '本期复盘数据未包含命中率明细' : '推荐个股在次日收盘上涨的平均比例',
        small: true
      })}
    </div>
    <p class="perf-link"><a class="text-link" href="./recommendation-review.html">查看完整战绩 →</a></p>`;
}

function strategyTodayHtml(model, def) {
  const count = strategyDayCount(model, def.id);
  if (count === null) {
    return `<p class="strategy-today-empty">${escapeHtml('本页未能读取该策略的当日名单数据，请到「个股推荐」页查看。')}</p>`;
  }
  const chips = [{ label: '当日入选', value: `${formatNumber(count)} 只` }];
  if (def.id === 'prebreakout_v41') {
    const roleCounts = model.candidateRoleCounts || {};
    const main = finiteNum(roleCounts.main);
    const watch = finiteNum(roleCounts.watch);
    const avoid = finiteNum(roleCounts.avoid);
    if (main !== null || watch !== null || avoid !== null) {
      chips.push(
        { label: '主攻', value: `${formatNumber(main ?? 0)} 只` },
        { label: '观察', value: `${formatNumber(watch ?? 0)} 只` },
        { label: '回避', value: `${formatNumber(avoid ?? 0)} 只` }
      );
    }
  }
  return `${chipList(chips)}
    <p class="strategy-today-link"><a class="text-link" href="./decision-candidates.html">查看当日完整名单 →</a></p>`;
}

function strategyCardHtml(model, def) {
  const registryRow = ((model.strategyRegistry || {}).strategies || []).find((row) => row && row.strategy_id === def.id) || {};
  const name = safeText(registryRow.strategy_name, def.fallbackName);
  const positioning = safeText(registryRow.positioning, '');
  return `<section class="panel strategy-card strategy-tone-${escapeHtml(def.tone)}">
    <div class="strategy-card-head">
      <div class="strategy-card-title">
        <h4>${escapeHtml(name)}</h4>
        <p class="strategy-oneliner">${escapeHtml(def.oneLiner)}</p>
      </div>
      <div class="strategy-badges">${strategyBadgesHtml(model, def)}</div>
    </div>
    <p class="strategy-principle">${escapeHtml(def.principle)}</p>
    ${positioning ? `<p class="strategy-positioning">定位：${escapeHtml(positioning)}</p>` : ''}
    <div class="strategy-block">
      <h5>当日入选</h5>
      ${strategyTodayHtml(model, def)}
    </div>
    <div class="strategy-block">
      <h5>历史表现</h5>
      ${performanceHtml(model, def)}
    </div>
  </section>`;
}

function strategiesTabHtml(model) {
  const cards = STRATEGY_DEFS.map((def) => strategyCardHtml(model, def)).join('\n');
  return `${weightDonutHtml(model)}
    ${sectionHead('三条策略', '同一天各自独立打分：主策略负责实盘推荐，辅助与研究线提供印证。以下统计全部来自当日与历史真实数据，没有数据就如实标注「待积累」。')}
    <div class="strategy-cards">${cards}</div>`;
}

// ---------------------------------------------------------------------------
// Tab 3：数据状态
// ---------------------------------------------------------------------------

function freshnessHtml(model) {
  const manifest = model.runManifest || {};
  const sources = manifest.sources || {};
  const rows = SOURCE_STEPS.map(([key, label, desc]) => [
    label,
    { text: friendlyTime(sources[key]) },
    desc
  ]);
  return `${sectionHead('数据新鲜度', `当前页面内容基于 ${dateCn(manifest.trade_date)} 收盘行情；下表是各环节最近一次更新时间。`)}
    ${dataTable({ columns: ['环节', '最近更新', '这一步做什么'], rows, emptyText: '暂无更新记录' })}`;
}

function healthHtml(model) {
  if (model.isMissing('systemHealth')) {
    return `${sectionHead('数据完整性')}
      ${missingSection('数据完整性', model.missingReason('systemHealth'))}`;
  }
  const health = model.systemHealth || {};
  const checks = health.checks || {};
  const keys = Object.keys(checks);
  if (!keys.length) {
    return `${sectionHead('数据完整性')}
      ${emptySection('数据完整性', '本期没有数据完整性检查结果。')}`;
  }

  const rows = [];
  const overall = health.ok;
  if (overall === true || overall === false) {
    rows.push([
      '整体自检',
      { html: badge(overall ? '全部通过' : '发现问题', overall ? 'ok' : 'bad') },
      overall ? '最近一次自动检查全部通过' : '最近一次自动检查未通过，部分内容可能不完整'
    ]);
  }

  const unknown = [];
  keys.forEach((key) => {
    const label = HEALTH_CHECK_LABELS[key];
    const value = checks[key];
    if (!label) {
      unknown.push(`${key} = ${safeText(value)}`);
      return;
    }
    if (value === true || value === false) {
      rows.push([
        label,
        { html: badge(value ? '正常' : '异常', value ? 'ok' : 'bad') },
        value ? '通过检查' : '未通过检查，相关内容可能缺失'
      ]);
      return;
    }
    const num = finiteNum(value);
    if (num !== null) {
      rows.push([
        label,
        { html: badge(num > 0 ? '正常' : '为 0', num > 0 ? 'ok' : 'warn') },
        `当前数值：${formatNumber(num)}`
      ]);
      return;
    }
    rows.push([label, { html: badge('—', 'flat') }, '暂无可读结果']);
  });

  const unknownHtml = unknown.length
    ? `<details class="notice-detail"><summary>更多技术检查项</summary><p>${escapeHtml(unknown.join('；'))}</p></details>`
    : '';

  // 研究线提示（research_state.warnings）：真实存在才显示，原文白话可直读。
  const warnings = ((model.researchState || {}).warnings || [])
    .filter((item) => typeof item === 'string' && item.trim() !== '');
  const warningsHtml = warnings.length
    ? `<div class="health-warnings">
        <h5>当前提示</h5>
        <ul>${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>
      </div>`
    : '';

  return `${sectionHead('数据完整性', `最近检查时间 ${friendlyTime(health.generated_at)}。`)}
    ${dataTable({ columns: ['检查项', '状态', '说明'], rows, emptyText: '暂无检查记录' })}
    ${warningsHtml}
    ${unknownHtml}`;
}

function complianceHtml() {
  const items = [
    '本站全部内容由量化模型自动生成，是一份选股研究记录，不构成任何投资建议，也不代表任何机构观点。',
    '历史表现不代表未来收益。任何策略都可能失效，过往统计（包括亏损记录）只说明过去，不预示将来。',
    '行情与统计数据来自公开数据接口，可能存在误差、缺失或延迟，请以交易所与券商的官方数据为准。',
    '若您参考本站内容进行交易，仓位与风险由您自行承担。请务必结合自身的风险承受能力做决定。',
    DISCLAIMER_TEXT
  ];
  return `${sectionHead('产品说明与免责声明')}
    ${elevatedCard(`<ul class="compliance-list">
        ${items.map((item) => `<li>${escapeHtml(item)}</li>`).join('\n        ')}
      </ul>`, { className: 'compliance-block' })}`;
}

// ---------------------------------------------------------------------------
// 系统解码（合同 v2）：源库 / 策略库 / 发布文件 / 门槛明细 / 调整日志 / AI 一致性 / 防回退监控
// 全部读 v2 标准字段，不猜。失败项用醒目状态。
// ---------------------------------------------------------------------------

const DECODE_SIDS = ['prebreakout_v41', 'greenfield_o2c_v1', 't1_factor_v1'];
const DECODE_SNAME = { prebreakout_v41: '启动前夕', greenfield_o2c_v1: 'O2C日内因子', t1_factor_v1: 'T+1胜率因子' };
const PUB_FILES = [
  'decision_state.json', 'recommendation_state.json', 'strategy_run_state.json',
  'review_state_unified.json', 'adjustment_log.json', 'system_health.json', 'publish_guard_state.json'
];

function gateStatusBadge(status) {
  const tone = { pass: 'ok', warn: 'warn', fail: 'fail' }[status] || 'flat';
  const label = { pass: '通过', warn: '注意', fail: '失败' }[status] || safeText(status, '—');
  return badge(label, tone);
}

function decodeGuardHtml(model) {
  const g = model.publishGuard;
  if (!g || model.isMissing('publishGuard')) {
    return `${sectionHead('发布防回退监控')}${emptySection('暂无监控产物（publish_guard_state.json 待生成）')}`;
  }
  const rows = (g.checks || []).map((c) => [
    safeText(c.name, ''), { html: badge(c.ok ? '正常' : '异常', c.ok ? 'ok' : 'fail') }, safeText(c.detail, '')
  ]);
  return `${sectionHead('发布防回退监控', `合同版本 ${safeText(g.contract_version, '?')} · 数据日 ${safeText(g.latest_trade_date, '?')} · 提交 ${escapeHtml(String(g.latest_commit || '').slice(0, 10) || '?')}`)}
    <div class="decode-kpis">
      ${statCard({ title: '总体状态', value: g.ok ? '健康' : '异常', tone: g.ok ? 'ok' : 'fail' })}
      ${statCard({ title: '失败项', value: formatNumber((g.failures || []).length), tone: (g.failures || []).length ? 'fail' : 'ok' })}
      ${statCard({ title: '告警项', value: formatNumber((g.warnings || []).length), tone: (g.warnings || []).length ? 'warn' : 'ok' })}
    </div>
    ${rows.length ? dataTable({ columns: ['检查项', '状态', '说明'], rows }) : ''}`;
}

function decodeSourceDbHtml(model) {
  const h = model.systemHealth || {};
  const sdb = h.source_database || {};
  const rows = [
    ['类型', { html: badge('共享只读源库', 'ok') }, '只存行情/财务/交易日历/基础因子/市场环境，策略结果不入源库'],
    ['路径', safeText(sdb.path || sdb.kind, '—'), ''],
    ['状态', { html: badge(sdb.exists === false ? '缺失' : '存在', sdb.exists === false ? 'fail' : 'ok') }, safeText(sdb.write_policy, '策略不得写入源库')],
  ];
  return `${sectionHead('源数据库状态')}${dataTable({ columns: ['项', '值', '说明'], rows })}`;
}

function decodeStrategyDbHtml(model) {
  const strategies = (model.strategyRunState || {}).strategies || [];
  if (!strategies.length) {
    return `${sectionHead('策略数据库状态')}${emptySection('暂无 strategy_run_state.strategies[]（待发布层生成）')}`;
  }
  const rows = strategies.map((s) => {
    const cov = (s.ai_coverage || {});
    const covPct = cov.value != null ? formatPct(cov.value * 100, 0) : '—';
    return [
      safeText(s.strategy_name || DECODE_SNAME[s.strategy_id], s.strategy_id),
      { html: badge(s.run_status === 'ok' ? '已运行' : safeText(s.run_status, '—'), s.run_status === 'ok' ? 'ok' : 'warn') },
      safeText(s.source_date, '—'),
      formatNumber(s.item_count || 0),
      `${covPct}${cov.have != null ? `（${formatNumber(cov.have)}/${formatNumber(cov.total || 0)}）` : ''}`,
      { html: gateStatusBadge(s.gate_status) },
      { html: s.research_only ? badge('研究观察', 'warn') : badge('可交易', 'ok') },
    ];
  });
  const dbRows = strategies.map((s) => [
    safeText(s.strategy_name || DECODE_SNAME[s.strategy_id], s.strategy_id),
    safeText(s.database_path, '—'),
  ]);
  return `${sectionHead('策略数据库状态', '各策略当日运行、数据日期、Top20、AI 覆盖率、门槛与是否研究观察')}
    ${dataTable({ columns: ['策略', '运行', '数据日', 'Top20', 'AI覆盖率', '门槛', '可交易性'], rows })}
    <details class="notice-detail"><summary>策略数据库路径</summary>
      ${dataTable({ columns: ['策略', '数据库路径'], rows: dbRows })}
    </details>`;
}

function decodePublishFilesHtml(model) {
  const h = model.systemHealth || {};
  const pubFiles = h.publication_files || {};
  const rows = PUB_FILES.map((f) => {
    const key = f.replace('.json', '');
    const meta = pubFiles[f] || pubFiles[key] || {};
    // 优先用 model 已加载文档判断存在+合法（已解析成功即合法）。
    const docKey = {
      'decision_state.json': 'decisionState', 'recommendation_state.json': 'recommendationState',
      'strategy_run_state.json': 'strategyRunState', 'review_state_unified.json': 'reviewUnified',
      'adjustment_log.json': 'adjustmentLog', 'system_health.json': 'systemHealth',
      'publish_guard_state.json': 'publishGuard'
    }[f];
    const loaded = docKey && model[docKey] && !model.isMissing(docKey);
    const exists = loaded || meta.exists === true;
    return [
      f,
      { html: badge(exists ? '存在·合法' : '缺失', exists ? 'ok' : 'fail') },
      safeText(meta.generated_at || (loaded ? (model[docKey].generated_at || '') : ''), '—'),
    ];
  });
  return `${sectionHead('发布文件状态', 'latest 目录关键合同文件的存在性、合法性与生成时间')}${dataTable({ columns: ['文件', '状态', '生成时间'], rows })}`;
}

function decodeGateDetailHtml(model) {
  const strats = (model.recommendationState || {}).strategies || {};
  if (!Object.keys(strats).length) {
    return `${sectionHead('策略门槛明细')}${emptySection('暂无 recommendation_state（待发布层生成）')}`;
  }
  const GNAMES = { ai_coverage: 'AI覆盖率', data_freshness: '数据新鲜度', review_samples: '复盘样本', review_gate: '复盘升级门槛', oos_icir: '样本外ICIR', net_excess: '净超额' };
  const blocks = DECODE_SIDS.map((sid) => {
    const s = strats[sid] || {};
    const gates = ((s.strategy_gate || {}).gates) || {};
    const rows = Object.entries(GNAMES).map(([k, label]) => {
      const gg = gates[k] || {};
      let v = gg.value != null ? safeText(String(gg.value)) : (gg.note ? safeText(gg.note) : '—');
      if (k === 'review_gate' && (gg.valid_review_days != null || gg.hit_rate_pct != null)) {
        v = `有效${formatNumber(gg.valid_review_days || 0)}日 / 命中${gg.hit_rate_pct == null ? '—' : formatNumber(gg.hit_rate_pct, 1) + '%'} / 超额${gg.avg_excess_return_pct == null ? '—' : formatNumber(gg.avg_excess_return_pct, 2) + '%'}`;
      }
      return [label, { html: gateStatusBadge(gg.status) }, v];
    });
    const verdict = (s.strategy_gate || {}).verdict;
    return `<div class="decode-strategy-gate">
      <h5>${escapeHtml(safeText(s.strategy_name || DECODE_SNAME[sid], sid))} ${s.research_only ? badge('研究观察', 'warn') : badge('可进入交易参考', 'ok')}</h5>
      ${dataTable({ columns: ['门槛', '状态', '值'], rows })}
    </div>`;
  }).join('');
  return `${sectionHead('策略门槛明细', '每套策略 5 项硬门槛：任一不达标即只研究观察、不进交易参考')}${blocks}`;
}

// O2C 距离可交易还差什么：把 6 项升级条件逐条摊开，明确缺口；样本不足/未达门槛不显示可买入。
function o2cGateDistanceSection(model) {
  const s = ((model.recommendationState || {}).strategies || {}).greenfield_o2c_v1;
  if (!s) {
    return `${sectionHead('O2C 距离可交易还差什么')}${emptySection('暂无 O2C 策略合同（待发布层生成）')}`;
  }
  const gates = ((s.strategy_gate || {}).gates) || {};
  const rg = s.review_gate || {};
  const aiCov = s.ai_coverage || {};
  const fresh = s.data_freshness || {};
  const rs = gates.review_samples || {};
  const passOf = (b) => (b ? badge('已满足', 'ok') : badge('未满足', 'warn'));
  const conds = [
    ['AI 覆盖率 ≥ 60%', aiCov.status === 'pass', aiCov.value != null ? `${formatPct(aiCov.value * 100, 0)}（${formatNumber(aiCov.have || 0)}/${formatNumber(aiCov.total || 0)}）` : '—'],
    ['数据新鲜度（源日=交易日）', fresh.status === 'pass', `${safeText(fresh.source_date, '—')} / ${safeText(fresh.trade_date, '—')}`],
    ['有效复盘样本日 ≥ 3', (rg.valid_review_days || 0) >= (rg.required_review_days || 3), `${formatNumber(rg.valid_review_days || 0)} / ${formatNumber(rg.required_review_days || 3)}`],
    ['平均次日命中率 ≥ 50%', rg.hit_rate_pct != null && rg.hit_rate_pct >= 50, rg.hit_rate_pct == null ? '—' : `${formatNumber(rg.hit_rate_pct, 2)}%`],
    ['平均次日超额 > 0', rg.avg_excess_return_pct != null && rg.avg_excess_return_pct > 0, rg.avg_excess_return_pct == null ? '—' : `${formatNumber(rg.avg_excess_return_pct, 2)}%`],
    ['最近 3 个有效日非连续为负', rg.recent_consecutive_negative === false, rg.recent_consecutive_negative === true ? '连续为负' : '否'],
  ];
  const rows = conds.map(([label, ok, val]) => [label, { html: passOf(ok) }, val]);
  const unmet = conds.filter(([, ok]) => !ok).map(([label]) => label);
  const headline = s.research_only
    ? `<p class="help-text"><strong>${badge('研究观察', 'warn')} O2C 暂不作为买入依据。</strong> ${unmet.length ? '距离可交易仍缺：' + escapeHtml(unmet.join('、')) + '。' : ''}${escapeHtml(safeText(rg.summary, ''))}</p>`
    : `<p class="help-text"><strong>${badge('可进入交易参考', 'ok')} O2C 已满足升级门槛</strong>（仍受市场环境门槛约束）。</p>`;
  return `${sectionHead('O2C 距离可交易还差什么', 'O2C 是否可交易由真实硬门槛决定（非 AI 主观）：AI 覆盖 / 数据新鲜 / 复盘样本 / 命中率 / 超额 / 非连负')}
    ${headline}
    ${dataTable({ columns: ['升级条件', '状态', '当前值'], rows })}
    <p class="soft">${escapeHtml(safeText(rg.benchmark_note, ''))}</p>`;
}

function decodeAdjustmentHtml(model) {
  const rows = ((model.adjustmentLog || {}).rows || []).slice(0, 15).map((r) => [
    safeText(r.stock_name || r.stock_code, '—'),
    safeText(DECODE_SNAME[r.strategy_id] || r.strategy_id, '—'),
    safeText(r.raw_action, '—'),
    safeText(r.final_action, '—'),
    { html: badge(r.changed ? '已调整' : '未变', r.changed ? 'warn' : 'flat') },
    safeText((r.reasons || []).join('；'), '—'),
  ]);
  return `${sectionHead('自动调整日志', '原始动作 → 最终动作 + 调整原因（仅展示真正发生变动的记录）')}
    ${rows.length ? dataTable({ columns: ['股票', '策略', '原始', '最终', '是否调整', '原因'], rows }) : emptySection('当日无动作调整记录')}`;
}

function decodeAiConsistencyHtml(model) {
  const strats = (model.recommendationState || {}).strategies || {};
  const rows = DECODE_SIDS.map((sid) => {
    const s = strats[sid] || {};
    const items = s.items || [];
    const realAi = items.filter((it) => it.ai_coverage_counted).length;
    const total = items.length;
    const cov = total ? formatPct(realAi / total * 100, 0) : '—';
    const tmpl = sid === 't1_factor_v1' ? total : 0; // T1 为模板说明
    return [
      safeText(s.strategy_name || DECODE_SNAME[sid], sid),
      formatNumber(total),
      formatNumber(realAi),
      formatNumber(tmpl),
      cov,
    ];
  });
  const t1 = strats['t1_factor_v1'] || {};
  const t1NoFakeAi = (t1.items || []).every((it) => it.ai_score == null);
  return `${sectionHead('AI 一致性检查', '真实深度 AI vs 模板说明；T1 保持非真实 AI 标记')}
    ${dataTable({ columns: ['策略', '推荐数', '真实AI', '模板说明', 'AI覆盖率'], rows })}
    <p class="help-text">T1 保持非真实 AI（ai_score 全为空）：${t1NoFakeAi ? '<strong>是 ✓</strong>' : '<strong style="color:var(--bad)">否（异常）</strong>'}</p>`;
}

function systemDecodeHtml(model) {
  return `${sectionHead('系统解码', '源库 → 策略库 → 发布文件 → 门槛 → 调整 → AI 一致性 → 防回退，全部读 v2 标准字段。')}
    ${decodeGuardHtml(model)}
    ${decodeSourceDbHtml(model)}
    ${decodeStrategyDbHtml(model)}
    ${decodePublishFilesHtml(model)}
    ${decodeGateDetailHtml(model)}
    ${o2cGateDistanceSection(model)}
    ${decodeAdjustmentHtml(model)}
    ${decodeAiConsistencyHtml(model)}`;
}

function dataStatusTabHtml(model) {
  return `${systemDecodeHtml(model)}
    ${freshnessHtml(model)}
    ${healthHtml(model)}
    ${complianceHtml()}`;
}

// ---------------------------------------------------------------------------
// 页面入口
// ---------------------------------------------------------------------------

export function renderResearch(model, opts = {}) {
  const initialTab = TABS.some((tab) => tab.key === opts.initialTab) ? opts.initialTab : 'how';
  // 旧 URL strategy-vs-market.html 预选策略中心 Tab 时，使用其专属页标题（VIEW_META.strategy）。
  const viewKey = initialTab === 'strategies' ? 'strategy' : 'research';

  const panels = [
    tabPanel('how', howTabHtml(model), { active: initialTab === 'how', groupId: TAB_GROUP }),
    tabPanel('strategies', strategiesTabHtml(model), { active: initialTab === 'strategies', groupId: TAB_GROUP }),
    tabPanel('dataStatus', dataStatusTabHtml(model), { active: initialTab === 'dataStatus', groupId: TAB_GROUP })
  ].join('\n');

  return renderShell(viewKey, model, `
    ${renderHero(
      model,
      '这套系统怎么为你工作',
      '从市场研判、策略打分到每日复盘，这一页讲清系统的工作方式、三条策略的原理与数据状态。所有统计都来自真实数据，亏损也照实展示。'
    )}
    ${tabsBar(TABS, initialTab, { groupId: TAB_GROUP })}
    ${panels}
  `);
}
