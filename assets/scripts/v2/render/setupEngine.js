// v4/render/setupEngine.js — 「策略重造 · 剧本引擎」章节（系统说明页 research-lab / data-view="research"）。
//
// 导出：setupEngineSection(model) —— 纯函数（model → HTML 字符串），无 document/window。
// 数据来源：model.setupEngine（data/latest/setup_engine_status.json；缺失 → 整段退占位，绝不编造）。
//
// 呈现（遵循页面既有视觉：sectionHead / badge / elevatedCard / gate-grid 抬升式卡片）：
//   1. 范式转变一段话（从横截面打分到剧本引擎，为什么——引用已冻结的证伪结论）；
//   2. 三层架构图示（简洁 HTML/CSS，无外链）；
//   3. 6 剧本状态表（状态徽章；关键数字为 null 显示「闯关中 / 待终审」，绝不编造）；
//   4. WTS 前瞻跟踪卡：n/60 进度条 +「60 笔审判制」说明 + 首日负值如实展示；
//   5. 诚实横幅。
//
// 诚实红线（与 DESIGN-V3 第 0 节一致，逐条落实）：
//   - 不出现任何收益承诺 / 预期收益数字 /「可部署」字样；
//   - REJECTED 与负值必须和正值同等醒目（负值走 pctHtml 红涨绿跌，首日负均值突出显示）；
//   - 所有样本内数字必须标注「样本内」；剧本关键数字未终审前一律显示占位，不编造。

import { escapeHtml, safeText, formatNumber, formatPct, pctHtml } from './format.js';
import { badge, sectionHead, emptySection, elevatedCard, dataTable } from './components.js';

// 剧本 status（注册表原值）→ 客户语言徽章文案 + 色调。
// 预注册 / 待冻结格点均为「闯关前」的中性状态（flat/info）；REJECTED 与 CANDIDATE 是终审结果。
const SETUP_STATUS = {
  registered: { label: '预注册 · 待闯关', tone: 'info' },
  registered_pending_grid: { label: '预注册 · 待冻结格点', tone: 'flat' },
  running: { label: '闯关中', tone: 'warn' },
  REJECTED: { label: 'REJECTED · 已证伪归档', tone: 'bad' },
  CANDIDATE: { label: 'CANDIDATE · 前瞻跟踪', tone: 'ok' }
};

function setupStatusMeta(setup) {
  const raw = safeText(setup.status, '');
  return SETUP_STATUS[raw] || { label: safeText(setup.status_cn, raw || '—'), tone: 'flat' };
}

// 关键数字：终审前全部为 null。有真实数字才显示（并标「样本内」）；否则显示占位，绝不编造。
function keyNumbersCell(setup) {
  const meta = setupStatusMeta(setup);
  const kn = setup.key_numbers || {};
  const net = kn.insample_net_per_trade_pct ?? kn.confirm_minus_unconfirm_pp;
  const t = kn.t_stat ?? kn.z_stat;
  if (net === null || net === undefined) {
    // 终审前如实标注：REJECTED 已有死因（归档），其余为闯关前 / 闯关中。
    const text = meta.tone === 'bad' ? '已证伪 · 死因归档' : '闯关中 · 待终审';
    return { html: `<span class="soft">${escapeHtml(text)}</span>` };
  }
  // 有真实数字：样本内净值走红涨绿跌上色，附 t/z 值，一律标「样本内」。
  const netHtml = pctHtml(Number(net), 2);
  const tText = t === null || t === undefined ? '' : ` · t=${escapeHtml(formatNumber(Number(t), 1))}`;
  return { html: `<span class="setup-net">样本内 ${netHtml}${tText}</span>` };
}

function paradigmHtml(data) {
  const paradigm = safeText(data.paradigm, '');
  const why = safeText(data.paradigm_why, '');
  if (!paradigm && !why) return '';
  return `${sectionHead('范式转变：从「打分选股」到「剧本引擎」', '为什么必须换架构。')}
    ${elevatedCard(`<div class="setup-paradigm">
      ${paradigm ? `<p class="setup-paradigm-lead">${escapeHtml(paradigm)}</p>` : ''}
      ${why ? `<p class="setup-paradigm-why">${escapeHtml(why)}</p>` : ''}
    </div>`, { className: 'setup-paradigm-card' })}`;
}

function layersHtml(data) {
  const layers = Array.isArray(data.three_layers) ? data.three_layers : [];
  if (!layers.length) return '';
  const cards = layers.map((layer, idx) => `<section class="setup-layer">
      <div class="setup-layer-head">
        <span class="setup-layer-index num" aria-hidden="true">第 ${idx + 1} 层</span>
        <h4>${escapeHtml(safeText(layer.name, ''))}</h4>
      </div>
      <p>${escapeHtml(safeText(layer.summary, ''))}</p>
    </section>`).join('\n');
  const gates = safeText(data.gates, '');
  const cost = safeText(data.cost_model, '');
  const notes = [
    gates ? `<p class="setup-meta-line"><strong>验证协议</strong>${escapeHtml(gates)}</p>` : '',
    cost ? `<p class="setup-meta-line"><strong>成本口径</strong>${escapeHtml(cost)}</p>` : ''
  ].filter(Boolean).join('\n');
  return `${sectionHead('三层架构', '每个剧本都要穿过这三层，才谈得上一次交易。')}
    <div class="setup-layers">${cards}</div>
    ${notes ? `<div class="setup-arch-notes">${notes}</div>` : ''}`;
}

