// tests/v5-render.test.mjs — v5 前端渲染回归（纯 Node，零依赖）。运行：node tests/v5-render.test.mjs
// 夹具：
//   preview/demo-20261008/       —— 20261008 已公开发布快照（bd241cdc），Top-20 + 逐股 AI 全量；
//   tests/fixtures/v5-maintenance —— 20261009 维护模式快照（个股名单撤下）。
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

import { SOURCES, ROUTE_DEPS, ROUTES, top20Model, reviewSeries, maintenanceOf, createLoader, normalizeStock, ageDays, challengerModel, arenaModel, doublePassModel, lifecycleModel, cohortModel } from '../assets/scripts/v5/data.js';
import { VIEWS } from '../assets/scripts/v5/views.js';
import { verdictStrip, maintenanceBanner, validationBanner, topbar } from '../assets/scripts/v5/shell.js';
import { drawer, showroom } from '../assets/scripts/v5/top20.js';
import { esc } from '../assets/scripts/v5/ui.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const load = (dir) => {
  const d = {};
  for (const [k, s] of Object.entries(SOURCES)) {
    const p = join(ROOT, dir, s.path.split('/').pop());
    if (existsSync(p)) d[k] = JSON.parse(readFileSync(p, 'utf-8'));
  }
  return d;
};
const demo = load('preview/demo-20261008');
const maint = load('tests/fixtures/v5-maintenance');
const live = load('tests/fixtures/v5-challenger'); // 20261009 实盘发布 + v4.4 挑战者合同

let passed = 0;
const test = (name, fn) => {
  const fail = (e) => { console.error(`not ok - ${name}\n${e.stack}`); process.exitCode = 1; };
  const ok = () => { passed += 1; console.log(`ok - ${name}`); };
  try { const r = fn(); if (r?.then) r.then(ok, fail); else ok(); } catch (e) { fail(e); }
};
const clean = (html, label) => {
  for (const bad of ['undefined', 'NaN', '[object Object]', 'null%']) assert.ok(!html.includes(bad), `${label} 含 "${bad}"`);
};
const ctx = (route, extra = {}) => ({ route, demo: false, demoAvailable: true, mode: route === 'candidates' ? 'matrix' : 'grid', ...extra });
const count = (html, needle) => html.split(needle).length - 1;

