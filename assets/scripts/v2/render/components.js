// v3/render/components.js — 共享 UI 组件（纯函数：参数 → HTML 字符串，无 DOM 依赖，Node 可执行）。
// v4 视觉：Brokerage Pro / 铜金抬升式卡片化（DESIGN-V4 第 1、3 节）。组件结构稳定、签名兼容 v3，
// 仅升级呈现层与新增焦点区构件（verdictPill / capsule / riskGauge 刻度盘 chrome / elevatedCard / kpiCard）。
//
// 转义纪律（DESIGN-V3 第 5 节，仍是铁律）：本文件所有组件一律在内部 escapeHtml；
// 唯一例外是以 Html 结尾命名的参数（valueHtml/noteHtml/innerHtml/bodyHtml 等），调用方自行保证安全。
//
// 公开 API（v3 全部保留）：
//   badge(text, tone='info')                       — 状态徽章；tone：ok/warn/bad/info/flat/up/down/brand
//   pill(label, value, tone='info')                — 顶栏/元信息胶囊（点 + 标签 + 数值）
//   chip(label, value)                             — 量化因子小标签（label + 数值）
//   chipList(items)                                — [{label, value}] → 因子标签组
//   statCard({title, value, valueHtml, note, noteHtml, tone, small}) — KPI 统计卡（抬升式）
//   sectionHead(title, sub='', link=null)          — 分区标题；link = {href, label}
//   missingSection(label, reason)                  — 数据文件缺失占位（文件 404/解析失败时用）
//   emptySection(title, explanation)               — 空数组解释（“今日该策略无入选标的”类，与缺失区分）
//   aiStatusBadge(item)                            — AI 状态徽章（ai-full / ai-stale / ai-none，spec 0.2）
//   disclaimerFooter()                             — 页脚免责声明（spec 0.7 固定文案）
//   DISCLAIMER_TEXT                                — 免责声明纯文本常量
//   tabsBar(tabs, activeKey, {groupId})            — 页内 Tab 栏（data-tabs 约定，见下）
//   tabPanel(key, innerHtml, {active, groupId})    — 与 tabsBar 配套的内容面板
//   dataTable({columns, rows, emptyText, tableClass}) — 表格辅助（自动包 .scroll-x 横滚容器）
//   riskGauge(score, label)                        — 风险刻度盘（charts.gauge + 铜金 chrome：刻度 + 状态胶囊）
//   themeToggle(extraClass)                        — 主题切换按钮组（data-theme-choice 约定）
//   dateBadge(tradeDate)                           — 顶栏「数据日期」徽章
// 公开 API（v4 新增，焦点区/卡片化构件）：
//   verdictPill(text, tone='brand')                — Hero 大裁决上方的脉冲标签
//   capsule(label, value|valueHtml, tone, icon)    — Hero 纪律/环境胶囊
//   elevatedCard(innerHtml, {className, tone})     — 抬升式卡片容器（背景+阴影+hover 上浮）
//   kpiCard({label, value, valueHtml, note, tone}) — 招牌大数字 KPI 卡（战绩/复盘横幅用）
//
// data-tabs 约定（app.js 据此挂事件）：
//   <div class="tabs-bar" data-tabs="GROUP">…<button data-tab="KEY" aria-selected>…</div>
//   <section class="tab-panel" data-tab-panel="KEY" data-tabs-group="GROUP" [hidden]>…</section>
//   点击按钮 → 同组面板按 KEY 切换 hidden，并同步地址栏 #tab=KEY。

import {
  escapeHtml, safeText, formatNumber, dateCn,
  aiStatusOf, aiSourceDate, riskTone
} from './format.js';
import { gauge } from './charts.js';

// 色调名归一化：兼容旧值（pass/fail），统一输出 tone-* 语义类。
const TONE_ALIASES = {
  ok: 'ok', pass: 'ok', good: 'ok',
  warn: 'warn', caution: 'warn',
  bad: 'bad', fail: 'bad', danger: 'bad',
  info: 'info', flat: 'flat', neutral: 'flat',
  up: 'up', down: 'down', brand: 'brand'
};

