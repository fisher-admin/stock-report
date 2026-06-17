// v3/render/format.js — 文本/数字格式化、术语转译、AI 状态判定（纯函数，无 DOM 依赖，Node 可执行）。
//
// 公开 API：
//   safeText(value, fallback)            — null/undefined/'' 统一兜底为占位符
//   escapeHtml(value)                    — HTML 转义（所有插值默认必须过它）
//   formatNumber(value, digits)          — 千分位数字；非法值返回 '—'
//   formatPct(value, digits, suffix)     — 百分比文本（不上色、不带正号）
//   formatSignedPct(value, digits)       — 带 +/- 号的百分比文本
//   optionalPct(value, digits)           — null 显示 '—'，否则同 formatPct
//   pctHtml(value, digits)               — 红涨绿跌上色的 <span>（产物已是 HTML，勿再转义）
//   dateCn(value)                        — '20260611' → '2026-06-11'（兼容 ISO / 带时间）
//   friendlyTime(value)                  — '2026-06-12 12:00:23' → '2026-06-12 12:00'
//   glossary(term)                       — 开发者术语 → 客户白话（未收录原样返回）
//   aiStatusOf(item)                     — 候选项 AI 状态：'ai-full' | 'ai-stale' | 'ai-none'
//   aiSourceDate(item)                   — AI 分析引用的源日期（已转 dateCn，无则 ''）
//   cleanAnalysisText(value)             — 过滤内部链路提示行；无内容返回 ''（不再编造兜底话术）
//   actionLabel / actionTone             — role_type（main/watch/avoid）直译与状态色
//   riskTone / toneFromGate / toneFromVerdict — 状态色调映射（ok/warn/bad/info）
//   strategyLabel / strategyTone         — 策略 id → 客户名称 / 色调
//   factorLabel / factorShortLabel       — 因子键名 → 白话标签（含 Alpha191 通配）
//
// 诚实性约定（DESIGN-V3 第 0 节）：本文件不得返回任何编造的业绩数字或模板分析话术；
// 数据为空时一律返回 '—' 或 ''，由组件层显示「暂无可验证数据」类说明。

