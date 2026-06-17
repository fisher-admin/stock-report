// tests/render.test.mjs — 前端 v3 渲染回归测试（纯 Node，零第三方依赖）。
// 运行：node tests/render.test.mjs
//
// 数据：tests/fixtures/*.json 为 2026-06-12/13 从 data/latest 与 data/recommendation_analytics
// 复制的真实快照（review_track_latest.json 裁剪 stock_rows 至最近 100 条）。
// 这份快照的特征即真实世界的"难看"情形，测试以此为准：
//   - run_manifest.trade_date=20260611（对 2026-06-13 而言已过期一个交易日）；
//   - candidate_state 20 只全为 role_type=avoid 且 ai_* 字段全 null；
//   - execution_state main=0 / watch=60；
//   - review_state.performance 的命中率字段为 0/null，但 date_stats 有 62 天真实逐日数据；
//   - T1 推荐 20 只的综合因子分全部为 0（数据异常场景）。
//
// 诚实性断言说明（DESIGN-V3.md 第 0 节）：
//   - 「等待放量确认」「跌破关键支撑或量价结构转弱」「暂无明确外部催化」是 v2 模板兜底话术，
//     已确认不存在于任何 fixtures 数据 → 全量渲染输出中出现即为渲染器编造，全站禁止；
//   - 「4.44」「-1.74%」「60%」是 v2 写死的业绩数字。真实数据可能巧合产生同样字样
//     （如 date_stats 中确有 60.0% 的命中率），因此分两层断言：
//       a) 降级模式（无任何可选数据）下全站严禁出现——此时任何业绩数字都只能是硬编码；
//       b) 全量数据下按视图断言：已核实当前 fixtures 在 candidates/dashboard/review
//          视图中不会从数据合法产生这些字样（review 页 60% 可由真实命中率产生，不禁）。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

import { RENDERERS } from '../assets/scripts/v2/render/views.js';
import { buildModel, computeStaleness, getSessionMode } from '../assets/scripts/v2/data/model.js';
import { SOURCES, VIEW_DEPS } from '../assets/scripts/v2/data/manifest.js';
import { escapeHtml, pctHtml, dateCn } from '../assets/scripts/v2/render/format.js';

// ---------------------------------------------------------------------------
// 基础设施
// ---------------------------------------------------------------------------

const FIXTURE_DIR = join(dirname(fileURLToPath(import.meta.url)), 'fixtures');
const fixture = (name) => JSON.parse(readFileSync(join(FIXTURE_DIR, name), 'utf-8'));
const clone = (value) => JSON.parse(JSON.stringify(value));

// SOURCES key → fixture 文件名（即 path 的 basename；reviewUnified → review_track_latest.json）。
const fullData = {};
for (const [key, spec] of Object.entries(SOURCES)) {
  fullData[key] = fixture(spec.path.split('/').pop());
}

// 固定"当前时刻"，保证测试与运行日期无关：
//   NOW_FRESH  = 2026-06-12（周五）12:00 上海 —— 数据日 20260611 视为新鲜（隔日属正常）；
//   NOW_SAT    = 2026-06-13（周六）12:00 上海 —— 周五整天数据缺失 → 过期 1 个交易日；
//   NOW_MON    = 2026-06-15（周一）12:00 上海 —— 交易日当天，旧数据 → "今日数据尚未生成"。
const NOW_FRESH = Date.UTC(2026, 5, 12, 4, 0, 0);
const NOW_SAT = Date.UTC(2026, 5, 13, 4, 0, 0);
const NOW_MON = Date.UTC(2026, 5, 15, 4, 0, 0);

let failures = 0;
let passes = 0;
function check(label, fn) {
  try {
    fn();
    passes += 1;
    console.log(`  ok - ${label}`);
  } catch (error) {
    failures += 1;
    console.error(`  FAIL - ${label}\n    ${error.message}`);
  }
}

const BAD_MARKERS = ['undefined', 'NaN', '[object Object]', '<script', '分析过程出错'];
function assertCleanHtml(html, viewKey) {
  assert.equal(typeof html, 'string', `${viewKey}: 渲染结果应为字符串`);
  assert.ok(html.length > 2000, `${viewKey}: HTML 过短（${html.length}）`);
  for (const marker of BAD_MARKERS) {
    assert.ok(!html.includes(marker), `${viewKey}: HTML 含有坏标记 "${marker}"`);
  }
}

