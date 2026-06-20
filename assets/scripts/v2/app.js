// v2/app.js — 唯一接触 DOM 的模块：读取 data-view → 按清单取数 → 纯函数渲染 → 挂事件。
// 渲染逻辑全部在 v2/render/*（纯函数），数据装配在 v2/data/*；本文件只做粘合。
//
// v3 交互约定（视图模块按这些 data-* 约定输出 HTML，事件统一在这里挂载）：
//   Tab：     <div data-tabs> 内放按钮 [data-tab="key"] 与面板 [data-tab-panel="key"]；
//             点击切换并同步地址栏 #tab=key，刷新/分享链接保位。
//   加载更多：按钮 [data-load-more="group"]（可选 data-batch-size，默认 20）；
//             待展开条目带 [data-load-more-item="group"] 且初始 hidden；批量显示，放完即藏按钮。
//   字段筛选：容器 [data-filter-scope] 内的 select/input 带 [data-filter-field="xxx"]，
//             行带 [data-filter-row] 与 data-xxx 属性；field="keyword" 时按行文本模糊匹配。
//   候选筛选：按钮 [data-filter] ↔ 卡片 .candidate-card[data-role]（沿用 v2 约定）。

import { escapeHtml } from './render/format.js';
import { rendererFor } from './render/views.js';
import { loadViewData } from './data/loader.js';
import { buildModel } from './data/model.js';

// ---------- 候选/策略/复盘 按钮组筛选（沿用 v2 约定） ----------

function mountFilterHandlers(root) {
  const buttons = Array.from(root.querySelectorAll('[data-filter]'));
  if (!buttons.length) return;
  const cards = Array.from(root.querySelectorAll('.candidate-card'));
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-filter');
      buttons.forEach((item) => item.classList.toggle('active', item === btn));
      cards.forEach((card) => {
        const role = card.getAttribute('data-role');
        card.classList.toggle('hidden', !(target === 'all' || role === target));
      });
    });
  });
}

function mountStrategyFilterHandlers(root) {
  const buttons = Array.from(root.querySelectorAll('[data-strategy-filter]'));
  if (!buttons.length) return;
  const sections = Array.from(root.querySelectorAll('.strategy-section'));
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-strategy-filter');
      buttons.forEach((item) => item.classList.toggle('active', item === btn));
      sections.forEach((section) => {
        const sectionId = section.id.replace('strategy-content-', '');
        section.classList.toggle('hidden', sectionId !== target);
      });
    });
  });
}

function mountReviewFilterHandlers(root) {
  const buttons = Array.from(root.querySelectorAll('[data-review-filter]'));
  if (!buttons.length) return;
  const rows = Array.from(root.querySelectorAll('[data-review-role]'));
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-review-filter');
      buttons.forEach((item) => item.classList.toggle('active', item === btn));
      rows.forEach((row) => {
        const role = row.getAttribute('data-review-role');
        row.classList.toggle('hidden', !(target === 'all' || role === target));
      });
    });
  });
}

// ---------- 通用字段筛选（历史战绩页：策略 + 日期 + 关键字） ----------

function mountFieldFilterHandlers(root) {
  root.querySelectorAll('[data-filter-scope]').forEach((scope) => {
    const controls = Array.from(scope.querySelectorAll('[data-filter-field]'));
    if (!controls.length) return;
    const apply = () => {
      const active = controls
        .map((control) => ({
          field: control.getAttribute('data-filter-field'),
          value: String(control.value || '').trim()
        }))
        .filter((item) => item.field && item.value && item.value !== 'all');
      scope.querySelectorAll('[data-filter-row]').forEach((row) => {
        const visible = active.every(({ field, value }) => {
          if (field === 'keyword') {
            return row.textContent.toLowerCase().includes(value.toLowerCase());
          }
          return (row.getAttribute(`data-${field}`) || '') === value;
        });
        row.classList.toggle('hidden', !visible);
        row.toggleAttribute('hidden', !visible);
      });
      // 整组折叠：可见行为 0 的日期组连同表头一起隐藏（用独立 class，不动「加载更多」的 hidden 属性）。
      scope.querySelectorAll('[data-filter-group]').forEach((group) => {
        const rows = group.querySelectorAll('[data-filter-row]');
        if (!rows.length) return;
        const anyVisible = Array.from(rows).some((r) => !r.classList.contains('hidden'));
        group.classList.toggle('filter-empty', !anyVisible);
      });
    };
    controls.forEach((control) => {
      control.addEventListener('change', apply);
      control.addEventListener('input', apply);
    });
  });
}