function toneClass(tone) {
  return `tone-${TONE_ALIASES[safeText(tone, 'info')] || 'info'}`;
}

export function badge(text, tone = 'info') {
  return `<span class="badge ${toneClass(tone)}">${escapeHtml(text)}</span>`;
}

export function pill(label, value, tone = 'info') {
  return `<span class="pill ${toneClass(tone)}"><span class="pill-dot" aria-hidden="true"></span><span class="pill-label">${escapeHtml(label)}</span><span class="pill-value num">${escapeHtml(value)}</span></span>`;
}

export function chip(label, value) {
  const valueText = value === null || value === undefined || value === '' ? '—' : String(value);
  return `<span class="chip"><span class="chip-label">${escapeHtml(label)}</span><span class="chip-value num">${escapeHtml(valueText)}</span></span>`;
}

export function chipList(items) {
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!list.length) return '';
  return `<div class="chip-list">${list.map((item) => chip(item.label, item.value)).join('')}</div>`;
}

// KPI 统计卡（抬升式卡片）。value/note 默认转义；valueHtml/noteHtml 传入时按原样使用（调用方负责安全）。
// 结构与类名（stat-card / stat-title / stat-value num / stat-note）稳定——回归测试据此取值，勿改。
export function statCard({ title, value, valueHtml, note, noteHtml, tone = '', small = false } = {}) {
  const valueContent = valueHtml != null ? valueHtml : escapeHtml(safeText(value));
  const noteContent = noteHtml != null ? noteHtml : (note ? escapeHtml(note) : '');
  return `<section class="stat-card${tone ? ` ${toneClass(tone)}` : ''}">
    <div class="stat-title">${escapeHtml(title)}</div>
    <div class="stat-value num${small ? ' small' : ''}">${valueContent}</div>
    ${noteContent ? `<div class="stat-note">${noteContent}</div>` : ''}
  </section>`;
}

// 招牌大数字 KPI 卡（战绩/复盘 KPI 横幅）：比 statCard 更突出的大数字 + 上方小标签。
// 复用 stat-value num 类以便共享 tabular 数字样式；额外 kpi-card 类提供更大字号与抬升外观。
export function kpiCard({ label, value, valueHtml, note, noteHtml, tone = '' } = {}) {
  const valueContent = valueHtml != null ? valueHtml : escapeHtml(safeText(value));
  const noteContent = noteHtml != null ? noteHtml : (note ? escapeHtml(note) : '');
  return `<section class="kpi-card${tone ? ` ${toneClass(tone)}` : ''}">
    <div class="kpi-label">${escapeHtml(label)}</div>
    <div class="kpi-value num">${valueContent}</div>
    ${noteContent ? `<div class="kpi-note">${noteContent}</div>` : ''}
  </section>`;
}

// 抬升式卡片容器：把任意已构造好的 innerHtml 包进铜金抬升卡（背景 + 阴影 + hover 上浮）。
// innerHtml 按约定不转义（以 Html 命名）；className/tone 内部安全处理。
export function elevatedCard(innerHtml, { className = '', tone = '' } = {}) {
  const extra = [tone ? toneClass(tone) : '', safeText(className, '')].filter(Boolean).join(' ');
  return `<section class="elevated-card${extra ? ` ${escapeHtml(extra)}` : ''}">${innerHtml}</section>`;
}

export function sectionHead(title, sub = '', link = null) {
  const subHtml = sub ? `<p class="section-sub">${escapeHtml(sub)}</p>` : '';
  const linkHtml = link && link.href
    ? `<a class="text-link section-more" href="${escapeHtml(link.href)}">${escapeHtml(link.label || '查看更多')}</a>`
    : '';
  return `<div class="section-head">
    <div class="section-head-text">
      <h3>${escapeHtml(title)}</h3>
      ${subHtml}
    </div>
    <span class="section-rule" aria-hidden="true"></span>
    ${linkHtml}
  </div>`;
}

