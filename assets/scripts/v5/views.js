// v5/views.js — 六个路由视图（model → HTML 字符串）。
import { esc, fmt, pct, ratio, badge, toneOf, panel, missing, kv, meter, stat, bars, line, spark, ageBadge, dateCn, dualLine } from './ui.js';
import { top20Model, reviewSeries, tradeDate, SOURCES, challengerModel, arenaModel } from './data.js';
import { showroom } from './top20.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const need = (d, key, fn) => (d[key] ? fn(d[key]) : missing(SOURCES[key].label));

// ---------------------------------------------------------------- 共用分区

function marketPulse(d) {
  const mc = d.marketContext || {};
  const ca = mc.close_actuals || {};
  const snap = d.marketState?.session_snapshot || d.marketState?.midday?.session_snapshot || {};
  const idx = ['shanghai', 'shenzhen', 'chinext'].map((k) => snap[k]).filter(Boolean)
    .map((s) => `<div class="idx"><span>${esc(s.label)}</span><b class="n">${fmt(s.close, 2)}</b>${pct(s.change_pct)}</div>`).join('');
  return panel({
    title: '市场脉搏',
    sub: mc.market_position || '',
    meta: ageBadge(mc.trade_date, tradeDate(d)),
    body: `${idx ? `<div class="idx-row">${idx}</div>` : ''}
    <div class="stats">${stat('上涨占比', ratio(ca.breadth), `${ca.n ?? '—'} 只`)}${stat('涨停 / 跌停', `<span class="n up">${ca.limit_up ?? '—'}</span> / <span class="n down">${ca.limit_down ?? '—'}</span>`)}${stat('平均涨跌', pct(ca.avg_pct))}${stat('市场周期', esc(mc.market_cycle || '—'))}</div>
    ${mc.external_factors?.summary ? `<p class="note">外盘：${esc(mc.external_factors.summary)}</p>` : ''}
    ${mc.policy ? `<p class="note">策略口径：${esc(mc.policy)}</p>` : ''}`
  });
}

function gatePipeline(d) {
  const g = d.systemVerdict?.gates || {};
  const CN = { freshness_gate: '数据新鲜度', market_gate: '市场环境', strategy_gate: '策略门槛', candidate_gate: '候选质量' };
  const steps = Object.entries(g).map(([k, v]) => `<li class="step g-${toneOf(v.status)}"><i></i><div><b>${esc(CN[k] || k)}</b>${badge(v.status || '—', toneOf(v.status))}<p>${esc(v.summary || '')}</p>${arr(v.blockers).length ? `<p class="blk">${arr(v.blockers).map(esc).join('；')}</p>` : ''}</div></li>`).join('');
  const fa = d.systemVerdict?.final_action || {};
  return panel({
    title: '决策闸门',
    sub: '新鲜度 → 市场 → 策略 → 候选',
    body: steps ? `<ol class="pipe">${steps}</ol>${fa.next_step?.message ? `<p class="note">下一步：${esc(fa.next_step.message)}</p>` : ''}` : missing(SOURCES.systemVerdict.label)
  });
}

function evidenceMini(d) {
  const r = reviewSeries(d);
  const ev = d.strategyEvaluation?.strategies || {};
  const ctl = ev.prebreakout_v43_control || Object.values(ev)[0];
  return panel({
    title: '策略健康',
    sub: '前瞻样本与近 20 日表现',
    body: `${ctl ? `<div class="prog"><span>${esc(ctl.strategy_name)} 前瞻样本</span>${meter(ctl.sample_trade_days, ctl.required_trade_days || 60, { label: `${ctl.sample_trade_days ?? 0}/${ctl.required_trade_days || 60} 交易日` })}${badge(ctl.effectiveness_status === 'not_validated' ? '尚未验证' : ctl.effectiveness_status, ctl.effectiveness_status === 'validated' ? 'pass' : 'warn')}</div>` : ''}
    ${r.days ? `<div class="stats">${stat('近20日次日均值', pct(r.avgRet20))}${stat('近20日命中率', r.avgHit20 !== null ? `${fmt(r.avgHit20, 1)}%` : '—')}</div>${spark(r.series.slice(-40).map((s) => s.cum))}<p class="note">累计曲线为 Top-20 等权次日净收益复利（含 0.3% 双边成本），${r.days} 个信号日。</p>` : missing(SOURCES.reviewTrack.label)}
    <a class="more" href="#/evidence">查看完整验证 →</a>`
  });
}

function freshness(d) {
  const ref = tradeDate(d);
  const src = d.runManifest?.sources || {};
  const CN = { market_generated_at: '晨判', midday_generated_at: '午盘', orchestrator_generated_at: '选股编排', ai_publish_generated_at: 'AI 分析', review_generated_at: '复盘', validation_report_generated_at: '校验', recommendation_db_generated_at: '推荐库', research_generated_at: '研究' };
  const rows = Object.entries(src).sort((a, b) => String(a[1]).localeCompare(String(b[1])))
    .map(([k, v]) => `<li><span>${esc(CN[k] || k)}</span><time>${esc(String(v).slice(5, 16))}</time></li>`).join('');
  const rm = d.runManifest || {};
  const flags = [['校验', rm.validation_ok], ['AI 完整', rm.ai_complete], ['可发布', rm.publish_ready]]
    .map(([k, v]) => badge(k, v === true ? 'pass' : v === false ? 'block' : 'mute')).join('');
  return panel({ title: '数据血缘', sub: `交易日 ${dateCn(ref)}`, body: `<div class="flags">${flags}</div><ol class="tl">${rows}</ol>` });
}

