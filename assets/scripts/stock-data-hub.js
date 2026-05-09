function assetPath(relativePath) {
  return {
    relativePath,
    href: new URL(relativePath, import.meta.url).href
  };
}

const PATHS = {
  dataJson: assetPath('../../data.json'),
  runManifest: assetPath('../../data/latest/run_manifest.json'),
  systemVerdict: assetPath('../../data/latest/system_verdict.json'),
  marketState: assetPath('../../data/latest/market_state.json'),
  strategyState: assetPath('../../data/latest/strategy_state.json'),
  candidateState: assetPath('../../data/latest/candidate_state.json'),
  reviewState: assetPath('../../data/latest/review_state.json'),
  reviewStateO2C: assetPath('../../data/latest/review_state_o2c.json'),
  researchState: assetPath('../../data/latest/research_state.json'),
  morningBrief: assetPath('../../data/recommendation_analytics/market_morning_brief_latest.json'),
  midday: assetPath('../../data/recommendation_analytics/midday_analysis_latest.json'),
  prebreakout: assetPath('../../data/recommendation_analytics/prebreakout_recommendations.json'),
  unified: assetPath('../../data/recommendation_analytics/unified_decision_payload.json'),
  marketHeatmap: assetPath('../../data/recommendation_analytics/market_industry_heatmap.json'),
  strategyHeatmap: assetPath('../../data/recommendation_analytics/industry_heatmap.json'),
  greenfieldTop20: assetPath('../../data/latest/greenfield_top20.json'),
  combinedRecommendation: assetPath('../../data/latest/combined_recommendation.json')
};

async function loadJson(pathSpec) {
  const href = typeof pathSpec === 'string' ? pathSpec : pathSpec.href;
  const label = typeof pathSpec === 'string' ? pathSpec : pathSpec.relativePath;
  const url = new URL(href);
  url.searchParams.set('ts', Date.now().toString());
  const resp = await fetch(url, { cache: 'no-store' });
  if (!resp.ok) {
    throw new Error(`${label} 加载失败（HTTP ${resp.status}）`);
  }
  return resp.json();
}

