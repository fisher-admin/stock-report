import { loadWorkbenchModel, escapeHtml, safeText, formatPct, formatNumber } from './stock-data-hub.js';

const VIEW_META = {
  dashboard: {
    title: '交易指南'
  },
  market: {
    title: '市场分析'
  },
  strategy: {
    title: '策略分析'
  },
  candidates: {
    title: '个股推荐'
  },
  review: {
    title: '复盘研究'
  },
  research: {
    title: '系统解码'
  },
  marketHeatmap: {
    title: '市场分析'
  },
  strategyHeatmap: {
    title: '策略分析'
  },
  industryActions: {
    title: '市场分析'
  }
};

const NAV = [
  { key: 'dashboard', href: './index.html', label: '交易指南' },
  { key: 'market', href: './market-overview.html', label: '市场分析' },
  { key: 'candidates', href: './decision-candidates.html', label: '个股推荐' },
  { key: 'review', href: './recommendation-review.html', label: '复盘研究' },
  { key: 'research', href: './research-lab.html', label: '系统解码' }
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

function actionLabel(action) {
  if (action === 'main') return '主攻';
  if (action === 'avoid') return '回避';
  return '观察';
}

function actionTone(action) {
  if (action === 'main') return 'pass';
  if (action === 'avoid') return 'fail';
  return 'warn';
}

function riskTone(score) {
  const value = Number(score) || 0;
  if (value >= 70) return 'fail';
  if (value >= 50) return 'warn';
  return 'pass';
}

function cleanAnalysisText(value, fallback = '暂无明确补充。') {
  const lines = safeText(value, '')
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/fallback|本地|正式模型|搜索链路|回填链路|兜底/.test(line));
  return lines.length ? lines.join('\n') : fallback;
}

function firstSentence(value, fallback = '暂无明确结论。') {
  const text = cleanAnalysisText(value, fallback).replace(/\s+/g, ' ');
  const match = text.match(/^(.{1,90}?[。；;]|.{1,90})/);
  return match ? match[1].trim() : fallback;
}

