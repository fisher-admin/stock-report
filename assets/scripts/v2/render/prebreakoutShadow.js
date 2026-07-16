// v2/render/prebreakoutShadow.js — 启动前夕 · 因子二次加工厂观察页
// 数据：model.prebreakoutShadowWatch ← data/latest/prebreakout_shadow_watch.json
// 加工厂：实验假设 / 权重 delta / 回测对照生产 / 各实验与冠军当日 Top 名单

import {
  escapeHtml, safeText, formatNumber, pctHtml, dateCn
} from './format.js';
import {
  badge, sectionHead, missingSection, emptySection, elevatedCard, statCard, dataTable
} from './components.js';
import { renderShell, renderHero } from './shell.js';

function finiteOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function pickRows(list) {
  return (Array.isArray(list) ? list : []).map((item) => [
    formatNumber(item.rank || '—'),
    safeText(item.ts_code, '—'),
    safeText(item.name, '—'),
    safeText(item.industry, '—'),
    item.score == null ? '—' : formatNumber(item.score, 1),
    item.prod_score == null ? '—' : formatNumber(item.prod_score, 1),
    formatNumber(item.confirm_hits || 0),
    item.prod_rank == null ? '—' : formatNumber(item.prod_rank),
    item.price == null ? '—' : formatNumber(item.price, 2),
    item.change_pct == null ? '—' : { html: pctHtml(item.change_pct, 2) }
  ]);
}

function pickTable(list, emptyText) {
  const rows = pickRows(list);
  if (!rows.length) {
    return emptySection('出厂名单', emptyText || '今日空仓或尚未生成名单。');
  }
  return dataTable({
    columns: ['#', '代码', '名称', '行业', '加工分', '生产分', '确认', '生产排名', '现价', '涨跌'],
    rows,
    emptyText: emptyText || '无'
  });
}

function heroBody(data) {
  const banner = safeText(data.honesty_banner, '');
  const bannerHtml = banner
    ? `<div class="s3-honesty-banner" role="note">
        <span class="s3-honesty-icon" aria-hidden="true">!</span>
        <p>${escapeHtml(banner)}</p>
      </div>`
    : '';
  const n = Array.isArray(data.latest_picks) ? data.latest_picks.length : 0;
  const realOk = data.production_control_available === true || data.real_production_control_status === 'available';
  const fp = safeText(data.production_config_fingerprint, '');
  return `${bannerHtml}<div class="s3-hero-meta">
    ${badge('研究展示 · 非决策', 'warn')}
    ${badge('生产因子冻结', data.production_frozen === false ? 'bad' : 'flat')}
    ${badge(safeText(data.production_version, 'v4.x') + ' 原料', 'flat')}
    ${badge('冠军出厂 ' + formatNumber(n) + ' 只', n > 0 ? 'ok' : 'flat')}
    ${realOk ? badge('真生产对照可用', 'ok') : badge('真生产对照不可用', 'warn')}
    ${fp ? badge('指纹 ' + fp.slice(0, 8), 'flat') : ''}
  </div>`;
}

function heroAside(data) {
  const cum = data.cumulative || {};
  const ch = data.champion || {};
  const chName = safeText(ch.name, data.champion_experiment_id || '—');
  const edgeBaseline = ch.edge_vs_research_baseline != null ? ch.edge_vs_research_baseline : ch.edge_avg_daily_pct;
  const edgeReal = ch.edge_vs_real_production;
  const realStatus = safeText(
    ch.real_production_control?.status || data.real_production_control_status,
    'unavailable'
  );
  return `<div class="s3-hero-panel">
    <div class="s3-hero-panel-title">加工厂质检席</div>
    <div class="s3-sig">观察冠军 <strong>${escapeHtml(chName)}</strong></div>
    <p class="s3-hero-panel-note"><code class="num">${escapeHtml(safeText(data.champion_experiment_id, '—'))}</code></p>
    <p class="s3-hero-panel-note">相对研究基线日均超额：${edgeBaseline == null ? '—' : pctHtml(edgeBaseline, 3)}</p>
    <p class="s3-hero-panel-note">相对真生产日均超额：${
      realStatus !== 'available' || edgeReal == null
        ? '对照不可用'
        : pctHtml(edgeReal, 3)
    }</p>
    <p class="s3-hero-panel-note">回测累计：${cum.cum_nav_pct == null ? '—' : pctHtml(cum.cum_nav_pct, 2)} · 胜率日 ${cum.win_rate_pct == null ? '—' : `${formatNumber(cum.win_rate_pct, 1)}%`}</p>
    <p class="s3-hero-panel-note soft">市场 20 日为流动性代理，非沪深300。名单为研究观察，非买入建议。</p>
  </div>`;
}