export function safeText(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

export function formatPct(value, digits = 1, suffix = '%') {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return `${num.toFixed(digits)}${suffix}`;
}

export function formatNumber(value, digits = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return num.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

export function escapeHtml(value) {
  return safeText(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function latestRows(doc, dateField, latestField, rankField) {
  const latest = safeText(doc?.[latestField], '');
  const rows = Array.isArray(doc?.rows) ? doc.rows : [];
  return rows
    .filter((row) => safeText(row?.[dateField], '') === latest)
    .sort((a, b) => Number(a?.[rankField] || 9999) - Number(b?.[rankField] || 9999));
}

function getShanghaiParts() {
  const formatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });
  const parts = Object.fromEntries(formatter.formatToParts(new Date()).map((part) => [part.type, part.value]));
  return {
    dateLabel: `${parts.year}-${parts.month}-${parts.day}`,
    timeLabel: `${parts.hour}:${parts.minute}:${parts.second}`,
    minutes: Number(parts.hour) * 60 + Number(parts.minute)
  };
}

function getSessionMode() {
  const now = getShanghaiParts();
  const minutes = now.minutes;
  if (minutes < 9 * 60 + 15) {
    return { key: 'preopen', label: '开盘前', summary: '先看结论卡，再看市场与策略，不要先翻名单。', now };
  }
  if ((minutes >= 9 * 60 + 15 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60)) {
    return { key: 'intraday', label: '盘中', summary: '先盯风控与行业主线，再筛看执行名单。', now };
  }
  return { key: 'postclose', label: '盘后', summary: '先看复盘和样本表现，再决定是否调整次日执行框架。', now };
}

function toneFromGate(status) {
  if (status === 'pass') return 'pass';
  if (status === 'warn') return 'warn';
  return 'fail';
}

function toneFromVerdict(action) {
  if (action === 'execute' || action === 'deploy') return 'pass';
  if (action === 'observe_only') return 'warn';
  return 'fail';
}

function buildWorkflow(verdict) {
  const gates = verdict.gates || {};
  return [
    {
      id: 'freshness',
      label: '数据新鲜度',
      summary: safeText(gates.freshness_gate?.summary),
      tone: toneFromGate(gates.freshness_gate?.status)
    },
    {
      id: 'market',
      label: '市场环境',
      summary: safeText(gates.market_gate?.summary),
      tone: toneFromGate(gates.market_gate?.status)
    },
    {
      id: 'strategy',
      label: '策略激活',
      summary: safeText(gates.strategy_gate?.summary),
      tone: toneFromGate(gates.strategy_gate?.status)
    },
    {
      id: 'candidate',
      label: '候选执行',
      summary: safeText(gates.candidate_gate?.summary),
      tone: toneFromGate(gates.candidate_gate?.status)
    }
  ];
}

function buildPrimarySignals({ runManifest, systemVerdict, marketState, strategyState, candidateState, midday }) {
  const verdict = systemVerdict.final_action || {};
  const candidateCounts = candidateState.role_counts || runManifest.candidate_role_counts || {};
  const strategy = (strategyState.strategies || [])[0] || {};
  const middayTradeDate = safeText(midday.trade_date, '');
  const decisionTradeDate = safeText(systemVerdict.dates?.decision_trade_date, '');
  const middayStale = middayTradeDate && decisionTradeDate && middayTradeDate !== decisionTradeDate;

  return {
    primary: [
      {
        label: '最终裁决',
        value: safeText(verdict.label),
        note: safeText(verdict.summary),
        tone: toneFromVerdict(verdict.action)
      },
      {
        label: '市场风险',
        value: `${formatNumber(marketState.market_summary?.risk_score ?? marketState.morning?.risk_score ?? 0)} / 100`,
        note: `${safeText(marketState.market_summary?.market_regime ?? marketState.morning?.regime)}｜先看市场，再决定是否看票。`,
        tone: Number(marketState.market_summary?.risk_score ?? marketState.morning?.risk_score ?? 0) >= 70 ? 'fail' : Number(marketState.market_summary?.risk_score ?? marketState.morning?.risk_score ?? 0) >= 50 ? 'warn' : 'pass'
      },
      {
        label: '策略状态',
        value: safeText(strategy.activation === 'active' ? '启动前夕已激活' : strategy.activation || '未激活'),
        note: `Top20=${formatNumber(strategy.top20_count || 0)}｜市场重合=${formatNumber(strategy.market_overlap_count || 0)}`,
        tone: strategy.activation === 'active' ? 'pass' : 'warn'
      },
      {
        label: '执行名单',
        value: `主攻 ${formatNumber(candidateCounts.main || 0)} / 观察 ${formatNumber(candidateCounts.watch || 0)}`,
        note: `当前可执行动作由 final_candidate_action 决定。`,
        tone: Number(candidateCounts.main || 0) > 0 ? 'pass' : Number(candidateCounts.watch || 0) > 0 ? 'warn' : 'fail'
      }
    ],
    secondary: [
      {
        label: '盘中快照',
        value: safeText(marketState.midday_source === 'published' ? '沿用已发布午盘' : marketState.midday_source || '未接入'),
        note: middayStale ? `午盘日期仍是 ${middayTradeDate}，落后决策日 ${decisionTradeDate}` : '午盘来源与决策流可联动显示。',
        tone: middayStale ? 'warn' : 'info'
      },
      {
        label: '发布闭环',
        value: runManifest.published ? '已写入发布仓' : '未完成发布',
        note: `validation_ok=${runManifest.validation_ok}｜publish_ready=${runManifest.publish_ready}`,
        tone: runManifest.publish_ready ? 'pass' : runManifest.published ? 'warn' : 'fail'
      }
    ],
    noise: [
      '四策略横向堆表不是当前交易员首屏重点。',
      '研究层的短线/长线 alpha 结果应沉到盘后研究，不应和盘前执行混排。',
      '过早展示大段复盘统计会稀释开盘前的执行信号。'
    ]
  };
}

function topCandidates(candidateState) {
  const items = candidateState.candidates || [];
  return items.slice(0, 20).map((item, idx) => ({
    ...item,
    displayRank: item.rank || idx + 1,
    finalAction: item.final_candidate_action || item.action || item.role_type || 'watch',
    displayAction: displayActionFromAdvice(item)
  }));
}

function displayActionFromAdvice(item) {
  const raw = safeText(item.final_candidate_action || item.action || item.role_type || 'watch', 'watch');
  const advice = safeText(item.ai_advice || item.ai_conclusion || item.review_ai_view || '', '');
  if (raw === 'avoid' || /回避|放弃|不碰|减仓/.test(advice)) return 'avoid';
  if (/买入|加仓|介入/.test(advice)) return 'main';
  return 'watch';
}

function countDisplayActions(candidates) {
  return candidates.reduce((acc, item) => {
    const key = item.displayAction || 'watch';
    acc[key] = (acc[key] || 0) + 1;
    acc.all += 1;
    return acc;
  }, { all: 0, main: 0, watch: 0, avoid: 0 });
}

function buildTimeBlocks(model) {
  const verdict = model.systemVerdict.final_action || {};
  const openingPlaybook = model.marketState.morning?.opening_playbook || [];
  const middayAdvice = model.marketState.midday?.midday_action_advice || model.midday?.midday_action_advice || '午盘信号未及时刷新，盘中只参考市场/策略两层。';
  const reviewSummary = model.reviewState.performance || {};
  return [
    {
      label: '开盘前',
      title: `${safeText(verdict.label)}｜先判可不可以出手`,
      note: safeText(verdict.summary),
      bullets: openingPlaybook.length ? openingPlaybook : ['先看 freshness / market / strategy / candidate 四级闸门。', '没有主攻名单时，不要把观察名单包装成买点。']
    },
    {
      label: '盘中',
      title: '行业主线 > 候选名单',
      note: safeText(middayAdvice),
      bullets: [
        `观察名单：${formatNumber(model.runManifest.candidate_role_counts?.watch || 0)} 只`,
        `主攻名单：${formatNumber(model.runManifest.candidate_role_counts?.main || 0)} 只`,
        safeText(model.marketState.market_summary?.market_regime || model.marketState.morning?.regime, '中性') + ' 环境下避免把提示语当成确认。'
      ]
    },
    {
      label: '盘后',
      title: '只复盘对下一日决策有用的结果',
      note: `次日命中率 ${formatPct(reviewSummary.next_day_hit_rate_pct, 2)}｜平均次日收益 ${formatPct(reviewSummary.avg_next_day_return_pct, 2)}`,
      bullets: [
        '复盘页只保留：命中率、收益漂移、重复出现个股、AI视角偏差。',
        '研究页保留系统健康与研究线，不再和交易执行层混排。'
      ]
    }
  ];
}

function extractResearchCards(researchState) {
  const cards = [];
  const validation = researchState.validation || {};
  const shortTerm = researchState.short_term || {};
  const longTerm = researchState.long_term || {};
  cards.push({
    title: '系统健康',
    value: validation.ok === false ? '需修复' : '可用',
    note: `validation=${safeText(validation.ok)}｜publish_ready=${safeText(validation.publish_ready)}`,
    tone: validation.ok === false ? 'warn' : 'pass'
  });
  cards.push({
    title: '短线研究线',
    value: safeText(shortTerm.status || shortTerm.label || '研究态'),
    note: safeText(shortTerm.summary || '短线 alpha 结果仅保留在研究层。'),
    tone: 'info'
  });
  cards.push({
    title: '长线研究线',
    value: safeText(longTerm.status || longTerm.label || '研究态'),
    note: safeText(longTerm.summary || '长线 alpha 结果仅保留在研究层。'),
    tone: 'info'
  });
  return cards;
}

async function loadJsonSafe(pathSpec, fallback = null) {
  try {
    return await loadJson(pathSpec);
  } catch {
    return fallback;
  }
}

export async function loadWorkbenchModel() {
  const [dataJson, runManifest, systemVerdict, marketState, strategyState, candidateState, reviewState, reviewStateO2C, researchState, morningBrief, midday, prebreakout, unified, marketHeatmap, strategyHeatmap, greenfieldTop20, combinedRecommendation] = await Promise.all([
    loadJsonSafe(PATHS.dataJson, {}),
    loadJson(PATHS.runManifest),
    loadJson(PATHS.systemVerdict),
    loadJson(PATHS.marketState),
    loadJson(PATHS.strategyState),
    loadJson(PATHS.candidateState),
    loadJson(PATHS.reviewState),
    loadJsonSafe(PATHS.reviewStateO2C, {}),
    loadJson(PATHS.researchState),
    loadJson(PATHS.morningBrief),
    loadJson(PATHS.midday),
    loadJson(PATHS.prebreakout),
    loadJson(PATHS.unified),
    loadJson(PATHS.marketHeatmap),
    loadJson(PATHS.strategyHeatmap),
    loadJsonSafe(PATHS.greenfieldTop20, {}),
    loadJsonSafe(PATHS.combinedRecommendation, {})
  ]);

  const model = {
    dataJson,
    runManifest,
    systemVerdict,
    marketState,
    strategyState,
    candidateState,
    reviewState,
    reviewStateO2C,
    researchState,
    morningBrief,
    midday,
    prebreakout,
    unified,
    marketHeatmap,
    strategyHeatmap,
    greenfieldTop20,
    combinedRecommendation,
    sessionMode: getSessionMode()
  };

  model.workflow = buildWorkflow(systemVerdict);
  model.signalTier = buildPrimarySignals(model);
  model.candidates = topCandidates(candidateState);
  model.displayCandidateCounts = countDisplayActions(model.candidates);
  model.timeBlocks = buildTimeBlocks(model);
  model.researchCards = extractResearchCards(researchState);
  model.strategy = (strategyState.strategies || [])[0] || {};
  model.verdict = systemVerdict.final_action || {};
  model.verdictTone = toneFromVerdict(model.verdict.action);
  model.marketSectors = marketState.top_market_sectors || [];
  model.industryActions = marketState.industry_actions || [];
  model.marketHeatmapLatestRows = latestRows(marketHeatmap, 'trade_date', 'latest_trade_date', 'market_heat_rank');
  model.strategyHeatmapLatestRows = latestRows(strategyHeatmap, 'recommend_date', 'latest_recommend_date', 'heat_rank');
  model.reviewLeaders = reviewState.top_repeat_recommendations || [];
  model.reviewSamples = reviewState.latest_sample || [];
  model.o2cReviewLeaders = reviewStateO2C.top_repeat_recommendations || [];
  model.o2cReviewSamples = reviewStateO2C.latest_sample || [];
  return model;
}