// v2 模板兜底话术（全站禁止）与 v2 硬编码业绩数字（按上述两层规则禁止）。
// 数字用带边界的正则：独立出现的「4.44」「-1.74%」「60%」命中；
// 数据合法产生的「34.44」「-0.60%」「4.448」等小数/长数字片段不误伤。
const TEMPLATE_PHRASES = ['等待放量确认', '跌破关键支撑或量价结构转弱', '暂无明确外部催化'];
const HARDCODED_NUMBERS = [
  ['4.44', /(?<![\d.])4\.44(?!\d)/],
  ['-1.74%', /(?<![\d.])-1\.74%/],
  ['60%', /(?<![\d.])60%/]
];
function findHardcodedNumber(html) {
  const hit = HARDCODED_NUMBERS.find(([, re]) => re.test(html));
  return hit ? hit[0] : null;
}

// 从 statCard 输出里提取指定标题的数值文本（剥掉内部 span 标签）。
function extractStatValue(html, title) {
  const re = new RegExp(
    `<div class="stat-title">${title}</div>\\s*<div class="stat-value num[^"]*">([\\s\\S]*?)</div>`
  );
  const match = html.match(re);
  return match ? match[1].replace(/<[^>]+>/g, '').trim() : null;
}

// ---------------------------------------------------------------------------
// 1. 全量真实数据渲染全部 9 个 data-view
// ---------------------------------------------------------------------------

console.log('# 1. 全量真实数据渲染全部 10 个 data-view');
const model = buildModel(fullData, [], NOW_FRESH);
const tradeDateCn = dateCn(fullData.runManifest.trade_date); // 当前 fixtures 为 2026-06-11
const VIEW_KEYS = Object.keys(RENDERERS);

check('注册表恰好包含 10 个视图', () => {
  assert.equal(VIEW_KEYS.length, 10, `视图数应为 10，实际 ${VIEW_KEYS.length}`);
});

for (const [viewKey, render] of Object.entries(RENDERERS)) {
  check(`${viewKey}: 渲染干净、含交易日与站点骨架`, () => {
    const html = render(model);
    assertCleanHtml(html, viewKey);
    assert.ok(html.includes(tradeDateCn), `缺少交易日 ${tradeDateCn}`);
    assert.ok(html.includes('A股智能选股系统'), '缺少站点品牌');
    assert.ok(html.includes('不构成任何投资建议'), '缺少页脚免责声明');
    assert.ok(!html.includes('section-missing'), '全量数据下不应出现缺失占位');
    for (const phrase of TEMPLATE_PHRASES) {
      assert.ok(!html.includes(phrase), `出现模板话术 "${phrase}"（数据中并不存在，必为渲染器编造）`);
    }
  });
}

// ---------------------------------------------------------------------------
// 2. 诚实性断言（AI 全空 / 硬编码业绩数字 / T1 全零异常）
// ---------------------------------------------------------------------------

console.log('# 2. 诚实性断言');

check('candidates: AI 全空时必须显性标注「无 AI 分析」', () => {
  const html = RENDERERS.candidates(model);
  assert.ok(html.includes('无 AI 分析'), '缺少「无 AI 分析」徽章/说明');
  // 20 只候选全部 ai_* 为 null → 还应有整组覆盖度说明。
  assert.ok(html.includes('均无 AI 个股分析'), '缺少整组 AI 覆盖度的如实说明');
});

check('candidates: 禁止 v2 硬编码业绩数字（4.44 / -1.74% / 60%）', () => {
  const html = RENDERERS.candidates(model);
  const found = findHardcodedNumber(html);
  assert.equal(found, null, `出现疑似硬编码数字 "${found}"（已核实 fixtures 数据不会合法产生）`);
});

check('dashboard: 禁止 v2 硬编码业绩数字（4.44 / -1.74%）', () => {
  const html = RENDERERS.dashboard(model);
  assert.ok(!HARDCODED_NUMBERS[0][1].test(html), '出现疑似硬编码数字 "4.44"');
  assert.ok(!HARDCODED_NUMBERS[1][1].test(html), '出现疑似硬编码数字 "-1.74%"');
});