function extractLabel(text, label, fallback) {
  const source = cleanAnalysisText(text, '');
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escaped}\\s*([\\s\\S]*?)(?=板块定位：|筹码判断：|事件催化：|触发条件：|失效条件：|$)`));
  return match && match[1].trim() ? match[1].trim() : fallback;
}

function sourceTimeRows(model) {
  const morning = model.marketState.morning || {};
  const midday = model.midday || model.marketState.midday || {};
  const review = model.reviewState || {};
  return [
    {
      label: '晨判分析',
      value: safeText(morning.generated_at || model.runManifest.sources?.market_generated_at),
      note: `市场数据 ${safeText(morning.market_data_trade_date || model.marketState.latest_trade_date)}`
    },
    {
      label: '盘中分析',
      value: safeText(midday.as_of_time || midday.generated_at || model.runManifest.sources?.midday_generated_at),
      note: `快照日期 ${safeText(midday.trade_date || model.marketState.latest_trade_date)}`
    },
    {
      label: '盘后推荐',
      value: safeText(review.generated_at || model.runManifest.sources?.review_generated_at),
      note: `推荐日期 ${safeText(review.latest_recommend_date || model.runManifest.trade_date)}`
    }
  ];
}

function riskGauge(score, label) {
  const value = Math.max(0, Math.min(100, Number(score) || 0));
  return `
    <div class="risk-gauge ${riskTone(value)}" style="--risk:${value};">
      <div class="risk-gauge-core">
        <strong>${formatNumber(value)}</strong>
        <span>/ 100</span>
      </div>
      <div class="risk-gauge-label">${escapeHtml(label)}</div>
    </div>
  `;
}

function renderThemeToggle(extraClass = '') {
  return `
    <div class="theme-toggle ${extraClass}" role="group" aria-label="视觉模式">
      <span>视觉模式</span>
      <button type="button" data-theme-choice="light">白天</button>
      <button type="button" data-theme-choice="dark">黑夜</button>
    </div>
  `;
}

function stockAnchorId(item) {
  const code = safeText(item.normalized_code || item.stock_code || item.code || item.ts_code || item.name, 'stock')
    .replace(/[^a-zA-Z0-9_-]/g, '-');
  return `stock-${code}`;
}

function renderShell(viewKey, model, inner) {
  const meta = VIEW_META[viewKey];
  const navKey = viewKey === 'strategy' || viewKey === 'strategyHeatmap' ? 'research' : viewKey === 'marketHeatmap' || viewKey === 'industryActions' ? 'market' : viewKey;
  const navHtml = NAV.map((item) => `
    <a class="side-link ${item.key === navKey ? 'active' : ''}" href="${item.href}">
      <strong>${item.label}</strong>
    </a>
  `).join('');
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
          <div class="brand-lockup">
            <div class="brand-mark" aria-hidden="true"><span></span></div>
            <div class="brand-name">
              <strong>FISHER</strong>
              <span>STOCK</span>
            </div>
          </div>
        </div>
        <div class="side-group">
          ${navHtml}
        </div>
      </aside>
      <div class="content">
        <div class="topbar">
          <div class="topbar-inner">
            <div class="page-title">
              <h2>${meta.title}</h2>
            </div>
            <div class="topbar-actions">
              <div class="header-pills">${topbar}</div>
              ${renderThemeToggle('topbar-theme')}
            </div>
          </div>
        </div>
        <main class="main">${inner}</main>
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
  const riskScore = model.marketState.market_summary?.risk_score ?? model.marketState.morning?.risk_score ?? model.runManifest.risk_score ?? 0;
  const regime = safeText(model.marketState.market_summary?.market_regime || model.marketState.morning?.regime || model.runManifest.market_regime);
  const counts = model.displayCandidateCounts || model.runManifest.candidate_role_counts || {};
  const rows = sourceTimeRows(model);
  return `
    <section class="hero">
      <div class="hero-main">
        <span class="eyebrow">${escapeHtml(model.sessionMode.label)} · ${escapeHtml(safeText(model.runManifest.trade_date))}</span>
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(subtitle)}</p>
        <div class="decision-strip">
          <div><span>买入</span><strong>${formatNumber(counts.main || 0)}</strong></div>
          <div><span>观察</span><strong>${formatNumber(counts.watch || 0)}</strong></div>
          <div><span>回避</span><strong>${formatNumber(counts.avoid || 0)}</strong></div>
        </div>
      </div>
      <div class="hero-risk">
        ${riskGauge(riskScore, regime)}
        <div class="source-stack">
          ${rows.map((row) => `
            <div class="source-row">
              <strong>${escapeHtml(row.label)}</strong>
              <span>${escapeHtml(row.value)}</span>
              <small>${escapeHtml(row.note)}</small>
            </div>
          `).join('')}
        </div>
      </div>
    </section>
  `;
}

function renderWorkflow(model) {
  return `
    <div class="section-head">
      <div>
        <h3>四级闸门</h3>
      </div>
    </div>
    <div class="workflow-strip">
      ${model.workflow.map((item) => `
        <section id="${item.id}" class="timeline-card ${item.tone}">
          <div class="panel-title">${escapeHtml(item.label)}</div>
          ${badge(item.tone === 'pass' ? '通过' : item.tone === 'warn' ? '警告' : '阻断', item.tone)}
          <h4>${escapeHtml(item.summary)}</h4>
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
    <div class="list-row" data-role="${escapeHtml(item.displayAction)}">
      <div class="rank-dot">${escapeHtml(formatNumber(item.displayRank))}</div>
      <div>
        <h5>${escapeHtml(item.name)} <span class="soft">${escapeHtml(item.code)}</span></h5>
        <p>${escapeHtml(item.industry_name || '未标注行业')}｜${escapeHtml(item.ai_advice || item.ai_conclusion || '暂无行动建议')}</p>
        <p style="margin-top:6px;">${escapeHtml(firstSentence(item.ai_conclusion || item.ai_summary || item.ai_points))}</p>
      </div>
      <div>
        ${badge(actionLabel(item.displayAction), actionTone(item.displayAction))}
        <div class="help-text">AI ${formatNumber(item.ai_score || 0)}｜置信 ${escapeHtml(item.ai_confidence || '—')}</div>
      </div>
    </div>
  `).join('');
}

function renderCandidateTableRows(items) {
  return items.map((item) => `
    <tr>
      <td>${escapeHtml(formatNumber(item.displayRank))}</td>
      <td>
        <a class="stock-detail-link" href="./decision-candidates.html#${stockAnchorId(item)}">
          <strong>${escapeHtml(item.name)}</strong>
          <div class="soft">${escapeHtml(item.code)}</div>
        </a>
      </td>
      <td>${escapeHtml(item.industry_name || '未标注')}</td>
      <td>${badge(actionLabel(item.displayAction), actionTone(item.displayAction))}</td>
      <td>${escapeHtml(item.ai_advice || '暂无')}</td>
      <td>${formatNumber(item.ai_score || 0)}</td>
      <td>${formatNumber(item.current_price || item.price || item.close, 2)}</td>
      <td>${formatPct(item.current_change_pct ?? item.change_pct, 2)}</td>
    </tr>
  `).join('');
}

