// tests/v5-render.test.mjs — v5 前端渲染回归（纯 Node，零依赖）。运行：node tests/v5-render.test.mjs
// 夹具：
//   preview/demo-20261008/       —— 20261008 已公开发布快照（bd241cdc），Top-20 + 逐股 AI 全量；
//   tests/fixtures/v5-maintenance —— 20261009 维护模式快照（个股名单撤下）。
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

import { SOURCES, ROUTE_DEPS, ROUTES, top20Model, reviewSeries, maintenanceOf, createLoader, normalizeStock, ageDays } from '../assets/scripts/v5/data.js';
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

test('演示快照：因子矩阵可排序表头 + 20 行', () => {
  const html = VIEWS.candidates(demo, ctx('candidates'));
  assert.equal(count(html, '<tr data-stock='), 20);
  assert.ok(html.includes('data-sort="score"') && html.includes('量稳定性'));
  clean(html, 'candidates/demo');
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
  for (const [label, d] of [['demo', demo], ['maint', maint], ['degraded', degraded]]) {
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

process.on('exit', () => console.log(`\n${passed} passed${process.exitCode ? ', FAILURES above' : ''}`));