const demoHref = (ctx) => (ctx.demoAvailable && !ctx.demo ? `#/${ctx.route}?demo=live` : '');

// ---------------------------------------------------------------- 视图

export function today(d, ctx) {
  const m = top20Model(d);
  return `<div class="layout-today">
  <div class="col-main">${showroom(m, { mode: ctx.mode, demoHref: demoHref(ctx), full: true })}</div>
  <div class="col-side">${marketPulse(d)}${gatePipeline(d)}${evidenceMini(d)}${freshness(d)}</div></div>`;
}

export function candidates(d, ctx) {
  const m = challengerModel(d);
  const doc = m.doc || {};
  const st = doc.strategy || {};
  const vg = doc.veto_gate || {};
  const hb = doc.hold_book || {};
  const dl = m.delta || {};
  const cnt = dl.counts || {};
  const q = ctx.demo ? '&demo=live' : '';
  const chip = (x, href, extra = '') => `<a class="dchip" href="${esc(href)}"><b>${esc(x.name)}</b><code>${esc(x.code)}</code>${extra}</a>`;
  const champ = top20Model(d);
  const header = `<section class="arena-h">
    <div class="arena-t"><span class="b b-warn">影子 · 挑战者</span><h1>v4.4 挑战者策略 <small>前沿候选因子试验区 · ${esc(st.holding_period_days || 5)} 日持仓</small></h1>
    <p>${esc(doc.honesty_banner || '影子策略 · 仅研究观察 · 非买入建议。')}</p></div>
    <div class="arena-vs"><a href="#/today${ctx.demo ? '?demo=live' : ''}" class="vs-side champ"><em>冠军 · 生产</em><b>${esc(champ.strategyName || 'v4.3')}</b><span>${champ.stocks.length} 只 · 信号日 ${dateCn(champ.signalDate)}</span></a>
    <span class="vs">VS</span>
    <div class="vs-side chal"><em>挑战者 · 影子</em><b>v4.4 否决门 · 5 日持有</b><span>${m.stocks.length} 只 · 信号日 ${dateCn(doc.trade_date)}${m.aligned ? '' : ' · ' + badge('与冠军信号日不一致', 'warn')}</span></div></div></section>`;
  const deltaStrip = m.stocks.length ? `<section class="delta">
    <div class="dcol d-cons"><h3>共同入选 <span>Consensus</span><b>${cnt.consensus ?? 0}</b></h3><p>两套策略同时选中</p><div class="dchips">${arr(dl.consensus).map((x) => chip(x, `#/candidates?s=${x.code}${q}`, `<small>冠军#${esc(x.champion_rank ?? '—')} → 挑战者#${esc(x.challenger_rank ?? '—')}</small>`)).join('') || '<p class="muted">无</p>'}</div></div>
    <div class="dcol d-only"><h3>新策略独享 <span>Challenger Alpha</span><b>${cnt.challenger_only ?? 0}</b></h3><p>仅 v4.4 选中，冠军名单之外</p><div class="dchips">${arr(dl.challenger_only).map((x) => chip(x, `#/candidates?s=${x.code}${q}`, `<small>分位 ${fmt((x.challenger_pct ?? 0) * 100, 1)}%</small>`)).join('') || '<p class="muted">无</p>'}</div></div>
    <div class="dcol d-out"><h3>被新策略否决 / 排除 <span>Vetoed</span><b>${(cnt.vetoed ?? 0) + (cnt.ranked_out ?? 0)}</b></h3><p>冠军入选但 v4.4 未选：否决门剔除 ${cnt.vetoed ?? 0} 只 · 排序未入选 ${cnt.ranked_out ?? 0} 只</p><div class="dchips">${arr(dl.champion_excluded).map((x) => chip(x, `#/today?s=${x.code}${q}`, `<small>${x.reason === 'vetoed' ? `否决 · 分位 ${fmt((x.challenger_pct ?? 0) * 100, 1)}%` : '排序未入选'} · 冠军#${esc(x.champion_rank ?? '—')}</small>`)).join('') || '<p class="muted">无</p>'}</div></div>
  </section>` : '';
  const empty = m.status === 'missing'
    ? `<strong>挑战者数据暂不可用</strong><p>数据源「${esc(SOURCES.v44Challenger.label)}」未发布。</p>`
    : `<strong>v4.4 挑战者名单尚未生成</strong><p>${esc(m.reason || '影子策略将在下一次收盘后运行时产出名单。')}</p>`;
  const room = showroom({ stocks: m.stocks, counts: {}, maintenance: m.maintenance, signalDate: doc.trade_date || '' }, {
    mode: ctx.mode, full: true, title: '挑战者 Top-20', sub: `${st.name || 'v4.4 影子策略'}`, id: 'challenger', cls: 'showroom chal-room',
    countsHtml: `<span class="cnt"><b>${cnt.consensus ?? 0}</b>共同</span><span class="cnt"><b>${cnt.challenger_only ?? 0}</b>独享</span>`,
    emptyMsg: empty
  });
  const fw = Object.entries(st.factor_weights || {}).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<li><span>${esc(k.replace(/[（(].*$/, ''))}</span>${meter(v, 0.3)}<b>${fmt(v * 100, 1)}%</b></li>`).join('');
  const notes = Object.entries(st.factor_notes || {}).map(([k, v]) => [({ core: '纯化核心', vpd_20: '量价背离 VPD', smf_20_rev: '聪明钱反转 SMF', veto: '否决门', booster: '增强排序' })[k] || k, esc(v)]);
  return `<div class="layout-wide arena">${header}${deltaStrip}${room}
  <div class="row3">
  ${panel({ title: '挑战者模型', sub: `challenger v${esc(st.challenger_version || '—')}`, body: notes.length ? kv(notes) : missing(SOURCES.v44Challenger.label) })}
  ${panel({ title: '否决门与持仓簿', body: doc.veto_gate ? kv([['否决门状态', badge(vg.status || '—', toneOf(vg.status))], ['当日全市场', `<span class="n">${esc(vg.scored ?? '—')}/${esc(vg.universe ?? '—')}</span> 已评分`], ['后 25% 否决', `<span class="n">${esc(vg.vetoed ?? '—')}</span> 只`], ['候选中被否决', `<span class="n">${esc(vg.vetoed_candidates ?? '—')}</span> 只`], ['调仓日', dateCn(hb.rebalance_date)], ['持有进度', `第 ${esc(hb.hold_day ?? '—')}/${esc(hb.hold_days ?? '—')} 日`]]) : missing(SOURCES.v44Challenger.label) })}
  ${panel({ title: 'v4.4 因子权重', sub: arr(st.dropped_factors).length ? `已剔除：${arr(st.dropped_factors).join('、')}` : '', body: fw ? `<ul class="hbars">${fw}</ul>` : missing(SOURCES.v44Challenger.label) })}
  </div></div>`;
}