test('数据清单只引用公开白名单文件', () => {
  const allow = new Set(readFileSync(join(ROOT, 'config/public-result-allowlist.txt'), 'utf-8').split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#')));
  for (const [k, s] of Object.entries(SOURCES)) assert.ok(allow.has(s.path), `${k} → ${s.path} 不在白名单`);
  const src = Object.values(SOURCES).map((s) => s.path).join(' ');
  for (const local of ['prebreakout_factory_watch', 'review_state_unified', 'factor_attribution_state']) assert.ok(!src.includes(local), local);
  for (const r of ROUTES) for (const k of ROUTE_DEPS[r]) assert.ok(SOURCES[k], `${r} 依赖未登记 ${k}`);
});

test('演示快照：Top-20 展厅 20 张卡片 + 每只都有 AI 结论', () => {
  const m = top20Model(demo);
  assert.equal(m.stocks.length, 20);
  assert.equal(m.maintenance, null);
  assert.equal(m.source, 'recommendation_state.final_recommendations');
  assert.ok(m.stocks.every((s) => /^\d{6}$/.test(s.code)), '6 位代码');
  assert.ok(m.stocks.every((s) => s.ai.conclusion && s.ai.points.length), 'AI 结论与分项');
  assert.ok(m.stocks.every((s) => s.keyFactors.length === 4), '四个核心因子');
  assert.deepEqual(m.stocks.map((s) => s.rank), [...m.stocks.map((s) => s.rank)].sort((a, b) => a - b));
  const html = VIEWS.today(demo, ctx('today'));
  assert.equal(count(html, 'class="card"'), 20);
  assert.ok(html.includes('格力电器') && html.includes('000651'));
  assert.ok(!html.includes('ghost-wrap'));
  clean(html, 'today/demo');
});

test('演示快照：AI 抽屉渲染论点、分项诊断、风险、因子与执行参考', () => {
  const m = top20Model(demo);
  const s = m.stocks.find((x) => x.code === '000651');
  const html = drawer(s, { index: 0, total: 20 });
  assert.ok(html.includes('role="dialog"'));
  assert.ok(html.includes(esc(s.ai.conclusion)));
  assert.ok(html.includes('技术形态/启动条件'), '分项标题被解析');
  assert.ok(html.includes('风险提示') && html.includes(esc(s.ai.risks[0])));
  assert.ok(html.includes('波动收敛') && html.includes('执行参考'));
  clean(html, 'drawer');
  for (const x of m.stocks) clean(drawer(x, { index: 0, total: 20 }), `drawer/${x.code}`);
});

test('挑战者竞技场：v4.4 头部、三栏差异条、20 张挑战者卡片', () => {
  const m = challengerModel(live);
  assert.equal(m.status, 'ok');
  assert.equal(m.stocks.length, 20);
  const c = live.v44Challenger.delta.counts;
  assert.equal(c.consensus + c.challenger_only, 20);
  assert.equal(c.consensus + c.vetoed + c.ranked_out, live.recommendationState.final_recommendations.length);
  assert.ok(m.stocks.every((s) => s.challenger && s.keyFactors.length === 3), '三个挑战者分量');
  const html = VIEWS.candidates(live, ctx('candidates', { mode: 'grid' }));
  assert.ok(html.includes('v4.4 影子基准') && html.includes('探索性影子基准') && html.includes('未验证') && html.includes('统计不显著') && html.includes('混杂实验'));
  assert.ok(html.includes('共同入选') && html.includes('仅影子入选') && html.includes('仅生产入选') && !html.includes('Challenger Alpha'));
  assert.equal(count(html, 'class="card"'), 20);
  assert.equal(count(html, 'class="dchip"'), c.consensus + c.challenger_only + c.vetoed + c.ranked_out);
  assert.ok(html.includes('已剔除：流动性') && !html.includes('<span>流动性</span>'), 'v4.4 已剔除的因子只列为剔除，不展示为权重');
  clean(html, 'candidates/live');
  const mx = VIEWS.candidates(live, ctx('candidates', { mode: 'matrix' }));
  assert.equal(count(mx, '<tr data-stock='), 20);
  assert.ok(mx.includes('挑战者·纯化核心'));
  clean(mx, 'candidates/live/matrix');
});

test('挑战者抽屉：共同入选带 AI，独享股只展示量化画像', () => {
  const m = challengerModel(live);
  const shared = m.stocks.find((s) => s.challenger.delta === 'consensus' && s.ai.conclusion);
  const only = m.stocks.find((s) => s.challenger.delta === 'challenger_only' && !s.ai.conclusion);
  assert.ok(shared && only);
  const a = drawer(shared, { index: 0, total: 20 });
  assert.ok(a.includes('v4.4 影子画像') && a.includes('共同入选') && a.includes('风险提示'));
  const b = drawer(only, { index: 1, total: 20 });
  assert.ok(b.includes('仅影子入选') && b.includes('未触发 AI 分析') && !b.includes('风险提示'));
  for (const s of m.stocks) clean(drawer(s, { index: 0, total: 20 }), `chal-drawer/${s.code}`);
});

test('冠军页不受挑战者影响；挑战者缺失时竞技场降级', () => {
  const today = VIEWS.today(live, ctx('today'));
  assert.equal(count(today, 'class="card"'), 20);
  assert.ok(!today.includes('影子画像') && !today.includes('仅影子入选'));
  assert.ok(today.includes('唯一生产观察名单'), '今日页声明唯一生产口径');
  const noChal = VIEWS.candidates(demo, ctx('candidates'));
  assert.ok(noChal.includes('影子基准数据暂不可用'));
  clean(noChal, 'candidates/demo');
  const unavailable = { ...live, v44Challenger: { status: 'unavailable', reason: '尚未生成', top20: [], delta: null } };
  assert.ok(VIEWS.candidates(unavailable, ctx('candidates')).includes('v4.4 影子名单尚未生成'));
  const m = VIEWS.candidates(maint, ctx('candidates'));
  assert.equal(count(m, 'card ghost'), 20);
});

test('维护模式：横幅 + 展厅保留 20 个占位与覆盖层，布局不被替换', () => {
  assert.ok(maintenanceOf(maint));
  const banner = maintenanceBanner(maint);
  assert.ok(banner.includes('系统校准升级中'));
  const html = VIEWS.today(maint, ctx('today'));
  assert.ok(html.includes('每日 Top-20 展厅'));
  assert.equal(count(html, 'card ghost'), 20);
  assert.ok(html.includes('class="overlay"') && html.includes('?demo=live'));
  assert.ok(html.includes('市场脉搏') && html.includes('决策闸门'), '侧栏照常');
  clean(html, 'today/maint');
  assert.ok(!VIEWS.today(maint, ctx('today', { demoAvailable: false })).includes('?demo=live'), '线上无演示快照时不给链接');
});

test('全部视图在两份快照与降级模式下干净渲染', () => {
  const degraded = { runManifest: maint.runManifest, systemVerdict: maint.systemVerdict };
  for (const [label, d] of [['demo', demo], ['maint', maint], ['live', live], ['degraded', degraded]]) {
    for (const r of ROUTES) {
      const html = topbar(d, ctx(r)) + verdictStrip(d) + VIEWS[r](d, ctx(r));
      assert.ok(html.length > 500, `${label}/${r} 过短`);
      clean(html, `${label}/${r}`);
    }
  }
  assert.ok(VIEWS.evidence(degraded, ctx('evidence')).includes('暂不可用'));
});

test('战绩序列按日期升序并复利', () => {
  const r = reviewSeries(demo);
  assert.ok(r.days > 100);
  assert.ok(r.series.every((s, i) => !i || s.date > r.series[i - 1].date));
  const eq = r.series.reduce((a, s) => a * (1 + s.ret / 100), 1);
  assert.ok(Math.abs((eq - 1) * 100 - r.cum) < 1e-9);
});

test('裁决条标注风险分口径（收盘实测 vs 盘前）', () => {
  const html = verdictStrip(maint);
  assert.ok(html.includes('收盘实测') && html.includes('盘前 50'));
  assert.ok(html.includes('仅观察'));
});

test('所有插值经过转义', () => {
  const evil = normalizeStock({ code: '000001', name: '<img src=x onerror=alert(1)>', industry: '"><u>', ai_conclusion: '<script>x</script>', ai_points: ['<i>a</i>：b'], factor_scores: { '<x>': 90 } });
  const html = drawer(evil, { index: 0, total: 1 }) + showroom({ stocks: [evil], counts: { main: 0, conditional_long: 0, watch: 1, avoid: 0 }, maintenance: null, signalDate: '20261008' });
  assert.ok(!/<img|<script|<i>a|"><u>|<x>/.test(html));
});

test('陈旧度计算', () => {
  assert.equal(ageDays('20260810', '20261009'), 60);
  assert.equal(ageDays('2026-10-09 07:00', '20261009'), 0);
  assert.equal(ageDays('', '20261009'), null);
});

test('取数器：演示模式改写路径、带 run_id 缓存参数、可选源缺失不抛错', async () => {
  const seen = [];
  const fetchImpl = async (url) => {
    seen.push(url);
    if (url.includes('run_manifest')) return { ok: true, json: async () => ({ run_id: 'r1' }) };
    return { ok: false, status: 404 };
  };
  const out = await createLoader({ fetchImpl, demo: true }).load(['runManifest', 'marketState']);
  assert.equal(out.data.runManifest.run_id, 'r1');
  assert.ok(out.missing.marketState.includes('404'));
  assert.ok(seen[0].startsWith('preview/demo-20261008/run_manifest.json'));
  assert.ok(seen.some((u) => u === 'preview/demo-20261008/market_state.json?v=r1'));
});

test('页面壳：index 资源版本一致，旧入口全部委托到哈希路由', () => {
  const index = readFileSync(join(ROOT, 'index.html'), 'utf-8');
  const css = index.match(/v5\.css\?v=([^"']+)/)?.[1];
  assert.ok(css, 'index.html 缺少 v5 CSS 版本号');
  assert.equal(index.match(/v5\/app\.js\?v=([^"']+)/)?.[1], css, 'CSS/JS 版本号不一致');
  const map = { 'decision-candidates.html': 'candidates', 'industry-compare.html': 'market', 'industry-heatmap.html': 'market', 'market-industry-heatmap.html': 'market', 'market-overview.html': 'market', 'prebreakout-shadow.html': 'lab', 'recommendation-review.html': 'evidence', 'research-lab.html': 'lab', 's3-watch.html': 'lab', 'sentiment.html': 'lab', 'strategy-vs-market.html': 'evidence' };
  for (const [file, route] of Object.entries(map)) {
    assert.ok(ROUTES.includes(route));
    const html = readFileSync(join(ROOT, file), 'utf-8');
    assert.ok(html.includes(`location.replace('./index.html' + location.search + '#/${route}')`), `${file} → #/${route}`);
    assert.ok(html.includes(`url=./index.html#/${route}`), `${file} 缺少无脚本回退`);
  }
});

test('历史竞技场：双线净值、记分卡、显著性与否决门归因', () => {
  const m = arenaModel(live);
  assert.equal(m.status, 'ok');
  assert.equal(m.curve.length, live.arenaLedger.window.curve_days);
  const html = VIEWS.evidence(live, ctx('evidence'));
  assert.ok(html.includes('双轨对照 · 历史回放') && html.includes('历史回放 · 回测'));
  assert.ok(html.includes('净值差主要来自换手摩擦的降低，而非已验证的预测能力') && html.includes('扣费前累计收益'), '摩擦归因声明');
  const dec = m.decomposition;
  assert.ok(dec && Math.abs(dec.netSpread - (live.arenaLedger.summary.base.v44.cum_net - live.arenaLedger.summary.base.v43.cum_net)) < 1e-9);
  assert.equal(count(html, 'class="ln-a"'), 2, '基准 + 压力两张图');
  assert.equal(count(html, 'class="ln-b"'), 2);
  assert.equal(count(html, 'class="hot"'), 2 * m.curve.length, '每日一个悬停热区');
  assert.ok(html.includes('绩效记分卡') && html.includes('年化摩擦拖累') && html.includes('最大回撤'));
  const t = live.arenaLedger.significance.daily_diff_t;
  assert.ok(html.includes(Math.abs(t) >= 2 ? '>显著<' : '>不显著<'), '显著性徽章与 t 一致');
  assert.ok(html.includes('否决门') && html.includes('规避价差'));
  assert.ok(!html.includes('-0.00'), '持平不显示负零');
  const perSig = html.slice(html.indexOf('逐信号日明细'), html.indexOf('回放方法与局限'));
  assert.equal(count(perSig, '<tr><td>'), live.arenaLedger.per_signal.length, '逐信号日行数');
  assert.ok(html.includes('前瞻验证（真实发布记录）'), '前瞻证据保留在竞技场下方');
  assert.ok(!/\b\d{6}\.(SH|SZ)\b/.test(JSON.stringify(live.arenaLedger)), '公开账本不含逐股代码');
  clean(html, 'evidence/live');
});

test('历史竞技场缺失或未生成时降级', () => {
  assert.ok(VIEWS.evidence(maint, ctx('evidence')).includes(SOURCES.arenaLedger.label));
  const stub = { ...live, arenaLedger: { status: 'unavailable', reason: '尚未运行历史回放' } };
  const html = VIEWS.evidence(stub, ctx('evidence'));
  assert.ok(html.includes('回放账本尚未生成') && html.includes('前瞻验证'));
  clean(html, 'evidence/stub');
});

test('双料王牌监控：收集中、已结算、缺失三种状态', () => {
  const g = (m) => ({ label: m, days: 2, trades: 5, mean_net: 0.01, mean_excess: 0.004, hit_trade: 0.6, hit_day_excess: 0.5, nw_t_excess: 1.1 });
  const stats = { DOUBLE: g('双料王牌 (DOUBLE-PASS)'), KEEP: g('保留'), DANGER: g('警示'), V43_ALL: g('全体'), BENCH: g('基准') };
  const spr = { DOUBLE_vs_NON_DOUBLE: { a: 'DOUBLE', b: 'NON_DOUBLE', days: 2, mean: 0.003, hit_day: 0.5, nw_t: 0.8 } };
  const ok = { ...live, doublePass: { status: 'ok', as_of: '20261020', official_days: 3, matured: { h1: 2, h5: 2 }, cost: { round_trip: 0.003 },
    stats: { h1: stats, h5: stats }, spreads: { h1: spr, h5: spr }, verdict: { status: 'insufficient_sample', required: 60, message: '样本不足' },
    curve: [{ date: '20261014', DOUBLE: 0.01, BENCH: 0.002 }, { date: '20261015', DOUBLE: 0.015, BENCH: 0.004 }],
    days: [{ date: '20261014', counts: { DOUBLE: 3, KEEP: 6, DANGER: 11 }, t1: { DOUBLE: 0.01 }, t5: { DOUBLE: 0.02, NON_DOUBLE: 0.01, BENCH: 0.005 }, double_members: [{ name: '贵阳银行', ret5: 0.02 }] }] } };
  assert.equal(doublePassModel(ok).status, 'ok');
  const html = VIEWS.evidence(ok, ctx('evidence'));
  assert.ok(html.includes('双料王牌 · 前瞻周期监控') && html.includes('样本不足') && html.includes('贵阳银行') && html.includes('2/60'));
  clean(html, 'evidence/doublePass');
  const col = { ...live, doublePass: { status: 'collecting', matured: { h1: 0, h5: 0 }, official_days: 1, days: [] } };
  assert.ok(VIEWS.evidence(col, ctx('evidence')).includes('尚无已结算信号日'));
  const miss = { ...live };
  delete miss.doublePass;
  assert.ok(VIEWS.evidence(miss, ctx('evidence')).includes(SOURCES.doublePass.label));
});

test('每日滚动分组：样本不足不下结论、中期快照、判定、收集中、缺失', () => {
  const b = (n, ex, t) => ({ cohorts: n, mean_net: 0.012, mean_excess: ex, hit_excess: 0.55, nw_t_excess: t, nw_lag: 4 });
  const st = (n) => ({ cohort_top20: b(n, 0.006, 1.4), cohort_top100: b(n, 0.004, 1.2), cohort_avoid70: b(n, 0.001, 0.9), bench_ew: { cohorts: n, mean_net: 0.008, mean_excess: null, hit_excess: null, nw_t_excess: null } });
  const win = (dd, e1, e5) => ({ signal_date: dd, entry: e1, exit: e5, window: `${dd} → ${e1}～${e5}`, cohort_top20: 0.02, cohort_top100: 0.015, cohort_avoid70: 0.01, bench_ew: 0.009 });
  const mk = (n, verdict) => ({ ...live, cohortMonitor: { contract_version: 'cohort_monitor_v1', status: 'ok', as_of: '20261028', official_cohorts: n + 2, matured: { h1: n + 1, h5: n },
    thresholds: { verdict_cohorts: 120, interim_cohorts: 60, nw_t: 2 }, stats: { h1: st(n + 1), h5: st(n) }, verdict,
    regime_h5: { up: { cohorts: 1, mean_excess: 0.003 }, down: { cohorts: 1, mean_excess: 0.008 } },
    rolling20_h5: [], sleeve_curve: [{ date: '20261027', sleeve_nav: 1.003, bench_nav: 1.002, excess: 0.001 }, { date: '20261028', sleeve_nav: 1.006, bench_nav: 1.003, excess: 0.003 }],
    blocked: { entry_limit_up: 1, exit_suspended: 0, exit_limit_down: 0 }, impact_stress: { mean_net_after_impact: { 1000000: 0.011, 5000000: 0.009 } },
    windows: [win('20261020', '20261021', '20261027'), win('20261021', '20261022', '20261028')], pending: [{ signal_date: '20261027', window_start: '20261028' }],
    spec: { caveat: '相邻组收益高度相关' } } });
  const early = mk(2, { status: 'insufficient_sample', cohorts: 2, required: 120, message: '未达预注册样本量，不下结论' });
  assert.equal(cohortModel(early).stage, 'insufficient_sample');
  const html = VIEWS.evidence(early, ctx('evidence'));
  assert.ok(html.includes('每日滚动分组 · 前瞻监控') && html.includes('2/120') && html.includes('不下结论') && html.includes('样本不足 · 不下结论'));
  assert.ok(html.includes('2026-10-21 → 2026-10-22～2026-10-28') && html.includes('2026-10-27 → 2026-10-28 起'), 'D → E1～E5 窗口与持有中');
  assert.ok(html.includes('5 份资金滚动净值') && html.includes('涨停开盘未买入 1') && html.includes('相邻组收益高度相关'));
  assert.ok(html.indexOf('2026-10-21 → 2026-10-22') < html.indexOf('2026-10-20 → 2026-10-21'), '最近窗口倒序');
  clean(html, 'evidence/cohort');
  const mid = mk(60, { status: 'interim_snapshot', cohorts: 60, required: 120, message: '中期快照' });
  assert.equal(cohortModel(mid).stage, 'interim_snapshot');
  assert.ok(VIEWS.evidence(mid, ctx('evidence')).includes('中期快照 · 只报告不判定'));
  // 合同若在 120 组前误给出 pass，前端仍按样本不足展示
  assert.equal(cohortModel(mk(10, { status: 'pass', cohorts: 10, required: 120 })).stage, 'insufficient_sample');
  const done = mk(120, { status: 'pass', cohorts: 120, required: 120, message: '按预注册口径：达标' });
  assert.ok(VIEWS.evidence(done, ctx('evidence')).includes('预注册判定：达标'));
  const col = { ...live, cohortMonitor: { contract_version: 'cohort_monitor_v1', status: 'collecting', message: '台账尚无官方分组', official_cohorts: 0, matured: { h1: 0, h5: 0 }, windows: [], pending: [] } };
  const h2 = VIEWS.evidence(col, ctx('evidence'));
  assert.ok(h2.includes('0/120') && h2.includes('不下结论') && h2.includes('尚无已结算 T+5 窗口') && h2.includes('尚无到期分组'));
  clean(h2, 'evidence/cohort-collecting');
  const un = { ...live, cohortMonitor: { status: 'unavailable', reason: 'cohort_monitor.json 缺失或版本不符' } };
  assert.ok(VIEWS.evidence(un, ctx('evidence')).includes('监控合同尚未生成'));
  const miss = { ...live };
  delete miss.cohortMonitor;
  assert.ok(VIEWS.evidence(miss, ctx('evidence')).includes(SOURCES.cohortMonitor.label));
  assert.ok(ROUTE_DEPS.evidence.includes('cohortMonitor'));
});

test('因子生命周期看板：分环境基线（上涨 / 下跌）', () => {
  const regime = { up: { n: 30, roll_n: 14, mean: 0.021, roll_mean: 0.024, baseline: 0.0191, baseline_days: 682 },
    down: { n: 10, roll_n: 6, mean: 0.15, roll_mean: 0.162, baseline: 0.1593, baseline_days: 613 } };
  const h5 = { n: 40, mean: 0.06, roll_mean: 0.065, gap_z: -0.4, baseline_mode: 'regime', baseline_expected: 0.0612, baseline: { validation: 0.081 }, regime, flags: [] };
  const pooled = { n: 40, mean: 0.03, roll_mean: 0.03, gap_z: -1.1, baseline_mode: 'pooled', baseline: { research: 0.05 },
    regime: { up: { n: 30, roll_n: 14, mean: 0.02, roll_mean: 0.02, baseline: null }, down: { n: 10, roll_n: 6, mean: 0.05, roll_mean: 0.06, baseline: null } }, flags: [] };
  const lc = { status: 'ok', as_of: '20261201', signal_days: 45, horizons: [5], matured: { h5: 40 }, roll_window: 20,
    regime_days: { h5: { up: 30, down: 10 } }, regime_baseline: { used: true, horizons: [5], window: '20210225–20260630' },
    signals: { comp_rank_ew6: '综合分 comp_rank_ew6', ivol_60: '特质波动率 ivol_60↓' },
    results: { comp_rank_ew6: { by_h: { h5 } }, ivol_60: { by_h: { h5: pooled } } }, alerts: [], progress: {} };
  const html = VIEWS.evidence({ ...live, factorLifecycle: lc }, ctx('evidence'));
  assert.ok(html.includes('上涨环境 IC') && html.includes('下跌环境 IC'), '两环境列');
  assert.ok(html.includes('+0.024') && html.includes('14 日') && html.includes('+0.162') && html.includes('6 日'), '滚动环境 IC 与日数');
  assert.ok(html.includes('涨 <span class="n up">+0.019</span> / 跌 <span class="n up">+0.159</span>'), '分环境基线');
  assert.ok(html.includes('匹配环境后的基线均值 0.061') && html.includes('上涨 30 日 / 下跌 10 日'), '匹配基线与环境日数');
  assert.ok(html.includes('h5 基线按每日环境分别取上涨 / 下跌回测基线'));
  assert.ok(html.includes('<span class="n up">+0.050</span>'), '无分环境基线的分项退回混合基线');
  clean(html, 'evidence/lifecycle-regime');
  const off = { ...lc, regime_baseline: { used: false, reason: '文件缺失' }, results: { comp_rank_ew6: { by_h: { h5: { ...h5, baseline_mode: 'pooled' } } } } };
  const h2 = VIEWS.evidence({ ...live, factorLifecycle: off }, ctx('evidence'));
  assert.ok(h2.includes('分环境基线暂不可用') && h2.includes('+0.081'));
  clean(h2, 'evidence/lifecycle-regime-off');
});

test('因子生命周期看板：有数据、收集中、缺失', () => {
  const by = (mean, extra = {}) => ({ n: 25, mean, hit: 0.6, nw_t: 2.1, roll_mean: mean, gap_z: -2.4, baseline: { validation: 0.081 }, flags: [], ...extra });
  const lc = { status: 'ok', as_of: '20261120', signal_days: 25, horizons: [1, 5, 10, 20], matured: { h1: 25, h5: 20, h10: 15, h20: 5 }, roll_window: 20,
    signals: { comp_rank_ew6: '综合分 comp_rank_ew6', ivol_60: '特质波动率 ivol_60↓' },
    results: { comp_rank_ew6: { by_h: { h1: by(0.01), h5: by(0.02, { flags: ['ic_below_backtest'] }), h10: by(0.03), h20: by(0.04) } }, ivol_60: { by_h: {} } },
    alerts: [{ signal: 'comp_rank_ew6', h: 5, flag: 'ic_below_backtest' }], progress: { books_matured_periods: 3, books_required_periods: 50, tier_h5_days: 12, tier_required_days: 60 } };
  lc.style = { status: 'ok', styles: { size: '市值', value: '价值' }, note: '恒等分解', baseline_corr_note: 'Phase3 发现期：ivol_60 与 turn_20 秩相关 +0.69',
    redundancy: { days: 5, n_eff: 3.7, n_factors: 6, high_pairs: [{ a: 'ivol_60', b: 'turn_20', rho: 0.83 }] },
    results: { h5: { DOUBLE: { label: '双料王牌', days: 10, excess: 0.012, alpha: 0.004, industry: 0.001, style: { size: 0.005, value: 0.002 }, exposure: { size: 1.1, value: 0.4 }, top_industry: '银行', top_industry_share: 0.6 } } } };
  const ok = { ...live, factorLifecycle: lc };
  assert.equal(lifecycleModel(ok).status, 'ok');
  const html = VIEWS.evidence(ok, ctx('evidence'));
  assert.ok(html.includes('因子生命周期看板') && html.includes('低于回测基线') && html.includes('1 条预警') && html.includes('3/50') && html.includes('12/60'));
  assert.ok(html.includes('3.7 / 6') && html.includes('选股 alpha') && html.includes('银行'), '风格归因与冗余');
  clean(html, 'evidence/lifecycle');
  const col = { ...live, factorLifecycle: { status: 'collecting', signal_days: 1, horizons: [1, 5], matured: { h1: 0, h5: 0 }, signals: { comp_rank_ew6: '综合分' }, results: {}, alerts: [] } };
  assert.ok(VIEWS.evidence(col, ctx('evidence')).includes('无预警'));
  const miss = { ...live };
  delete miss.factorLifecycle;
  assert.ok(!VIEWS.evidence(col, ctx('evidence')).includes('涨 <span'), '收集中无分环境基线');

  assert.ok(VIEWS.evidence(miss, ctx('evidence')).includes(SOURCES.factorLifecycle.label));
});

test('发布前验证未全部通过：顶部如实标注，通过时无横幅', () => {
  const partial = { systemVerdict: { publish_validation: { status: 'partial', issues: ['ai_incomplete', 'not_publish_ready'] } } };
  const html = validationBanner(partial);
  assert.ok(html.includes('验证未全部通过') && html.includes('AI 个股分析未全部生成'));
  assert.ok(!html.includes('ai_incomplete'), '不直出内部字段名');
  assert.equal(validationBanner({ systemVerdict: { publish_validation: { status: 'passed', issues: [] } } }), '');
  assert.equal(validationBanner({ systemVerdict: {} }), '');
});

process.on('exit', () => console.log(`\n${passed} passed${process.exitCode ? ', FAILURES above' : ''}`));
