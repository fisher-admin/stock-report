// v5/data.js — 数据清单 + 取数 + 纯函数模型（Node 可测，无 DOM）。
// 只读取 config/public-result-allowlist.txt 内的公开结果合同；本机专用文件
// （prebreakout_factory_watch / review_state_unified / factor_attribution_state）永不读取。

export const SOURCES = {
  runManifest: { path: 'data/latest/run_manifest.json', label: '运行清单' },
  systemVerdict: { path: 'data/latest/system_verdict.json', label: '系统裁决' },
  decisionState: { path: 'data/latest/decision_state.json', label: '今日裁决' },
  marketContext: { path: 'data/latest/market_context.json', label: '市场环境' },
  candidateState: { path: 'data/latest/candidate_state.json', label: '候选状态' },
  recommendationState: { path: 'data/latest/recommendation_state.json', label: 'Top-20 推荐合同' },
  strategyBacktests: { path: 'data/strategy_backtests.json', label: '策略 Top-20 快照' },
  marketState: { path: 'data/latest/market_state.json', label: '市场状态' },
  marketHeatmap: { path: 'data/recommendation_analytics/market_industry_heatmap_latest.json', label: '行业热力' },
  reviewTrack: { path: 'data/latest/review_track_latest.json', label: '历史战绩' },
  strategyEvaluation: { path: 'data/latest/strategy_evaluation.json', label: '策略评估' },
  dualTrack: { path: 'data/latest/dual_track_state.json', label: '双轨验证' },
  s3Watchlist: { path: 'data/latest/s3_watchlist.json', label: 'S3 分时形态' },
  setupEngine: { path: 'data/latest/setup_engine_status.json', label: '剧本引擎' },
  factorEvolution: { path: 'data/latest/factor_evolution_state.json', label: '因子进化' },
  sentiment: { path: 'data/latest/sentiment_state.json', label: 'AI 观点分布' },
  strategyRegistry: { path: 'data/latest/strategy_registry.json', label: '策略档案' },
  strategyRunState: { path: 'data/latest/strategy_run_state.json', label: '策略运行' },
  publishGuard: { path: 'data/latest/publish_guard_state.json', label: '发布守卫' },
  systemHealth: { path: 'data/latest/system_health.json', label: '系统健康' },
  v44Challenger: { path: 'data/latest/v44_challenger_state.json', label: 'v4.4 挑战者影子' }
};

const CORE = ['runManifest', 'systemVerdict', 'decisionState', 'marketContext', 'candidateState'];
export const ROUTE_DEPS = {
  today: [...CORE, 'recommendationState', 'strategyBacktests', 'marketState', 'reviewTrack', 'strategyEvaluation'],
  candidates: [...CORE, 'recommendationState', 'strategyBacktests', 'v44Challenger'],
  market: [...CORE, 'marketState', 'marketHeatmap'],
  evidence: [...CORE, 'reviewTrack', 'strategyEvaluation', 'dualTrack'],
  lab: [...CORE, 's3Watchlist', 'setupEngine', 'dualTrack', 'factorEvolution', 'sentiment'],
  system: [...CORE, 'publishGuard', 'systemHealth', 'strategyRegistry', 'strategyRunState']
};
export const ROUTES = Object.keys(ROUTE_DEPS);

// 演示数据：20261008 已公开发布快照（commit bd241cdc），仅存在于仓库 preview/，
// Pages 构建不复制该目录，因此线上 ?demo=live 会自动降级为实时数据。
export const DEMO_BASE = 'preview/demo-20261008/';

export function createLoader({ fetchImpl = fetch, demo = false } = {}) {
  const cache = new Map();
  let version = '';
  const url = (key) => {
    const p = SOURCES[key].path;
    return (demo ? DEMO_BASE + p.split('/').pop() : p) + (version ? `?v=${encodeURIComponent(version)}` : '');
  };
  async function get(key) {
    if (!cache.has(key)) {
      cache.set(key, (async () => {
        try {
          const res = await fetchImpl(url(key), key === 'runManifest' ? { cache: 'no-store' } : {});
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return { ok: true, data: await res.json() };
        } catch (err) {
          return { ok: false, error: String(err.message || err) };
        }
      })());
    }
    return cache.get(key);
  }
  return {
    async load(keys) {
      const manifest = await get('runManifest');
      if (manifest.ok && !version) version = manifest.data.run_id || manifest.data.generated_at || '';
      const out = { data: {}, missing: {} };
      await Promise.all(keys.map(async (k) => {
        const r = await get(k);
        if (r.ok) out.data[k] = r.data; else out.missing[k] = r.error;
      }));
      return out;
    }
  };
}

