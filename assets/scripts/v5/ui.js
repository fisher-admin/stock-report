// v5/ui.js — 格式化 + 组件 + SVG 图表（纯字符串函数，所有插值必须经 esc）。
import { ageDays } from './data.js';

export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const isNum = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(+v);

export const fmt = (v, d = 2) => (isNum(v) ? (+v).toFixed(d) : '—');
export const dir = (v) => (!isNum(v) || +v === 0 ? 'flat' : +v > 0 ? 'up' : 'down');
export const pct = (v, d = 2) => (isNum(v) ? `<span class="n ${dir(v)}">${+v > 0 ? '+' : ''}${(+v).toFixed(d)}%</span>` : '<span class="n flat">—</span>');
export const ratio = (v, d = 1) => (isNum(v) ? `${(+v * 100).toFixed(d)}%` : '—');
export const dateCn = (s) => {
  const m = String(s || '').match(/^(\d{4})-?(\d{2})-?(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : esc(s || '—');
};

const TONES = { pass: 'pass', ok: 'pass', healthy: 'pass', present: 'pass', warn: 'warn', warning: 'warn', fail: 'block', failed: 'block', block: 'block', missing: 'block' };
export const toneOf = (s) => TONES[String(s || '').toLowerCase()] || 'mute';
export const badge = (text, tone = 'mute', title = '') => `<span class="b b-${tone}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
export const ACTION_TONE = { main: 'up', conditional_long: 'warn', watch: 'info', avoid: 'mute' };

export function ageBadge(src, ref, staleAfter = 7) {
  const a = ageDays(src, ref);
  if (a === null) return '';
  if (a > staleAfter) return badge(`陈旧 ${a} 天`, 'warn', `源日期 ${src}，运行交易日 ${ref}`);
  return `<span class="age">${dateCn(src)}</span>`;
}

export function panel({ title, sub = '', meta = '', body, cls = '', id = '' }) {
  return `<section class="pnl ${cls}"${id ? ` id="${esc(id)}"` : ''}>
  <header class="pnl-h"><h2>${esc(title)}</h2>${sub ? `<span class="pnl-sub">${esc(sub)}</span>` : ''}<span class="pnl-meta">${meta}</span></header>
  <div class="pnl-b">${body}</div></section>`;
}

export const missing = (label, reason = '') =>
  `<div class="missing">数据源「${esc(label)}」暂不可用${reason ? `<small>${esc(reason)}</small>` : ''}</div>`;

export const kv = (pairs) => `<dl class="kv">${pairs.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;

export function meter(value, max, { label = '', tone = 'info' } = {}) {
  const p = isNum(value) && max ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return `<div class="meter m-${tone}" role="meter" aria-valuenow="${esc(value)}" aria-valuemax="${esc(max)}"><i style="width:${p.toFixed(1)}%"></i>${label ? `<span>${esc(label)}</span>` : ''}</div>`;
}

export const stat = (label, value, foot = '') =>
  `<div class="stat"><span class="stat-l">${esc(label)}</span><span class="stat-v">${value}</span>${foot ? `<span class="stat-f">${foot}</span>` : ''}</div>`;

// 因子分 0–100 → 热度色阶（--heat 0..1）
export const heat = (v) => (isNum(v) ? `<span class="heat" style="--h:${Math.max(0, Math.min(1, (v - 40) / 60)).toFixed(2)}">${fmt(v, 1)}</span>` : '<span class="heat">—</span>');

// ---------------------------------------------------------------- 图表

export function bars(values, { h = 120, labels = [] } = {}) {
  const xs = values.map((v) => (isNum(v) ? +v : 0));
  if (!xs.length) return '';
  const max = Math.max(...xs.map(Math.abs), 1e-9);
  const w = xs.length * 10;
  const mid = h / 2;
  const rects = xs.map((v, i) => {
    const bh = (Math.abs(v) / max) * (mid - 2);
    return `<rect class="${dir(v)}" x="${i * 10 + 1}" y="${(v >= 0 ? mid - bh : mid).toFixed(1)}" width="8" height="${Math.max(bh, 0.5).toFixed(1)}"><title>${esc(labels[i] || '')} ${v.toFixed(2)}%</title></rect>`;
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"><line class="axis" x1="0" x2="${w}" y1="${mid}" y2="${mid}"/>${rects}</svg>`;
}

export function line(series, { h = 120, zero = true, cls = 'ln', band = null } = {}) {
  const pts = series.filter(isNum).map(Number);
  if (pts.length < 2) return '';
  let lo = Math.min(...pts), hi = Math.max(...pts);
  if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (band) { lo = Math.min(lo, band[0]); hi = Math.max(hi, band[1]); }
  const span = hi - lo || 1;
  const w = 400;
  const y = (v) => (h - 4 - ((v - lo) / span) * (h - 8)).toFixed(1);
  const d = pts.map((v, i) => `${i ? 'L' : 'M'}${((i / (pts.length - 1)) * w).toFixed(1)},${y(v)}`).join('');
  const z = zero ? `<line class="axis" x1="0" x2="${w}" y1="${y(0)}" y2="${y(0)}"/>` : '';
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img">${z}<path class="${cls} ${dir(pts[pts.length - 1])}" d="${d}"/></svg>`;
}

export const spark = (series) => line(series, { h: 32, cls: 'ln sp' });