function factoryOverview(data) {
  const factory = data.factory || {};
  const nPicks = Array.isArray(data.latest_picks) ? data.latest_picks.length : 0;
  const momLabel = safeText(data.market_mom20_label, 'liquidity_proxy_mom20');
  const head = sectionHead(
    '加工厂概览',
    '原料 = 生产 prebreakout 子分；二次加工 = 实验权重/增减因子/门控；默认超额对照 = 研究基线（生产权重+实验门控），非真生产策略。'
  );
  const cards = [
    statCard('活跃实验', formatNumber(factory.n_experiments || (data.experiments || []).length || 0)),
    statCard('冠军出厂 Top', formatNumber(nPicks) + ' 只'),
    statCard('信号日', dateCn(data.trade_date || data.latest_signal_date)),
    statCard('流动性代理20日', data.market_mom20_pct == null ? '—' : pctHtml(data.market_mom20_pct, 2))
  ].join('');
  return `${head}<div class="stat-grid">${cards}</div>
    <p class="help-text">生产版本 ${safeText(data.production_version, '—')} · ${data.production_frozen === false ? badge('警告：未冻结', 'bad') : badge('生产已冻结', 'ok')} · 市场指标 ${escapeHtml(momLabel)}</p>`;
}

function championPicksSection(data) {
  const list = Array.isArray(data.latest_picks) ? data.latest_picks : [];
  const ch = data.champion || {};
  const chName = safeText(ch.name, data.champion_experiment_id || '—');
  const meta = data.selection_meta || {};
  const head = sectionHead(
    '冠军出厂 Top 名单（今日）',
    `信号日 ${dateCn(data.latest_signal_date || data.trade_date)} · ${chName} · 最多 ${formatNumber(meta.max_names || '—')} 只 · 研究观察，非买入建议。`
  );
  if (!list.length) {
    return `${head}${emptySection('出厂名单', '冠军实验今日空仓或尚未生成名单（可能是市场门触发）。')}`;
  }
  const intro = `<div class="stat-grid" style="margin-bottom:12px">
    ${statCard('出厂只数', formatNumber(list.length))}
    ${statCard('候选过门控', formatNumber(meta.candidates_after_score_confirm || '—'))}
    ${statCard('分数门槛', meta.threshold_score == null ? '—' : formatNumber(meta.threshold_score, 1))}
    ${statCard('确认命中≥', formatNumber(meta.min_confirm_hits || 0))}
  </div>
  <p class="help-text">加工分 = 二次加工权重/乘子后的综合分；生产分为同日生产子分加权，仅作对照。</p>`;
  return `${head}${intro}${pickTable(list)}`;
}

function allExperimentsPicksSection(data) {
  const list = Array.isArray(data.experiments) ? data.experiments : [];
  const head = sectionHead(
    '各实验出厂 Top（对照）',
    '每个二次加工实验当日独立选股；冠军以外仅作对照，不作为主观察名单。'
  );
  if (!list.length) {
    return `${head}${emptySection('实验名单', '暂无活跃实验。')}`;
  }
  const blocks = list.map((exp) => {
    const picks = Array.isArray(exp.today_picks) ? exp.today_picks : [];
    const isCh = !!exp.is_champion;
    const title = `${safeText(exp.name, exp.id)}${isCh ? ' · 冠军' : ''}`;
    return `<article class="elevated-card strategy-card" style="margin-bottom:12px">
      <div class="strategy-card-head">
        <h4>${escapeHtml(title)} ${isCh ? badge('冠军', 'ok') : ''} ${badge(`Top ${formatNumber(picks.length)}`, picks.length ? 'ok' : 'flat')}</h4>
        <code class="soft num">${escapeHtml(safeText(exp.id, ''))}</code>
      </div>
      ${picks.length
        ? pickTable(picks)
        : '<p class="soft">今日空仓 / 未过门控。</p>'}
    </article>`;
  }).join('');
  return `${head}${blocks}`;
}