check('review: 禁止 v2 硬编码业绩数字（4.44 / -1.74%）', () => {
  const html = RENDERERS.review(model);
  assert.ok(!HARDCODED_NUMBERS[0][1].test(html), '出现疑似硬编码数字 "4.44"');
  assert.ok(!HARDCODED_NUMBERS[1][1].test(html), '出现疑似硬编码数字 "-1.74%"');
});

check('candidates: T1 因子分全为 0 → 整组判异常，不照常列卡片', () => {
  const rows = fullData.t1FactorRecommendations.rows || [];
  assert.ok(rows.length > 0 && rows.every((row) => Number(row.score) === 0), '前置条件：fixtures T1 分数应全为 0');
  const html = RENDERERS.candidates(model);
  assert.ok(html.includes('因子数据异常') && html.includes('名单不可用'), '缺少 T1 数据异常的显性说明');
  assert.ok(html.includes('全部为 0'), '缺少"因子分全部为 0"的事实陈述');
});

check('candidates: T1 回测字段全 0/null → 显示"暂无可验证"而非编造数字', () => {
  const bt = (fullData.researchStateT1 || {}).backtest_summary || {};
  const allDegenerate = Object.values(bt).every((value) => value === null || Number(value) === 0);
  assert.ok(allDegenerate, '前置条件：fixtures T1 回测字段应全为 0/null');
  const html = RENDERERS.candidates(model);
  assert.ok(html.includes('暂无可验证的历史回测数据'), 'T1 回测退化数据应如实说明，禁止兜底数字');
});

check('candidates: T1 研究预览标注存在', () => {
  assert.equal(fullData.researchStateT1.status, 'research_preview', '前置条件：T1 应为研究预览状态');
  const html = RENDERERS.candidates(model);
  assert.ok(html.includes('研究预览'), 'T1 应标注研究预览');
});

check('dashboard: 执行层 main=0 时如实显示"今日无主攻标的"', () => {
  assert.equal(Number(fullData.executionState.main_count), 0, '前置条件：执行清单应无主攻');
  const html = RENDERERS.dashboard(model);
  assert.ok(html.includes('今日无主攻标的'), '主攻为空必须显性说明');
});

check('candidates: 60/60 全分歧时显示口径说明而非误导名单', () => {
  const exec = fullData.executionState;
  assert.ok((exec.divergence_stocks || []).length >= (exec.executions || []).length, '前置条件：应为全分歧');
  const html = RENDERERS.candidates(model);
  assert.ok(html.includes('全部只被单一策略选中'), '全分歧应转为口径说明');
});

// ---------------------------------------------------------------------------
// 2b. 情绪因子页（sentiment）——真实分布、口径说明、空态/缺失占位
// ---------------------------------------------------------------------------

console.log('# 2b. 情绪因子页（sentiment）');

check('sentiment: 全量数据渲染干净、含口径说明、含真实分布', () => {
  const html = RENDERERS.sentiment(model);
  assertCleanHtml(html, 'sentiment');
  // 口径必须显性：是「推荐个股 AI 观点」聚合，不是全市场情绪。
  assert.ok(html.includes('非全市场情绪'), '缺少「非全市场情绪」口径说明');
  assert.ok(html.includes('数据口径与局限'), '缺少口径与局限区块');
  // 四桶标签都应出现。
  for (const bucket of ['看多', '中性偏多', '中性', '看空']) {
    assert.ok(html.includes(bucket), `缺少情绪桶「${bucket}」`);
  }
  // 真实占比来自 fixture（看多 7.5% / 中性偏多 71.4%），不是写死的 50/0。
  const dist = fullData.sentimentState.distribution || {};
  const bullishPct = `${(Number(dist['看多'].ratio) * 100).toFixed(1)}%`;
  const holdPct = `${(Number(dist['中性偏多'].ratio) * 100).toFixed(1)}%`;
  assert.ok(html.includes(bullishPct), `缺少看多真实占比 ${bullishPct}`);
  assert.ok(html.includes(holdPct), `缺少中性偏多真实占比 ${holdPct}`);
  // 情绪温度盘用真实 avg_ai_score（gauge 渲染为整数刻度）。
  assert.ok(html.includes('情绪温度'), '缺少情绪温度区块');
  assert.ok(html.includes('情绪趋势'), '缺少情绪趋势区块');
});

