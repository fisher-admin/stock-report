// v3/render/shell.js — 页面骨架：导航 / 顶栏 / 过期横幅 / Hero 容器 / 免责页脚（纯函数，无 DOM 依赖）。
// v4 视觉：Brokerage Pro / 铜金抬升式（DESIGN-V4 第 1、3 节）。骨架结构与信息架构稳定（NAV / VIEW_META 不变），
// 仅升级呈现：抬升式顶栏（品牌铜金 + 数据日期徽章 + 主题切换）、焦点式 Hero（大裁决标签 + 风险刻度盘 + 纪律胶囊）。
//
// 公开 API（v3 签名全部兼容）：
//   NAV                                   — 主导航 5 项（顺序即客户心智路径，DESIGN-V3 第 1 节）
//   VIEW_META                             — data-view → { title, navKey }，每页唯一标题
//   renderShell(viewKey, model, bodyHtml) — 整页骨架（签名与 v2 兼容）
//   renderHero(model, title, subtitle, opts) — Hero 容器；opts = { eyebrow, bodyHtml, asideHtml }
//   renderStalenessBanner(model)          — 数据过期黄色横幅（接 model.staleness，spec 0.5）
//   renderMissingNotice(model)            — 可选数据缺失提示（客户语言）
//   sourceTimeRows(model)                 — 数据更新时间三行（白话标签）
//
// model.staleness 约定（由 data/model.js 计算）：
//   { isStale: boolean, tradeDate: 'YYYYMMDD'|'YYYY-MM-DD', label?|message?: string }
//   shell 只负责渲染；isStale 缺失/为假时不出横幅。文案优先用 model 给的 label/message。

import { escapeHtml, safeText, dateCn, friendlyTime } from './format.js';
import { riskGauge, themeToggle, disclaimerFooter, dateBadge, verdictPill } from './components.js';

// 主导航：顺序固定（今日操作 → 个股推荐 → 市场行情 → 历史战绩 → 系统说明）。
export const NAV = [
  { key: 'dashboard', href: './index.html', label: '今日操作', desc: '今天该不该动手' },
  { key: 'candidates', href: './decision-candidates.html', label: '个股推荐', desc: '三条策略推了什么' },
  { key: 'market', href: './market-overview.html', label: '市场行情', desc: '大盘与行业环境' },
  { key: 'review', href: './recommendation-review.html', label: '历史战绩', desc: '过往推荐真实收益' },
  { key: 'research', href: './research-lab.html', label: '系统说明', desc: '系统怎么工作' }
];

// 每个 data-view 的唯一标题；薄壳页（旧 URL）归属到对应主导航项。
export const VIEW_META = {
  dashboard: { title: '今日操作', navKey: 'dashboard' },
  candidates: { title: '个股推荐', navKey: 'candidates' },
  market: { title: '市场行情', navKey: 'market' },
  review: { title: '历史战绩', navKey: 'review' },
  research: { title: '系统说明', navKey: 'research' },
  marketHeatmap: { title: '市场行情 · 全市场热力', navKey: 'market' },
  strategyHeatmap: { title: '市场行情 · 策略热力', navKey: 'market' },
  industryActions: { title: '市场行情 · 行业动作', navKey: 'market' },
  strategy: { title: '系统说明 · 策略中心', navKey: 'research' },
  // 情绪因子页：唯一标题，不进主 NAV；从市场页页脚链接进入，导航高亮归属市场行情。
  sentiment: { title: '情绪因子', navKey: 'market' },
  // S3 观察名单页：唯一标题，不进主 NAV；从系统说明页剧本引擎章节链接进入，导航高亮归属系统说明。
  s3Watch: { title: 'S3 分时形态 · top-20 观察名单', navKey: 'research' }
};

// 数据更新时间 → 白话三行（开发字段不外露）。
export function sourceTimeRows(model) {
  const manifest = model.runManifest || {};
  const sources = manifest.sources || {};
  const marketState = model.marketState || {};
  const morning = marketState.morning || {};
  const midday = model.midday || marketState.midday || {};
  const review = model.reviewState || {};
  return [
    {
      label: '早间市场分析',
      value: friendlyTime(morning.generated_at || sources.market_generated_at),
      note: `基于 ${dateCn(morning.market_data_trade_date || marketState.latest_trade_date || manifest.trade_date)} 收盘行情`
    },
    {
      label: '盘中市场快照',
      value: friendlyTime(midday.as_of_time || midday.generated_at || sources.midday_generated_at),
      note: `快照交易日 ${dateCn(midday.trade_date || marketState.latest_trade_date || manifest.trade_date)}`
    },
    {
      label: '收盘后推荐复盘',
      value: friendlyTime(review.generated_at || sources.review_generated_at),
      note: `推荐日期 ${dateCn(review.latest_recommend_date || manifest.trade_date)}`
    }
  ];
}

// 数据过期横幅（spec 0.5）：model.staleness.isStale 为真时全站顶栏渲染。
export function renderStalenessBanner(model) {
  const staleness = model && model.staleness ? model.staleness : {};
  const isStale = staleness.isStale ?? staleness.stale ?? false;
  if (!isStale) return '';
  const date = dateCn(staleness.tradeDate || staleness.dataDate || (model.runManifest || {}).trade_date);
  const message = safeText(staleness.message, '') || safeText(staleness.label, '')
    || `数据更新于 ${date}，今日数据尚未生成。当前页面展示的是最近一个交易日的内容，请留意时效。`;
  return `<div class="stale-banner" role="status">
    <span class="stale-icon" aria-hidden="true">!</span>
    <p>${escapeHtml(message)}</p>
  </div>`;
}

