// v5/top20.js — Top-20 展厅（卡片网格 / 因子矩阵）+ 逐股 AI 深度分析抽屉。
import { esc, fmt, pct, badge, heat, ACTION_TONE, dateCn, panel } from './ui.js';

const scoreRing = (v) => {
  const p = Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 0;
  return `<span class="ring" style="--p:${p.toFixed(1)}"><b>${fmt(v, 1)}</b></span>`;
};

export const deltaTag = (c) => (c.delta === 'consensus'
  ? badge(c.championRank ? `共同入选 · 冠军#${c.championRank}` : '共同入选', 'pass')
  : badge('新策略独享', 'warn'));

function card(s) {
  const kf = s.keyFactors.map((f) => `<li><span>${esc(f.name)}</span><i style="--w:${Math.max(0, Math.min(100, f.value)).toFixed(0)}%"></i><b>${fmt(f.value, 0)}</b></li>`).join('');
  return `<button class="card" data-stock="${esc(s.code)}" aria-label="${esc(s.name)} AI 深度分析">
  <div class="card-h"><span class="rk">${s.rank ?? '—'}</span><div class="nm"><strong>${esc(s.name)}</strong><code>${esc(s.code)}</code></div>${scoreRing(s.score)}</div>
  <div class="card-tags">${s.industry ? badge(s.industry, 'mute') : ''}${s.challenger ? deltaTag(s.challenger) : badge(s.actionCn, ACTION_TONE[s.action] || 'mute')}${s.aiScore !== null ? `<span class="ai-chip">AI ${fmt(s.aiScore, 0)}</span>` : ''}<span class="px">${fmt(s.close, 2)} ${pct(s.chg)}</span></div>
  ${kf ? `<ul class="kf${s.challenger ? ' kf-c' : ''}">${kf}</ul>` : ''}
  <p class="concl">${esc(s.ai.conclusion || (s.challenger ? `挑战者分位 ${fmt((s.challenger.pct ?? 0) * 100, 1)}% · 该股未触发 AI 分析（影子策略不调用 AI）` : '该股暂无 AI 结论'))}</p>
  <span class="card-cta">AI 深度分析 →</span></button>`;
}

function slot(i) {
  return `<div class="card ghost" aria-hidden="true"><div class="card-h"><span class="rk">${i + 1}</span><div class="nm"><i class="sk w60"></i><i class="sk w30"></i></div><span class="ring"></span></div>
  <div class="card-tags"><i class="sk w30"></i><i class="sk w20"></i></div><ul class="kf">${'<li><i class="sk w90"></i></li>'.repeat(4)}</ul><i class="sk w90"></i><i class="sk w60"></i></div>`;
}