export function market(d) {
  const ms = d.marketState || {};
  const sum = ms.market_summary || {};
  const snap = ms.session_snapshot || ms.midday?.session_snapshot || {};
  const idx = Object.values(snap).filter((s) => s && s.label)
    .map((s) => `<div class="idx big"><span>${esc(s.label)}</span><b class="n">${fmt(s.close, 2)}</b>${pct(s.change_pct)}<small>${esc(s.source_kind === 'exact_close' ? '收盘' : s.source_kind || '')} · ${dateCn(s.as_of)}</small></div>`).join('');
  const sec = (rows) => `<table class="tbl compact"><thead><tr><th>行业</th><th class="num">涨跌</th><th class="num">上涨占比</th><th class="num">热度</th><th>趋势</th></tr></thead><tbody>${arr(rows).map((r) => `<tr><td>${esc(r.industry || r.industry_name)}</td><td class="num">${pct(r.avg_pct_chg)}</td><td class="num">${ratio(r.up_ratio)}</td><td class="num">${fmt(r.market_heat ?? r.market_heat_ema_5, 2)}</td><td>${esc(r.trend_signal || '')}</td></tr>`).join('')}</tbody></table>`;
  const hm = d.marketHeatmap;
  const hmRows = arr(hm?.rows).filter((r) => r.trade_date === hm.latest_trade_date)
    .sort((a, b) => (b.market_heat_ema_5 ?? 0) - (a.market_heat_ema_5 ?? 0));
  const tiles = hmRows.map((r) => {
    const v = +r.avg_pct_chg || 0;
    return `<div class="tile ${v > 0 ? 'up' : v < 0 ? 'down' : 'flat'}" style="--a:${Math.min(1, Math.abs(v) / 3).toFixed(2)}" title="${esc(r.industry_name)} ${v.toFixed(2)}% · 上涨 ${ratio(r.up_ratio)} · ${r.stock_count} 只"><span>${esc(r.industry_name)}</span><b>${v > 0 ? '+' : ''}${v.toFixed(2)}</b></div>`;
  }).join('');
  const mv = ms.midday?.market_view_midday || {};
  const acts = arr(ms.industry_actions).slice(0, 8).map((a) => `<li>${badge(a.action, a.action === '增配' ? 'up' : a.action === '回避' || a.action === '减配' ? 'down' : 'mute')}<b>${esc(a.industry)}</b><span>${esc(a.action_summary || a.reason || '')}</span></li>`).join('');
  return `<div class="layout-wide">
  ${panel({ title: '指数快照', meta: ageBadge(ms.latest_trade_date, tradeDate(d)), body: idx ? `<div class="idx-grid">${idx}</div>` : missing(SOURCES.marketState.label) })}
  <div class="row2">
  ${panel({ title: '行业热力', sub: hm ? `${hmRows.length} 个行业 · 按 5 日热度排序` : '', meta: hm ? ageBadge(hm.latest_trade_date, tradeDate(d)) : '', body: tiles ? `<div class="heatmap">${tiles}</div>` : missing(SOURCES.marketHeatmap.label) })}
  ${panel({ title: '行业广度', body: `<div class="stats">${stat('上涨行业', `${sum.positive_sector_count ?? '—'}/${sum.sector_count ?? '—'}`, ratio(sum.positive_sector_ratio))}${stat('强信号行业', sum.strong_signal_sector_count ?? '—')}${stat('行业均涨跌', pct(sum.average_sector_change_pct))}</div>${mv.summary ? `<h4 class="h4">午盘观点</h4><p class="note">${esc(mv.summary)}</p>` : ''}${mv.action_advice ? `<p class="note">${esc(mv.action_advice)}</p>` : ''}` })}
  </div>
  <div class="row2">
  ${panel({ title: '领涨行业', body: ms.top_market_sectors ? sec(ms.top_market_sectors) : missing(SOURCES.marketState.label) })}
  ${panel({ title: '领跌行业', body: ms.bottom_market_sectors ? sec(ms.bottom_market_sectors) : missing(SOURCES.marketState.label) })}
  </div>
  ${acts ? panel({ title: '行业动作', sub: '市场主线 × 策略覆盖', body: `<ul class="acts">${acts}</ul>` }) : ''}
  </div>`;
}