export function safeText(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

export function escapeHtml(value) {
  return safeText(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatNumber(value, digits = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return num.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

export function formatPct(value, digits = 1, suffix = '%') {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return `${num.toFixed(digits)}${suffix}`;
}

export function formatSignedPct(value, digits = 2) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(digits)}%`;
}

export function optionalPct(value, digits = 2) {
  return value === null || value === undefined || value === '' ? '—' : formatPct(value, digits);
}

// 红涨绿跌上色的百分比（A 股配色：正数 --up 红，负数 --down 绿）。
// 返回值已是 HTML，调用方不得再 escapeHtml；按约定此类产物只应传入以 Html 结尾的参数位。
export function pctHtml(value, digits = 2) {
  const num = Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(num)) {
    return '<span class="num pct-flat">—</span>';
  }
  const cls = num > 0 ? 'pct-up' : num < 0 ? 'pct-down' : 'pct-flat';
  const sign = num > 0 ? '+' : '';
  return `<span class="num ${cls}">${sign}${num.toFixed(digits)}%</span>`;
}

// '20260611' → '2026-06-11'；'2026-06-12T15:40:08' → '2026-06-12'；其余原样（兜底 '—'）。
export function dateCn(value) {
  const text = safeText(value, '');
  if (!text) return '—';
  const compact = text.match(/^(\d{4})(\d{2})(\d{2})$/);
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]}`;
  const iso = text.match(/^(\d{4}-\d{2}-\d{2})/);
  if (iso) return iso[1];
  return text;
}

// 生成时间转「日期 时:分」，去掉秒与时区噪音；纯日期走 dateCn。
export function friendlyTime(value) {
  const text = safeText(value, '');
  if (!text) return '—';
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  if (match) return `${match[1]} ${match[2]}`;
  return dateCn(text);
}

// 开发者术语 → 客户白话。键统一小写匹配；未收录原样返回（调用方自行兜底）。
export const GLOSSARY = {
  regime: '市场状态',
  market_regime: '市场状态',
  ic: '因子有效性',
  icir: '因子有效性稳定度',
  sharpe: '风险调整后收益（夏普）',
  max_drawdown: '最大回撤',
  drawdown: '最大回撤',
  hit_rate: '次日上涨命中率',
  win_rate: '胜率',
  winner_rate: '获利盘比例',
  chip_conc: '筹码集中度',
  chip_support: '下方筹码支撑',
  chip_resistance: '上方筹码压力',
  cost_deviation: '成本偏离度',
  volume_ratio: '量比',
  position_limit: '仓位上限',
  gate: '安全检查',
  freshness_gate: '数据新鲜度检查',
  market_gate: '市场环境检查',
  strategy_gate: '策略激活检查',
  candidate_gate: '候选执行检查',
  publish_ready: '数据校验完成',
  provider: '数据来源',
  akshare: '公开行情数据接口',
  research_preview: '研究预览（未实盘验证）',
  main: '主攻',
  watch: '观察',
  avoid: '回避',
  consensus: '多策略共识',
  divergence: '策略分歧',
  equity_curve: '净值曲线',
  alpha: '超额收益',
  factor: '量化因子',
  vwap: '日内均价',
  ema: '移动均线',
  benchmark: '对比基准',
  backtest: '历史回测',
  sentiment: '市场情绪',
  prebreakout: '启动前夕',
  o2c: '日内开收盘',
  t1: '次日交易'
};

export function glossary(term) {
  const key = safeText(term, '').trim().toLowerCase();
  if (!key) return safeText(term, '');
  return GLOSSARY[key] || safeText(term);
}

// ---------------------------------------------------------------------------
// AI 状态判定（DESIGN-V3 第 0.2 节）
// - 'ai-full'  ：ai_summary / ai_points / ai_conclusion 真实存在（非模板生成）
// - 'ai-stale' ：有真实 AI 内容，但 ai_source_stale 为真，或源日期早于当前数据日
// - 'ai-none' ：AI 字段全空，或内容是模板批量生成（ai_source_kind 含 template/模板）
// 严禁在 'ai-none' 时编造分析话术——该状态下界面只展示真实量化因子。
// ---------------------------------------------------------------------------

function hasMeaningfulText(value) {
  if (typeof value === 'string') return value.trim().length > 0;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.some(hasMeaningfulText);
  if (value && typeof value === 'object') return Object.values(value).some(hasMeaningfulText);
  return false;
}

function compactDate(value) {
  return safeText(value, '').replace(/[^0-9]/g, '').slice(0, 8);
}

export function aiSourceDate(item = {}) {
  const raw = item.ai_source_date || item.ai_analysis_date || item.ai_generated_at || '';
  const compact = compactDate(raw);
  return compact.length === 8 ? dateCn(compact) : '';
}

export function aiStatusOf(item = {}) {
  const kindText = `${safeText(item.ai_source_kind, '')} ${safeText(item.ai_source_name, '')}`;
  const isTemplate = /template|模板/i.test(kindText);
  const hasAi = !isTemplate && (
    hasMeaningfulText(item.ai_summary)
    || hasMeaningfulText(item.ai_points)
    || hasMeaningfulText(item.ai_conclusion)
  );
  if (!hasAi) return 'ai-none';
  if (item.ai_source_stale) return 'ai-stale';
  const src = compactDate(item.ai_source_date || item.ai_analysis_date || '');
  const cur = compactDate(item.current_price_trade_date || item.trade_date || item.recommend_date || item.review_recommend_date || '');
  if (src.length === 8 && cur.length === 8 && src < cur) return 'ai-stale';
  return 'ai-full';
}

// 过滤 AI 文本中的内部链路提示行（fallback/兜底/链路等开发词）。
// 没有可保留内容时返回 ''——由组件层呈现「无 AI 分析」，这里不编故事。
export function cleanAnalysisText(value) {
  const lines = safeText(value, '')
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/fallback|正式模型|搜索链路|回填链路|兜底|本地模型/i.test(line));
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// 动作 / 状态色调（状态色与涨跌色严格分离：动作用 ok/warn/bad/info，涨跌用 pctHtml）
// ---------------------------------------------------------------------------

export function actionLabel(action) {
  if (action === 'main') return '主攻';
  if (action === 'avoid') return '回避';
  if (action === 'watch') return '观察';
  return glossary(action) === safeText(action) ? '观察' : glossary(action);
}

export function actionTone(action) {
  if (action === 'main') return 'ok';
  if (action === 'avoid') return 'bad';
  return 'info';
}

export function riskTone(score) {
  const value = Number(score) || 0;
  if (value >= 70) return 'bad';
  if (value >= 50) return 'warn';
  return 'ok';
}

export function toneFromGate(status) {
  if (status === 'pass') return 'ok';
  if (status === 'warn') return 'warn';
  return 'bad';
}

export function toneFromVerdict(action) {
  if (action === 'execute' || action === 'deploy') return 'ok';
  if (action === 'observe_only') return 'warn';
  return 'bad';
}

// ---------------------------------------------------------------------------
// 策略与因子白话标签
// ---------------------------------------------------------------------------

export function strategyLabel(strategyId) {
  if (strategyId === 'prebreakout_v41') return '启动前夕';
  if (strategyId === 'greenfield_o2c_v1') return 'O2C 日内';
  if (strategyId === 't1_factor_v1') return 'T1 因子';
  return safeText(strategyId, '未知策略');
}

export function strategyTone(strategyId) {
  if (strategyId === 'prebreakout_v41') return 'brand';
  if (strategyId === 'greenfield_o2c_v1') return 'info';
  if (strategyId === 't1_factor_v1') return 'warn';
  return 'info';
}

const FACTOR_LABELS = {
  g_intraday_vwap_deviation: '日内均价偏离度',
  g_volume_price_divergence: '量价背离信号',
  g_chip_pullback_support: '回踩筹码支撑',
  g_long_cost_concentration: '多头成本集中度',
  g_close_strength_ratio: '收盘强度',
  g_intraday_range_expansion: '日内振幅扩张',
  chip_conc: '筹码集中度',
  chip_support: '下方筹码支撑',
  chip_resistance: '上方筹码压力',
  winner_rate: '获利盘比例',
  cost_deviation: '成本偏离度',
  volume_ratio: '量比'
};

const FACTOR_SHORT_LABELS = {
  g_intraday_vwap_deviation: '均价偏离',
  g_volume_price_divergence: '量价背离',
  g_chip_pullback_support: '筹码支撑',
  g_long_cost_concentration: '成本集中',
  g_close_strength_ratio: '收盘强度',
  g_intraday_range_expansion: '日内振幅',
  chip_conc: '筹码集中',
  chip_support: '筹码支撑',
  chip_resistance: '筹码压力',
  winner_rate: '获利盘',
  cost_deviation: '成本偏离',
  volume_ratio: '量比'
};

const ALPHA_KEY = /^alpha[_-]?0*(\d+)$/i;

export function factorLabel(key) {
  const text = safeText(key, '');
  if (FACTOR_LABELS[text]) return FACTOR_LABELS[text];
  const alpha = text.match(ALPHA_KEY);
  if (alpha) return `Alpha${alpha[1]} 因子`;
  return glossary(text);
}

export function factorShortLabel(key) {
  const text = safeText(key, '');
  if (FACTOR_SHORT_LABELS[text]) return FACTOR_SHORT_LABELS[text];
  const alpha = text.match(ALPHA_KEY);
  if (alpha) return `A${alpha[1]}`;
  return glossary(text);
}
