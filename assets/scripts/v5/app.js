// v5/app.js — 唯一接触 DOM 的模块：哈希路由 → 取数 → 渲染 → 事件。
import { createLoader, ROUTE_DEPS, ROUTES, DEMO_BASE, top20Model, challengerModel } from './data.js';
import { VIEWS } from './views.js';
import { topbar, verdictStrip, maintenanceBanner, demoNotice, footer, NAV } from './shell.js';
import { drawer } from './top20.js';
import { esc } from './ui.js';

const root = document.getElementById('app');
const html = document.documentElement;
const store = {
  get(k, d) { try { return localStorage.getItem(`sr5.${k}`) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`sr5.${k}`, v); } catch { /* 无痕模式 */ } }
};

const loaders = {};
const loaderFor = (demo) => (loaders[demo] ||= createLoader({ demo }));
const modes = { today: store.get('mode.today', 'grid'), candidates: store.get('mode.candidates', 'grid') };
let cur = { key: '', data: {}, stocks: [] };
let demoAvailable = null;

function applyPrefs() {
  const theme = store.get('theme', '');
  if (theme) html.dataset.theme = theme; else delete html.dataset.theme;
  html.dataset.updown = store.get('updown', 'cn');
}

function parse() {
  const [path, q = ''] = location.hash.replace(/^#\/?/, '').split('?');
  const params = new URLSearchParams(q);
  const search = new URLSearchParams(location.search);
  const route = ROUTES.includes(path) ? path : (document.body.dataset.route || 'today');
  const demo = (params.get('demo') || search.get('demo')) === 'live';
  return { route, demo, stock: params.get('s') || '' };
}

function hrefFor({ route, demo, stock }) {
  const q = new URLSearchParams();
  if (demo) q.set('demo', 'live');
  if (stock) q.set('s', stock);
  const s = q.toString();
  return `#/${route}${s ? `?${s}` : ''}`;
}

// 演示快照只存在于仓库分支预览（Pages 构建不复制 preview/）。线上 github.io 不探测，
// 避免每次加载在控制台留下 404；显式 ?demo=live 时仍会探测并在缺失时降级。
const PREVIEW_HOST = !location.hostname.endsWith('github.io');

async function probeDemo(requested) {
  if (demoAvailable !== null) return demoAvailable;
  if (!requested && !PREVIEW_HOST) return false;
  try { demoAvailable = (await fetch(`${DEMO_BASE}run_manifest.json`, { cache: 'no-store' })).ok; } catch { demoAvailable = false; }
  return demoAvailable;
}

async function render() {
  const st = parse();
  const key = `${st.route}|${st.demo}|${modes[st.route] || ''}`;
  if (key === cur.key) return renderDrawer(st);
  const avail = await probeDemo(st.demo);
  const useDemo = st.demo && avail;
  const { data, missing } = await loaderFor(useDemo).load(ROUTE_DEPS[st.route]);
  if (!data.runManifest && !data.systemVerdict) {
    root.innerHTML = `<div class="fatal"><h1>数据暂时无法加载</h1><p>核心文件 run_manifest / system_verdict 缺失：${esc(Object.values(missing).join('；'))}</p><button class="btn" onclick="location.reload()">重试</button></div>`;
    return;
  }
  const ctx = { route: st.route, demo: useDemo, demoAvailable: avail, mode: modes[st.route] || 'grid' };
  root.innerHTML = `${topbar(data, ctx)}${maintenanceBanner(data)}${demoNotice(st.demo, useDemo)}
  ${verdictStrip(data)}<main id="main" class="main r-${st.route}">${VIEWS[st.route](data, ctx)}</main>${footer(data, missing)}<div id="dr"></div>`;
  const meta = NAV.find(([id]) => id === st.route);
  document.title = `${meta ? meta[1] : ''} · A股智能选股系统`;
  cur = { key, data, stocks: st.route === 'today' ? top20Model(data).stocks : st.route === 'candidates' ? challengerModel(data).stocks : [] };
  renderDrawer(st);
}

function renderDrawer(st) {
  const box = document.getElementById('dr');
  if (!box) return;
  const i = cur.stocks.findIndex((s) => s.code === st.stock);
  const open = i >= 0;
  box.innerHTML = open ? drawer(cur.stocks[i], { index: i, total: cur.stocks.length }) : '';
  document.body.classList.toggle('dr-open', open);
  if (open) box.querySelector('.drawer')?.focus({ preventScroll: true });
}

function go(patch, replace = false) {
  const h = hrefFor({ ...parse(), ...patch });
  if (replace) { history.replaceState(null, '', h); render(); } else location.hash = h;
}

function step(delta) {
  const st = parse();
  const i = cur.stocks.findIndex((s) => s.code === st.stock);
  if (i < 0 || !cur.stocks.length) return;
  go({ stock: cur.stocks[(i + delta + cur.stocks.length) % cur.stocks.length].code }, true);
}

function sortTable(th) {
  const table = th.closest('table');
  const idx = [...th.parentNode.children].indexOf(th);
  const asc = th.getAttribute('aria-sort') !== 'ascending';
  table.querySelectorAll('th').forEach((x) => x.removeAttribute('aria-sort'));
  th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
  const val = (tr) => {
    const t = tr.children[idx]?.textContent.trim() || '';
    const n = parseFloat(t.replace(/[+%,]/g, ''));
    return Number.isFinite(n) && /^[-+\d.]/.test(t) ? n : t;
  };
  const body = table.tBodies[0];
  [...body.rows].sort((a, b) => {
    const x = val(a), y = val(b);
    const c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'zh');
    return asc ? c : -c;
  }).forEach((r) => body.appendChild(r));
}