check('sentiment: 不出现编造的 50/0 占位且无 NaN/undefined', () => {
  const html = RENDERERS.sentiment(model);
  // legacy 静态页写死过 49.12 / 71.64% / 10.71% 等假数字，新页严禁出现。
  assert.ok(!html.includes('49.12'), '出现 legacy 写死的情绪分 49.12');
  assert.ok(!html.includes('71.64%'), '出现 legacy 写死的持有比例 71.64%');
  assert.ok(!html.includes('10.71%'), '出现 legacy 写死的买入比例 10.71%');
});

check('sentiment: 文件缺失 → missingSection 占位（不编数字）', () => {
  const missing = [{ key: 'sentimentState', label: SOURCES.sentimentState.label, reason: 'HTTP 404' }];
  const missingModel = buildModel({ runManifest: fullData.runManifest, systemVerdict: fullData.systemVerdict }, missing, NOW_FRESH);
  const html = RENDERERS.sentiment(missingModel);
  assertCleanHtml(html, 'sentiment(missing)');
  assert.ok(html.includes('section-missing'), '缺失时应出现 missingSection 占位');
  // 缺失态严禁出现任何具体情绪占比数字。
  assert.ok(!/\d+\.\d%/.test(html.replace(/var\([^)]*\)/g, '')), '缺失态不应出现具体百分比数字');
});

check('sentiment: 样本为 0 → emptySection 解释（文件在但无可归类样本）', () => {
  const zeroData = clone(fullData);
  zeroData.sentimentState = {
    generated_at: '2026-06-15 15:39:40', trade_date: '20260611',
    source: '基于近 N 个交易日 AI 对推荐个股的观点聚合，非全市场情绪',
    window_days: 0, sample_count: 0,
    distribution: { 看多: { count: 0, ratio: 0 }, 中性偏多: { count: 0, ratio: 0 }, 中性: { count: 0, ratio: 0 }, 看空: { count: 0, ratio: 0 } },
    avg_ai_score: null, daily_series: []
  };
  const zeroModel = buildModel(zeroData, [], NOW_FRESH);
  const html = RENDERERS.sentiment(zeroModel);
  assertCleanHtml(html, 'sentiment(empty)');
  assert.ok(html.includes('section-empty'), '样本为 0 时应出现 emptySection 解释');
  assert.ok(!html.includes('section-missing'), '文件在场时不应出现 missingSection');
});

check('sentiment: AI 观点注入 <script> 经数据进入也不破坏（口径文案恒定）', () => {
  // 渲染层只读聚合后的数值字段，无自由文本注入面，验证转义纪律下输出仍干净。
  const html = RENDERERS.sentiment(model);
  assert.ok(!html.includes('<script>alert'), 'sentiment 输出不应含可执行脚本');
});

// ---------------------------------------------------------------------------
// 3. 战绩页数字正确性（用 fixtures daily_comparison 在测试内独立手算）
// ---------------------------------------------------------------------------

console.log('# 3. 战绩页数字正确性（独立手算对照渲染输出）');

function handComputeNav(dailyComparison) {
  const rows = (dailyComparison || [])
    .filter((row) => row && /^\d{8}$/.test(String(row.recommend_date || '').replace(/-/g, '')))
    .slice()
    .sort((a, b) => (String(a.recommend_date) < String(b.recommend_date) ? -1 : 1));
  let nav = 1;
  let peak = 1;
  let maxDrawdown = 0;
  let validDays = 0;
  for (const row of rows) {
    const raw = row.avg_next_day_return_pct;
    if (raw === null || raw === undefined || raw === '') continue;
    const ret = Number(raw);
    if (!Number.isFinite(ret)) continue;
    nav *= 1 + ret / 100;
    validDays += 1;
    if (nav > peak) peak = nav;
    const dd = (peak - nav) / peak;
    if (dd > maxDrawdown) maxDrawdown = dd;
  }
  return { finalNav: nav, cumulativePct: (nav - 1) * 100, maxDrawdownPct: maxDrawdown * 100, validDays };
}