// ---------- 通用 Tab（hash 同步，刷新保位） ----------

function readHashTab() {
  const hash = window.location.hash ? window.location.hash.slice(1) : '';
  if (!hash.includes('=')) return '';
  try {
    return new URLSearchParams(hash).get('tab') || '';
  } catch (_) {
    return '';
  }
}

function activateTab(container, key, { syncHash = false } = {}) {
  const buttons = Array.from(container.querySelectorAll('[data-tab]'));
  if (!buttons.some((btn) => btn.getAttribute('data-tab') === key)) return false;
  buttons.forEach((btn) => {
    const on = btn.getAttribute('data-tab') === key;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  // 面板按约定渲染在 tabs 容器之外，以 data-tabs-group 与容器的 data-tabs 配对；
  // 同时兼容直接嵌在容器内的面板写法。
  const group = container.getAttribute('data-tabs') || 'main';
  const panels = new Set([
    ...container.querySelectorAll('[data-tab-panel]'),
    ...document.querySelectorAll(`[data-tab-panel][data-tabs-group="${escapeSelector(group)}"]`)
  ]);
  panels.forEach((panel) => {
    const on = panel.getAttribute('data-tab-panel') === key;
    panel.classList.toggle('hidden', !on);
    panel.toggleAttribute('hidden', !on);
  });
  if (syncHash) {
    try {
      history.replaceState(null, '', `#tab=${encodeURIComponent(key)}`);
    } catch (_) {
      window.location.hash = `tab=${key}`;
    }
  }
  return true;
}

function currentTabKey(container) {
  const activeBtn = container.querySelector('[data-tab].active');
  if (activeBtn) return activeBtn.getAttribute('data-tab');
  const firstBtn = container.querySelector('[data-tab]');
  return firstBtn ? firstBtn.getAttribute('data-tab') : '';
}

function mountTabHandlers(root) {
  const containers = Array.from(root.querySelectorAll('[data-tabs]'));
  if (!containers.length) return;
  const hashTab = readHashTab();
  containers.forEach((container) => {
    // 初始：URL 上的 #tab= 优先；否则按渲染时标好的 .active（或第一个 Tab）归一化面板显隐。
    const initial = (hashTab && activateTab(container, hashTab)) ? hashTab : currentTabKey(container);
    if (initial && !hashTab) activateTab(container, initial);
    container.querySelectorAll('[data-tab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        activateTab(container, btn.getAttribute('data-tab'), { syncHash: true });
      });
    });
  });
  // 浏览器前进/后退（或手改地址栏）时跟随切换。
  window.addEventListener('hashchange', () => {
    const key = readHashTab();
    if (!key) return;
    containers.forEach((container) => activateTab(container, key));
  });
}

// ---------- 「加载更多」批次展开 ----------

function escapeSelector(value) {
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
  return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

function mountLoadMoreHandlers(root) {
  root.querySelectorAll('[data-load-more]').forEach((button) => {
    const group = button.getAttribute('data-load-more') || '';
    const sizeAttr = Number(button.getAttribute('data-batch-size'));
    const batchSize = Number.isFinite(sizeAttr) && sizeAttr > 0 ? sizeAttr : 20;
    const hiddenItems = () => Array.from(
      root.querySelectorAll(`[data-load-more-item="${escapeSelector(group)}"]`)
    ).filter((el) => el.hasAttribute('hidden') || el.classList.contains('hidden'));
    const syncButton = () => {
      if (!hiddenItems().length) {
        button.classList.add('hidden');
        button.setAttribute('hidden', '');
      }
    };
    button.addEventListener('click', () => {
      hiddenItems().slice(0, batchSize).forEach((el) => {
        el.removeAttribute('hidden');
        el.classList.remove('hidden');
      });
      syncButton();
    });
    syncButton();
  });
}

// ---------- 主题（默认跟随系统；用户显式选择写 localStorage 后优先并停止跟随） ----------

const THEME_STORAGE_KEY = 'stockReportTheme';

// 把主题写到 <html> 与 <body>（app.css 同时支持 :root[data-theme] 与 body[data-theme]，
// V4 设计令牌挂在 :root[data-theme]，二者都设可避免主题破损）。
function setThemeAttr(theme) {
  const selected = theme === 'light' ? 'light' : 'dark';
  if (document.documentElement) document.documentElement.setAttribute('data-theme', selected);
  if (document.body) document.body.dataset.theme = selected;
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.classList.toggle('active', button.getAttribute('data-theme-choice') === selected);
  });
  return selected;
}

function readSavedTheme() {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch (_) {
    // localStorage 在某些受限环境不可用。
  }
  return '';
}

