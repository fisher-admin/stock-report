import { loadWorkbenchModel, escapeHtml, safeText, formatPct, formatNumber } from './stock-data-hub.js';

const VIEW_META = {
  dashboard: {
    title: '交易作战台',
    subtitle: '把“能不能出手、先看什么、接下来做什么”压缩成一个工作流。'
  },
  market: {
    title: '盘前/盘中市场层',
    subtitle: '先判断环境，再决定是否进入执行层。'
  },
  strategy: {
    title: '启动前夕策略层',
    subtitle: '只保留 prebreakout_v41，其他策略沉到研究层。'
  },
  candidates: {
    title: '执行候选层',
    subtitle: '名单不是结果，最终动作以 final_candidate_action 为准。'
  },
  review: {
    title: '盘后复盘层',
    subtitle: '只复盘对下一交易日有用的收益、偏差与重复样本。'
  },
  research: {
    title: '研究与健康层',
    subtitle: '系统健康、研究线、验证线统一沉到后台研究面板。'
  },
  marketHeatmap: {
    title: '全市场行业热力',
    subtitle: '把市场热度矩阵压成执行辅助证据，而不是单独的主舞台。'
  },
  strategyHeatmap: {
    title: '启动前夕行业热力',
    subtitle: '只看 prebreakout_v41 的行业聚焦与表现漂移。'
  },
  industryActions: {
    title: '统一行业动作',
    subtitle: '把 market_only / overlap / strategy_only 行业动作聚合成一张动作指挥板。'
  }
};

const NAV = [
  { key: 'dashboard', href: './index.html', label: '交易作战台', note: '一屏看结论 / 风险 / 执行' },
  { key: 'market', href: './market-overview.html', label: '市场层', note: '环境、指数、行业主线' },
  { key: 'strategy', href: './strategy-vs-market.html', label: '策略层', note: '只看启动前夕策略' },
  { key: 'candidates', href: './decision-candidates.html', label: '执行候选层', note: '主攻 / 观察 / 回避' },
  { key: 'review', href: './recommendation-review.html', label: '盘后复盘层', note: '收益、样本、偏差' },
  { key: 'research', href: './research-lab.html', label: '研究与健康层', note: '验证、研究线、系统噪音' }
];

const DEEP_LINKS = [
  { href: './industry-heatmap.html', label: '策略行业热力' },
  { href: './market-industry-heatmap.html', label: '全市场行业热力' },
  { href: './industry-compare.html', label: '统一行业动作' }
];

function pill(label, value, tone = 'info') {
  return `<div class="pill ${tone}"><span class="pill-dot"></span><span>${escapeHtml(label)}：${escapeHtml(value)}</span></div>`;
}

function badge(text, tone = 'info') {
  return `<span class="badge ${tone}">${escapeHtml(text)}</span>`;
}

function bar(value, tone = 'info') {
  const width = Math.max(4, Math.min(100, Number(value) || 0));
  return `<div class="bar-track"><div class="bar-fill ${tone}" style="width:${width}%"></div></div>`;
}

function renderShell(viewKey, model, inner) {
  const meta = VIEW_META[viewKey];
  const navHtml = NAV.map((item) => `
    <a class="side-link ${item.key === viewKey ? 'active' : ''}" href="${item.href}">
      <strong>${item.label}</strong>
      <span>${item.note}</span>
    </a>
  `).join('');
  const workflowHtml = model.workflow.map((item) => `
    <a class="workflow-link ${item.id === viewKey ? 'active' : ''}" href="#${item.id}">
      <strong>${item.label}</strong>
      <span>${item.summary}</span>
    </a>
  `).join('');
  const deepLinks = DEEP_LINKS.map((item) => `<a class="side-link" href="${item.href}"><strong>${item.label}</strong><span>深钻分析页</span></a>`).join('');
  const topbar = [
    pill('交易日', safeText(model.runManifest.trade_date), 'info'),
    pill('会话阶段', safeText(model.sessionMode.label), 'info'),
    pill('最终裁决', safeText(model.verdict.label), model.verdictTone),
    pill('发布状态', model.runManifest.publish_ready ? '闭环完成' : model.runManifest.published ? '已写发布仓但未闭环' : '未发布', model.runManifest.publish_ready ? 'pass' : model.runManifest.published ? 'warn' : 'fail')
  ].join('');

  return `
    <div class="app-shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-kicker"><span class="brand-dot"></span>Fisher Stock Workbench</div>
          <h1>执行优先，不看噪音</h1>
          <p>前端只围绕“启动前夕策略 + 市场环境 + 执行动作”组织。研究线保留，但沉到底层。</p>
        </div>
        <div class="side-group">
          <div class="side-label">工作流导航</div>
          ${navHtml}
        </div>
        <div class="side-group">
          <div class="side-label">四级闸门</div>
          <div class="workflow-tree">${workflowHtml}</div>
        </div>
        <div class="side-group">
          <div class="side-label">深钻工具</div>
          ${deepLinks}
        </div>
        <div class="sidebar-foot">
          <div class="meta-title">当前最关键</div>
          <div class="metric-value">${escapeHtml(safeText(model.verdict.label))}</div>
          <div class="metric-note">${escapeHtml(safeText(model.verdict.summary))}</div>
        </div>
      </aside>
      <div class="content">
        <div class="topbar">
          <div class="topbar-inner">
            <div class="page-title">
              <h2>${meta.title}</h2>
              <p>${meta.subtitle}</p>
            </div>
            <div class="header-pills">${topbar}</div>
          </div>
        </div>
        <main class="main">${inner}<div class="footer-note">前端唯一事实源：<code>data/latest/*.json</code> + <code>data/recommendation_analytics/*.json</code>。系统契约不改，执行层只做表现重排与降噪。</div></main>
      </div>
    </div>
  `;
}

