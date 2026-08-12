// v2/data/manifest.js — 每个视图的按需数据清单（替代 legacy 一次性拉全 18 个 JSON ≈ 6.6MB）。
// required：缺失则整页报错（仅 run_manifest + system_verdict）。
// optional：缺失时对应分区显示占位与原因，页面其余部分照常渲染。
// path 相对站点根目录。公开页不允许回退读取本机原始明细。
//
// v3 扩展（DESIGN-V3.md 第 3 节）：
//   decisionState / marketContext —— 首页 Hero（今日一句话裁决 + 仓位指引）；
//   reviewUnified —— 历史战绩页（只读取公开结果摘要）；
//   strategyRegistry / systemHealth —— 系统说明页（策略档案 + 健康检查）。

export const SOURCES = {
  runManifest: { path: 'data/latest/run_manifest.json', label: '运行清单' },
  systemVerdict: { path: 'data/latest/system_verdict.json', label: '系统裁决' },
  decisionState: { path: 'data/latest/decision_state.json', label: '今日裁决' },
  marketContext: { path: 'data/latest/market_context.json', label: '市场环境与仓位指引' },
  marketState: { path: 'data/latest/market_state.json', label: '市场状态' },
  strategyState: { path: 'data/latest/strategy_state.json', label: '策略状态' },
  strategyRegistry: { path: 'data/latest/strategy_registry.json', label: '策略档案' },
  systemHealth: { path: 'data/latest/system_health.json', label: '系统健康检查' },
  strategyRunState: { path: 'data/latest/strategy_run_state.json', label: '策略运行状态' },
  recommendationState: { path: 'data/latest/recommendation_state.json', label: '推荐合同' },
  adjustmentLog: { path: 'data/latest/adjustment_log.json', label: '自动调整日志' },
  publishGuard: { path: 'data/latest/publish_guard_state.json', label: '发布防回退监控' },
  candidateState: { path: 'data/latest/candidate_state.json', label: '候选名单' },
  reviewState: { path: 'data/latest/review_state.json', label: '复盘统计' },
  sentimentState: { path: 'data/latest/sentiment_state.json', label: '情绪因子' },
  reviewUnified: {
    path: 'data/latest/review_track_latest.json',
    label: '历史战绩汇总'
  },
  researchState: { path: 'data/latest/research_state.json', label: '研究状态' },
  setupEngine: { path: 'data/latest/setup_engine_status.json', label: '剧本引擎状态' },
  s3Watchlist: { path: 'data/latest/s3_watchlist.json', label: 'S3 分时形态观察名单' },
  prebreakoutShadowWatch: { path: 'data/latest/prebreakout_shadow_watch.json', label: '双轨策略观察与验证' },
  executionState: { path: 'data/latest/execution_state.json', label: '执行清单' },
  researchStateT1: { path: 'data/latest/research_state_t1.json', label: 'T1 研究状态' },
  greenfieldTop20: { path: 'data/latest/greenfield_top20.json', label: 'O2C Top20' },
  midday: { path: 'data/recommendation_analytics/midday_analysis_latest.json', label: '午盘快照' },
  unified: { path: 'data/recommendation_analytics/unified_decision_payload.json', label: '统一决策载荷' },
  t1FactorRecommendations: { path: 'data/recommendation_analytics/t1_factor_recommendations.json', label: 'T1 因子推荐' },
  marketHeatmap: {
    path: 'data/recommendation_analytics/market_industry_heatmap_latest.json',
    label: '全市场行业热力'
  },
  strategyHeatmap: {
    path: 'data/recommendation_analytics/industry_heatmap_latest.json',
    label: '策略行业热力'
  }
};

// legacy 加载但任何视图都未渲染、v2 已彻底移除的数据源（记录在案，防止回潮）：
//   morning_brief_latest.json（8K）、prebreakout_recommendations.json（1.4MB）、
//   combined_recommendation.json（112K）。