const P = (v, dg = 2) => (v === null || v === undefined || !Number.isFinite(+v) ? '<span class="n flat">—</span>' : pct(+v * 100, dg));
const pp = (v, dg = 1) => (v === null || v === undefined || !Number.isFinite(+v) ? '—' : `${(+v * 100).toFixed(dg)}%`);

function arenaSection(d) {
  const m = arenaModel(d);
  if (m.status !== 'ok') {
    return panel({ title: '双轨竞技场 · 历史回放', body: m.status === 'missing' ? missing(SOURCES.arenaLedger.label) : `<div class="missing">回放账本尚未生成${m.reason ? `<small>${esc(m.reason)}</small>` : ''}</div>` });
  }
  const { doc, v43, v44, sig, attr } = m;
  const w = doc.window || {};
  const veto = attr.veto || {};
  const last = m.curve[m.curve.length - 1] || {};
  const delta = (a, b, kind = 'pct', better = 'high') => {
    if (a === null || a === undefined || b === null || b === undefined) return '<span class="n flat">—</span>';
    const dv = b - a;
    const good = better === 'high' ? dv > 0 : dv < 0;
    const txt = kind === 'num' ? `${dv > 0 ? '+' : ''}${dv.toFixed(2)}` : `${dv > 0 ? '+' : ''}${(dv * 100).toFixed(2)}pp`;
    return `<span class="n ${Math.abs(dv) < 1e-12 ? 'flat' : good ? 'better' : 'worse'}">${txt}</span>`;
  };
  const rows = [
    ['累计净收益', P(v43.cum_net), P(v44.cum_net), delta(v43.cum_net, v44.cum_net)],
    ['累计净收益（压力成本 0.5%）', P(v43.stress?.cum_net), P(v44.stress?.cum_net), delta(v43.stress?.cum_net, v44.stress?.cum_net)],
    [`T+1 胜率（逐股，${m.t1Days} 个已结算信号日）`, pp(v43.t1Hit), pp(v44.t1Hit), delta(v43.t1Hit, v44.t1Hit)],
    [`T+5 胜率（逐股，${m.t5Days} 个已结算信号日）`, pp(v43.t5Hit), pp(v44.t5Hit), delta(v43.t5Hit, v44.t5Hit)],
    ['T+1 平均净收益', P(v43.t1Mean, 3), P(v44.t1Mean, 3), delta(v43.t1Mean, v44.t1Mean)],
    ['T+5 平均净收益', P(v43.t5Mean, 3), P(v44.t5Mean, 3), delta(v43.t5Mean, v44.t5Mean)],
    ['实现夏普（年化）', fmt(v43.sharpe, 2), fmt(v44.sharpe, 2), delta(v43.sharpe, v44.sharpe, 'num')],
    ['最大回撤', P(v43.max_drawdown), P(v44.max_drawdown), delta(v43.max_drawdown, v44.max_drawdown)],
    ['年化摩擦拖累（实测换手）', pp(v43.friction_annualized), pp(v44.friction_annualized), delta(v43.friction_annualized, v44.friction_annualized, 'pct', 'low')],
    ['单次调仓平均换手', pp(v43.avg_turnover_per_rebalance, 0), pp(v44.avg_turnover_per_rebalance, 0), ''],
    ['调仓次数 / 盈利日占比', `${esc(v43.rebalances ?? '—')} / ${pp(v43.hit_days, 0)}`, `${esc(v44.rebalances ?? '—')} / ${pp(v44.hit_days, 0)}`, '']
  ].map(([k, a, b, c]) => `<tr><th scope="row">${esc(k)}</th><td class="num">${a}</td><td class="num">${b}</td><td class="num">${c}</td></tr>`).join('');
  const tSig = Number.isFinite(+sig.daily_diff_t) ? Math.abs(+sig.daily_diff_t) >= 2 : false;
  const grp = (label, mean, n, tone) => `<div class="agrp a-${tone}"><span>${esc(label)}</span><b>${P(mean, 2)}</b><small>T+5 均值 · ${esc(n ?? 0)} 个样本</small></div>`;
  const ps = m.perSignal.map((r) => `<tr><td>${dateCn(r.date)}${r.v44_rebalance ? ' <span class="chip">调仓</span>' : ''}</td><td class="num">${esc(r.overlap)}</td><td class="num">${P(r.v43_t1)}</td><td class="num">${P(r.v44_t1)}</td><td class="num">${P(r.v43_t5)}</td><td class="num">${P(r.v44_t5)}</td><td class="num">${esc(r.vetoed_n)}/${esc(r.candidates_n)}</td><td class="num">${esc(r.v43_in_vetoed)}</td></tr>`).join('');
  return `<section class="arena-h hist"><div class="arena-t"><span class="b b-info">历史回放 · 回测</span><h1>双轨竞技场 <small>冠军 v4.3 vs 挑战者 v4.4 · ${esc(w.signals)} 个信号日</small></h1><p>${esc(doc.honesty_banner || '')}</p></div>
    <div class="arena-kpi"><div><em>冠军 v4.3</em><b>${P(v43.cum_net)}</b></div><div><em>挑战者 v4.4</em><b>${P(v44.cum_net)}</b></div><div><em>超额（挑战者−冠军）</em><b>${P(last.spread)}</b><small>配对 t = ${fmt(sig.daily_diff_t, 2)} ${tSig ? badge('显著', 'pass') : badge('不显著', 'warn')}</small></div></div></section>
  ${panel({ title: '累计净值对比', sub: `${dateCn(w.signal_from)} 信号起 · 次日开盘入场 · 截至 ${dateCn(w.price_to)}`, cls: 'arena-curve',
    meta: `<span class="legend"><i class="lg-a"></i>冠军 v4.3（每日调仓）<i class="lg-b"></i>挑战者 v4.4（5 日持仓·否决门）</span><div class="seg" role="group" aria-label="成本口径"><button data-act="cost" data-cost="base" class="on">基准成本 0.3%</button><button data-act="cost" data-cost="stress">压力成本 0.5%</button></div>`,
    body: `<div class="readout" aria-live="polite">悬停图表查看逐日数值</div><div class="cv cv-base">${dualLine(m.curve, { id: 'arena-base' })}</div><div class="cv cv-stress">${dualLine(m.curveStress, { id: 'arena-stress' })}</div>
    <figure class="spread-fig"><figcaption>超额收益（挑战者 − 冠军，累计，%）</figcaption>${bars(m.curve.map((r) => r.spread * 100), { h: 70, labels: m.curve.map((r) => dateCn(r.date)) })}</figure>` })}
  <div class="row2">
  ${panel({ title: '绩效记分卡', sub: '净收益已扣除按实际换手计算的交易成本', body: `<div class="tbl-wrap"><table class="tbl score"><thead><tr><th>指标</th><th class="num">冠军 v4.3</th><th class="num">挑战者 v4.4</th><th class="num">差值</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="note">${esc(sig.note || '')} 本窗口配对日收益差均值 ${P(sig.daily_diff_mean, 3)}，t = ${fmt(sig.daily_diff_t, 2)}；${esc(sig.paired_days)} 日样本下夏普标准误约 ±${fmt(sig.sharpe_se_approx, 1)}。</p>` })}
  ${panel({ title: '归因与否决门效果', sub: veto.note || '', body: `<div class="agrps">${grp('共同入选', attr.consensus_t5_mean, attr.consensus_n, 'cons')}${grp('挑战者独享', attr.challenger_only_t5_mean, attr.challenger_only_n, 'only')}${grp('冠军独有（被排除）', attr.champion_excluded_t5_mean, attr.champion_excluded_n, 'out')}</div>
    <h4 class="h4">否决门：被否决 vs 保留候选（T+5 净收益，按信号日等权）</h4>
    <div class="vbar">${[['被否决', veto.vetoed_t5_mean, 'out'], ['保留', veto.survivor_t5_mean, 'cons']].map(([k, v, c]) => `<div class="a-${c}"><span>${k}</span><b>${P(v, 2)}</b></div>`).join('')}<div><span>规避价差</span><b>${P(veto.avoidance_spread_t5, 2)}</b></div></div>
    ${kv([['被否决组更差的信号日', `<span class="n">${esc(veto.days_vetoed_worse ?? '—')}/${esc(veto.days ?? '—')}</span>`], ['冠军入选但会被否决', `<span class="n">${esc(veto.v43_picks_vetoed_n ?? 0)}</span> 只 · T+5 ${P(veto.v43_picks_vetoed_t5_mean)}`], ['冠军入选且未被否决', `T+5 ${P(veto.v43_picks_kept_t5_mean)}`]])}` })}
  </div>
  ${panel({ title: '逐信号日明细', sub: '重合 = 两套 Top-20 交集；否决 = 被否决/进入否决门的候选数', body: `<details><summary>展开 ${esc(m.perSignal.length)} 个信号日</summary><div class="tbl-wrap"><table class="tbl compact"><thead><tr><th>信号日</th><th class="num">重合</th><th class="num">v4.3 T+1</th><th class="num">v4.4 T+1</th><th class="num">v4.3 T+5</th><th class="num">v4.4 T+5</th><th class="num">否决/候选</th><th class="num">冠军被否决</th></tr></thead><tbody>${ps}</tbody></table></div></details>` })}
  ${panel({ title: '回放方法与局限', body: `${kv(Object.entries(doc.methodology || {}).filter(([k]) => k !== 'not_point_in_time').map(([k, v]) => [({ selection: '选股', entry: '入场与计价', v43_rebalance: '冠军调仓', v44_rebalance: '挑战者调仓', cost: '成本', metrics: '指标口径', isolation: '隔离', production_parity: '生产一致性' })[k] || k, esc(v)]))}${arr(doc.methodology?.not_point_in_time).length ? `<p class="note">非时点数据：${arr(doc.methodology.not_point_in_time).map(esc).join('；')}。</p>` : ''}` })}`;
}