function renderNoticeBlock(model) {
  const notices = [];
  const middayTradeDate = safeText(model.midday.trade_date, '');
  const decisionTradeDate = safeText(model.systemVerdict.dates?.decision_trade_date, '');
  if (middayTradeDate && decisionTradeDate && middayTradeDate !== decisionTradeDate) {
    notices.push(`
      <div class="notice warn">
        <div class="notice-icon">!</div>
        <div>
          <strong>盘中快照已陈旧</strong>
          <p>当前午盘文件还是 ${escapeHtml(middayTradeDate)}，决策主线已是 ${escapeHtml(decisionTradeDate)}。页面会把它降级为辅助证据，不会把旧午盘混成主信号。</p>
        </div>
      </div>
    `);
  }
  if (model.runManifest.published && !model.runManifest.publish_ready) {
    notices.push(`
      <div class="notice info">
        <div class="notice-icon">i</div>
        <div>
          <strong>发布仓已刷新，但全链路尚未闭环</strong>
          <p>当前 <code>published=true</code>、<code>publish_ready=false</code>。页面会明确区分“已写公开仓”和“验证闭环通过”，避免误判系统已完全完成。</p>
        </div>
      </div>
    `);
  }
  return notices.join('');
}

function renderHero(model, title, subtitle) {
  const primary = model.signalTier.primary;
  return `
    <section class="hero">
      <div>
        <span class="eyebrow">${escapeHtml(model.sessionMode.label)} · ${escapeHtml(safeText(model.runManifest.trade_date))}</span>
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(subtitle)}</p>
        <div class="metric-grid" style="margin-top:18px;">
          ${primary.map((item) => `
            <div class="metric">
              <div class="metric-title">${escapeHtml(item.label)}</div>
              <div class="metric-value">${escapeHtml(item.value)}</div>
              ${bar(item.label === '市场风险' ? Number(model.marketState.market_summary?.risk_score ?? model.marketState.morning?.risk_score ?? 0) : item.label === '执行名单' ? Number(model.runManifest.candidate_role_counts?.watch || 0) * 5 : item.label === '策略状态' ? 100 : model.workflow.filter((step) => step.tone === 'pass').length * 25, item.tone)}
              <div class="metric-note">${escapeHtml(item.note)}</div>
            </div>
          `).join('')}
        </div>
      </div>
      <div class="panel">
        <div class="panel-title">当前操作次序</div>
        <div class="kpi-row">
          ${model.timeBlocks.map((block) => `
            <div class="kpi-item">
              <span>${escapeHtml(block.label)}</span>
              <strong>${escapeHtml(block.title)}</strong>
            </div>
          `).join('')}
        </div>
        <div class="help-text" style="margin-top:12px;">${escapeHtml(model.sessionMode.summary)}</div>
      </div>
    </section>
  `;
}