const expected = handComputeNav(fullData.reviewUnified.daily_comparison);
const reviewHtml = RENDERERS.review(model);

check('review: 手算净值有真实样本（62 天）且累计为负', () => {
  assert.equal(expected.validDays, 62, `fixtures 应有 62 个可结算交易日，实际 ${expected.validDays}`);
  assert.ok(expected.cumulativePct < 0, '当前 fixtures 的累计收益应为负数（照实呈现的前提）');
});

check('review: 累计净值收益与手算一致（容差 0.1）', () => {
  const text = extractStatValue(reviewHtml, '累计净值收益');
  assert.ok(text, '未找到「累计净值收益」统计卡');
  const rendered = Number.parseFloat(text);
  assert.ok(Number.isFinite(rendered), `渲染值不可解析："${text}"`);
  assert.ok(Math.abs(rendered - expected.cumulativePct) <= 0.1,
    `累计收益偏差过大：渲染 ${rendered} vs 手算 ${expected.cumulativePct.toFixed(4)}`);
  assert.ok(rendered < 0, '负收益必须照实显示为负数');
});

check('review: 负累计收益用 down 色类呈现（红涨绿跌）', () => {
  const re = /累计净值收益<\/div>\s*<div class="stat-value num[^"]*">([\s\S]*?)<\/div>/;
  const match = reviewHtml.match(re);
  assert.ok(match, '未找到累计净值收益取值块');
  assert.ok(match[1].includes('pct-down'), '负收益应带 pct-down 类');
});

check('review: 期末净值与手算一致（容差 0.1）', () => {
  const match = reviewHtml.match(/期末净值 ([\d.]+)/);
  assert.ok(match, '未找到「期末净值」说明');
  const rendered = Number.parseFloat(match[1]);
  assert.ok(Math.abs(rendered - expected.finalNav) <= 0.1,
    `期末净值偏差过大：渲染 ${rendered} vs 手算 ${expected.finalNav.toFixed(4)}`);
});

check('review: 最大回撤与手算一致（容差 0.1）', () => {
  const text = extractStatValue(reviewHtml, '最大回撤');
  assert.ok(text, '未找到「最大回撤」统计卡');
  const rendered = Number.parseFloat(text);
  assert.ok(Number.isFinite(rendered), `渲染值不可解析："${text}"`);
  assert.ok(Math.abs(rendered - expected.maxDrawdownPct) <= 0.1,
    `最大回撤偏差过大：渲染 ${rendered} vs 手算 ${expected.maxDrawdownPct.toFixed(4)}`);
});

check('review: 覆盖交易日与手算一致', () => {
  const text = extractStatValue(reviewHtml, '覆盖交易日');
  assert.ok(text && text.includes(String(expected.validDays)), `覆盖交易日应为 ${expected.validDays}，实际 "${text}"`);
});

// 层一：进阶绩效指标区 + 分层归因 + 回测数据导出
check('review: 进阶绩效指标区出现，关键指标卡齐全且夏普为有限数', () => {
  assert.ok(reviewHtml.includes('进阶绩效指标'), '缺「进阶绩效指标」区');
  for (const label of ['年化收益', '年化波动', '夏普比率', '卡玛比率', '日胜率', '盈亏比', '最大连亏']) {
    assert.ok(reviewHtml.includes(label), `缺绩效指标卡：${label}`);
  }
  const sharpe = Number.parseFloat(extractStatValue(reviewHtml, '夏普比率') || '');
  assert.ok(Number.isFinite(sharpe), `夏普比率渲染值不可解析（应为有限数）`);
});

check('review: 分层归因三表出现（行业 / AI 观点 / 评分段）', () => {
  assert.ok(reviewHtml.includes('分层归因'), '缺「分层归因」区');
  assert.ok(reviewHtml.includes('按行业'), '缺按行业分层表');
  assert.ok(reviewHtml.includes('按 AI 观点'), '缺按 AI 观点分层表');
  assert.ok(reviewHtml.includes('按量化评分段'), '缺按评分段分层表');
});