// ---------------------------------------------------------------- 模型

const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(+v) ? null : +v);

export const ACTION_CN = { main: '主攻', conditional_long: '条件做多', watch: '观察', avoid: '回避' };

export function maintenanceOf(d) {
  for (const k of ['candidateState', 'recommendationState', 'runManifest', 'systemVerdict']) {
    const m = d[k]?.maintenance ?? d[k]?.final_action?.maintenance;
    if (m && typeof m === 'object' && m.enabled) return m;
  }
  return d.systemVerdict?.final_action?.maintenance === true ? { enabled: true, title: '系统校准升级中' } : null;
}

export function tradeDate(d) {
  return d.runManifest?.trade_date || d.decisionState?.trade_date || d.systemVerdict?.dates?.decision_trade_date || '';
}

const FACTOR_KEYS = [
  ['波动收敛', '波动收敛'], ['量稳定性', '量稳定性'], ['动量', '动量'], ['温和放量', '温和放量']
];
export const shortFactor = (k) => String(k).replace(/[（(].*$/, '').trim();

function splitPoint(p) {
  const s = String(p);
  const i = s.search(/[：:]/);
  return i > 0 && i < 14 ? { head: s.slice(0, i), body: s.slice(i + 1).trim() } : { head: '', body: s };
}

export function normalizeStock(r, cand = {}) {
  const code = String(r.stock_code || r.code || r.normalized_code || '').slice(0, 6);
  const factors = Object.entries(r.factor_scores || {})
    .map(([k, v]) => ({ name: shortFactor(k), full: k, value: num(v) }))
    .filter((f) => f.value !== null);
  const key = FACTOR_KEYS.map(([label, prefix]) => {
    const f = factors.find((x) => x.name.startsWith(prefix));
    return f ? { ...f, name: label } : null;
  }).filter(Boolean);
  const action = r.final_action || cand.final_action || r.gate_adjusted_action || r.raw_action || 'watch';
  return {
    code,
    tsCode: r.ts_code || r.display_code || code,
    name: r.stock_name || r.name || cand.name || code,
    industry: r.industry || r.industry_name || cand.industry_name || '',
    rank: num(r.rank_no ?? r.rank),
    score: num(r.score),
    pubScore: num(r.publication_score),
    aiScore: num(r.ai_score ?? cand.ai_score),
    close: num(r.close ?? r.price ?? cand.close),
    chg: num(r.change_pct ?? r.change ?? cand.change_pct),
    action,
    actionCn: ACTION_CN[action] || action,
    factors,
    keyFactors: key,
    chip: { conc: num(r.chip_conc), winner: num(r.winner_rate), vr: num(r.volume_ratio), rsi: num(r.rsi_6) },
    ai: {
      status: r.ai_status || (r.has_ai_analysis ? 'ready' : ''),
      advice: r.ai_advice || cand.ai_advice || '',
      conclusion: r.ai_conclusion || r.ai_summary || cand.ai_conclusion || '',
      points: arr(r.ai_points?.length ? r.ai_points : cand.ai_points).map(splitPoint),
      risks: arr(r.ai_risks?.length ? r.ai_risks : cand.ai_risks),
      catalysts: arr(r.ai_catalysts?.length ? r.ai_catalysts : cand.ai_catalysts),
      evidenceTime: r.ai_evidence_time || '',
      provenance: r.ai_provenance || r.ai_temporal_class || '',
      mayChangeRank: r.ai_may_change_rank === true
    },
    plan: {
      buyZone: r.buy_zone || '', invalidation: r.invalidation || '', nextDay: r.next_day_handling || '',
      tier: num(r.position_tier), holding: num(r.holding_period_days), entry: r.planned_entry_time || '',
      cutoff: r.signal_data_cutoff || ''
    },
    rule: { cap: r.rule_action_cap || '', reasons: arr(r.rule_risk_reasons), adjust: arr(r.adjustment_reasons) },
    market: { action: cand.market_action || '', summary: cand.market_action_summary || '' },
    settlement: r.settlement_status || '',
    strategy: r.strategy_name || cand.strategy_name || '',
    version: r.strategy_version || ''
  };
}

export function top20Model(d) {
  const rs = d.recommendationState || {};
  const cands = arr(d.candidateState?.candidates);
  const candBy = new Map(cands.map((c) => [String(c.code || c.normalized_code).slice(0, 6), c]));
  let rows = arr(rs.final_recommendations);
  let source = 'recommendation_state.final_recommendations';
  if (!rows.length) {
    rows = arr(d.strategyBacktests?.strategies).flatMap((s) => arr(s.top20));
    source = 'strategy_backtests.top20';
  }
  if (!rows.length && cands.length) { rows = cands; source = 'candidate_state.candidates'; }
  const stocks = rows.map((r) => normalizeStock(r, candBy.get(String(r.stock_code || r.code).slice(0, 6)) || {}))
    .sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999));
  const strat = Object.values(rs.strategies || {})[0] || d.strategyBacktests?.strategies?.[0] || {};
  const counts = { main: 0, conditional_long: 0, watch: 0, avoid: 0 };
  stocks.forEach((s) => { if (s.action in counts) counts[s.action] += 1; });
  return {
    stocks, source, counts,
    signalDate: rows[0]?.recommend_date || rows[0]?.source_date || rs.trade_date || tradeDate(d),
    strategyName: strat.strategy_name || strat.name || stocks[0]?.strategy || '',
    version: strat.strategy_version || stocks[0]?.version || '',
    maintenance: maintenanceOf(d)
  };
}

