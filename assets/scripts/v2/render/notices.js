// v2/render/notices.js — 顶部通知条（纯函数，无 DOM 依赖）。
//
// v3 重写要点：
//   1. 过期横幅最高优先（诚实性规范 0.5）：数据落后交易日 → 全站黄色横幅，明确"不能据此操作"。
//   2. 可选数据缺失通知保留，文案白话化；技术原因折叠到"技术信息"里，不直出给客户。
//   3. 午盘陈旧提示保留但白话化。
//   4. 删除 publish_ready / published 等内部字段直出（诚实性规范 0.8）。

import { escapeHtml, safeText } from './format.js';

// 全站数据过期横幅（最高优先级，所有页面顶部第一条）。
export function renderStaleBanner(model) {
  const staleness = model.staleness || {};
  if (!staleness.isStale) return '';
  const tradeDate = safeText(staleness.tradeDate, '');
  const daysLate = Number(staleness.daysLate) || 0;
  const heading = safeText(staleness.label, `数据更新于 ${tradeDate}，今日数据尚未生成`);
  const detail = staleness.isTodayTradingDay
    ? `今天的最新数据还没有生成，您看到的仍是 ${escapeHtml(tradeDate)} 收盘后的内容（已落后 ${daysLate} 个交易日）。`
    : `今天是休市日，且最近 ${daysLate} 个交易日的数据尚未更新，您看到的是 ${escapeHtml(tradeDate)} 收盘后的内容。`;
  return `
    <div class="notice warn notice-stale" role="alert">
      <div class="notice-icon">!</div>
      <div>
        <strong>${escapeHtml(heading)}</strong>
        <p>${detail}页面内容仅供回顾参考，请不要把它当作今天的操作依据。</p>
      </div>
    </div>
  `;
}

// 午盘快照陈旧检测：盘中文件交易日落后于决策主线交易日时给出提示信息。
export function getMiddayStaleInfo(model) {
  const middayTradeDate = safeText(model.midday.trade_date, '');
  const decisionTradeDate = safeText(model.systemVerdict.dates?.decision_trade_date || model.runManifest.trade_date, '');
  if (!middayTradeDate || !decisionTradeDate || middayTradeDate === decisionTradeDate) return null;
  return {
    middayTradeDate,
    decisionTradeDate,
    asOfTime: safeText(model.midday.as_of_time || model.midday.generated_at || model.runManifest.sources?.midday_generated_at, ''),
    sessionTradeDate: safeText(model.midday.session_trade_date, '')
  };
}

// Hero 内的午盘陈旧补充条（白话版）。
export function renderHeroSupplement(model) {
  const stale = getMiddayStaleInfo(model);
  if (!stale) return '';
  return `
    <div class="hero-supplement warn">
      <div class="notice-icon">!</div>
      <div>
        <strong>盘中快照不是最新的</strong>
        <p>盘中行情快照拍摄于 ${escapeHtml(stale.middayTradeDate)}，比当前数据日 ${escapeHtml(stale.decisionTradeDate)} 旧。它只作参考补充，不影响页面的主要结论。</p>
      </div>
    </div>
  `;
}

// 通知条合集：过期横幅永远排第一；午盘陈旧提示按需附加。
// 内部发布状态（publish_ready/published 等）不再出现在客户界面。
export function renderNoticeBlock(model, { includeMiddayStale = false } = {}) {
  const notices = [renderStaleBanner(model)];
  const stale = includeMiddayStale ? getMiddayStaleInfo(model) : null;
  if (stale) {
    notices.push(`
      <div class="notice warn">
        <div class="notice-icon">!</div>
        <div>
          <strong>盘中快照不是最新的</strong>
          <p>盘中行情快照拍摄于 ${escapeHtml(stale.middayTradeDate)}，比当前数据日 ${escapeHtml(stale.decisionTradeDate)} 旧。页面只把它当参考补充，主要结论不受影响。</p>
        </div>
      </div>
    `);
  }
  return notices.filter(Boolean).join('');
}

// 可选数据源缺失提示：白话主文案 + 技术原因折叠，客户不看也不影响理解。
export function renderMissingNotice(model) {
  if (!Array.isArray(model.missing) || !model.missing.length) return '';
  const labels = model.missing.map((item) => safeText(item.label, '未知数据')).join('、');
  const reasons = model.missing
    .map((item) => `${safeText(item.label, '未知数据')}：${safeText(item.reason, '数据缺失')}`)
    .join('；');
  return `
    <div class="notice info">
      <div class="notice-icon">i</div>
      <div>
        <strong>部分数据暂时没有加载出来</strong>
        <p>本次未能读取：${escapeHtml(labels)}。对应板块会显示说明，页面其他内容不受影响。</p>
        <details class="notice-detail"><summary>技术信息</summary><p>${escapeHtml(reasons)}</p></details>
      </div>
    </div>
  `;
}