check('review: 提供可回测的 CSV 明细与策略评价小结下载入口', () => {
  assert.ok(reviewHtml.includes('recommendation_history.csv'), '缺历史荐股 CSV 下载链接');
  assert.ok(reviewHtml.includes('strategy_evaluation.json'), '缺策略评价小结下载链接');
});

check('review: 重复推荐榜带累计收益列，且亏损照实列出', () => {
  const repeats = ((fullData.reviewUnified.strategies || {}).prebreakout_v41 || {}).top_repeat_recommendations || [];
  const top = repeats[0];
  assert.ok(top && Number(top.avg_cumulative_return_pct) < 0, '前置条件：榜首应为负累计收益');
  assert.ok(reviewHtml.includes(escapeHtml(top.stock_name)), `缺少榜首 ${top.stock_name}`);
  const expectedPct = `${Number(top.avg_cumulative_return_pct).toFixed(2)}%`;
  assert.ok(reviewHtml.includes(expectedPct), `缺少榜首累计收益 ${expectedPct}（不许只报次数、隐藏亏损）`);
});

check('review: 全部推荐记录含最新一天的样本股票与口径说明', () => {
  const sample = (fullData.reviewUnified.stock_rows || [])[0];
  assert.ok(sample, '前置条件：stock_rows 不应为空');
  assert.ok(reviewHtml.includes(escapeHtml(sample.stock_name)), `缺少样本 ${sample.stock_name}`);
  assert.ok(reviewHtml.includes('不含交易成本'), '缺少统计口径说明');
  assert.ok(reviewHtml.includes('review_state_unified.json'), '缺少原始数据下载链接');
});

check('research: 策略中心历史表现与 date_stats 手算累计一致（不用 performance 占位字段）', () => {
  const perf = fullData.reviewState.performance || {};
  assert.ok(perf.next_day_hit_rate_pct === 0 || perf.next_day_hit_rate_pct === null,
    '前置条件：performance 命中率应为 0/null 占位');
  const handResearch = handComputeNav(fullData.reviewState.date_stats);
  const html = RENDERERS.research(model);
  const expectedText = `${handResearch.cumulativePct.toFixed(2)}%`;
  assert.ok(html.includes(expectedText), `策略中心应展示 date_stats 手算累计收益 ${expectedText}`);
});

// ---------------------------------------------------------------------------
// 4. 数据过期横幅（buildModel 注入 nowMs）
// ---------------------------------------------------------------------------

console.log('# 4. 数据过期横幅（staleness）');

check('computeStaleness: 周五看周四数据 → 不过期；周六/周一 → 过期', () => {
  const fresh = computeStaleness('20260611', new Date(NOW_FRESH));
  assert.equal(fresh.isStale, false, '隔日数据在周五不应判过期');
  const sat = computeStaleness('20260611', new Date(NOW_SAT));
  assert.equal(sat.isStale, true, '周六应判过期（周五数据缺失）');
  assert.equal(sat.daysLate, 1);
  assert.ok(sat.label.includes('最近 1 个交易日的数据尚未生成'), `周末文案不符：${sat.label}`);
  const mon = computeStaleness('20260611', new Date(NOW_MON));
  assert.equal(mon.isStale, true);
  assert.ok(mon.label.includes('今日数据尚未生成'), `交易日文案不符：${mon.label}`);
});

check('过期横幅：周六注入 nowMs 后全站渲染出黄色横幅与文案', () => {
  const staleModel = buildModel(fullData, [], NOW_SAT);
  assert.equal(staleModel.staleness.isStale, true);
  for (const [viewKey, render] of Object.entries(RENDERERS)) {
    const html = render(staleModel);
    assert.ok(html.includes('stale-banner'), `${viewKey}: 缺少过期横幅`);
    assert.ok(html.includes('数据更新于 2026-06-11'), `${viewKey}: 缺少过期文案`);
  }
});

check('过期横幅：数据新鲜（周五）时不渲染横幅', () => {
  const html = RENDERERS.dashboard(model);
  assert.ok(!html.includes('stale-banner'), '新鲜数据不应出现过期横幅');
});

check('时段提示：周末绝不显示"交易时段"类文案', () => {
  const weekend = getSessionMode(new Date(NOW_SAT));
  assert.equal(weekend.key, 'closed');
  assert.equal(weekend.label, '周末休市');
});

