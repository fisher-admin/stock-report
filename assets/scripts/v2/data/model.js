// v2/data/model.js — 把已加载的 JSON 组装成视图模型（纯函数，无 DOM 依赖，可在 Node 中测试）。
//
// v3 重写要点（DESIGN-V3.md 第 0/3 节）：
//   - model.staleness：交易日感知的数据过期判定（周六日顺延），全站过期横幅的唯一依据；
//   - 动作只信管线字段（role_type / adjusted_action），删除对 AI 文案的中文正则二次推断；
//   - topCandidates 仅作 candidate 层数据透传（附 displayRank 与 aiStatus，不再改写动作）；
//   - model.aiCoverage：候选 AI 分析覆盖度计数（full/stale/none），供徽章与提示使用；
//   - buildModel(data, missing, nowMs) 第三参用于测试注入时间，浏览器默认取当前时间。

import { safeText, toneFromGate, toneFromVerdict } from '../render/format.js';
import { latestRows, MARKET_HEATMAP_FIELDS, STRATEGY_HEATMAP_FIELDS } from './summarize.js';

const DAY_MS = 24 * 60 * 60 * 1000;

// 取上海时区的"当前时刻"拆解（年月日时分秒 + 星期），全模块统一从这里读时间。
function shanghaiParts(now = new Date()) {
  const formatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    weekday: 'short',
    hour12: false
  });
  const parts = Object.fromEntries(formatter.formatToParts(now).map((part) => [part.type, part.value]));
  // zh-CN 的 short weekday 形如 "周六"。
  const weekdayMap = { 周日: 0, 周一: 1, 周二: 2, 周三: 3, 周四: 4, 周五: 5, 周六: 6 };
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    second: parts.second,
    weekday: weekdayMap[parts.weekday] ?? new Date(now).getUTCDay(),
    dateLabel: `${parts.year}-${parts.month}-${parts.day}`,
    timeLabel: `${parts.hour}:${parts.minute}:${parts.second}`
  };
}

// 当前时段提示：周末/盘前/盘中/盘后。周末绝不显示"盘中"类文案（诚实性规范 0.5）。
export function getSessionMode(now = new Date()) {
  const sh = shanghaiParts(now);
  const minutes = sh.hour * 60 + sh.minute;
  const meta = { dateLabel: sh.dateLabel, timeLabel: sh.timeLabel, minutes };
  if (sh.weekday === 0 || sh.weekday === 6) {
    return { key: 'closed', label: '周末休市', summary: '今天股市不开盘，页面内容是最近一个交易日的记录。', now: meta };
  }
  if (minutes < 9 * 60 + 15) {
    return { key: 'preopen', label: '开盘前', summary: '开盘前请先看今日结论与仓位指引，再看名单。', now: meta };
  }
  if ((minutes >= 9 * 60 + 15 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60)) {
    return { key: 'intraday', label: '交易时段', summary: '盘中请先确认风险提示，再对照观察名单。', now: meta };
  }
  return { key: 'postclose', label: '收盘后', summary: '收盘后适合回看战绩与明日安排。', now: meta };
}

// 解析 "20260611" / "2026-06-11" 为 UTC 零点时间戳（仅作日历计算，不涉及时区时刻）。
function parseDateKey(raw) {
  const digits = safeText(raw, '').replace(/-/g, '');
  if (!/^\d{8}$/.test(digits)) return null;
  const year = Number(digits.slice(0, 4));
  const month = Number(digits.slice(4, 6));
  const day = Number(digits.slice(6, 8));
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return Date.UTC(year, month - 1, day);
}

