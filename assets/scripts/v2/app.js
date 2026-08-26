// v5/app.js — Hermes Quant 前端主控制器：
// 职责：读取 data-view → 加载数据模型 → 纯函数渲染 → 挂载交互（Tab、筛选、主题、手风琴、搜索、快捷键、复制）。

import { escapeHtml } from './render/format.js';
import { rendererFor } from './render/views.js';
import { loadViewData } from './data/loader.js';
import { buildModel, getSessionMode } from './data/model.js';

// ---------- 浮动 Toast 提示 ----------

function showToast(message, duration = 2000) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = `
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 8px;
      pointer-events: none;
    `;
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.style.cssText = `
    background: var(--surface-3, #1a253c);
    color: var(--text, #fff);
    border: 1px solid var(--card-border, rgba(255,255,255,0.15));
    border-radius: 8px;
    padding: 10px 18px;
    font-size: 13px;
    font-weight: 600;
    box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    display: flex;
    align-items: center;
    gap: 8px;
    opacity: 0;
    transform: translateY(8px);
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  `;
  toast.innerHTML = `<span style="color:var(--brand-2, #38bdf8)">✓</span> ${escapeHtml(message)}`;
  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.style.opacity = '1';
    toast.style.transform = 'translateY(0)';
  });

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(-4px)';
    setTimeout(() => toast.remove(), 200);
  }, duration);
}

// ---------- 复制到剪贴板事件 ----------

function mountCopyHandlers(root) {
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy-text]');
    if (!btn) return;
    const text = btn.getAttribute('data-copy-text');
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      const label = btn.getAttribute('data-copy-label') || text;
      showToast(`已复制: ${label}`);
    }).catch(() => {
      // 降级使用 prompt
    });
  });
}

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
      // 整组折叠：可见行为 0 的日期组连同表头一起隐藏
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

function escapeSelector(value) {
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
  return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

function activateTab(container, key, { syncHash = false } = {}) {
  const buttons = Array.from(container.querySelectorAll('[data-tab]'));
  if (!buttons.some((btn) => btn.getAttribute('data-tab') === key)) return false;
  buttons.forEach((btn) => {
    const on = btn.getAttribute('data-tab') === key;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });

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
    const initial = (hashTab && activateTab(container, hashTab)) ? hashTab : currentTabKey(container);
    if (initial && !hashTab) activateTab(container, initial);
    container.querySelectorAll('[data-tab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        activateTab(container, btn.getAttribute('data-tab'), { syncHash: true });
      });
    });
  });

  window.addEventListener('hashchange', () => {
    const key = readHashTab();
    if (!key) return;
    containers.forEach((container) => activateTab(container, key));
  });
}

// ---------- 「加载更多」批次展开 ----------

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

// ---------- 主题（跟随系统 + 持久化） ----------

const THEME_STORAGE_KEY = 'stockReportTheme';

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
  } catch (_) {}
  return '';
}

function systemPrefersDark() {
  return !(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
}

function applyTheme(theme) {
  const selected = setThemeAttr(theme);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, selected);
  } catch (_) {}
}

function mountThemeHandlers(root) {
  const saved = readSavedTheme();
  setThemeAttr(saved || (systemPrefersDark() ? 'dark' : 'light'));

  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (media) {
    const onSystemChange = (event) => {
      if (readSavedTheme()) return;
      setThemeAttr(event.matches ? 'dark' : 'light');
    };
    if (typeof media.addEventListener === 'function') media.addEventListener('change', onSystemChange);
    else if (typeof media.addListener === 'function') media.addListener(onSystemChange);
  }

  root.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.addEventListener('click', () => applyTheme(button.getAttribute('data-theme-choice')));
  });
}

// ---------- 锚点滚动 ----------

function scrollToHashTarget() {
  const hash = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '';
  if (!hash || hash.includes('=')) return;
  const target = document.getElementById(hash);
  if (!target) return;
  requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
}

// ---------- 个股 AI 分析手风琴 ----------