function experimentsSection(data) {
  const list = Array.isArray(data.experiments) ? data.experiments : [];
  const head = sectionHead(
    '实验台（二次加工假设与回测）',
    '日均超额默认相对「研究基线」(生产权重+本实验门控)。「跑赢真生产」仅在 selection_history 对照可用时显示。'
  );
  if (!list.length) {
    return `${head}${emptySection('实验台', '暂无活跃实验配置。')}`;
  }

  const blocks = list.map((exp) => {
    const isCh = !!exp.is_champion;
    const title = `${safeText(exp.name, exp.id)}${isCh ? ' · 观察冠军' : ''}`;
    const hyp = safeText(exp.hypothesis, '');
    const inspired = Array.isArray(exp.inspired_by) ? exp.inspired_by : [];
    const edge = finiteOrNull(exp.edge_vs_research_baseline != null ? exp.edge_vs_research_baseline : exp.edge_avg_daily_pct);
    const edgeReal = finiteOrNull(exp.edge_vs_real_production);
    const realOk = exp.real_production_control_status === 'available';
    const beatBadge = realOk
      ? (exp.beats_production ? badge('跑赢真生产', 'ok') : badge('未跑赢真生产', 'flat'))
      : (exp.beats_research_baseline
        ? badge('跑赢研究基线', 'flat')
        : badge('对照：研究基线', 'flat'));
    const deltaRows = (Array.isArray(exp.weight_delta) ? exp.weight_delta : [])
      .filter((r) => Math.abs(Number(r.delta) || 0) >= 0.005)
      .slice(0, 8)
      .map((r) => {
        const d = Number(r.delta) || 0;
        const sign = d > 0 ? '+' : '';
        return `<span class="chip">${escapeHtml(safeText(r.desc || r.factor, ''))} ${sign}${formatNumber(d * 100, 1)}pt</span>`;
      })
      .join(' ');

    return `<article class="elevated-card strategy-card" style="margin-bottom:12px">
      <div class="strategy-card-head">
        <h4>${escapeHtml(title)} ${isCh ? badge('观察冠军', 'ok') : ''} ${beatBadge}</h4>
        <code class="soft num">${escapeHtml(safeText(exp.id, ''))}</code>
      </div>
      <p>${escapeHtml(hyp)}</p>
      ${inspired.length ? `<p class="soft"><strong>灵感：</strong>${escapeHtml(inspired.join('；'))}</p>` : ''}
      <div class="stat-grid" style="margin:10px 0">
        ${statCard('实验累计', exp.experiment_total_return_pct == null ? '—' : pctHtml(exp.experiment_total_return_pct, 2))}
        ${statCard('研究基线累计', exp.production_total_return_pct == null ? '—' : pctHtml(exp.production_total_return_pct, 2))}
        ${statCard('vs 研究基线', edge == null ? '—' : pctHtml(edge, 3))}
        ${statCard('vs 真生产', !realOk || edgeReal == null ? '不可用' : pctHtml(edgeReal, 3))}
        ${statCard('实验胜率日', exp.experiment_win_days_pct == null ? '—' : `${formatNumber(exp.experiment_win_days_pct, 1)}%`)}
        ${statCard('今日出厂', formatNumber(exp.today_n || 0) + ' 只')}
      </div>
      ${deltaRows ? `<div class="chip-row"><strong>权重变动：</strong> ${deltaRows}</div>` : '<p class="soft">权重 = 生产原样（仅门控）</p>'}
    </article>`;
  }).join('');

  return `${head}${blocks}`;
}

function comparisonSection(data) {
  const cmp = data.comparison || {};
  const head = sectionHead('与生产 Top20 对照', '生产仍发 20 只；出厂名单为二次加工子集。');
  const cards = [
    statCard('生产 Top20', formatNumber(cmp.prod_n || 0)),
    statCard('出厂只数', formatNumber(cmp.shadow_n || 0)),
    statCard('重叠', formatNumber(cmp.overlap_n || 0)),
    statCard('出厂独有', formatNumber((cmp.shadow_only || []).length))
  ].join('');
  const overlap = Array.isArray(cmp.overlap_codes) ? cmp.overlap_codes : [];
  const only = Array.isArray(cmp.shadow_only) ? cmp.shadow_only : [];
  return `${head}<div class="stat-grid">${cards}</div>
    <p class="soft" style="margin-top:8px"><strong>重叠：</strong>${overlap.length ? escapeHtml(overlap.join('、')) : '—'}</p>
    <p class="soft"><strong>出厂独有：</strong>${only.length ? escapeHtml(only.join('、')) : '—'}</p>`;
}