export function evidence(d) {
  const r = reviewSeries(d);
  const ev = d.strategyEvaluation || {};
  const dt = d.dualTrack || {};
  const integ = ev.integrity || dt.evaluation_integrity || {};
  const sc = integ.settlement_counts || {};
  const tot = (sc.settled || 0) + (sc.pending_settlement || 0) + (sc.data_missing || 0);
  const seg = (k, cls, label) => (sc[k] ? `<i class="${cls}" style="flex:${sc[k]}" title="${label} ${sc[k]}"></i>` : '');
  const tracks = Object.entries(ev.strategies || {}).map(([id, s]) => `<tr><td><b>${esc(s.strategy_name)}</b><br><code>${esc(id)}</code></td><td>${badge(s.flow_status || '—', toneOf(s.flow_status))}</td><td class="w-meter">${meter(s.sample_trade_days, s.required_trade_days || 60, { label: `${s.sample_trade_days ?? 0}/${s.required_trade_days || 60}` })}</td><td>${badge(s.effectiveness_status === 'not_validated' ? '尚未验证' : s.effectiveness_status || '—', s.effectiveness_status === 'validated' ? 'pass' : 'warn')}</td><td class="chips">${arr(s.failed_gates).map((g) => `<span class="chip">${esc(g)}</span>`).join('')}</td></tr>`).join('');
  const meth = ev.methodology || d.reviewTrack?.methodology || {};
  const labels = r.series.map((s) => s.date);
  const exc = Object.entries(integ.ai_exclusion_counts || {}).map(([k, v]) => [k, `<span class="n">${v}</span>`]);
  return `<div class="layout-wide arena">
  ${arenaSection(d)}
  <h2 class="sec-h">前瞻验证（真实发布记录）</h2>
  ${panel({ title: '历史战绩', sub: 'Top-20 等权 · T+1 开盘入场 · 含成本', meta: ageBadge(d.reviewTrack?.trade_date, tradeDate(d)), body: r.days ? `
    <div class="stats s5">${stat('信号日', r.days)}${stat('次日均值', pct(r.avgRet, 3))}${stat('平均命中率', `${fmt(r.avgHit, 1)}%`)}${stat('累计（复利）', pct(r.cum))}${stat('最大回撤', pct(r.maxDD))}</div>
    <div class="charts"><figure><figcaption>累计净值曲线</figcaption>${line(r.series.map((s) => s.cum), { h: 160 })}</figure>
    <figure><figcaption>逐日次日收益（%）</figcaption>${bars(r.series.map((s) => s.ret), { h: 160, labels })}</figure>
    <figure><figcaption>逐日命中率（%，50 为基准）</figcaption>${line(r.series.map((s) => s.hit - 50), { h: 120, cls: 'ln hit' })}</figure></div>
    <p class="note">${dateCn(r.series[0]?.date)} 至 ${dateCn(r.series[r.days - 1]?.date)}。历史段混合了多个策略版本，仅作审计对照，不用于晋级判定。</p>` : missing(SOURCES.reviewTrack.label) })}
  ${panel({ title: '前瞻验证轨道', sub: '每组需 ≥60 个新交易日样本并通过全部闸门', body: tracks ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>策略组</th><th>运行</th><th>样本进度</th><th>有效性</th><th>未通过的闸门</th></tr></thead><tbody>${tracks}</tbody></table></div>${ev.historical_audit?.note ? `<p class="note">${esc(ev.historical_audit.note)}</p>` : ''}` : missing(SOURCES.strategyEvaluation.label) })}
  <div class="row2">
  ${panel({ title: '结算完整性', body: tot ? `<div class="stack">${seg('settled', 'pass', '已结算')}${seg('pending_settlement', 'warn', '待结算')}${seg('data_missing', 'block', '数据缺失')}</div>${kv([['已结算', sc.settled], ['待结算', sc.pending_settlement], ['数据缺失', sc.data_missing], ['虚假/不可能收益', integ.fake_or_impossible_return_count], ['代理价格行', integ.proxy_rows], ['AI 改名次行', integ.rank_changed_rows]].map(([k, v]) => [k, `<span class="n">${esc(v ?? '—')}</span>`]))}` : missing(SOURCES.strategyEvaluation.label) })}
  ${panel({ title: 'AI 证据剔除', sub: `合格 ${integ.ai_effectiveness_eligible_rows ?? '—'} 行`, body: exc.length ? `${kv(exc)}<p class="note">仅当日新鲜证据可计入 AI 有效性；未来回填、缺证据时间等一律剔除。</p>` : '<p class="muted">无</p>' })}
  </div>
  ${panel({ title: '方法论', body: kv([['信号时点', esc(meth.signal_timing)], ['主持有期', meth.primary_holding_period_days ? `${meth.primary_holding_period_days} 日` : '—'], ['成本', `${meth.round_trip_cost ?? '—'}（压力 ${meth.stress_round_trip_cost ?? '—'}）`], ['基准', esc(meth.benchmark)], ['缺价处理', esc(meth.missing_price_policy || '—')], ['代理价格', esc(meth.proxy_policy || '—')], ['AI 口径', esc(meth.ai_policy || '—')]]) })}
  </div>`;
}

export function lab(d) {
  const ref = tradeDate(d);
  const s3 = d.s3Watchlist;
  const s3b = s3 ? (() => {
    const c = s3.cumulative || {};
    const [done, total] = String(c.progress_60 || '0/60').split('/').map(Number);
    const ser = arr(s3.daily_series);
    return `<p class="honest">${esc(s3.honesty_banner)}</p>${meter(done, total || 60, { label: `前瞻 ${c.progress_60 || '—'} 笔`, tone: 'warn' })}
    <div class="stats">${stat('命中日', `${c.hit_days ?? '—'}/${c.n_days ?? '—'}`, c.hit_days_pct != null ? `${c.hit_days_pct}%` : '')}${stat('累计（中位成本）', pct(c.cum_net_med_pct))}${stat('累计（p75 成本）', pct(c.cum_net_p75_pct))}${stat('最大回撤', pct(c.max_drawdown_med_pct))}</div>
    <div class="charts c2"><figure><figcaption>累计（中位）</figcaption>${line(ser.map((x) => x.cum_med_pct), { h: 90 })}</figure><figure><figcaption>累计（p75）</figcaption>${line(ser.map((x) => x.cum_p75_pct), { h: 90 })}</figure></div>
    ${s3.backtest_context?.p75_warning ? `<p class="note">${esc(s3.backtest_context.p75_warning)}</p>` : ''}`;
  })() : missing(SOURCES.s3Watchlist.label);
  const se = d.setupEngine;
  const setups = se ? arr(se.setups).map((s) => `<li class="setup st-${esc(String(s.status || '').toLowerCase())}"><div><b>${esc(s.name_cn || s.id)}</b>${badge(s.status_cn || s.status, /REJECT/i.test(s.status) ? 'block' : /CANDIDATE/i.test(s.status) ? 'pass' : 'mute')}</div><p>${esc(s.hypothesis || '')}</p>${s.verdict_note ? `<small>${esc(s.verdict_note)}</small>` : ''}</li>`).join('') : '';
  const wts = se?.wts_tracking;
  const dt = d.dualTrack || {};
  const shortT = arr(dt.short_track_strategies).map((s) => `<tr><td><b>${esc(s.display_name)}</b><br><small>${esc(s.role || '')}</small></td><td>${badge(s.operational_status || s.status, toneOf(s.operational_status === 'healthy' ? 'pass' : s.status))}</td><td class="num">${s.candidate_count ?? '—'}</td><td>${esc(s.failure_reason || '')}</td></tr>`).join('');
  const evt = dt.event_track;
  const fe = d.factorEvolution;
  const fc = arr(fe?.factor_contribution).map((f) => `<tr><td>${esc(f.factor_name)}</td><td class="num">${fmt(f.this_week_ic, 4)}</td><td class="num">${fmt(f.last_week_ic, 4)}</td><td>${f.trend === 'up' ? '<span class="n up">↑</span>' : f.trend === 'down' ? '<span class="n down">↓</span>' : '·'}</td><td class="num">${fmt(f.weight, 3)}</td></tr>`).join('');
  const arch = { ...(d.decisionState?.archived_strategies || {}) };
  const archived = Object.values(arch).map((a) => `<li><b>${esc(a.strategy_name)}</b> <code>${esc(a.strategy_id)}</code><p>${esc(a.reason || '')}</p></li>`).join('');
  const retired = arr(dt.retired_strategies).filter((id) => !arch[id]).map((id) => `<span class="chip">${esc(id)}</span>`).join('');
  const sen = d.sentiment;
  const dist = sen ? Object.entries(sen.distribution || {}).map(([k, v]) => `<li><span>${esc(k)}</span>${meter(v.ratio, 1)}<b>${v.count}</b></li>`).join('') : '';
  return `<div class="layout-wide">
  ${dt.honesty_banner ? `<p class="honest">${esc(dt.honesty_banner)}</p>` : ''}
  <div class="row2">
  ${panel({ title: '双轨影子组合', sub: `运行 ${dt.flow_status || '—'} · 有效性 ${dt.effectiveness_status || '—'}`, meta: ageBadge(dt.trade_date, ref), body: shortT ? `<div class="tbl-wrap"><table class="tbl compact"><thead><tr><th>组别</th><th>状态</th><th class="num">候选</th><th>说明</th></tr></thead><tbody>${shortT}</tbody></table></div>${evt ? `<h4 class="h4">事件轨 · ${esc(evt.display_name)}</h4>${kv([['信号日', dateCn(evt.signal_date)], ['新公告事件', evt.new_announcement_event_count], ['合格事件', evt.eligible_event_count], ['样本', `${evt.sample_trade_days ?? '—'} 交易日`], ['证据范围', esc(evt.evidence_scope || '')]].map(([k, v]) => [k, esc(v)]))}` : ''}` : missing(SOURCES.dualTrack.label) })}
  ${panel({ title: s3?.title || 'S3 分时形态', sub: s3?.ranking_rule || '', meta: s3 ? ageBadge(s3.latest_signal_date, ref) : '', body: s3b })}
  </div>
  ${panel({ title: '剧本引擎', sub: se?.paradigm ? '从横截面打分转向剧本触发' : '', meta: se ? ageBadge(se.generated_at, ref) : '', body: se ? `<p class="note">${esc(se.paradigm)}</p><ul class="setups">${setups}</ul>${wts ? `<h4 class="h4">${esc(wts.name)}</h4>${kv([['状态', esc(wts.status_cn || wts.status)], ['账本', `${wts.ledger_n}/${wts.threshold}`], ['首日净均值', pct(wts.first_day_net_mean_pct, 3)], ['正/负', `${wts.first_day_positive}/${wts.first_day_negative}`]])}` : ''}` : missing(SOURCES.setupEngine.label) })}
  <div class="row2">
  ${panel({ title: '因子进化（O2C）', meta: fe ? ageBadge(fe.trade_date, ref) : '', body: fc ? `<div class="tbl-wrap"><table class="tbl compact"><thead><tr><th>因子</th><th class="num">本周 IC</th><th class="num">上周 IC</th><th>趋势</th><th class="num">权重</th></tr></thead><tbody>${fc}</tbody></table></div>${fe.evolution_recommendation ? `<p class="note">升级建议：${fe.evolution_recommendation.should_upgrade ? '升级' : '不升级'}（${esc(fe.evolution_recommendation.reason)}）</p>` : ''}` : missing(SOURCES.factorEvolution.label) })}
  ${panel({ title: 'AI 观点分布', sub: sen ? `近 ${sen.window_days} 日 · ${sen.sample_count} 条` : '', body: dist ? `<ul class="hbars">${dist}</ul><p class="note">${esc(sen.source)}</p>` : missing(SOURCES.sentiment.label) })}
  </div>
  ${panel({ title: '已归档策略', sub: '保留死因，不换皮重来', body: `${archived ? `<ul class="arch">${archived}</ul>` : ''}${retired ? `<div class="chips">${retired}</div>` : ''}` || '<p class="muted">无</p>' })}
  </div>`;
}

export function system(d) {
  const sv = d.systemVerdict || {};
  const ps = sv.pipeline_status || {};
  const dc = sv.date_contract || {};
  const pg = d.publishGuard;
  const sh = d.systemHealth;
  const reg = d.strategyRegistry;
  const rs = d.strategyRunState;
  const flag = (v) => badge(v === true ? '是' : v === false ? '否' : '—', v === true ? 'pass' : v === false ? 'block' : 'mute');
  return `<div class="layout-wide"><div class="row2">
  ${panel({ title: '流水线状态', sub: ps.lifecycle_label || '', body: `${kv([['选股', flag(ps.selection_ok)], ['研究', flag(ps.research_ok)], ['发布', flag(ps.publish_ok)], ['发布已恢复', flag(ps.publish_recovered)], ['编排器', flag(ps.orchestrator_ok)], ['执行权限', esc(ps.execution_authority || '—')]])}${ps.note ? `<p class="note">${esc(ps.note)}</p>` : ''}` })}
  ${panel({ title: '日期合同', sub: dc.summary || '', body: `<p>${badge(dc.status || '—', toneOf(dc.status))}</p>${kv(Object.entries(dc.checked_fields || {}).map(([k, v]) => [k, `<code>${esc(v)}</code>`]))}` })}
  </div><div class="row2">
  ${panel({ title: '发布守卫', meta: pg ? badge(pg.ok ? '通过' : '失败', pg.ok ? 'pass' : 'block') : '', body: pg ? `<ul class="checks">${arr(pg.checks).map((c) => `<li>${badge(c.ok ? '✓' : '✗', c.ok ? 'pass' : 'block')}<b>${esc(c.name)}</b><span>${esc(c.detail || '')}</span></li>`).join('')}</ul>${kv([['最新提交', `<code>${esc(String(pg.latest_commit || '').slice(0, 8))}</code>`], ['合同版本', esc(pg.contract_version)]])}` : missing(SOURCES.publishGuard.label) })}
  ${panel({ title: '系统健康', meta: sh ? badge(sh.ok ? '健康' : '异常', sh.ok ? 'pass' : 'block') : '', body: sh ? kv(Object.entries(sh.checks || {}).map(([k, v]) => [k, typeof v === 'boolean' ? flag(v) : `<span class="n">${esc(v)}</span>`])) : missing(SOURCES.systemHealth.label) })}
  </div>
  ${panel({ title: '策略档案', body: reg ? `<div class="tbl-wrap"><table class="tbl compact"><thead><tr><th>策略</th><th>版本</th><th>定位</th><th>证据类型</th><th>权重</th></tr></thead><tbody>${arr(reg.strategies).map((s) => `<tr><td><b>${esc(s.strategy_name)}</b><br><code>${esc(s.canonical_strategy_id || s.strategy_id)}</code></td><td><code>${esc(s.strategy_version || '')}</code></td><td>${esc(s.positioning || '')}</td><td>${esc(s.evidence_type || '')}</td><td class="num">${fmt(rs?.strategy_weights?.[s.strategy_id], 2)}</td></tr>`).join('')}</tbody></table></div>${arr(reg.observation_strategies).length ? `<h4 class="h4">观察策略</h4><div class="chips">${arr(reg.observation_strategies).map((s) => `<span class="chip">${esc(s.strategy_name || s.strategy_id || s)}</span>`).join('')}</div>` : ''}` : missing(SOURCES.strategyRegistry.label) })}
  </div>`;
}

export const VIEWS = { today, candidates, market, evidence, lab, system };