// 数据文件缺失（404 / 解析失败）→ 占位说明。与 emptySection（文件在但数组为空）语义不同。
export function missingSection(label, reason = '数据文件缺失或暂未生成') {
  return `<section class="section-missing" role="note">
    <div class="missing-title">${escapeHtml(label)}</div>
    <p>${escapeHtml(reason)}。这部分内容暂时无法展示，其余板块不受影响。</p>
  </section>`;
}

// 空数组 ≠ 文件缺失：数据正常生成、但当日确实没有内容时，给出可理解的解释（spec 0.6）。
export function emptySection(title, explanation = '今日暂无符合条件的内容') {
  return `<section class="section-empty" role="note">
    <div class="empty-title">${escapeHtml(title)}</div>
    <p>${escapeHtml(explanation)}</p>
  </section>`;
}

// AI 状态徽章（spec 0.2）：三种状态显性区分，严禁用模板话术冒充 AI 分析。
export function aiStatusBadge(item = {}) {
  const status = aiStatusOf(item);
  if (status === 'ai-full') {
    return '<span class="badge ai-badge ai-full">AI 已分析</span>';
  }
  if (status === 'ai-stale') {
    const date = aiSourceDate(item);
    return `<span class="badge ai-badge ai-stale">AI 分析（${escapeHtml(date || '旧日')}）</span>`;
  }
  return '<span class="badge ai-badge ai-none">无 AI 分析 · 仅量化信号</span>';
}

export const DISCLAIMER_TEXT = '本站内容为量化模型自动生成的研究记录，不构成任何投资建议。股市有风险，入市需谨慎。历史表现不代表未来收益。';

export function disclaimerFooter() {
  return `<footer class="disclaimer" role="contentinfo">
    <p>${escapeHtml(DISCLAIMER_TEXT)}</p>
  </footer>`;
}

// 页内 Tab 栏。tabs: [{key, label, note?}]；activeKey 缺省取第一项。
export function tabsBar(tabs, activeKey, { groupId = 'main' } = {}) {
  const list = Array.isArray(tabs) ? tabs.filter((tab) => tab && tab.key) : [];
  if (!list.length) return '';
  const active = list.some((tab) => tab.key === activeKey) ? activeKey : list[0].key;
  const buttons = list.map((tab) => {
    const isActive = tab.key === active;
    const note = tab.note ? `<span class="tab-note">${escapeHtml(tab.note)}</span>` : '';
    return `<button type="button" class="tab-btn${isActive ? ' active' : ''}" role="tab" aria-selected="${isActive ? 'true' : 'false'}" data-tab="${escapeHtml(tab.key)}">${escapeHtml(tab.label)}${note}</button>`;
  }).join('');
  return `<div class="tabs-bar scroll-x" role="tablist" data-tabs="${escapeHtml(groupId)}">${buttons}</div>`;
}

// 与 tabsBar 配套的面板。innerHtml 按约定不转义（以 Html 结尾参数）。
export function tabPanel(key, innerHtml, { active = false, groupId = 'main' } = {}) {
  return `<section class="tab-panel" role="tabpanel" data-tab-panel="${escapeHtml(key)}" data-tabs-group="${escapeHtml(groupId)}"${active ? '' : ' hidden'}>${innerHtml}</section>`;
}

// 表格辅助。columns: 字符串或 {label, align:'right'|'left'}；
// rows: 单元格数组；单元格可为原始值（转义）或 {text}|{html, align, className}。
// 自动包 .scroll-x（窄屏横滚 + 渐变提示）；rows 为空时渲染 emptyText 行。
export function dataTable({ columns = [], rows = [], emptyText = '暂无记录', tableClass = '' } = {}) {
  const colDefs = columns.map((col) => (typeof col === 'object' && col !== null ? col : { label: col }));
  const headHtml = colDefs.map((col) => `<th${col.align === 'right' ? ' class="ta-r"' : ''} scope="col">${escapeHtml(col.label)}</th>`).join('');

  const cellHtml = (cell, idx) => {
    const colAlign = colDefs[idx] && colDefs[idx].align === 'right';
    if (cell !== null && typeof cell === 'object' && !Array.isArray(cell)) {
      const classes = [
        cell.className || '',
        (cell.align === 'right' || (colAlign && cell.align !== 'left')) ? 'num ta-r' : ''
      ].filter(Boolean).join(' ');
      const content = cell.html != null ? cell.html : escapeHtml(safeText(cell.text));
      return `<td${classes ? ` class="${classes}"` : ''}>${content}</td>`;
    }
    return `<td${colAlign ? ' class="num ta-r"' : ''}>${escapeHtml(safeText(cell))}</td>`;
  };

  const bodyHtml = rows.length
    ? rows.map((row) => `<tr>${(row || []).map(cellHtml).join('')}</tr>`).join('\n')
    : `<tr><td class="table-empty" colspan="${Math.max(colDefs.length, 1)}">${escapeHtml(emptyText)}</td></tr>`;

  return `<div class="scroll-x">
    <table class="data-table${tableClass ? ` ${escapeHtml(tableClass)}` : ''}">
      <thead><tr>${headHtml}</tr></thead>
      <tbody>${bodyHtml}</tbody>
    </table>
  </div>`;
}