function renderCandidateDetailCard(item) {
  const action = item.displayAction || 'watch';
  const trigger = extractLabel(item.ai_points, '触发条件：', '等待放量确认或回踩关键支撑后再动作。');
  const invalidation = extractLabel(item.ai_points, '失效条件：', '跌破关键支撑或量价结构转弱。');
  const sector = extractLabel(item.ai_points, '板块定位：', item.industry_name ? `${item.industry_name} 板块内跟踪。` : '板块位置暂不明确。');
  const chip = extractLabel(item.ai_points, '筹码判断：', item.winner_rate == null ? '筹码数据不足，先按趋势确认。' : `获利盘 ${formatPct(item.winner_rate, 1)}，需结合放量确认。`);
  const catalyst = extractLabel(item.ai_points, '事件催化：', '暂无明确外部催化，优先看量价确认。');
  const trend = [
    cleanAnalysisText(item.ai_trend, ''),
    cleanAnalysisText(item.ai_ma, ''),
    cleanAnalysisText(item.ai_volume, '')
  ].filter(Boolean).join(' ');
  const riskItems = [
    ...(Array.isArray(item.ai_risks) ? item.ai_risks : []),
    item.ai_risk_warning
  ].map((risk) => cleanAnalysisText(risk, '')).filter(Boolean);

  return `
    <article id="${stockAnchorId(item)}" class="candidate-card detail-card" data-role="${escapeHtml(action)}">
      <div class="candidate-head">
        <div>
          <div class="panel-title">#${escapeHtml(formatNumber(item.displayRank))} · ${escapeHtml(item.industry_name || '未标注行业')}</div>
          <h4>${escapeHtml(item.name)} <span class="soft">${escapeHtml(item.code)}</span></h4>
        </div>
        <div class="candidate-actions">
          ${badge(actionLabel(action), actionTone(action))}
          ${badge(item.ai_advice || '暂无建议', actionTone(action))}
        </div>
      </div>
      <p class="candidate-summary">${escapeHtml(firstSentence(item.ai_conclusion || item.ai_summary || item.ai_points))}</p>
      <div class="candidate-metrics">
        <div><span>AI分</span><strong>${formatNumber(item.ai_score || 0)}</strong></div>
        <div><span>置信度</span><strong>${escapeHtml(item.ai_confidence || '—')}</strong></div>
        <div><span>现价</span><strong>${formatNumber(item.current_price || item.price || item.close, 2)}</strong></div>
        <div><span>涨跌</span><strong>${formatPct(item.current_change_pct ?? item.change_pct, 2)}</strong></div>
        <div><span>数据日</span><strong>${escapeHtml(item.current_price_trade_date || item.review_recommend_date || '—')}</strong></div>
      </div>
      <div class="analysis-grid">
        <div><span>触发条件</span><p>${escapeHtml(trigger)}</p></div>
        <div><span>失效条件</span><p>${escapeHtml(invalidation)}</p></div>
        <div><span>板块位置</span><p>${escapeHtml(sector)}</p></div>
        <div><span>筹码判断</span><p>${escapeHtml(chip)}</p></div>
        <div><span>事件催化</span><p>${escapeHtml(catalyst)}</p></div>
        <div><span>趋势量价</span><p>${escapeHtml(trend || '等待价格和量能继续确认。')}</p></div>
      </div>
      <div class="risk-line"><strong>主要风险</strong><span>${escapeHtml(riskItems.slice(0, 2).join('；') || '未发现新的明确风险，仍需按失效条件控制。')}</span></div>
    </article>
  `;
}