// ---------------------------------------------------------------------------
// 5. 红涨绿跌
// ---------------------------------------------------------------------------

console.log('# 5. 红涨绿跌（A 股配色）');

check('pctHtml: 正数带 up 类与 + 号，负数带 down 类，零/空为 flat', () => {
  assert.ok(pctHtml(1.23).includes('pct-up') && pctHtml(1.23).includes('+1.23%'));
  assert.ok(pctHtml(-1.23).includes('pct-down') && pctHtml(-1.23).includes('-1.23%'));
  assert.ok(pctHtml(0).includes('pct-flat'));
  assert.ok(pctHtml(null).includes('pct-flat') && pctHtml(null).includes('—'));
});

check('全量渲染：正涨幅输出含 up 类、负收益输出含 down 类', () => {
  const shanghaiChange = Number(((fullData.marketState.session_snapshot || {}).shanghai || {}).change_pct);
  assert.ok(shanghaiChange > 0, '前置条件：上证当日应为正涨幅');
  const marketHtml = RENDERERS.market(model);
  assert.ok(marketHtml.includes('pct-up'), 'market: 正涨幅缺少 pct-up');
  assert.ok(reviewHtml.includes('pct-down'), 'review: 负收益缺少 pct-down');
});

// ---------------------------------------------------------------------------
// 6. 空态（空数组 ≠ 文件缺失：渲染 emptySection 而非空白/占位）
// ---------------------------------------------------------------------------

console.log('# 6. 空态：空数组渲染解释而非空字符串');

const emptyData = clone(fullData);
emptyData.candidateState = { generated_at: '2026-06-12 12:00:00', latest_trade_date: '20260611', candidates: [] };
emptyData.executionState = {
  generated_at: '2026-06-12 15:40:00', trade_date: '20260611',
  total_execution_count: 0, main_count: 0, watch_count: 0, avoid_count: 0,
  consensus_in_execution: 0, divergence_stocks: [], strategy_counts: {}, executions: []
};
emptyData.greenfieldTop20 = { trade_date: '20260611', top20: [] };
emptyData.t1FactorRecommendations = { trade_date: '20260611', rows: [] };
emptyData.researchStateT1 = { status: 'research_preview', top20: [] };
emptyData.reviewState = { ...clone(fullData.reviewState), date_stats: [], latest_sample: [], top_repeat_recommendations: [] };
emptyData.reviewUnified = { generated_at: '', trade_date: '', strategies: {}, daily_comparison: [], stock_rows: [] };
const emptyModel = buildModel(emptyData, [], NOW_FRESH);

check('candidates: 三策略空名单各自渲染解释性空态', () => {
  const html = RENDERERS.candidates(emptyModel);
  assertCleanHtml(html, 'candidates(empty)');
  assert.ok(html.includes('section-empty'), '缺少空态容器');
  assert.ok(html.includes('今日启动前夕策略无入选标的'), '缺少启动前夕空态解释');
  assert.ok(html.includes('今日 O2C 策略无入选标的'), '缺少 O2C 空态解释');
  assert.ok(html.includes('今日 T1 策略无入选标的'), '缺少 T1 空态解释');
  assert.ok(!html.includes('section-missing'), '文件齐全时不应出现缺失占位');
});

check('dashboard: 执行清单为空 → 解释性空态而非空白', () => {
  const html = RENDERERS.dashboard(emptyModel);
  assertCleanHtml(html, 'dashboard(empty)');
  assert.ok(html.includes('今日没有执行建议'), '缺少执行清单空态解释');
  assert.ok(html.includes('暂无可验证数据'), '近期战绩无数据应显示"暂无可验证数据"');
});

check('review: 明细为空 → "暂无可验证数据"，禁止编造业绩', () => {
  const html = RENDERERS.review(emptyModel);
  assertCleanHtml(html, 'review(empty)');
  assert.ok(html.includes('暂无可验证数据'), '缺少战绩空态说明');
  assert.ok(html.includes('暂无推荐明细'), '缺少推荐明细空态说明');
  const found = findHardcodedNumber(html);
  assert.equal(found, null, `空数据下出现业绩数字 "${found}"，必为硬编码`);
});