function formatDateKey(raw) {
  const digits = safeText(raw, '').replace(/-/g, '');
  if (!/^\d{8}$/.test(digits)) return safeText(raw, '');
  return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6, 8)}`;
}

// 数据过期判定（诚实性规范 0.5）：
// daysLate = 数据交易日与"今天"之间被错过的工作日数（不含两端；周六日不计）。
// 例：数据=周四，今天=周五 → 0（周五的数据本就在周五晚才生成，不算过期）；
//     数据=周四，今天=周六 → 1（周五整天的数据缺失）；
//     数据=周五，今天=下周一 → 0（周末顺延，属正常）。
// 局限：法定节假日无法识别，假期后第一天可能短暂误报，横幅文案保持克制。
export function computeStaleness(tradeDateRaw, now = new Date()) {
  const fresh = { isStale: false, tradeDate: '', daysLate: 0, label: '', isTodayTradingDay: false };
  const tradeUtc = parseDateKey(tradeDateRaw);
  if (tradeUtc === null) return fresh;

  const sh = shanghaiParts(now);
  const todayUtc = Date.UTC(sh.year, sh.month - 1, sh.day);
  const tradeDate = formatDateKey(tradeDateRaw);
  const isTodayTradingDay = sh.weekday >= 1 && sh.weekday <= 5;

  let daysLate = 0;
  // 只数严格位于 (tradeDate, today) 之间的工作日；上限护栏防止异常数据导致长循环。
  let cursor = tradeUtc + DAY_MS;
  let guard = 0;
  while (cursor < todayUtc && guard < 3700) {
    const dow = new Date(cursor).getUTCDay();
    if (dow >= 1 && dow <= 5) daysLate += 1;
    cursor += DAY_MS;
    guard += 1;
  }

  const isStale = daysLate >= 1;
  let label = `数据更新于 ${tradeDate}`;
  if (isStale) {
    label = isTodayTradingDay
      ? `数据更新于 ${tradeDate}，今日数据尚未生成`
      : `数据更新于 ${tradeDate}，最近 ${daysLate} 个交易日的数据尚未生成`;
  }
  return { isStale, tradeDate, daysLate, label, isTodayTradingDay };
}

export function buildWorkflow(verdict, decisionState) {
  // 合同 v2：优先读 decision_state.gates（统一四闸 *_gate），回退 system_verdict.gates。
  const gates = (decisionState && decisionState.gates) || verdict.gates || {};
  const entries = [
    ['freshness', '数据新鲜度', gates.freshness_gate],
    ['market', '市场环境', gates.market_gate],
    ['strategy', '策略激活', gates.strategy_gate],
    ['candidate', '候选执行', gates.candidate_gate]
  ];
  return entries.map(([id, label, gate]) => ({
    id,
    label,
    summary: safeText(gate?.summary),
    tone: toneFromGate(gate?.status)
  }));
}

function hasRealText(value) {
  if (Array.isArray(value)) return value.some((item) => hasRealText(item));
  return typeof value === 'string' ? value.trim() !== '' : value !== null && value !== undefined && value !== '';
}

// AI 状态三分类（诚实性规范 0.2）：
//   full  = ai_summary / ai_points 真实存在且未标陈旧 → 「AI 已分析」
//   stale = 有内容但 ai_source_stale 标记引用旧日分析 → 「AI 分析（旧）」
//   none  = ai 字段全空 → 「无 AI 分析 · 仅量化信号」，渲染层严禁用模板话术填充。
export function candidateAiStatus(item) {
  const hasContent = hasRealText(item?.ai_summary) || hasRealText(item?.ai_points) || hasRealText(item?.ai_conclusion);
  if (!hasContent) return 'none';
  if (item?.ai_source_stale) return 'stale';
  return 'full';
}

// candidate 层数据透传：不再用 AI 文案的中文正则改写动作（诚实性规范 0.4）。
// roleType 直读管线字段 role_type；aiStatus 供徽章使用；其余字段原样透传给卡片渲染。
export function topCandidates(candidateState) {
  // 按 rank 升序（rank 缺失/非正退到 score 降序），让「第 1 名」排最前、徽章单调递增。
  const items = (candidateState.candidates || []).slice().sort((a, b) => {
    const ra = Number(a.rank) > 0 ? Number(a.rank) : Infinity;
    const rb = Number(b.rank) > 0 ? Number(b.rank) : Infinity;
    if (ra !== rb) return ra - rb;
    return (Number(b.score) || 0) - (Number(a.score) || 0);
  });
  return items.slice(0, 20).map((item, idx) => ({
    ...item,
    displayRank: item.rank || idx + 1,
    roleType: safeText(item.role_type, 'watch'),
    aiStatus: candidateAiStatus(item)
  }));
}

// 候选分层计数：只数 role_type（candidate 层口径，与执行层 execution_state 分开命名）。
export function countRoleTypes(candidates) {
  return (candidates || []).reduce((acc, item) => {
    const key = item.roleType === 'main' || item.roleType === 'avoid' ? item.roleType : 'watch';
    acc[key] += 1;
    acc.all += 1;
    return acc;
  }, { all: 0, main: 0, watch: 0, avoid: 0 });
}

// 候选 AI 覆盖度：徽章与"今日无 AI 分析"提示的数据来源。
export function countAiCoverage(candidates) {
  return (candidates || []).reduce((acc, item) => {
    const key = item.aiStatus === 'full' || item.aiStatus === 'stale' ? item.aiStatus : 'none';
    acc[key] += 1;
    acc.total += 1;
    return acc;
  }, { total: 0, full: 0, stale: 0, none: 0 });
}

// 系统说明页的研究线卡片。客户语言：不出现 validation/publish_ready 等内部字段名（规范 0.8）。
export function extractResearchCards(researchState) {
  const validation = researchState.validation || {};
  const shortTerm = researchState.short_term || {};
  const longTerm = researchState.long_term || {};
  return [
    {
      title: '系统自检',
      value: validation.ok === false ? '发现问题' : '正常',
      note: validation.ok === false
        ? '最近一次自动检查未通过，相关数据可能不完整。'
        : '最近一次自动检查通过，数据链路完整。',
      tone: validation.ok === false ? 'warn' : 'pass'
    },
    {
      title: '短线研究线',
      value: safeText(shortTerm.status || shortTerm.label || '研究中'),
      note: safeText(shortTerm.summary || '短线方向仍在内部研究，结果不进入推荐。'),
      tone: 'info'
    },
    {
      title: '长线研究线',
      value: safeText(longTerm.status || longTerm.label || '研究中'),
      note: safeText(longTerm.summary || '长线方向仍在内部研究，结果不进入推荐。'),
      tone: 'info'
    }
  ];
}

// data: loader 给出的 {key: doc|null}；missing: [{key,label,reason}]；
// nowMs: 可选，毫秒时间戳（测试注入用）。浏览器调用不传 → 取当前时间。
export function buildModel(data, missing = [], nowMs = undefined) {
  const doc = (key) => data[key] || {};
  const now = Number.isFinite(nowMs) ? new Date(nowMs) : new Date();

  const model = {
    runManifest: doc('runManifest'),
    systemVerdict: doc('systemVerdict'),
    decisionState: doc('decisionState'),
    marketContext: doc('marketContext'),
    marketState: doc('marketState'),
    strategyState: doc('strategyState'),
    strategyRegistry: doc('strategyRegistry'),
    systemHealth: doc('systemHealth'),
    strategyRunState: doc('strategyRunState'),
    recommendationState: doc('recommendationState'),
    adjustmentLog: doc('adjustmentLog'),
    publishGuard: doc('publishGuard'),
    candidateState: doc('candidateState'),
    reviewState: doc('reviewState'),
    reviewUnified: doc('reviewUnified'),
    sentimentState: doc('sentimentState'),
    researchState: doc('researchState'),
    setupEngine: doc('setupEngine'),
    s3Watchlist: doc('s3Watchlist'),
    prebreakoutShadowWatch: doc('prebreakoutShadowWatch'),
    executionState: doc('executionState'),
    researchStateT1: doc('researchStateT1'),
    midday: doc('midday'),
    unified: doc('unified'),
    marketHeatmap: doc('marketHeatmap'),
    strategyHeatmap: doc('strategyHeatmap'),
    greenfieldTop20: doc('greenfieldTop20'),
    t1FactorRecommendations: doc('t1FactorRecommendations'),
    sessionMode: getSessionMode(now),
    missing: Array.isArray(missing) ? missing : []
  };

  // 数据过期判定：对比 run_manifest.trade_date（缺失时退到 system_verdict 决策日）与当前日期。
  const tradeDateRaw = model.runManifest.trade_date || model.systemVerdict.dates?.decision_trade_date || '';
  model.staleness = computeStaleness(tradeDateRaw, now);

  model.workflow = buildWorkflow(model.systemVerdict, model.decisionState);

  // candidate 层（策略分层口径）。
  model.candidates = topCandidates(model.candidateState);
  model.candidateRoleCounts = data.candidateState
    ? countRoleTypes(model.candidates)
    : { all: 0, main: 0, watch: 0, avoid: 0, ...(model.runManifest.candidate_role_counts || {}) };
  // 兼容别名（v2 旧渲染器引用名；口径已改为 role_type 直读，不再二次推断）。
  model.displayCandidateCounts = model.candidateRoleCounts;
  model.aiCoverage = countAiCoverage(model.candidates);

  // execution 层（执行建议口径，首页 Hero 唯一数字来源）。
  model.executionSummary = {
    total: Number(model.executionState.total_execution_count) || 0,
    main: Number(model.executionState.main_count) || 0,
    watch: Number(model.executionState.watch_count) || 0,
    avoid: Number(model.executionState.avoid_count) || 0,
    consensus: Number(model.executionState.consensus_in_execution) || 0
  };

  model.researchCards = extractResearchCards(model.researchState);
  model.strategy = (model.strategyState.strategies || [])[0] || {};
  model.verdict = model.systemVerdict.final_action || {};
  model.verdictTone = toneFromVerdict(model.verdict.action);
  model.marketSectors = model.marketState.top_market_sectors || [];
  model.industryActions = model.marketState.industry_actions || [];
  model.marketHeatmapLatestRows = latestRows(
    model.marketHeatmap, MARKET_HEATMAP_FIELDS.dateField, MARKET_HEATMAP_FIELDS.latestField, MARKET_HEATMAP_FIELDS.rankField
  );
  model.strategyHeatmapLatestRows = latestRows(
    model.strategyHeatmap, STRATEGY_HEATMAP_FIELDS.dateField, STRATEGY_HEATMAP_FIELDS.latestField, STRATEGY_HEATMAP_FIELDS.rankField
  );
  model.reviewLeaders = model.reviewState.top_repeat_recommendations || [];
  model.reviewSamples = model.reviewState.latest_sample || [];

  // 历史战绩页的归一化入口（review_track_latest.json 与全量 unified 字段一致）。
  model.reviewTrack = {
    generatedAt: safeText(model.reviewUnified.generated_at, ''),
    tradeDate: safeText(model.reviewUnified.trade_date, ''),
    strategies: model.reviewUnified.strategies || {},
    dailyComparison: model.reviewUnified.daily_comparison || [],
    stockRows: model.reviewUnified.stock_rows || []
  };

  const missingKeys = new Set(model.missing.map((item) => item.key));
  model.isMissing = (key) => missingKeys.has(key);
  model.missingReason = (key) => (model.missing.find((item) => item.key === key) || {}).reason || '数据缺失';
  return model;
}