function renderDashboard(model) {
  const counts = model.displayCandidateCounts || {};
  const riskScore = model.marketState.market_summary?.risk_score ?? model.marketState.morning?.risk_score ?? model.runManifest.risk_score ?? 0;
  const verdictTitle = safeText(model.verdict.label, '结论缺失');
  const verdictText = verdictTitle === '可执行'
    ? '今日形成推荐清单，按买入、观察、回避分层阅读。'
    : safeText(model.verdict.summary, '当前只展示已发布事实。');
  return renderShell('dashboard', model, `
    ${renderNoticeBlock(model)}
    ${renderHero(model, verdictTitle, verdictText)}
    <div class="section-head">
      <div>
        <h3>今日结果</h3>
      </div>
    </div>
    <div class="result-grid">
      <section class="stat-card">
        <div class="panel-title">市场环境</div>
        <div class="stat-value">${escapeHtml(safeText(model.marketState.market_summary?.market_regime || model.marketState.morning?.regime || model.runManifest.market_regime))}</div>
        <div class="stat-note">风险 ${formatNumber(riskScore)} / 100</div>
      </section>
      <section class="stat-card">
        <div class="panel-title">个股推荐</div>
        <div class="stat-value">${formatNumber(model.candidates.length)}</div>
        <div class="stat-note">买入 ${formatNumber(counts.main || 0)}｜观察 ${formatNumber(counts.watch || 0)}｜回避 ${formatNumber(counts.avoid || 0)}</div>
      </section>
      <section class="stat-card">
        <div class="panel-title">发布状态</div>
        <div class="stat-value">${model.runManifest.publish_ready ? '已完成' : model.runManifest.published ? '已发布' : '待发布'}</div>
        <div class="stat-note">${escapeHtml(safeText(model.runManifest.generated_at))}</div>
      </section>
      <section class="stat-card">
        <div class="panel-title">数据日期</div>
        <div class="stat-value">${escapeHtml(safeText(model.runManifest.trade_date))}</div>
        <div class="stat-note">推荐日期 ${escapeHtml(safeText(model.reviewState.latest_recommend_date || model.runManifest.detail_latest_recommend_date))}</div>
      </section>
    </div>
    <div class="section-head">
      <div>
        <h3>今日20支个股</h3>
      </div>
    </div>
    <section class="table-wrap compact-table">
      <table>
        <thead><tr><th>#</th><th>股票</th><th>行业</th><th>动作</th><th>建议</th><th>AI分</th><th>现价</th><th>涨跌</th></tr></thead>
        <tbody>${renderCandidateTableRows(model.candidates)}</tbody>
      </table>
    </section>
  `);
}