const STOCK_TOGGLE_SEL = '[data-stock-analysis-toggle], [data-ai-toggle]';

function mountAiToggleHandlers(root) {
  function setOpen(card, open) {
    const link = card.querySelector(STOCK_TOGGLE_SEL);
    if (link) link.setAttribute('aria-expanded', open ? 'true' : 'false');
    card.classList.toggle('ai-open', open);
  }
  function collapseOthers(except) {
    root.querySelectorAll('.candidate-card').forEach((other) => {
      const w = other.querySelector('[data-ai-wrap]');
      if (w && other !== except && !w.hidden) { w.hidden = true; setOpen(other, false); }
    });
  }
  function toggle(el, { forceOpen = false, scroll = false } = {}) {
    const card = el.closest('.candidate-card');
    const wrap = card && card.querySelector('[data-ai-wrap]');
    if (!wrap) return;
    const willOpen = forceOpen ? true : wrap.hidden;
    collapseOthers(card);
    wrap.hidden = !willOpen;
    setOpen(card, willOpen);
    if (willOpen) {
      const id = card.id;
      if (id) { try { history.replaceState(null, '', `#${id}`); } catch (_) {} }
      if (scroll) card.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }
  root.addEventListener('click', (e) => {
    const el = e.target.closest(STOCK_TOGGLE_SEL);
    if (el && root.contains(el)) {
      e.preventDefault();
      toggle(el);
    }
  });
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    const el = e.target.closest(STOCK_TOGGLE_SEL);
    if (el && root.contains(el)) { e.preventDefault(); toggle(el); }
  });

  function expandFromHash() {
    const hash = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '';
    if (!hash || !hash.startsWith('stock-')) return;
    const card = document.getElementById(hash);
    const link = card && card.querySelector(STOCK_TOGGLE_SEL);
    if (link) requestAnimationFrame(() => toggle(link, { forceOpen: true, scroll: true }));
  }
  expandFromHash();
  window.addEventListener('hashchange', expandFromHash);
}

// ---------- 全局键盘快捷键 ----------

function mountKeyboardShortcuts() {
  window.addEventListener('keydown', (e) => {
    // 忽略在输入框中的按键
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;

    if (e.key === 't' || e.key === 'T') {
      const current = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = current === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      showToast(`已切换至 ${next === 'dark' ? '暗色 (Obsidian)' : '亮色 (Quartz)'} 模式`);
    } else if (e.key === '1') {
      window.location.href = './index.html';
    } else if (e.key === '2') {
      window.location.href = './decision-candidates.html';
    } else if (e.key === '3') {
      window.location.href = './market-overview.html';
    } else if (e.key === '4') {
      window.location.href = './recommendation-review.html';
    } else if (e.key === '5') {
      window.location.href = './research-lab.html';
    }
  });
}

// ---------- 入口 ----------

async function main() {
  const root = document.getElementById('app');
  const viewKey = document.body.dataset.view || 'dashboard';
  const render = rendererFor(viewKey);

  setThemeAttr(readSavedTheme() || (systemPrefersDark() ? 'dark' : 'light'));
  root.innerHTML = `
    <div class="main">
      <section class="section-empty" style="margin-top:60px;">
        <div class="empty-title">正在装载 Hermes 量化终端...</div>
        <p class="empty-desc">正在读取最新交易日模型数据与投研因子，请稍候</p>
      </section>
    </div>
  `;

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
    mountCopyHandlers(root);
    mountKeyboardShortcuts();
    scrollToHashTarget();
  } catch (error) {
    root.innerHTML = `
      <main class="main">
        <section class="notice fail" role="alert">
          <div class="notice-icon">×</div>
          <div class="notice-body">
            <strong>页面暂时打不开</strong>
            <p>数据没有加载成功，可能是网络不稳定或数据正在更新中。请稍后刷新页面再试。</p>
            <details style="margin-top:8px;font-size:12px;color:var(--text-4)"><summary>技术详情</summary><p>${escapeHtml(error.message || String(error))}</p></details>
          </div>
        </section>
      </main>
    `;
  }
}

main();
