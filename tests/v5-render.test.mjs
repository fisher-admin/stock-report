// tests/v5-render.test.mjs — v5 前端渲染回归（纯 Node，零依赖）。运行：node tests/v5-render.test.mjs
// 夹具：
//   preview/demo-20261008/       —— 20261008 已公开发布快照（bd241cdc），Top-20 + 逐股 AI 全量；
//   tests/fixtures/v5-maintenance —— 20261009 维护模式快照（个股名单撤下）。
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

import { SOURCES, ROUTE_DEPS, ROUTES, top20Model, reviewSeries, maintenanceOf, createLoader, normalizeStock, ageDays, challengerModel, arenaModel } from '../assets/scripts/v5/data.js';
import { VIEWS } from '../assets/scripts/v5/views.js';
import { verdictStrip, maintenanceBanner, topbar } from '../assets/scripts/v5/shell.js';
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
  assert.ok(html.includes('v4.4 挑战者策略') && html.includes('前沿候选因子试验区'));
  assert.ok(html.includes('共同入选') && html.includes('新策略独享') && html.includes('被新策略否决'));
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
  assert.ok(a.includes('v4.4 挑战者画像') && a.includes('共同入选') && a.includes('风险提示'));
  const b = drawer(only, { index: 1, total: 20 });
  assert.ok(b.includes('新策略独享') && b.includes('未触发 AI 分析') && !b.includes('风险提示'));
  for (const s of m.stocks) clean(drawer(s, { index: 0, total: 20 }), `chal-drawer/${s.code}`);
});

test('冠军页不受挑战者影响；挑战者缺失时竞技场降级', () => {
  const today = VIEWS.today(live, ctx('today'));
  assert.equal(count(today, 'class="card"'), 20);
  assert.ok(!today.includes('挑战者画像') && !today.includes('新策略独享'));
  const noChal = VIEWS.candidates(demo, ctx('candidates'));
  assert.ok(noChal.includes('挑战者数据暂不可用'));
  clean(noChal, 'candidates/demo');
  const unavailable = { ...live, v44Challenger: { status: 'unavailable', reason: '尚未生成', top20: [], delta: null } };
  assert.ok(VIEWS.candidates(unavailable, ctx('candidates')).includes('v4.4 挑战者名单尚未生成'));
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
  assert.ok(html.includes('双轨竞技场') && html.includes('历史回放 · 回测'));
  assert.equal(count(html, 'class="ln-a"'), 2, '基准 + 压力两张图');
  assert.equal(count(html, 'class="ln-b"'), 2);
  assert.equal(count(html, 'class="hot"'), 2 * m.curve.length, '每日一个悬停热区');
  assert.ok(html.includes('绩效记分卡') && html.includes('年化摩擦拖累') && html.includes('最大回撤'));
  const t = live.arenaLedger.significance.daily_diff_t;
  assert.ok(html.includes(Math.abs(t) >= 2 ? '>显著<' : '>不显著<'), '显著性徽章与 t 一致');
  assert.ok(html.includes('否决门') && html.includes('规避价差'));
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

process.on('exit', () => console.log(`\n${passed} passed${process.exitCode ? ', FAILURES above' : ''}`));