function dailySeriesSection(data) {
  const series = Array.isArray(data.daily_series) ? data.daily_series : [];
  const head = sectionHead('冠军实验 · 逐日回测（T+1 开→收，扣简化成本）', '空仓日记 0 收益。与生产同门控对照见实验卡。');
  if (!series.length) return `${head}${emptySection('逐日回测', '尚无回测序列。')}`;
  const rows = [...series].reverse().slice(0, 40).map((row) => [
    dateCn(row.signal_date),
    formatNumber(row.n || 0),
    { html: pctHtml(row.avg_o2c_pct, 2) },
    row.win_rate_pct == null ? '—' : `${formatNumber(row.win_rate_pct, 1)}%`,
    { html: pctHtml(row.cum_nav_pct, 2) }
  ]);
  return `${head}${dataTable({
    columns: ['信号日', '只数', '日均开收', '组合内胜率', '累计净值'],
    rows,
    emptyText: '暂无'
  })}`;
}

function howToSection() {
  const head = sectionHead('如何新增二次加工实验', '不改 pipeline.py；只加 YAML。');
  const body = elevatedCard(`<ol class="plain-list">
    <li>在 <code>strategy_research/prebreakout_shadow/experiments/</code> 新建 <code>exp_*.yaml</code></li>
    <li>填写 <code>hypothesis</code> / <code>inspired_by</code>（外来库灵感）</li>
    <li>设置 <code>weights</code>（null=生产）或 <code>disabled_factors</code></li>
    <li>配置 <code>gates</code> 与 <code>backtest.lookback_days</code></li>
    <li>跑 <code>prebreakout_factory.py</code> 或等 19:30 管线；本页自动刷新公开 JSON</li>
    <li>冠军持续跑赢生产后再讨论是否晋升写入生产配置</li>
  </ol>`);
  return `${head}${body}`;
}

export function renderPrebreakoutShadow(model) {
  const data = (model && typeof model.prebreakoutShadowWatch === 'object' && model.prebreakoutShadowWatch) || {};
  const missing = model && typeof model.isMissing === 'function' && model.isMissing('prebreakoutShadowWatch');
  const hasBody = data && (
    (Array.isArray(data.experiments) && data.experiments.length)
    || (Array.isArray(data.latest_picks) && data.latest_picks.length)
    || (Array.isArray(data.daily_series) && data.daily_series.length)
    || data.trade_date
  );

  const title = safeText(data.title, '') || '启动前夕 · 因子二次加工厂';
  const subtitle = '学习外来库 → 调子分/增减因子 → 回测对照生产 → 观察页质检。生产默认冻结。';

  if (missing || !hasBody) {
    const hero = renderHero(model, title, subtitle, { eyebrow: '二次加工厂', bodyHtml: '', asideHtml: '' });
    return renderShell(
      'prebreakoutShadow',
      model,
      `${hero}<div class="s3-watch">${missingSection(
        '加工厂观察',
        missing
          ? '数据文件未生成：data/latest/prebreakout_shadow_watch.json'
          : '暂无内容，请先跑 prebreakout_factory.py'
      )}</div>`
    );
  }

  const hero = renderHero(model, title, subtitle, {
    eyebrow: `信号日 ${dateCn(data.latest_signal_date || data.trade_date)} · 二次加工厂`,
    bodyHtml: heroBody(data),
    asideHtml: heroAside(data)
  });

  // 名单前置：用户首先看到工厂生成的股票 Top
  const body = `${hero}
    <div class="s3-watch">
      <section class="s3-section">${championPicksSection(data)}</section>
      <section class="s3-section">${factoryOverview(data)}</section>
      <section class="s3-section">${allExperimentsPicksSection(data)}</section>
      <section class="s3-section">${comparisonSection(data)}</section>
      <section class="s3-section">${experimentsSection(data)}</section>
      <section class="s3-section">${dailySeriesSection(data)}</section>
      <section class="s3-section">${howToSection()}</section>
      <p class="help-text" style="margin-top:16px">
        生产名单见 <a class="text-link" href="./decision-candidates.html">个股推荐</a> ·
        <a class="text-link" href="./s3-watch.html">S3 观察</a> ·
        <a class="text-link" href="./research-lab.html">系统说明</a>
      </p>
    </div>`;

  return renderShell('prebreakoutShadow', model, body);
}