// ---------------------------------------------------------------------------
// Hero 焦点区构件（v4 新增）
// ---------------------------------------------------------------------------

// Hero 大裁决上方的脉冲标签。text 转义；tone 决定铜金/上涨/下跌/中性配色。
export function verdictPill(text, tone = 'brand') {
  return `<span class="verdict-pill ${toneClass(tone)}"><span class="verdict-pulse" aria-hidden="true"></span>${escapeHtml(text)}</span>`;
}

// Hero 纪律/环境胶囊：label + 值。值可走 valueHtml（已是上色 HTML，如 pctHtml）或纯文本 value。
// icon 为可选装饰字符（转义后作纯展示）。
export function capsule(label, { value, valueHtml, tone = 'flat', icon = '' } = {}) {
  const valueContent = valueHtml != null ? valueHtml : (value != null && value !== '' ? `<b class="num">${escapeHtml(value)}</b>` : '');
  const iconHtml = icon ? `<span class="capsule-ico" aria-hidden="true">${escapeHtml(icon)}</span>` : '';
  return `<span class="capsule ${toneClass(tone)}">${iconHtml}<span class="capsule-label">${escapeHtml(label)}</span>${valueContent ? ` · ${valueContent}` : ''}</span>`;
}

// 风险刻度盘：charts.gauge 的语义包装，外加铜金 chrome（标题 + 0/50/100 刻度 + 状态胶囊）。
// 仍调用 charts.gauge 画半圆弧与中心数字；本组件只负责卡片外观与刻度/状态说明。
export function riskGauge(score, label = '') {
  const num = Number(score);
  const valid = Number.isFinite(num);
  const scoreText = valid ? formatNumber(Math.max(0, Math.min(100, num))) : '—';
  const tone = valid ? riskTone(num) : 'flat';
  const regimeText = safeText(label, '').trim();
  const regime = regimeText
    ? `<div class="gauge-regime ${toneClass(tone)}">${escapeHtml(regimeText)}</div>`
    : '';
  return `<div class="risk-gauge" data-score="${escapeHtml(scoreText)}">
    <div class="gauge-title">风险刻度盘</div>
    <div class="gauge-figure">
      ${gauge(score, { label: '' })}
    </div>
    ${regime}
    <div class="gauge-scale" aria-hidden="true">
      <span>0 · 低险</span><span>50</span><span>100 · 高险</span>
    </div>
  </div>`;
}

// 主题切换（app.js 按 [data-theme-choice] 挂事件并维护 active 类）。
export function themeToggle(extraClass = '') {
  return `<div class="theme-toggle${extraClass ? ` ${escapeHtml(extraClass)}` : ''}" role="group" aria-label="界面配色">
    <button type="button" data-theme-choice="light" aria-label="切换为亮色界面">亮色</button>
    <button type="button" data-theme-choice="dark" aria-label="切换为暗色界面">暗色</button>
  </div>`;
}

// 便捷导出：日期徽章（顶栏「数据日期」用），保持组件内部转义。
export function dateBadge(tradeDate) {
  return pill('数据日期', dateCn(tradeDate), 'brand');
}
