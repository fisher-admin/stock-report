// v2/render/prebreakoutShadow.js — 启动前夕影子研究观察页（prebreakout-shadow.html）。
//
// 数据：model.prebreakoutShadowWatch ← data/latest/prebreakout_shadow_watch.json
// 缺失 → 整页退占位，不编造。
// 诚实红线：研究观察·非买入；不改生产因子；负收益同等醒目。

import {
  escapeHtml, safeText, formatNumber, formatPct, pctHtml, dateCn
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

function statusBadge(status) {
  if (status === 'settled') return badge('已结算', 'ok');
  if (status === 'miss') return badge('未成交', 'flat');
  return badge('待结算', 'warn');
}

function heroBody(data) {
  const banner = safeText(data.honesty_banner, '');
  const bannerHtml = banner
    ? `<div class="s3-honesty-banner" role="note">
        <span class="s3-honesty-icon" aria-hidden="true">!</span>
        <p>${escapeHtml(banner)}</p>
      </div>`
    : '';
  return `${bannerHtml}<div class="s3-hero-meta">
    ${badge('研究观察 · 非买入建议', 'warn')}
    ${badge('不改生产因子权重', 'flat')}
    ${badge(safeText(data.shadow_id, 'prebreakout_shadow_v5'), 'flat')}
  </div>`;
}

function heroAside(data) {
  const cum = (data && data.cumulative) || {};
  const meta = (data && data.selection_meta) || {};
  const marketNote = safeText(data.market_note || meta.market_note, '—');
  const mom = finiteOrNull(data.market_mom20_pct);
  return `<div class="s3-hero-panel">
    <div class="s3-hero-panel-title">影子研究席</div>
    <div class="s3-sig">市场门 <code class="num">${escapeHtml(marketNote)}</code></div>
    <p class="s3-hero-panel-note">20 日大盘股等权：${mom === null ? '—' : `${formatNumber(mom, 2)}%`}（${escapeHtml(safeText(data.market_mom20_source, 'proxy'))}）</p>
    <p class="s3-hero-panel-note">累计已结算 ${formatNumber(cum.settled_n || 0)} 笔 · 胜率 ${cum.win_rate_pct == null ? '—' : `${formatNumber(cum.win_rate_pct, 1)}%`}</p>
    <p class="s3-hero-panel-note soft">生产 prebreakout_v41 照常发布；本页仅对照观察。</p>
  </div>`;
}

function statsSection(data) {
  const cum = data.cumulative || {};
  const stats = data.ledger_stats || {};
  const head = sectionHead('累计纸面表现（T+1 开→收）', '入场=信号日次一交易日开盘；出场=当日收盘。含一字板记 miss。非实盘。');
  const cards = [
    statCard('已结算笔数', formatNumber(cum.settled_n ?? stats.settled_n ?? 0)),
    statCard('信号日数', formatNumber(cum.signal_dates ?? stats.signal_dates ?? 0)),
    statCard('平均开收', cum.avg_o2c_pct == null && stats.avg_o2c_pct == null
      ? '—'
      : pctHtml(cum.avg_o2c_pct ?? stats.avg_o2c_pct, 2)),
    statCard('胜率', cum.win_rate_pct == null && stats.win_rate_pct == null
      ? '—'
      : `${formatNumber(cum.win_rate_pct ?? stats.win_rate_pct, 1)}%`),
    statCard('累计净值', cum.cum_nav_pct == null ? '—' : pctHtml(cum.cum_nav_pct, 2)),
    statCard('待结算', formatNumber(stats.pending_n || 0))
  ].join('');
  return `${head}<div class="stat-grid">${cards}</div>`;
}

function todayPicksSection(data) {
  const list = Array.isArray(data.latest_picks) ? data.latest_picks : [];
  const signalDate = dateCn(data.latest_signal_date || data.trade_date);
  const meta = data.selection_meta || {};
  const head = sectionHead(
    '今日影子名单',
    `信号日 ${signalDate}。名额 cap=${formatNumber(meta.market_cap ?? meta.selected ?? list.length)}；`
    + `分位门槛 ${meta.threshold_score == null ? '—' : formatNumber(meta.threshold_score, 1)}；`
    + `行业≤${formatNumber(meta.max_per_industry || 3)} · 确认子分≥${formatNumber(meta.min_confirm_hits || 2)}。`
  );
  if (!list.length) {
    const emptyNote = meta.empty_book
      ? '今日市场门/门槛触发空仓（研究观察的有效信号，不是故障）。'
      : '本期未读到影子名单内容。';
    return `${head}${emptySection('今日影子名单', emptyNote)}`;
  }

  const rows = list.map((item) => {
    const status = safeText(item && item.status, 'pending');
    let statusHtml = statusBadge(status);
    if (status === 'settled') {
      statusHtml += `<div class="s3-settle-grid"><span>跳空 ${pctHtml(item.gap_pct, 2)}</span><span>开收 ${pctHtml(item.o2c_pct, 2)}</span></div>`;
    } else if (status === 'miss') {
      statusHtml += `<div class="s3-miss-reason soft">${escapeHtml(safeText(item.miss_reason, '未成交'))}</div>`;
    } else {
      statusHtml += `<div class="soft">T+1 ${dateCn(item.trade_date) || '待定'} 开盘入场假设</div>`;
    }
    return [
      formatNumber(item.rank || '—'),
      escapeHtml(safeText(item.ts_code, '—')),
      escapeHtml(safeText(item.name, '—')),
      escapeHtml(safeText(item.industry, '—')),
      item.score == null ? '—' : formatNumber(item.score, 1),
      formatNumber(item.confirm_hits || 0),
      item.prod_rank == null ? '—' : formatNumber(item.prod_rank),
      statusHtml
    ];
  });

  return `${head}${dataTable(
    ['#', '代码', '名称', '行业', '生产分', '确认', '生产排名', '状态'],
    rows,
    { emptyText: '今日无影子标的' }
  )}`;
}

function comparisonSection(data) {
  const cmp = data.comparison || {};
  const head = sectionHead(
    '与生产 Top20 对照',
    '生产 prebreakout_v41 仍每日发布 20 只；影子名单是后处理子集（或空仓）。'
  );
  const cards = [
    statCard('生产 Top20', formatNumber(cmp.prod_n || 0)),
    statCard('影子只数', formatNumber(cmp.shadow_n || 0)),
    statCard('重叠', formatNumber(cmp.overlap_n || 0)),
    statCard('影子独有', formatNumber((cmp.shadow_only || []).length))
  ].join('');
  const overlap = Array.isArray(cmp.overlap_codes) ? cmp.overlap_codes : [];
  const only = Array.isArray(cmp.shadow_only) ? cmp.shadow_only : [];
  const detail = `<div class="elevated-card" style="margin-top:12px">
    <p><strong>重叠代码：</strong>${overlap.length ? escapeHtml(overlap.join('、')) : '—'}</p>
    <p><strong>影子独有：</strong>${only.length ? escapeHtml(only.join('、')) : '（无，或空仓）'}</p>
  </div>`;
  return `${head}<div class="stat-grid">${cards}</div>${detail}`;
}

function dailySeriesSection(data) {
  const series = Array.isArray(data.daily_series) ? data.daily_series : [];
  const head = sectionHead('逐日纸面表现', '每个信号日影子组合等权 T+1 开→收均值；累计净值为复利。');
  if (!series.length) {
    return `${head}${emptySection('逐日表现', '尚无已结算信号日。')}`;
  }
  const rows = [...series].reverse().map((row) => [
    dateCn(row.signal_date),
    formatNumber(row.n || 0),
    pctHtml(row.avg_o2c_pct, 2),
    row.win_rate_pct == null ? '—' : `${formatNumber(row.win_rate_pct, 1)}%`,
    pctHtml(row.cum_nav_pct, 2)
  ]);
  return `${head}${dataTable(
    ['信号日', '只数', '日均开收', '胜率', '累计净值'],
    rows,
    { emptyText: '暂无' }
  )}`;
}

function rulesSection(data) {
  const rules = data.rules || {};
  const head = sectionHead('研究规则（后处理，不改生产因子）', '以下参数只影响影子层筛选。');
  const items = [
    `最多名额：${formatNumber(rules.max_names || 8)}`,
    `单行业上限：${formatNumber(rules.max_per_industry || 3)}`,
    `分数门槛：≥ max(全市场${formatNumber(rules.score_percentile || 90)}分位, ${formatNumber(rules.min_score_floor || 70)})`,
    `确认子分：${escapeHtml((rules.confirm_keys || []).join(' / ') || '—')} 至少 ${formatNumber(rules.min_confirm_hits || 2)} 项 ≥ 60`,
    `生产因子：${rules.does_not_modify_production_factors === false ? '已修改（异常）' : '未修改（v4.3 原样）'}`
  ];
  const list = items.map((t) => `<li>${t}</li>`).join('');
  return `${head}${elevatedCard(`<ul class="plain-list">${list}</ul>`)}`;
}

export function renderPrebreakoutShadow(model) {
  const data = (model && typeof model.prebreakoutShadowWatch === 'object' && model.prebreakoutShadowWatch) || {};
  const missing = model && typeof model.isMissing === 'function' && model.isMissing('prebreakoutShadowWatch');
  const hasBody = data && (
    (Array.isArray(data.latest_picks) && data.latest_picks.length)
    || (Array.isArray(data.daily_series) && data.daily_series.length)
    || (data.cumulative && typeof data.cumulative === 'object')
    || data.trade_date
  );

  const title = safeText(data.title, '') || '启动前夕 · 影子研究观察';
  const subtitle = '不改生产因子权重的后处理对照实验——研究观察，非买入建议。';

  if (missing || !hasBody) {
    const hero = renderHero(model, title, subtitle, {
      eyebrow: '启动前夕 · 影子研究',
      bodyHtml: '',
      asideHtml: ''
    });
    const body = `${hero}
      <div class="s3-watch">
        ${missingSection('启动前夕影子观察', missing
          ? '观察数据文件暂未生成或读取失败（data/latest/prebreakout_shadow_watch.json）'
          : '观察数据暂无内容')}
      </div>`;
    return renderShell('prebreakoutShadow', model, body);
  }

  const hero = renderHero(model, title, subtitle, {
    eyebrow: `信号日 ${dateCn(data.latest_signal_date || data.trade_date)} · 影子研究`,
    bodyHtml: heroBody(data),
    asideHtml: heroAside(data)
  });

  const body = `${hero}
    <div class="s3-watch">
      <section class="s3-section">${statsSection(data)}</section>
      <section class="s3-section">${todayPicksSection(data)}</section>
      <section class="s3-section">${comparisonSection(data)}</section>
      <section class="s3-section">${dailySeriesSection(data)}</section>
      <section class="s3-section">${rulesSection(data)}</section>
      <p class="help-text" style="margin-top:16px">
        生产启动前夕仍见 <a class="text-link" href="./decision-candidates.html">个股推荐</a>；
        本页仅研究对照。相关：<a class="text-link" href="./s3-watch.html">S3 观察名单</a> ·
        <a class="text-link" href="./research-lab.html">系统说明</a>
      </p>
    </div>`;

  return renderShell('prebreakoutShadow', model, body);
}
