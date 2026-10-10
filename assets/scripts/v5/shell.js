// v5/shell.js — 顶栏、全局裁决条、导航、维护横幅。
import { esc, badge, toneOf, dateCn, fmt } from './ui.js';
import { maintenanceOf, tradeDate } from './data.js';

export const NAV = [
  ['today', '今日', '冠军 Top-20'],
  ['candidates', '挑战者', 'v4.4 影子对照'],
  ['market', '市场', '指数与行业'],
  ['evidence', '验证', '战绩与样本'],
  ['lab', '研究', '影子与剧本'],
  ['system', '系统', '运行与守卫']
];

const GATE_CN = { freshness_gate: '新鲜度', market_gate: '市场', strategy_gate: '策略', candidate_gate: '候选' };

export function verdictStrip(d) {
  const sv = d.systemVerdict || {};
  const fa = sv.final_action || {};
  const ds = d.decisionState || {};
  const mc = d.marketContext || {};
  const gates = sv.gates || ds.gates || {};
  const basis = mc.verdict_basis === 'close_actuals' ? '收盘实测' : mc.verdict_basis ? esc(mc.verdict_basis) : '盘前';
  const risk = mc.risk_score ?? ds.risk_score ?? d.runManifest?.risk_score;
  const pills = Object.entries(gates).map(([k, g]) =>
    `<li class="gate g-${toneOf(g.status)}" title="${esc(g.summary || '')}"><i></i>${esc(GATE_CN[k] || k)}</li>`).join('<li class="gate-sep" aria-hidden="true"></li>');
  return `<div class="verdict">
  <div class="v-main"><span class="v-label">${esc(ds.final_verdict || fa.label || '—')}</span><span class="v-sum">${esc(fa.summary || '')}</span></div>
  <ul class="v-gates" aria-label="四道闸">${pills}</ul>
  <div class="v-facts">
    <span><em>市况</em>${esc(mc.regime || ds.market_regime || '—')}</span>
    <span><em>仓位</em>${esc(mc.position_limit || '—')}</span>
    <span title="口径：${basis}${mc.preopen_risk_score != null ? `；盘前 ${fmt(mc.preopen_risk_score, 0)}` : ''}"><em>风险分</em><b class="n">${fmt(risk, 0)}</b><small>${basis}</small></span>
    ${badge(fa.execution_authority === 'observe_only_no_auto_order' || sv.pipeline_status?.execution_authority === 'observe_only_no_auto_order' ? '仅观察 · 不自动下单' : '执行权限未声明', 'info')}
  </div></div>`;
}

export function maintenanceBanner(d) {
  const m = maintenanceOf(d);
  if (!m) return '';
  return `<div class="maint" role="status"><span class="maint-dot" aria-hidden="true"></span>
  <div><strong>${esc(m.title || '系统校准升级中')}</strong> <span class="maint-en">${esc(m.title_en || '')}</span>
  <p>${esc(m.message || '')}</p></div>
  ${m.since ? `<span class="maint-since">自 ${esc(String(m.since).slice(0, 16).replace('T', ' '))}</span>` : ''}</div>`;
}

export function topbar(d, { route, demo, demoAvailable }) {
  const q = demo ? '?demo=live' : '';
  const nav = NAV.map(([id, cn, sub]) =>
    `<a href="#/${id}${q}" class="nav-i${id === route ? ' on' : ''}"${id === route ? ' aria-current="page"' : ''}><span>${cn}</span><small>${sub}</small></a>`).join('');
  const demoSw = demoAvailable || demo
    ? `<div class="seg" role="group" aria-label="数据源"><a href="#/${route}" class="${demo ? '' : 'on'}">实时</a><a href="#/${route}?demo=live" class="${demo ? 'on' : ''}">演示 10-08</a></div>` : '';
  return `<header class="top">
  <a class="brand" href="#/today${q}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17l5-6 4 3 6-8 3 2"/></svg><span>A股智能选股<small>Quant Daily · v5</small></span></a>
  <nav class="nav" aria-label="主导航">${nav}</nav>
  <div class="top-r">${demoSw}
    <span class="td">交易日 <b>${dateCn(tradeDate(d))}</b></span>
    <button class="ib" data-act="updown" title="切换涨跌配色（A股红涨 / 国际绿涨）" aria-label="切换涨跌配色"><span class="ud-a">红涨</span><span class="ud-b">绿涨</span></button>
    <button class="ib" data-act="theme" title="切换明暗主题" aria-label="切换明暗主题"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 13A9 9 0 1111 3a7 7 0 0010 10z"/></svg></button>
  </div></header>`;
}

export function demoNotice(demo, demoOk) {
  if (!demo) return '';
  return demoOk
    ? `<div class="demo-note">${badge('演示数据', 'warn')} 当前展示 2026-10-08 已公开发布快照（维护模式前最后一期），用于评审 Top-20 展厅；非今日数据。<a href="#/today">返回实时</a></div>`
    : `<div class="demo-note">${badge('演示不可用', 'block')} 本站点未附带演示快照，已显示实时数据。</div>`;
}

export function footer(d, missing) {
  const miss = Object.keys(missing);
  return `<footer class="foot">
  ${miss.length ? `<p class="foot-miss">部分数据源缺失：${miss.map(esc).join('、')}（对应分区已降级显示）</p>` : ''}
  <p>内容由量化模型与 AI 自动生成，仅供研究记录，不构成投资建议。run_id <code>${esc(d.runManifest?.run_id || '—')}</code> · 生成于 ${esc(d.runManifest?.generated_at || '—')}</p></footer>`;
}