export function reviewSeries(d) {
  const rows = arr(d.reviewTrack?.daily_comparison)
    .filter((r) => num(r.avg_next_day_return_pct) !== null)
    .sort((a, b) => String(a.recommend_date).localeCompare(String(b.recommend_date)));
  let eq = 1;
  const series = rows.map((r) => {
    eq *= 1 + num(r.avg_next_day_return_pct) / 100;
    return { date: r.recommend_date, ret: num(r.avg_next_day_return_pct), hit: num(r.next_day_hit_rate_pct), cum: (eq - 1) * 100, n: r.sample_count };
  });
  const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const last20 = series.slice(-20);
  return {
    series,
    days: series.length,
    avgRet: mean(series.map((s) => s.ret)),
    avgHit: mean(series.map((s) => s.hit).filter((h) => h !== null)),
    cum: series.length ? series[series.length - 1].cum : null,
    avgRet20: mean(last20.map((s) => s.ret)),
    avgHit20: mean(last20.map((s) => s.hit).filter((h) => h !== null)),
    maxDD: (() => { let pk = 1, dd = 0; series.forEach((s) => { const v = 1 + s.cum / 100; pk = Math.max(pk, v); dd = Math.min(dd, v / pk - 1); }); return dd * 100; })()
  };
}

// 陈旧度：源日期与运行交易日的日历天差；>7 天视为陈旧。
export function ageDays(srcDate, refDate) {
  const p = (s) => {
    const m = String(s || '').match(/(\d{4})-?(\d{2})-?(\d{2})/);
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : null;
  };
  const a = p(srcDate), b = p(refDate);
  return a === null || b === null ? null : Math.round((b - a) / 86400000);
}

// ---------------------------------------------------------------- v4.4 挑战者（影子）

const COMP_SHORT = { core: '纯化核心', vpd_20: '量价背离', smf_20_rev: '聪明钱反转' };

export function challengerModel(d) {
  const c = d.v44Challenger;
  if (!c) return { status: 'missing', stocks: [], maintenance: maintenanceOf(d) };
  const delta = c.delta || {};
  const consensus = new Map(arr(delta.consensus).map((x) => [x.code, x]));
  const stocks = arr(c.top20).map((r) => {
    const s = normalizeStock({ ...r, change_pct: r.change_pct, chip_conc: r.chip?.chip_conc, winner_rate: r.chip?.winner_rate,
      volume_ratio: r.chip?.volume_ratio, rsi_6: r.chip?.rsi_6, final_action: 'watch' });
    const comps = Object.entries(r.components || {}).map(([k, v]) => ({ key: k, name: COMP_SHORT[k] || k, full: v.label || k, value: num(v.pct), z: num(v.z) }));
    const cons = consensus.get(s.code);
    s.keyFactors = comps.filter((f) => f.value !== null);
    s.factors = [...s.keyFactors.map((f) => ({ ...f, name: `挑战者·${f.name}` })), ...s.factors];
    s.actionCn = '影子观察';
    s.action = 'watch';
    s.challenger = {
      pct: num(r.challenger_pct), booster: num(r.booster_score), comps, hold: r.hold_status || '',
      delta: cons ? 'consensus' : 'challenger_only', championRank: cons ? num(cons.champion_rank) : null,
      aiShared: r.ai_source === 'shared_same_day_analysis'
    };
    return s;
  }).sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999));
  return {
    status: c.status || 'ok', reason: c.reason || '', stocks, delta, doc: c,
    aligned: c.aligned !== false, maintenance: maintenanceOf(d)
  };
}