function setupsTableHtml(data) {
  const setups = Array.isArray(data.setups) ? data.setups : [];
  if (!setups.length) {
    return `${sectionHead('六大剧本状态')}
      ${emptySection('六大剧本状态', '本期未读取到剧本注册表内容。')}`;
  }
  const rows = setups.map((setup) => {
    const meta = setupStatusMeta(setup);
    // S3 分时形态日频化剧本（CANDIDATE · 前瞻跟踪）：链接到独立的 top-20 观察看板。
    const isS3 = /^S3(_|$)/i.test(safeText(setup.id, ''));
    const watchLink = isS3
      ? '<a class="text-link setup-watch-link" href="./s3-watch.html">→ top-20 观察看板</a>'
      : '';
    return [
      { html: `<div class="setup-name">${escapeHtml(safeText(setup.name_cn, setup.id))}</div>
        <div class="setup-id soft num">${escapeHtml(safeText(setup.id, ''))}</div>
        ${watchLink}` },
      { html: badge(meta.label, meta.tone) },
      { html: `<span class="setup-hyp">${escapeHtml(safeText(setup.hypothesis, ''))}</span>` },
      { html: `<span class="setup-novelty">${escapeHtml(safeText(setup.novelty, '—'))}</span>` },
      { html: `<span class="soft">${escapeHtml(safeText(setup.decision_point, '—'))}</span>` },
      keyNumbersCell(setup)
    ];
  });
  const disc = safeText(data.discipline, '');
  const life = safeText(data.lifecycle_note, '');
  return `${sectionHead('六大剧本状态', '每个剧本 = T 日稀疏条件 × 合法决策点触发 × 固定出场。关键数字在主会话终审前一律显示「闯关中 / 待终审」，不编造。')}
    ${dataTable({
      columns: ['剧本', '状态', '假设', '新颖性', '决策点', '关键数字'],
      rows,
      emptyText: '暂无剧本'
    })}
    ${disc ? `<p class="help-text setup-discipline"><strong>预注册纪律：</strong>${escapeHtml(disc)}</p>` : ''}
    ${life ? `<p class="help-text">${escapeHtml(life)}</p>` : ''}`;
}

// WTS 前瞻跟踪卡：n/60 进度条 + 60 笔审判制说明 + 首日负均值如实展示。
function wtsCardHtml(data) {
  const wts = data.wts_tracking;
  if (!wts || typeof wts !== 'object') {
    return `${sectionHead('WTS 前瞻裁判席')}
      ${emptySection('WTS 前瞻裁判席', '本期未读取到 WTS 前瞻跟踪状态。')}`;
  }
  const n = Number(wts.ledger_n);
  const threshold = Number(wts.threshold) > 0 ? Number(wts.threshold) : 60;
  const hasN = Number.isFinite(n);
  const pct = hasN ? Math.min(100, Math.round((n / threshold) * 100)) : 0;
  const progressText = hasN ? `${formatNumber(n)} / ${formatNumber(threshold)} 笔` : `待积累 / ${formatNumber(threshold)} 笔`;

  // 首日净均值：负值必须和正值同等醒目——走 pctHtml 红涨绿跌上色。
  const firstMean = wts.first_day_net_mean_pct;
  const hasMean = firstMean !== null && firstMean !== undefined && Number.isFinite(Number(firstMean));
  const pos = Number(wts.first_day_positive);
  const neg = Number(wts.first_day_negative);
  const breakdown = (Number.isFinite(pos) && Number.isFinite(neg))
    ? `<span class="wts-breakdown">正 ${formatNumber(pos)} 笔 / 负 ${formatNumber(neg)} 笔</span>`
    : '';

  const firstDayHtml = hasMean
    ? `<div class="wts-firstday">
        <span class="wts-firstday-label">前瞻首日净均值（@30bp，样本外）</span>
        <span class="wts-firstday-val num">${pctHtml(Number(firstMean), 3)}</span>
        ${breakdown}
        <p class="wts-firstday-note">首日为负如实展示——前瞻跟踪就是持续的衰减监测器，不因难看而隐藏。</p>
      </div>`
    : '';

  const statusMeta = SETUP_STATUS.CANDIDATE; // WTS 是唯一幸存者，但只在裁判席上
  const verdictRule = safeText(wts.verdict_rule, '');
  const caveat = safeText(wts.caveat, '');
  const note = safeText(wts.note, '');
  const decisionPoint = safeText(wts.decision_point, '');

  const inner = `<div class="wts-head">
      <div>
        <h4>${escapeHtml(safeText(wts.name, '竞价弱转强（WTS）'))}</h4>
        ${decisionPoint ? `<p class="soft wts-decision">${escapeHtml(decisionPoint)}</p>` : ''}
      </div>
      <div class="wts-badges">
        ${badge(safeText(wts.status_cn, '已冻结 · 仅前瞻跟踪'), 'warn')}
        ${badge(`${formatNumber(Number.isFinite(Number(wts.rules)) ? Number(wts.rules) : 4)} 条冻结规则`, 'flat')}
      </div>
    </div>
    <div class="wts-progress" role="img" aria-label="前瞻跟踪进度 ${escapeHtml(progressText)}，审判门槛 ${escapeHtml(String(threshold))} 笔">
      <div class="wts-progress-top">
        <span class="wts-progress-label">前瞻跟踪进度</span>
        <span class="wts-progress-val num">${escapeHtml(progressText)}</span>
      </div>
      <div class="wts-progress-track">
        <div class="wts-progress-fill" style="width:${pct}%"></div>
        <span class="wts-progress-goal" aria-hidden="true"></span>
      </div>
      <div class="wts-progress-scale" aria-hidden="true">
        <span>0</span><span>${escapeHtml(String(threshold))} · 可复议门槛</span>
      </div>
    </div>
    ${firstDayHtml}
    ${verdictRule ? `<p class="help-text wts-rule"><strong>60 笔审判制：</strong>${escapeHtml(verdictRule)}</p>` : ''}
    ${caveat ? `<p class="help-text wts-caveat"><strong>REGIME 依赖警告：</strong>${escapeHtml(caveat)}</p>` : ''}
    ${note ? `<p class="help-text">${escapeHtml(note)}</p>` : ''}`;

  return `${sectionHead('WTS 前瞻裁判席', '唯一走完全套证伪流程的幸存者——只上裁判席，不宣布可交易。')}
    ${elevatedCard(inner, { className: 'wts-card', tone: 'warn' })}`;
}