// ---------------------------------------------------------------------------
// 7. 转义纪律（注入 <script> 必须被转义）
// ---------------------------------------------------------------------------

console.log('# 7. 转义纪律');

check('candidates: 候选股名/行业含 <script> 与引号注入 → 全部转义', () => {
  const injected = clone(fullData);
  injected.candidateState.candidates[0].name = '<script>alert("xss")</script>';
  injected.candidateState.candidates[0].industry_name = '"><img src=x onerror=alert(1)>';
  const html = RENDERERS.candidates(buildModel(injected, [], NOW_FRESH));
  assert.ok(!html.includes('<script'), '出现未转义的 <script');
  assert.ok(html.includes('&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;'), '注入内容应以转义形式可见');
  assert.ok(!html.includes('"><img src=x'), '出现未转义的属性注入');
});

check('review: 推荐明细 AI 观点含 <script> → 转义', () => {
  const injected = clone(fullData);
  injected.reviewUnified.stock_rows[0].ai_view = '<script>钓鱼</script>';
  injected.reviewUnified.stock_rows[0].stock_name = '注入&测试<b>';
  const html = RENDERERS.review(buildModel(injected, [], NOW_FRESH));
  assert.ok(!html.includes('<script'), '出现未转义的 <script');
  assert.ok(html.includes('&lt;script&gt;钓鱼&lt;/script&gt;'), '注入内容应以转义形式可见');
  assert.ok(html.includes('注入&amp;测试&lt;b&gt;'), '股名应被转义');
});

check('escapeHtml: 基础转义与空值兜底', () => {
  assert.equal(escapeHtml('<script>"&\'</script>'), '&lt;script&gt;&quot;&amp;&#39;&lt;/script&gt;');
  assert.equal(escapeHtml(null), '—');
});

// ---------------------------------------------------------------------------
// 8. 降级模式：只有 required（run_manifest + system_verdict），其余全部缺失
// ---------------------------------------------------------------------------

console.log('# 8. 降级模式（仅核心两源）');

const degradedData = { runManifest: fullData.runManifest, systemVerdict: fullData.systemVerdict };
const degradedMissing = Object.keys(SOURCES)
  .filter((key) => key !== 'runManifest' && key !== 'systemVerdict')
  .map((key) => ({ key, label: SOURCES[key].label, reason: 'HTTP 404' }));
const degradedModel = buildModel(degradedData, degradedMissing, NOW_FRESH);

for (const [viewKey, render] of Object.entries(RENDERERS)) {
  check(`${viewKey}: 降级渲染不抛错、有缺失提示、零硬编码业绩`, () => {
    const html = render(degradedModel);
    assertCleanHtml(html, viewKey);
    assert.ok(html.includes('部分内容暂时无法读取'), '应显示数据缺失通知条');
    assert.ok(html.includes('section-missing'), '核心分区缺数据时应有占位');
    // 无任何可选数据 → 任何业绩数字/模板话术都只能是硬编码，全部禁止。
    const found = findHardcodedNumber(html);
    assert.equal(found, null, `降级输出含 "${found}"，必为硬编码`);
    for (const phrase of TEMPLATE_PHRASES) {
      assert.ok(!html.includes(phrase), `降级输出含模板话术 "${phrase}"，必为编造`);
    }
  });
}

// ---------------------------------------------------------------------------
// 9. 数据清单完整性
// ---------------------------------------------------------------------------

console.log('# 9. 数据清单完整性');

check('manifest: 每个视图依赖键存在于 SOURCES，required 只含 2 个核心源', () => {
  for (const [view, deps] of Object.entries(VIEW_DEPS)) {
    assert.deepEqual(deps.required, ['runManifest', 'systemVerdict'], `${view} required 应只含核心源`);
    for (const key of [...deps.required, ...deps.optional]) {
      assert.ok(SOURCES[key], `${view} 引用未知数据源 ${key}`);
    }
  }
});

check('manifest: 每个数据源都有对应 fixture（防 SOURCES 增项后测试脱节）', () => {
  for (const [key, spec] of Object.entries(SOURCES)) {
    assert.ok(fullData[key] && typeof fullData[key] === 'object', `数据源 ${key}（${spec.path}）缺少 fixture`);
  }
});

console.log(`\n${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