function matrix(stocks) {
  const names = [];
  stocks.forEach((s) => s.factors.forEach((f) => { if (!names.includes(f.name)) names.push(f.name); }));
  const th = (k, label, cls = '') => `<th data-sort="${esc(k)}" class="${cls}" tabindex="0" role="columnheader">${esc(label)}</th>`;
  const head = `<tr>${th('rank', '#', 'num')}${th('name', '股票')}${th('industry', '行业')}${th('score', '综合分', 'num')}${th('aiScore', 'AI', 'num')}${th('chg', '涨跌', 'num')}${th('action', '动作')}${names.map((n) => th(`f:${n}`, n, 'num fx')).join('')}</tr>`;
  const rows = stocks.map((s) => {
    const fm = Object.fromEntries(s.factors.map((f) => [f.name, f.value]));
    return `<tr data-stock="${esc(s.code)}" tabindex="0"><td class="num">${s.rank ?? '—'}</td><td><strong>${esc(s.name)}</strong> <code>${esc(s.code)}</code></td><td>${esc(s.industry)}</td><td class="num">${fmt(s.score, 1)}</td><td class="num">${fmt(s.aiScore, 0)}</td><td class="num">${pct(s.chg)}</td><td>${badge(s.actionCn, ACTION_TONE[s.action] || 'mute')}</td>${names.map((n) => `<td class="num fx">${heat(fm[n])}</td>`).join('')}</tr>`;
  }).join('');
  return `<div class="tbl-wrap"><table class="tbl" data-sortable><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
}

export function showroom(m, { mode = 'grid', demoHref = '', full = false, title = '每日 Top-20 展厅', sub = null, countsHtml = null, emptyMsg = '', cls = 'showroom', id = 'top20' } = {}) {
  const { stocks, counts, maintenance } = m;
  const cnt = countsHtml ?? ['main', 'conditional_long', 'watch', 'avoid']
    .map((k) => `<span class="cnt c-${k}"><b>${counts[k]}</b>${({ main: '主攻', conditional_long: '条件', watch: '观察', avoid: '回避' })[k]}</span>`).join('');
  const toggle = `<div class="seg" role="group" aria-label="展示方式"><button data-act="mode" data-mode="grid" class="${mode === 'grid' ? 'on' : ''}">卡片</button><button data-act="mode" data-mode="matrix" class="${mode === 'matrix' ? 'on' : ''}">因子矩阵</button></div>`;
  let body;
  if (stocks.length) {
    body = mode === 'matrix' ? matrix(stocks) : `<div class="grid20">${stocks.map(card).join('')}</div>`;
    if (full && mode === 'grid') body += `<p class="hint">点击任一股票查看 AI 深度分析；按 <kbd>J</kbd>/<kbd>K</kbd> 在抽屉内切换，<kbd>Esc</kbd> 关闭。</p>`;
  } else {
    const msg = maintenance
      ? `<strong>${esc(maintenance.title || '系统校准升级中')}</strong><p>校准完成后，这里将展示每日 Top-20 候选：综合分、因子子分、执行状态与逐股 AI 深度分析。</p>${demoHref ? `<a class="btn" href="${esc(demoHref)}">预览已填充的展厅（10-08 快照）</a>` : ''}`
      : emptyMsg || '<strong>今日没有入选股票</strong><p>策略未产出 Top-20 名单，或名单尚未发布。</p>';
    body = `<div class="ghost-wrap"><div class="grid20">${Array.from({ length: 20 }, (_, i) => slot(i)).join('')}</div><div class="overlay" role="status">${msg}</div></div>`;
  }
  return panel({
    title,
    sub: sub ?? [m.strategyName, m.version && `v${m.version.split('+')[0]}`].filter(Boolean).join(' · '),
    meta: `<span class="cnts">${cnt}</span><span class="age">信号日 ${dateCn(m.signalDate)}</span>${toggle}`,
    body,
    cls,
    id
  });
}

const list = (xs, cls = '') => (xs.length ? `<ul class="${cls}">${xs.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '<p class="muted">无</p>');

function challengerSec(c) {
  const comps = c.comps.map((f) => `<li title="${esc(f.full)}"><span>${esc(f.name)}</span><i style="--w:${Math.max(0, Math.min(100, f.value ?? 0)).toFixed(0)}%;--h:${Math.max(0, Math.min(1, ((f.value ?? 0) - 40) / 60)).toFixed(2)}"></i><b>${f.z !== null ? (f.z > 0 ? '+' : '') + fmt(f.z, 2) + 'σ' : '—'}</b></li>`).join('');
  return `<section class="dr-sec chal"><h4>v4.4 挑战者画像</h4>
    <div class="card-tags">${deltaTag(c)}${badge(c.hold === 'carried' ? '持有期沿用' : c.hold === 'held' ? '持有中' : '本期新调入', 'mute')}${badge(`挑战者分位 ${fmt((c.pct ?? 0) * 100, 1)}%`, 'info')}${c.booster !== null ? badge(`增强排序分 ${fmt(c.booster, 2)}`, 'mute') : ''}</div>
    <ul class="fbars">${comps}</ul>
    <p class="note">综合分 = 0.40×纯化核心 + 0.34×量价背离 + 0.26×聪明钱反转（截面 z）；后 25% 一律否决，幸存者按 0.5×z(v4.4 分)+0.5×z(挑战者分) 重排，名单每 5 个交易日调仓一次。</p>
    ${c.aiShared ? '' : '<p class="note">该股为挑战者独享，未触发 AI 分析；以下仅展示量化因子。</p>'}</section>`;
}

export function drawer(s, { index, total }) {
  if (!s) return '';
  const pts = s.ai.points.map((p) => `<li>${p.head ? `<h4>${esc(p.head)}</h4>` : ''}<p>${esc(p.body)}</p></li>`).join('');
  const fac = s.factors.slice().sort((a, b) => b.value - a.value)
    .map((f) => `<li title="${esc(f.full)}"><span>${esc(f.name)}</span><i style="--w:${Math.max(0, Math.min(100, f.value)).toFixed(0)}%;--h:${Math.max(0, Math.min(1, (f.value - 40) / 60)).toFixed(2)}"></i><b>${fmt(f.value, 1)}</b></li>`).join('');
  const plan = [['参考区间', s.plan.buyZone], ['失效条件', s.plan.invalidation], ['次日处理', s.plan.nextDay]].filter(([, v]) => v);
  return `<div class="dr-scrim" data-act="close"></div>
<aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="dr-t" tabindex="-1">
  <header class="dr-h">
    <div><span class="rk">${s.rank ?? '—'}</span><h3 id="dr-t">${esc(s.name)} <code>${esc(s.tsCode)}</code></h3>
    <div class="card-tags">${s.industry ? badge(s.industry, 'mute') : ''}${badge(s.actionCn, ACTION_TONE[s.action] || 'mute')}<span class="px">${fmt(s.close, 2)} ${pct(s.chg)}</span></div></div>
    <nav class="dr-nav"><button data-act="prev" aria-label="上一只">‹</button><span>${index + 1}/${total}</span><button data-act="next" aria-label="下一只">›</button><button data-act="close" aria-label="关闭">✕</button></nav>
  </header>
  <div class="dr-b">
    <div class="dr-scores"><div>${scoreRing(s.score)}<small>综合分</small></div><div><span class="ring ai" style="--p:${Number.isFinite(s.aiScore) ? s.aiScore : 0}"><b>${fmt(s.aiScore, 0)}</b></span><small>AI 分</small></div>
      <div class="dr-chip">${[['筹码集中度', fmt(s.chip.conc, 4)], ['获利盘', s.chip.winner !== null ? `${fmt(s.chip.winner, 1)}%` : '—'], ['量比', fmt(s.chip.vr, 2)], ['RSI6', fmt(s.chip.rsi, 1)]].map(([k, v]) => `<span><em>${k}</em>${v}</span>`).join('')}</div></div>
    ${s.challenger ? challengerSec(s.challenger) : ''}
    ${s.challenger && !s.ai.conclusion ? '' : `<section class="dr-sec thesis"><h4>投资逻辑${s.challenger ? '（v4.3 同日 AI 分析）' : ''}</h4><p>${esc(s.ai.conclusion || '暂无 AI 结论')}</p>
      <div class="ai-meta">${s.ai.advice ? badge(`AI 建议：${s.ai.advice}`, 'info') : ''}${s.ai.provenance ? badge(s.ai.provenance === 'fresh_same_day' ? '当日新鲜分析' : s.ai.provenance, s.ai.provenance === 'fresh_same_day' ? 'pass' : 'warn') : ''}${s.ai.mayChangeRank ? '' : badge('AI 不改排名', 'mute')}</div></section>`}
    ${pts ? `<section class="dr-sec"><h4>分项诊断</h4><ol class="pts">${pts}</ol></section>` : ''}
    ${s.challenger && !s.ai.conclusion ? '' : `<div class="dr-two"><section class="dr-sec cat"><h4>催化因素</h4>${list(s.ai.catalysts)}</section><section class="dr-sec risk"><h4>风险提示</h4>${list(s.ai.risks)}</section></div>`}
    ${fac ? `<section class="dr-sec"><h4>因子子分</h4><ul class="fbars">${fac}</ul></section>` : ''}
    ${plan.length ? `<section class="dr-sec"><h4>执行参考</h4><dl class="kv">${plan.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}${s.plan.holding ? `<dt>持有周期</dt><dd>${s.plan.holding} 个交易日</dd>` : ''}</dl></section>` : ''}
    ${s.rule.reasons.length || s.market.summary ? `<section class="dr-sec gate"><h4>规则与市场约束</h4>${s.rule.cap ? `<p>动作上限：${badge(s.rule.cap, 'warn')}</p>` : ''}${list([...s.rule.reasons, s.market.summary].filter(Boolean))}</section>` : ''}
    <p class="dr-foot">${s.challenger ? '影子策略 · 仅研究观察 · 无执行权限。' : ''}AI 证据时间 ${esc(s.ai.evidenceTime || '—')} · 信号截止 ${esc(s.plan.cutoff || '—')} · 计划入场 ${esc(s.plan.entry || '—')}。AI 仅做解释与风险复核，不改变排名与名单；不构成投资建议。</p>
  </div></aside>`;
}