function bannerHtml(data) {
  const text = safeText(data.honesty_banner, '');
  if (!text) return '';
  return `<div class="setup-honesty-banner" role="note">
    <span class="setup-honesty-icon" aria-hidden="true">!</span>
    <p>${escapeHtml(text)}</p>
  </div>`;
}

// 语料挖掘卡：713 个聚宽精选策略的图谱+合成+判决全账（数据缺失 → 整块不渲染，不编造）。
function corpusMiningHtml(data) {
  const cm = data.corpus_mining;
  if (!cm || typeof cm !== 'object') return '';
  const highlights = Array.isArray(cm.atlas_highlights) ? cm.atlas_highlights : [];
  const guardrails = Array.isArray(cm.guardrails_gained) ? cm.guardrails_gained : [];
  return `${sectionHead(safeText(cm.title, '策略语料挖掘'), '外部策略语料只作想法池，不作证据：全部结论由本系统五道闸引擎重新产生。')}
    ${elevatedCard(`<div class="setup-paradigm">
      ${cm.funnel ? `<p class="setup-paradigm-lead"><strong>漏斗全账：</strong>${escapeHtml(safeText(cm.funnel, ''))}</p>` : ''}
      ${cm.structural_conclusion ? `<p class="setup-paradigm-why"><strong>结构性结论：</strong>${escapeHtml(safeText(cm.structural_conclusion, ''))}</p>` : ''}
      ${highlights.length ? `<ul class="setup-corpus-list">${highlights.map((h) => `<li>${escapeHtml(safeText(h, ''))}</li>`).join('')}</ul>` : ''}
      ${guardrails.length ? `<p class="help-text"><strong>本轮新增护栏：</strong>${guardrails.map((g) => escapeHtml(safeText(g, ''))).join('；')}</p>` : ''}
    </div>`, { className: 'setup-paradigm-card' })}`;
}

// 章节入口。model.setupEngine 缺失或空 → 整段退占位（不编造）。
export function setupEngineSection(model) {
  const data = model && model.setupEngine ? model.setupEngine : {};
  const missing = model && typeof model.isMissing === 'function' && model.isMissing('setupEngine');
  const hasBody = data && (Array.isArray(data.setups) && data.setups.length || data.wts_tracking);

  const head = sectionHead(
    '策略重造 · 剧本引擎',
    '这是一份公开的研究议程：系统正在把「每日给全市场打分选股」重造为「今天哪个剧本被触发、值不值得打」。全部内容为研究记录，不构成任何投资建议。'
  );

  if (missing || !hasBody) {
    return `<div class="setup-engine">
      ${head}
      ${emptySection('策略重造 · 剧本引擎', '剧本引擎状态文件暂未生成，本章节暂不展示；系统说明页其余内容不受影响。')}
    </div>`;
  }

  return `<div class="setup-engine">
    ${head}
    ${bannerHtml(data)}
    ${paradigmHtml(data)}
    ${layersHtml(data)}
    ${setupsTableHtml(data)}
    ${corpusMiningHtml(data)}
    ${wtsCardHtml(data)}
  </div>`;
}