const SHELL_OPTIONAL = ['marketState', 'candidateState', 'reviewState', 'midday'];

// market 页是「大盘指数 | 行业热力 | 行业动作」三 Tab 合并页；
// marketHeatmap / strategyHeatmap / industryActions 三个旧 URL 渲染同一页面并预选 Tab，
// 因此四个视图共用同一份数据清单（DESIGN-V3.md 第 3 节）。
const MARKET_OPTIONAL = ['marketHeatmap', 'strategyHeatmap', 'unified', ...SHELL_OPTIONAL];

// research 页是「系统如何工作 | 策略中心 | 数据状态」三 Tab 合并页；
// strategy 旧 URL 渲染同一页面预选「策略中心」Tab，数据清单与 research 一致。
const RESEARCH_OPTIONAL = [
  'researchState', 'strategyState', 'strategyRegistry', 'systemHealth', 'executionState',
  // 合同 v2 系统解码页所需
  'strategyRunState', 'recommendationState', 'adjustmentLog', 'publishGuard', 'decisionState',
  // 剧本引擎（策略重造方案 v2）——研究议程展示，缺失时整段退占位
  'setupEngine',
  ...SHELL_OPTIONAL
];

export const VIEW_DEPS = {
  dashboard: {
    required: ['runManifest', 'systemVerdict'],
    optional: ['decisionState', 'marketContext', 'executionState', ...SHELL_OPTIONAL]
  },
  market: {
    required: ['runManifest', 'systemVerdict'],
    optional: [...MARKET_OPTIONAL]
  },
  candidates: {
    required: ['runManifest', 'systemVerdict'],
    // 合同 v2：recommendationState 作为个股推荐页主数据源（三策略统一），原始文件作补充。
    optional: ['recommendationState', 'decisionState', 'executionState', 'greenfieldTop20', 'researchStateT1', ...SHELL_OPTIONAL]
  },
  review: {
    required: ['runManifest', 'systemVerdict'],
    // 战绩页只消费 reviewUnified（→reviewTrack），且自带 Hero asideHtml（不走默认风险盘/来源时间），
    // 故无需 SHELL_OPTIONAL；时间戳如需可取 runManifest.sources。省约 176KB 首屏载荷。
    optional: ['reviewUnified']
  },
  research: {
    required: ['runManifest', 'systemVerdict'],
    optional: [...RESEARCH_OPTIONAL]
  },
  // S3 分时形态 · top-20 观察名单（独立页面 s3-watch.html）。
  // 观察页只消费 s3Watchlist 一份摘要文件（缺失 → 整页退占位，不报错）；
  // 其余数据源不需要，避免拉全量 SHELL_OPTIONAL。
  s3Watch: {
    required: ['runManifest', 'systemVerdict'],
    optional: ['s3Watchlist']
  },
  // 启动前夕影子研究观察页：只消费一份摘要；缺失 → 占位。
  prebreakoutShadow: {
    required: ['runManifest', 'systemVerdict'],
    optional: ['prebreakoutShadowWatch']
  },
  sentiment: {
    required: ['runManifest', 'systemVerdict'],
    optional: ['sentimentState', ...SHELL_OPTIONAL]
  },
  strategy: {
    required: ['runManifest', 'systemVerdict'],
    optional: [...RESEARCH_OPTIONAL]
  },
  marketHeatmap: {
    required: ['runManifest', 'systemVerdict'],
    optional: [...MARKET_OPTIONAL]
  },
  strategyHeatmap: {
    required: ['runManifest', 'systemVerdict'],
    optional: [...MARKET_OPTIONAL]
  },
  industryActions: {
    required: ['runManifest', 'systemVerdict'],
    optional: [...MARKET_OPTIONAL]
  }
};

export function depsForView(viewKey) {
  return VIEW_DEPS[viewKey] || VIEW_DEPS.dashboard;
}