root.addEventListener('click', (e) => {
  const t = e.target.closest('[data-act],[data-stock],th[data-sort]');
  if (!t) return;
  if (t.matches('th[data-sort]')) return sortTable(t);
  const act = t.dataset.act;
  if (!act && t.dataset.stock) return go({ stock: t.dataset.stock });
  if (act === 'close') return go({ stock: '' });
  if (act === 'prev') return step(-1);
  if (act === 'next') return step(1);
  if (act === 'mode') {
    const r = parse().route;
    modes[r] = t.dataset.mode;
    store.set(`mode.${r}`, t.dataset.mode);
    return render();
  }
  if (act === 'cost') {
    const box = t.closest('.arena');
    box?.classList.toggle('stress', t.dataset.cost === 'stress');
    t.parentNode.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === t));
    return;
  }
  if (act === 'theme') {
    const dark = html.dataset.theme ? html.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    store.set('theme', dark ? 'light' : 'dark');
    return applyPrefs();
  }
  if (act === 'updown') {
    store.set('updown', html.dataset.updown === 'cn' ? 'intl' : 'cn');
    return applyPrefs();
  }
});

root.addEventListener('keydown', (e) => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('tr[data-stock]')) { e.preventDefault(); go({ stock: e.target.dataset.stock }); }
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('th[data-sort]')) { e.preventDefault(); sortTable(e.target); }
});

document.addEventListener('keydown', (e) => {
  if (!document.body.classList.contains('dr-open') || e.metaKey || e.ctrlKey) return;
  if (e.key === 'Escape') go({ stock: '' });
  else if (e.key === 'j' || e.key === 'ArrowRight') step(1);
  else if (e.key === 'k' || e.key === 'ArrowLeft') step(-1);
});

// 竞技场净值图读数：悬停热区时更新读数行与十字线（数值来自热区 <title>）
root.addEventListener('pointerover', (e) => {
  const hot = e.target.closest('svg.dual rect.hot');
  if (!hot) return;
  const svg = hot.ownerSVGElement;
  const cross = svg.querySelector('.cross');
  const cx = (+hot.getAttribute('x') + +hot.getAttribute('width') / 2).toFixed(1);
  cross.setAttribute('x1', cx);
  cross.setAttribute('x2', cx);
  svg.classList.add('hovering');
  const out = svg.closest('.pnl')?.querySelector('.readout');
  if (out) out.textContent = hot.querySelector('title')?.textContent || '';
});
root.addEventListener('pointerout', (e) => {
  const svg = e.target.closest?.('svg.dual');
  if (svg && !svg.contains(e.relatedTarget)) svg.classList.remove('hovering');
});

window.addEventListener('hashchange', render);
applyPrefs();
render();