function renderMarket(model) {
  const indices = Object.values(model.marketState.session_snapshot || model.midday.session_snapshot || {}).slice(0, 8);
  const sourceRows = sourceTimeRows(model);
  return renderShell('market', model, `
    ${renderNoticeBlock(model)}
    ${renderHero(model, `${safeText(model.marketState.market_summary?.market_regime || model.marketState.morning?.regime)}｜风险 ${formatNumber(model.marketState.market_summary?.risk_score ?? model.marketState.morning?.risk_score ?? 0)}/100`, safeText(model.marketState.morning?.ai_action_advice || model.marketState.midday?.midday_action_advice || '环境层暂无新的行动建议。'))}
    <div class="section-head">
      <div>
        <h3>数据来源</h3>
      </div>
    </div>
    <div class="result-grid">
      ${sourceRows.map((row) => `
        <section class="stat-card">
          <div class="panel-title">${escapeHtml(row.label)}</div>
          <div class="stat-value small">${escapeHtml(row.value)}</div>
          <div class="stat-note">${escapeHtml(row.note)}</div>
        </section>
      `).join('')}
      <section class="stat-card">
        <div class="panel-title">盘中来源</div>
        <div class="stat-value small">${escapeHtml(safeText(model.marketState.midday_source))}</div>
        <div class="stat-note">指数源见下方快照</div>
      </section>
    </div>
    <div class="section-head">
      <div>
        <h3>指数快照</h3>
      </div>
    </div>
    <section class="panel">
      <div class="candidate-grid">
        ${indices.map((item) => `
          <div class="stat-card flat">
            <div class="panel-title">${escapeHtml(item.label)}</div>
            <div class="stat-value">${formatNumber(item.close, 2)}</div>
            <div class="stat-note">${formatPct(item.change_pct, 2)}｜${escapeHtml(item.provider)}｜${escapeHtml(item.source_kind || 'exact')}</div>
          </div>
        `).join('')}
      </div>
    </section>
    <div class="section-head">
      <div>
        <h3>市场主线</h3>
      </div>
      <a class="text-link" href="./market-industry-heatmap.html">行业热力</a>
    </div>
    <div class="market-grid">
      <section class="panel">
        <div class="panel-title">今日主线行业</div>
        <div class="list">${renderSectorRows(model.marketSectors, 8)}</div>
      </section>
      <section class="panel">
        <div class="panel-title">行业动作</div>
        <div class="list">${renderSectorRows(model.industryActions, 8)}</div>
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
  const counts = model.displayCandidateCounts || { all: candidates.length };
  return renderShell('candidates', model, `
    ${renderHero(model, '20支个股', `买入 ${formatNumber(counts.main || 0)}｜观察 ${formatNumber(counts.watch || 0)}｜回避 ${formatNumber(counts.avoid || 0)}`)}
    <div class="section-head">
      <div>
        <h3>完整推荐清单</h3>
      </div>
    </div>
    <section class="panel">
      <div class="filter-bar">
        <button class="filter-btn active" data-filter="all">全部 ${formatNumber(candidates.length)}</button>
        <button class="filter-btn" data-filter="main">买入 ${formatNumber(counts.main || 0)}</button>
        <button class="filter-btn" data-filter="watch">观察 ${formatNumber(counts.watch || 0)}</button>
        <button class="filter-btn" data-filter="avoid">回避 ${formatNumber(counts.avoid || 0)}</button>
      </div>
      <div class="candidate-detail-list">
        ${candidates.map((item) => renderCandidateDetailCard(item)).join('')}
      </div>
    </section>
  `);
}

function reviewFilterKey(value) {
  const text = safeText(value, '');
  if (/买入|主攻|增配|介入/.test(text)) return 'buy';
  if (/持有/.test(text)) return 'hold';
  if (/观望|观察/.test(text)) return 'watch';
  if (/回避|剔除/.test(text)) return 'avoid';
  return 'other';
}

function optionalPct(value, digits = 2) {
  return value === null || value === undefined || value === '' ? '—' : formatPct(value, digits);
}

function reviewSampleCounts(samples) {
  return samples.reduce((acc, item) => {
    const key = reviewFilterKey(item.ai_view);
    acc.all += 1;
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, { all: 0, buy: 0, hold: 0, watch: 0, avoid: 0, other: 0 });
}

function renderReviewSampleRows(items) {
  return items.map((item) => {
    const role = reviewFilterKey(item.ai_view);
    return `
      <tr data-review-role="${escapeHtml(role)}">
        <td>${escapeHtml(item.recommend_date || item.trade_date || '')}</td>
        <td><strong>${escapeHtml(item.stock_name || item.name || '')}</strong><div class="soft">${escapeHtml(item.stock_code || item.code || '')}</div></td>
        <td>${escapeHtml(item.sector_name || item.industry_name || '未标注')}</td>
        <td>${escapeHtml(item.ai_view || '—')}</td>
        <td>${item.ai_score == null ? '—' : formatNumber(item.ai_score, 0)}</td>
        <td>${item.recommend_price == null ? '—' : formatNumber(item.recommend_price, 2)}</td>
        <td>${optionalPct(item.next_day_return_pct, 2)}</td>
        <td>${optionalPct(item.cumulative_return_pct, 2)}</td>
        <td>${formatNumber(item.cumulative_recommend_count || item.recommend_count || 0)}</td>
      </tr>
    `;
  }).join('');
}

function renderReview(model) {
  const perf = model.reviewState.performance || {};
  const aiViews = model.reviewState.ai_view_stats || [];
  const samples = model.reviewSamples || [];
  const sampleCounts = reviewSampleCounts(samples);
  return renderShell('review', model, `
    ${renderHero(model, '复盘结果', `命中率 ${formatPct(perf.next_day_hit_rate_pct, 2)}｜平均次日收益 ${formatPct(perf.avg_next_day_return_pct, 2)}｜累计收益 ${formatPct(perf.avg_cumulative_return_pct, 2)}`)}
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
    <section class="table-wrap review-sample-wide">
      <div class="sample-head">
        <div class="panel-title">最新样本</div>
        <div class="filter-bar review-filter">
          <button class="filter-btn active" data-review-filter="all">全部 ${formatNumber(sampleCounts.all)}</button>
          <button class="filter-btn" data-review-filter="buy">买入 ${formatNumber(sampleCounts.buy)}</button>
          <button class="filter-btn" data-review-filter="hold">持有 ${formatNumber(sampleCounts.hold)}</button>
          <button class="filter-btn" data-review-filter="watch">观望 ${formatNumber(sampleCounts.watch)}</button>
          <button class="filter-btn" data-review-filter="avoid">回避 ${formatNumber(sampleCounts.avoid)}</button>
          <button class="filter-btn" data-review-filter="other">其他 ${formatNumber(sampleCounts.other)}</button>
        </div>
      </div>
      <table class="review-sample-table">
        <thead><tr><th>日期</th><th>股票</th><th>行业</th><th>AI视角</th><th>AI分</th><th>推荐价</th><th>次日收益</th><th>累计收益</th><th>出现次数</th></tr></thead>
        <tbody>${renderReviewSampleRows(samples)}</tbody>
      </table>
    </section>
    <section class="panel review-principle">
      <div class="panel-title">盘后层原则</div>
      <ul class="break-list">
        <li>Primary Signal：命中率、次日收益、重复出现个股。</li>
        <li>Secondary Evidence：AI 视角分布、行业统计、样本明细。</li>
        <li>System Noise：所有历史字段一次性平铺到首屏，导致无法直接抽出下一日要改什么。</li>
      </ul>
    </section>
  `);
}

function renderResearch(model) {
  const warnings = model.researchState.warnings || [];
  const validation = model.researchState.validation || {};
  const strategy = model.strategy || {};
  const summary = strategy.summary || {};
  return renderShell('research', model, `
    ${renderHero(model, '策略、闸门、健康状态', '系统判断集中在这里查看。')}
    <div class="section-head">
      <div>
        <h3>策略分析</h3>
      </div>
    </div>
    <div class="strategy-grid">
      <section class="panel">
        <div class="panel-title">${escapeHtml(safeText(strategy.strategy_name || '启动前夕策略'))}</div>
        <div class="mini-grid">
          <div class="mini-card"><strong>${escapeHtml(safeText(strategy.activation))}</strong><span>状态</span></div>
          <div class="mini-card"><strong>${formatNumber(strategy.top20_count || 0)}</strong><span>候选</span></div>
          <div class="mini-card"><strong>${formatNumber(strategy.market_overlap_count || 0)}</strong><span>市场重合</span></div>
          <div class="mini-card"><strong>${formatNumber(strategy.avg_ai_score || 0, 1)}</strong><span>平均AI分</span></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-title">策略表现</div>
        <div class="kpi-row">
          <div class="kpi-item"><span>年化收益</span><strong>${formatPct(summary.ann_return, 2)}</strong></div>
          <div class="kpi-item"><span>胜率</span><strong>${formatPct(summary.win_rate, 1)}</strong></div>
          <div class="kpi-item"><span>最大回撤</span><strong>${formatPct(summary.max_drawdown, 2)}</strong></div>
          <div class="kpi-item"><span>重点行业</span><strong>${escapeHtml((strategy.top_industries || []).slice(0, 3).join(' / ') || '—')}</strong></div>
        </div>
      </section>
    </div>
    ${renderWorkflow(model)}
    <div class="section-head">
      <div>
        <h3>研究与健康</h3>
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

function applyTheme(theme) {
  const selected = theme === 'light' ? 'light' : 'dark';
  document.body.dataset.theme = selected;
  try {
    localStorage.setItem('stockReportTheme', selected);
  } catch (_) {
    // localStorage can be unavailable in restrictive browser contexts.
  }
  document.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.classList.toggle('active', button.getAttribute('data-theme-choice') === selected);
  });
}

function getInitialTheme() {
  try {
    const saved = localStorage.getItem('stockReportTheme');
    if (saved === 'light' || saved === 'dark') return saved;
  } catch (_) {
    // Ignore storage failures.
  }
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function mountThemeHandlers(root) {
  root.querySelectorAll('[data-theme-choice]').forEach((button) => {
    button.addEventListener('click', () => applyTheme(button.getAttribute('data-theme-choice')));
  });
  applyTheme(document.body.dataset.theme || getInitialTheme());
}

function scrollToHashTarget() {
  const hash = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '';
  if (!hash) return;
  const target = document.getElementById(hash);
  if (!target) return;
  requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
}

async function main() {
  const root = document.getElementById('app');
  const viewKey = document.body.dataset.view || 'dashboard';
  const render = renderers[viewKey] || renderDashboard;
  applyTheme(getInitialTheme());
  root.innerHTML = '<div class="main"><section class="empty"><div class="panel-title">加载中</div><div class="metric-value">正在读取最新数据…</div></section></div>';
  try {
    const model = await loadWorkbenchModel();
    root.innerHTML = render(model);
    mountFilterHandlers(root);
    mountReviewFilterHandlers(root);
    mountThemeHandlers(root);
    scrollToHashTarget();
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