function systemPrefersDark() {
  return !(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
}

// 用户显式点选：写入 localStorage（此后优先于系统），并刷新当前主题。
function applyTheme(theme) {
  const selected = setThemeAttr(theme);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, selected);
  } catch (_) {
    // 忽略存储失败：本次会话仍生效，只是不持久。
  }
}

// 初始主题：有用户显式选择 → 用它；否则跟随系统（深/浅），并监听系统切换实时跟随，
// 直到用户首次点选主题为止（点选后写 localStorage，监听器据此不再覆盖）。
function mountThemeHandlers(root) {
  const saved = readSavedTheme();
  setThemeAttr(saved || (systemPrefersDark() ? 'dark' : 'light'));

  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (media) {
    const onSystemChange = (event) => {
      // 仅当用户尚未在 localStorage 固定选择时，才跟随系统变化。
      if (readSavedTheme()) return;
      setThemeAttr(event.matches ? 'dark' : 'light');
    };
    if (typeof media.addEventListener === 'function') media.addEventListener('change', onSystemChange);
    else if (typeof media.addListener === 'function') media.addListener(onSystemChange); // 旧版 Safari
  }

  root.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.addEventListener('click', () => applyTheme(button.getAttribute('data-theme-choice')));
  });
}

// ---------- 锚点滚动（非 Tab 形态的 hash） ----------

function scrollToHashTarget() {
  const hash = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '';
  if (!hash || hash.includes('=')) return;
  const target = document.getElementById(hash);
  if (!target) return;
  requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
}

// ---------- 个股 AI 分析手风琴（点击卡头原地展开；同时只展开一只） ----------

function mountAiToggleHandlers(root) {
  function setHint(card, open) {
    const hint = card.querySelector('.ai-toggle-hint');
    if (hint) hint.textContent = open ? '收起分析 ▴' : '展开分析 ▾';
    const head = card.querySelector('[data-ai-toggle]');
    if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    card.classList.toggle('ai-open', open);
  }
  function toggle(head) {
    const card = head.closest('.candidate-card');
    const wrap = card && card.querySelector('[data-ai-wrap]');
    if (!wrap) return;
    const willOpen = wrap.hidden;
    root.querySelectorAll('.candidate-card').forEach((other) => {
      const w = other.querySelector('[data-ai-wrap]');
      if (w && other !== card && !w.hidden) { w.hidden = true; setHint(other, false); }
    });
    wrap.hidden = !willOpen;
    setHint(card, willOpen);
  }
  root.addEventListener('click', (e) => {
    const head = e.target.closest('[data-ai-toggle]');
    if (head && root.contains(head)) toggle(head);
  });
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    const head = e.target.closest('[data-ai-toggle]');
    if (head && root.contains(head)) { e.preventDefault(); toggle(head); }
  });
}

// ---------- 入口 ----------

async function main() {
  const root = document.getElementById('app');
  const viewKey = document.body.dataset.view || 'dashboard';
  const render = rendererFor(viewKey);
  // 首屏先按「已保存选择 → 否则系统偏好」上色，避免闪烁；不写 localStorage（保持跟随系统的能力）。
  // 正式的系统变化监听与按钮事件在渲染后的 mountThemeHandlers 里挂载。
  setThemeAttr(readSavedTheme() || (systemPrefersDark() ? 'dark' : 'light'));
  root.innerHTML = '<div class="main"><section class="empty"><div class="panel-title">加载中</div><div class="metric-value">正在读取最新数据，请稍候…</div></section></div>';
  try {
    const { data, missing } = await loadViewData(viewKey);
    const model = buildModel(data, missing);
    root.innerHTML = render(model);
    mountTabHandlers(root);
    mountLoadMoreHandlers(root);
    mountFieldFilterHandlers(root);
    mountFilterHandlers(root);
    mountStrategyFilterHandlers(root);
    mountReviewFilterHandlers(root);
    mountThemeHandlers(root);
    mountAiToggleHandlers(root);
    scrollToHashTarget();
  } catch (error) {
    root.innerHTML = `
      <main class="main">
        <section class="notice fail" role="alert">
          <div class="notice-icon">×</div>
          <div>
            <strong>页面暂时打不开</strong>
            <p>数据没有加载成功，可能是网络不稳定，或数据正在更新中。请稍后刷新页面再试；如果反复出现，请过一会儿再来。</p>
            <details class="notice-detail"><summary>技术信息</summary><p>${escapeHtml(error.message || String(error))}</p></details>
          </div>
        </section>
      </main>
    `;
  }
}

main();