function renderWorkflow(model) {
  return `
    <div class="section-head">
      <div>
        <h3>四级闸门</h3>
        <p>首要决策信号只来自 freshness → market → strategy → candidate 四层，不再让表格抢结论。</p>
      </div>
    </div>
    <div class="workflow-strip">
      ${model.workflow.map((item) => `
        <section id="${item.id}" class="timeline-card ${item.tone}">
          <div class="panel-title">${escapeHtml(item.label)}</div>
          ${badge(item.tone === 'pass' ? '通过' : item.tone === 'warn' ? '警告' : '阻断', item.tone)}
          <h4>${escapeHtml(item.summary)}</h4>
          <p>${escapeHtml(item.id === 'candidate' ? '只有主攻名单才进入执行层；观察名单只保留看板。' : '该层通过后才允许下钻到下一层。')}</p>
        </section>
      `).join('')}
    </div>
  `;
}

function renderTimeBlocks(model) {
  return `
    <div class="section-head">
      <div>
        <h3>同一首页覆盖三段工作节奏</h3>
        <p>开盘前看结论，盘中看主线与执行，盘后看偏差与回补，不再拆成互相割裂的孤页。</p>
      </div>
    </div>
    <div class="panel-grid">
      ${model.timeBlocks.map((block) => `
        <section class="panel">
          <div class="panel-title">${escapeHtml(block.label)}</div>
          <h4 style="margin:0 0 8px;font-size:22px;">${escapeHtml(block.title)}</h4>
          <p>${escapeHtml(block.note)}</p>
          <ul class="break-list">${block.bullets.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>
        </section>
      `).join('')}
    </div>
  `;
}

function renderSectorRows(items, limit = 6) {
  return (items || []).slice(0, limit).map((item) => `
    <div class="list-row">
      <div class="rank-dot">${escapeHtml(formatNumber(item.market_rank || item.strategy_rank || 0))}</div>
      <div>
        <h5>${escapeHtml(item.industry || item.industry_name || '未标注')}</h5>
        <p>${escapeHtml(item.action_summary || item.trend_signal || '无额外说明')}</p>
      </div>
      <div>
        ${badge(item.action || item.trend_signal || '观察', item.action === '增配' ? 'pass' : item.action === '回避' ? 'fail' : 'warn')}
        <div class="help-text">热度 ${formatPct((item.market_heat || item.strategy_heat || 0) * 100, 1)}</div>
      </div>
    </div>
  `).join('');
}

function renderHeatmapRows(items, type = 'market') {
  return (items || []).slice(0, 12).map((item, idx) => `
    <tr>
      <td>${idx + 1}</td>
      <td>${escapeHtml(item.industry_name || item.sector_name || '未标注')}</td>
      <td>${formatPct(((type === 'market' ? item.market_heat_ema_5 : item.heat_ema_5) || 0) * 100, 1)}</td>
      <td>${formatPct(type === 'market' ? item.avg_pct_chg : item.avg_next_day_return_pct, 2)}</td>
      <td>${escapeHtml(item.trend_signal || '—')}</td>
      <td>${formatNumber(type === 'market' ? item.stock_count : item.recommendation_count || 0)}</td>
    </tr>
  `).join('');
}

function renderCandidateList(items, limit = 8) {
  return items.slice(0, limit).map((item) => `
    <div class="list-row" data-role="${escapeHtml(item.finalAction)}">
      <div class="rank-dot">${escapeHtml(formatNumber(item.displayRank))}</div>
      <div>
        <h5>${escapeHtml(item.name)} <span class="soft">${escapeHtml(item.code)}</span></h5>
        <p>${escapeHtml(item.industry_name || '未标注行业')}｜${escapeHtml(item.ai_advice || item.ai_conclusion || '暂无行动建议')}</p>
        <p style="margin-top:6px;">${escapeHtml(item.ai_summary || item.ai_points || '暂无 AI 摘要')}</p>
      </div>
      <div>
        ${badge(item.finalAction === 'main' ? '主攻' : item.finalAction === 'avoid' ? '回避' : '观察', item.finalAction === 'main' ? 'pass' : item.finalAction === 'avoid' ? 'fail' : 'warn')}
        <div class="help-text">AI ${formatNumber(item.ai_score || 0)}｜置信 ${escapeHtml(item.ai_confidence || '—')}</div>
      </div>
    </div>
  `).join('');
}

function renderDashboard(model) {
  const watchCandidates = model.candidates.filter((item) => item.finalAction !== 'avoid');
  return renderShell('dashboard', model, `
    ${renderNoticeBlock(model)}
    ${renderHero(model, safeText(model.verdict.label, '结论缺失'), safeText(model.verdict.summary, '当前总控台仅展示事实层，不对缺失结论做脑补。'))}
    ${renderWorkflow(model)}
    ${renderTimeBlocks(model)}
    <div class="section-head">
      <div>
        <h3>首屏只保留决策员真正要看的两件事</h3>
        <p>左边看执行名单，右边看市场/板块，不再让冗长说明抢占注意力。</p>
      </div>
    </div>
    <div class="watch-grid">
      <section class="panel">
        <div class="section-head" style="margin:0 0 12px;">
          <div>
            <h3 style="font-size:18px;">执行工作台</h3>
            <p>观察名单不再伪装成买点，只按最终动作分层。</p>
          </div>
        </div>
        <div class="mini-grid">
          <div class="mini-card"><strong>${formatNumber(model.runManifest.candidate_role_counts?.main || 0)}</strong><span>主攻</span></div>
          <div class="mini-card"><strong>${formatNumber(model.runManifest.candidate_role_counts?.watch || 0)}</strong><span>观察</span></div>
          <div class="mini-card"><strong>${formatNumber(model.runManifest.candidate_role_counts?.avoid || 0)}</strong><span>回避</span></div>
          <div class="mini-card"><strong>${formatNumber(model.candidates.length)}</strong><span>候选总数</span></div>
        </div>
        <div class="list" style="margin-top:14px;">${renderCandidateList(watchCandidates, 6)}</div>
      </section>
      <section class="panel">
        <div class="section-head" style="margin:0 0 12px;">
          <div>
            <h3 style="font-size:18px;">市场主线</h3>
            <p>行业热力是执行辅助证据，不再单独占一页抢占首屏。</p>
          </div>
        </div>
        <div class="list">${renderSectorRows(model.industryActions, 6)}</div>
      </section>
    </div>
  `);
}

function renderMarket(model) {
  const indices = Object.values(model.marketState.session_snapshot || {}).slice(0, 8);
  return renderShell('market', model, `
    ${renderNoticeBlock(model)}
    ${renderHero(model, `${safeText(model.marketState.market_summary?.market_regime || model.marketState.morning?.regime)}｜风险 ${formatNumber(model.marketState.market_summary?.risk_score ?? model.marketState.morning?.risk_score ?? 0)}/100`, safeText(model.marketState.morning?.ai_action_advice || model.marketState.midday?.midday_action_advice || '环境层暂无新的行动建议。'))}
    ${renderWorkflow(model)}
    <div class="section-head">
      <div>
        <h3>环境快照</h3>
        <p>指数、重点板块、行业动作合并进同一工作面，避免来回跳页。</p>
      </div>
    </div>
    <div class="market-grid">
      <section class="panel">
        <div class="panel-title">指数/外盘/汇率快照</div>
        <div class="candidate-grid">
          ${indices.map((item) => `
            <div class="stat-card">
              <div class="panel-title">${escapeHtml(item.label)}</div>
              <div class="stat-value">${formatNumber(item.close, 2)}</div>
              <div class="stat-note">涨跌 ${formatPct(item.change_pct, 2)}｜源 ${escapeHtml(item.provider)}</div>
            </div>
          `).join('')}
        </div>
      </section>
      <section class="panel">
        <div class="panel-title">今日主线行业</div>
        <div class="list">${renderSectorRows(model.marketSectors, 6)}</div>
      </section>
    </div>
    <div class="panel-grid" style="margin-top:16px;">
      <section class="table-wrap">
        <div class="panel-title">市场层首要证据</div>
        <table>
          <thead><tr><th>字段</th><th>值</th><th>用途</th></tr></thead>
          <tbody>
            <tr><td>regime</td><td>${escapeHtml(safeText(model.marketState.market_summary?.market_regime || model.marketState.morning?.regime))}</td><td>决定是否允许进入策略与候选层</td></tr>
            <tr><td>risk_score</td><td>${formatNumber(model.marketState.market_summary?.risk_score ?? model.marketState.morning?.risk_score ?? 0)}</td><td>定义风险温度，不再埋进长文</td></tr>
            <tr><td>focus_sectors</td><td>${escapeHtml((model.marketState.morning?.focus_sectors || []).join(' / '))}</td><td>盘前主观察清单</td></tr>
            <tr><td>industry_actions</td><td>${formatNumber(model.industryActions.length)}</td><td>告诉交易员哪些板块该增配/观察/回避</td></tr>
            <tr><td>midday_source</td><td>${escapeHtml(safeText(model.marketState.midday_source))}</td><td>盘中证据是否可用、是否陈旧</td></tr>
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-title">环境层降噪原则</div>
        <ul class="break-list">
          <li>首要信号：<code>regime</code>、<code>risk_score</code>、<code>focus_sectors</code>、<code>industry_actions</code>。</li>
          <li>辅助证据：指数快照、A50/金龙、午盘摘要。</li>
          <li>系统噪音：把多个时间点的长文说明平铺到首屏，或者把旧午盘直接混成今天主结论。</li>
        </ul>
      </section>
    </div>
  `);
}

function renderStrategy(model) {
  const strategy = model.strategy || {};
  const summary = strategy.summary || {};
  const samples = strategy.sample_candidates || [];
  return renderShell('strategy', model, `
    ${renderHero(model, safeText(strategy.strategy_name || '启动前夕策略'), strategy.activation === 'active' ? '当前执行层只保留这一条主策略，其他研究线全部沉到研究页。' : '策略未激活时，前端只保留说明，不强行渲染名单。')}
    ${renderWorkflow(model)}
    <div class="section-head">
      <div>
        <h3>策略信号板</h3>
        <p>这页不再展示多策略横向堆表，只把启动前夕策略拆成可执行证据。</p>
      </div>
    </div>
    <div class="strategy-grid">
      <section class="panel">
        <div class="panel-title">策略状态</div>
        <div class="mini-grid">
          <div class="mini-card"><strong>${escapeHtml(safeText(strategy.activation))}</strong><span>激活状态</span></div>
          <div class="mini-card"><strong>${formatNumber(strategy.market_overlap_count || 0)}</strong><span>市场重合行业</span></div>
          <div class="mini-card"><strong>${formatNumber(strategy.top20_count || 0)}</strong><span>候选池规模</span></div>
          <div class="mini-card"><strong>${formatNumber(strategy.avg_ai_score || 0, 1)}</strong><span>平均 AI 分</span></div>
        </div>
        <div class="kpi-row" style="margin-top:14px;">
          <div class="kpi-item"><span>年化收益</span><strong>${formatPct(summary.ann_return, 2)}</strong></div>
          <div class="kpi-item"><span>夏普</span><strong>${formatNumber(summary.sharpe, 3)}</strong></div>
          <div class="kpi-item"><span>胜率</span><strong>${formatPct(summary.win_rate, 1)}</strong></div>
          <div class="kpi-item"><span>最大回撤</span><strong>${formatPct(summary.max_drawdown, 2)}</strong></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-title">策略只关心的行业与样本</div>
        <div class="help-text">Top 行业：${escapeHtml((strategy.top_industries || []).join(' / '))}</div>
        <div class="list" style="margin-top:12px;">
          ${samples.map((item, idx) => `
            <div class="list-row">
              <div class="rank-dot">${idx + 1}</div>
              <div>
                <h5>${escapeHtml(item.name)} <span class="soft">${escapeHtml(item.code)}</span></h5>
                <p>${escapeHtml(item.industry_name || '未标注')}｜${escapeHtml(item.ai_advice || '暂无建议')}</p>
              </div>
              <div>
                ${badge(`AI ${formatNumber(item.ai_score || 0)}`, 'info')}
                <div class="help-text">涨跌 ${formatPct(item.current_change_pct, 2)}</div>
              </div>
            </div>
          `).join('')}
        </div>
      </section>
    </div>
    <div class="panel-grid" style="margin-top:16px;">
      <section class="panel">
        <div class="panel-title">首要决策信号</div>
        <ul class="break-list">
          <li><strong>Primary Signal：</strong>activation、market_overlap_count、candidate_role_counts。</li>
          <li><strong>Secondary Evidence：</strong>summary 收益特征、top_industries、样本票 AI 建议。</li>
          <li><strong>System Noise：</strong>四策略并排炫表、把研究线指标塞进执行页面。</li>
        </ul>
      </section>
      <section class="panel">
        <div class="panel-title">策略层实施原则</div>
        <ul class="break-list">
          <li>只保留 <code>prebreakout_v41</code> 作为执行层主策略。</li>
          <li>其他策略结果保留在研究页，默认不进入首页和策略层。</li>
          <li>执行层页面不再从多策略数组里“挑着看”，而是直接绑定启动前夕策略。</li>
        </ul>
      </section>
    </div>
  `);
}

function renderCandidates(model) {
  const candidates = model.candidates;
  const counts = candidates.reduce((acc, item) => {
    const key = item.finalAction || 'watch';
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, { all: candidates.length });
  const confidenceCounts = candidates.reduce((acc, item) => {
    const key = item.ai_confidence || '未标注';
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  return renderShell('candidates', model, `
    ${renderHero(model, `执行层：主攻 ${formatNumber(counts.main || 0)} / 观察 ${formatNumber(counts.watch || 0)}`, '这里只显示最终动作，不再把“候选池”和“可执行池”混成一张表。')}
    <div class="section-head">
      <div>
        <h3>候选工作流</h3>
        <p>用最终动作驱动列表。没有主攻时，观察名单只作为追踪对象。</p>
      </div>
    </div>
    <div class="exec-grid">
      <section class="panel">
        <div class="filter-bar">
          <button class="filter-btn active" data-filter="all">全部 ${formatNumber(candidates.length)}</button>
          <button class="filter-btn" data-filter="main">主攻 ${formatNumber(counts.main || 0)}</button>
          <button class="filter-btn" data-filter="watch">观察 ${formatNumber(counts.watch || 0)}</button>
          <button class="filter-btn" data-filter="avoid">回避 ${formatNumber(counts.avoid || 0)}</button>
        </div>
        <div class="candidate-grid">
          ${candidates.map((item) => `
            <article class="candidate-card" data-role="${escapeHtml(item.finalAction)}">
              <div class="topline">
                <div>
                  <div class="panel-title">#${escapeHtml(formatNumber(item.displayRank))} · ${escapeHtml(item.industry_name || '未标注行业')}</div>
                  <h4>${escapeHtml(item.name)} <span class="soft">${escapeHtml(item.code)}</span></h4>
                </div>
                ${badge(item.finalAction === 'main' ? '主攻' : item.finalAction === 'avoid' ? '回避' : '观察', item.finalAction === 'main' ? 'pass' : item.finalAction === 'avoid' ? 'fail' : 'warn')}
              </div>
              <p>${escapeHtml(item.ai_summary || item.ai_conclusion || item.ai_points || '暂无 AI 摘要')}</p>
              <div class="value-pair"><span>AI 分</span><strong>${formatNumber(item.ai_score || 0)}</strong></div>
              <div class="value-pair"><span>置信度</span><strong>${escapeHtml(item.ai_confidence || '—')}</strong></div>
              <div class="value-pair"><span>现价 / 涨跌</span><strong>${formatNumber(item.current_price || item.price || item.close, 2)} / ${formatPct(item.current_change_pct ?? item.change_pct, 2)}</strong></div>
              <div class="value-pair"><span>建议</span><strong>${escapeHtml(item.ai_advice || '暂无')}</strong></div>
            </article>
          `).join('')}
        </div>
      </section>
      <section class="panel">
        <div class="panel-title">执行层规则</div>
        <ul class="break-list">
          <li>Primary Signal：<code>final_candidate_action</code>、主攻/观察/回避计数。</li>
          <li>Secondary Evidence：AI 分、置信度、行业、摘要、当前涨跌。</li>
          <li>System Noise：把所有候选按原始 rank 平铺而不区分可执行级别。</li>
        </ul>
        <div class="panel-title" style="margin-top:18px;">置信度分布</div>
        <div class="list">
          ${Object.entries(confidenceCounts).map(([key, value]) => `
            <div class="list-row">
              <div class="rank-dot">${escapeHtml(key.slice(0, 1))}</div>
              <div><h5>${escapeHtml(key)}</h5><p>当前执行层中的 AI 置信度数量</p></div>
              <div><strong>${formatNumber(value)}</strong></div>
            </div>
          `).join('')}
        </div>
      </section>
    </div>
  `);
}

function renderReview(model) {
  const perf = model.reviewState.performance || {};
  const aiViews = model.reviewState.ai_view_stats || [];
  return renderShell('review', model, `
    ${renderHero(model, '盘后复盘只保留可转化为明日动作的结果', `命中率 ${formatPct(perf.next_day_hit_rate_pct, 2)}｜平均次日收益 ${formatPct(perf.avg_next_day_return_pct, 2)}｜累计收益 ${formatPct(perf.avg_cumulative_return_pct, 2)}`)}
    <div class="review-grid">
      <section class="table-wrap">
        <div class="panel-title">AI 视角 vs 结果偏差</div>
        <table>
          <thead><tr><th>AI视角</th><th>推荐数</th><th>次日收益</th><th>累计收益</th><th>平均AI分</th></tr></thead>
          <tbody>
            ${aiViews.slice(0, 10).map((item) => `
              <tr>
                <td>${escapeHtml(item.ai_view)}</td>
                <td>${formatNumber(item.recommendation_count || 0)}</td>
                <td>${formatPct(item.avg_next_day_return_pct, 2)}</td>
                <td>${formatPct(item.avg_cumulative_return_pct, 2)}</td>
                <td>${item.avg_ai_score == null ? '—' : formatNumber(item.avg_ai_score, 2)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-title">重复出现个股</div>
        <div class="list">
          ${(model.reviewLeaders || []).slice(0, 8).map((item, idx) => `
            <div class="list-row">
              <div class="rank-dot">${idx + 1}</div>
              <div>
                <h5>${escapeHtml(item.stock_name || item.name || item.code)}</h5>
                <p>${escapeHtml(item.stock_code || item.code || '')}</p>
              </div>
              <div><strong>${formatNumber(item.recommend_count || item.cumulative_recommend_count || 0)}</strong></div>
            </div>
          `).join('')}
        </div>
      </section>
    </div>
    <div class="panel-grid" style="margin-top:16px;">
      <section class="table-wrap">
        <div class="panel-title">最新样本</div>
        <table>
          <thead><tr><th>日期</th><th>股票</th><th>AI视角</th><th>次日收益</th><th>累计收益</th></tr></thead>
          <tbody>
            ${(model.reviewSamples || []).slice(0, 10).map((item) => `
              <tr>
                <td>${escapeHtml(item.recommend_date || item.trade_date || '')}</td>
                <td>${escapeHtml(item.stock_name || item.name || '')} <span class="soft">${escapeHtml(item.stock_code || item.code || '')}</span></td>
                <td>${escapeHtml(item.ai_view || '—')}</td>
                <td>${formatPct(item.next_day_return_pct, 2)}</td>
                <td>${formatPct(item.cumulative_return_pct, 2)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-title">盘后层原则</div>
        <ul class="break-list">
          <li>Primary Signal：命中率、次日收益、重复出现个股。</li>
          <li>Secondary Evidence：AI 视角分布、行业统计、样本明细。</li>
          <li>System Noise：所有历史字段一次性平铺到首屏，导致无法直接抽出下一日要改什么。</li>
        </ul>
      </section>
    </div>
  `);
}

function renderResearch(model) {
  const warnings = model.researchState.warnings || [];
  const validation = model.researchState.validation || {};
  return renderShell('research', model, `
    ${renderHero(model, '研究与健康层默认后置，不抢执行层注意力', '系统健康、研究线、验证线统一沉到底部，只在需要排障/复盘时展开。')}
    <div class="section-head">
      <div>
        <h3>研究与健康概览</h3>
        <p>研究层只承载“为什么没闭环、下一轮该修什么、研究线在做什么”。</p>
      </div>
    </div>
    <div class="research-grid">
      ${model.researchCards.map((card) => `
        <section class="stat-card">
          <div class="panel-title">${escapeHtml(card.title)}</div>
          <div class="stat-value">${escapeHtml(card.value)}</div>
          ${bar(card.tone === 'pass' ? 100 : card.tone === 'warn' ? 62 : 35, card.tone)}
          <div class="stat-note">${escapeHtml(card.note)}</div>
        </section>
      `).join('')}
    </div>
    <div class="panel-grid" style="margin-top:16px;">
      <section class="table-wrap">
        <div class="panel-title">验证闭环</div>
        <table>
          <thead><tr><th>字段</th><th>值</th><th>意义</th></tr></thead>
          <tbody>
            <tr><td>ok</td><td>${escapeHtml(safeText(validation.ok))}</td><td>整体验证是否通过</td></tr>
            <tr><td>ai_complete</td><td>${escapeHtml(safeText(validation.ai_complete))}</td><td>20/20 AI 字段是否补满</td></tr>
            <tr><td>publish_ready</td><td>${escapeHtml(safeText(validation.publish_ready))}</td><td>是否允许声明发布闭环完成</td></tr>
            <tr><td>retained_strategies</td><td>${escapeHtml((validation.retained_strategies || []).join(' / '))}</td><td>当前研究层仍保留的后台策略</td></tr>
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-title">当前警告 / 噪音</div>
        <ul class="break-list">
          ${(warnings.length ? warnings : ['当前 research_state 没有额外 warnings。']).map((item) => `<li>${escapeHtml(typeof item === 'string' ? item : JSON.stringify(item))}</li>`).join('')}
        </ul>
      </section>
    </div>
  `);
}

function renderMarketHeatmap(model) {
  const rows = model.marketHeatmapLatestRows || [];
  const doc = model.marketHeatmap || {};
  return renderShell('marketHeatmap', model, `
    ${renderHero(model, `全市场行业热力｜${safeText(doc.latest_trade_date)}`, '这页只做主线行业的深钻验证，不再承担首页结论职责。')}
    <div class="panel-grid">
      <section class="panel">
        <div class="panel-title">最新 Top 行业</div>
        <div class="list">${renderSectorRows(rows.map((item) => ({ industry: item.industry_name, trend_signal: item.trend_signal, market_heat: item.market_heat_ema_5, market_rank: item.market_heat_rank, action_summary: `平均涨跌 ${formatPct(item.avg_pct_chg, 2)}｜上涨占比 ${formatPct((item.up_ratio || 0) * 100, 1)}` })), 8)}</div>
      </section>
      <section class="table-wrap">
        <div class="panel-title">热力矩阵（最新交易日）</div>
        <table>
          <thead><tr><th>#</th><th>行业</th><th>热度</th><th>平均涨跌</th><th>趋势</th><th>股票数</th></tr></thead>
          <tbody>${renderHeatmapRows(rows, 'market')}</tbody>
        </table>
      </section>
    </div>
  `);
}

function renderStrategyHeatmap(model) {
  const rows = model.strategyHeatmapLatestRows || [];
  const doc = model.strategyHeatmap || {};
  return renderShell('strategyHeatmap', model, `
    ${renderHero(model, `启动前夕行业热力｜${safeText(doc.latest_recommend_date)}`, '这里只解释启动前夕策略最近一轮主要集中在哪些行业，以及这些行业的表现漂移。')}
    <div class="panel-grid">
      <section class="panel">
        <div class="panel-title">策略聚焦行业</div>
        <div class="list">${renderSectorRows(rows.map((item) => ({ industry: item.sector_name, trend_signal: item.trend_signal, strategy_heat: item.heat_ema_5, strategy_rank: item.heat_rank, action_summary: `次日收益 ${formatPct(item.avg_next_day_return_pct, 2)}｜累计收益 ${formatPct(item.avg_cumulative_return_pct, 2)}` })), 8)}</div>
      </section>
      <section class="table-wrap">
        <div class="panel-title">策略热力矩阵（最新推荐日）</div>
        <table>
          <thead><tr><th>#</th><th>行业</th><th>热度</th><th>次日收益</th><th>趋势</th><th>推荐数</th></tr></thead>
          <tbody>${renderHeatmapRows(rows, 'strategy')}</tbody>
        </table>
      </section>
    </div>
  `);
}

function renderIndustryActions(model) {
  const items = model.unified.industry_actions || [];
  return renderShell('industryActions', model, `
    ${renderHero(model, '统一行业动作指挥板', '这里把市场热力、策略覆盖和行业动作合成一张执行辅助图，不再拆成多页对照。')}
    <div class="table-wrap">
      <div class="panel-title">行业动作总表</div>
      <table>
        <thead><tr><th>行业</th><th>类型</th><th>动作</th><th>热度</th><th>趋势</th><th>摘要</th></tr></thead>
        <tbody>
          ${items.slice(0, 20).map((item) => `
            <tr>
              <td>${escapeHtml(item.industry)}</td>
              <td>${escapeHtml(item.kind || '—')}</td>
              <td>${badge(item.action || '观察', item.action === '增配' ? 'pass' : item.action === '回避' ? 'fail' : 'warn')}</td>
              <td>${formatPct(((item.market_heat || item.strategy_heat || 0) * 100), 1)}</td>
              <td>${escapeHtml(item.trend_signal || '—')}</td>
              <td>${escapeHtml(item.action_summary || item.reason || '—')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `);
}

const renderers = {
  dashboard: renderDashboard,
  market: renderMarket,
  strategy: renderStrategy,
  candidates: renderCandidates,
  review: renderReview,
  research: renderResearch,
  marketHeatmap: renderMarketHeatmap,
  strategyHeatmap: renderStrategyHeatmap,
  industryActions: renderIndustryActions
};

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

async function main() {
  const root = document.getElementById('app');
  const viewKey = document.body.dataset.view || 'dashboard';
  const render = renderers[viewKey] || renderDashboard;
  root.innerHTML = '<div class="main"><section class="empty"><div class="panel-title">加载中</div><div class="metric-value">正在读取最新数据…</div></section></div>';
  try {
    const model = await loadWorkbenchModel();
    root.innerHTML = render(model);
    mountFilterHandlers(root);
  } catch (error) {
    root.innerHTML = `
      <main class="main">
        <section class="notice fail">
          <div class="notice-icon">×</div>
          <div>
            <strong>前端未能读取数据源</strong>
            <p>${escapeHtml(error.message || String(error))}</p>
          </div>
        </section>
      </main>
    `;
  }
}

main();