// 可选数据缺失提示：用客户语言列出受影响板块，不暴露文件路径/HTTP 细节。
export function renderMissingNotice(model) {
  const missing = Array.isArray(model && model.missing) ? model.missing : [];
  if (!missing.length) return '';
  const labels = missing.map((item) => safeText(item.label, '部分数据')).join('、');
  return `<div class="notice tone-info" role="note">
    <span class="notice-icon" aria-hidden="true">i</span>
    <div class="notice-body">
      <strong>部分内容暂时无法读取</strong>
      <p>${escapeHtml(labels)} 暂未生成或读取失败，相关板块会显示占位说明，其余内容不受影响。</p>
    </div>
  </div>`;
}

// 裁决基调：把 system_verdict / decision 的最终动作映射成脉冲标签文案与色调（仅用于 Hero 标签的氛围，
// 不是「买/观/避」计数——DESIGN-V4 第 1 节：Hero 不出执行层三计数，那一套由视图自己出）。
function verdictTone(model) {
  const verdict = model.verdict || {};
  const decision = model.decisionState || {};
  const action = safeText(verdict.action || decision.final_action || decision.final_verdict, '').toLowerCase();
  if (/execute|deploy|可执行|进攻|加仓/.test(action)) return { tone: 'ok', text: '今日裁决' };
  if (/observe|watch|观察|降级|谨慎/.test(action)) return { tone: 'warn', text: '今日裁决' };
  if (/avoid|halt|stop|回避|空仓|防御/.test(action)) return { tone: 'bad', text: '今日裁决' };
  return { tone: 'brand', text: '今日裁决' };
}

// Hero 容器（焦点式）。左主区：脉冲裁决标签 + 大裁决标题 + 一句话结论 + 视图自带 bodyHtml；
// 右侧 aside：默认风险刻度盘 + 数据更新时间三行；视图可用 opts.asideHtml / bodyHtml 覆盖。
// 注意：此处不渲染任何「买/观/避」计数（spec 第 4 节：首页只信执行层一套数字，由视图自己出）。
export function renderHero(model, title, subtitle, opts = {}) {
  const manifest = model.runManifest || {};
  const context = model.marketContext || {};
  const marketSummary = (model.marketState || {}).market_summary || {};
  const riskScore = context.risk_score ?? marketSummary.risk_score ?? manifest.risk_score;
  const regime = safeText(context.regime || marketSummary.market_regime || manifest.market_regime, '');
  const eyebrow = safeText(opts.eyebrow, '') || `数据日期 ${dateCn(manifest.trade_date)}`;
  const rows = sourceTimeRows(model);
  const vt = verdictTone(model);

  const defaultAside = `
    ${riskGauge(riskScore, regime ? `市场状态：${regime}` : '市场风险评分')}
    <div class="source-stack">
      ${rows.map((row) => `<div class="source-row">
        <strong>${escapeHtml(row.label)}</strong>
        <span class="num">${escapeHtml(row.value)}</span>
        <small>${escapeHtml(row.note)}</small>
      </div>`).join('')}
    </div>`;

  return `<section class="hero">
    <div class="hero-main">
      <div class="hero-kicker">
        ${verdictPill(vt.text, vt.tone)}
        <span class="eyebrow">${escapeHtml(eyebrow)}</span>
      </div>
      <h2 class="hero-title">${escapeHtml(title)}</h2>
      ${subtitle ? `<p class="hero-sub">${escapeHtml(subtitle)}</p>` : ''}
      ${opts.bodyHtml || ''}
    </div>
    <aside class="hero-aside">${opts.asideHtml != null ? opts.asideHtml : defaultAside}</aside>
  </section>`;
}

function navHtml(activeNavKey) {
  return NAV.map((item) => `<a class="side-link${item.key === activeNavKey ? ' active' : ''}" href="${escapeHtml(item.href)}"${item.key === activeNavKey ? ' aria-current="page"' : ''}>
      <strong>${escapeHtml(item.label)}</strong>
      <span>${escapeHtml(item.desc)}</span>
    </a>`).join('');
}

// 整页骨架。签名与 v2 兼容：renderShell(viewKey, model, bodyHtml)。
// 结构：跳转链接 → 侧导航（≥1100px）/顶部横滚导航（窄屏）→ 抬升式顶栏（标题 + 数据日期徽章 + 主题切换）
//      → 过期横幅 → main（缺失提示 + 视图内容）→ 免责页脚。
export function renderShell(viewKey, model, bodyHtml) {
  const meta = VIEW_META[viewKey] || VIEW_META.dashboard;
  const manifest = (model && model.runManifest) || {};

  return `<div class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"><span></span></div>
        <div class="brand-name">
          <strong>A股智能选股系统</strong>
          <span>量化选股研究记录</span>
        </div>
      </div>
      <nav class="side-nav" aria-label="主导航">
        ${navHtml(meta.navKey)}
      </nav>
    </aside>
    <div class="content">
      <header class="topbar">
        <div class="topbar-inner">
          <h1 class="page-title">${escapeHtml(meta.title)}</h1>
          <div class="topbar-actions">
            ${dateBadge(manifest.trade_date)}
            ${themeToggle('topbar-theme')}
          </div>
        </div>
      </header>
      ${renderStalenessBanner(model || {})}
      <main id="main-content" class="main">
        ${renderMissingNotice(model || {})}
        ${bodyHtml}
      </main>
      ${disclaimerFooter()}
    </div>
  </div>`;
}
