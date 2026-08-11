import assert from 'node:assert/strict';

import { buildModel } from '../assets/scripts/v2/data/model.js';
import { renderPrebreakoutShadow } from '../assets/scripts/v2/render/prebreakoutShadow.js';

const strategies = [
  ['prebreakout_v43_control', 'v4.3 对照组', 20],
  ['prebreakout_v43_top15', 'v4.3 Top15 行业约束组', 15],
  ['prebreakout_v44_balanced', 'v4.4 五类等权组', 20]
].map(([strategy_id, display_name, count]) => ({
  strategy_id,
  display_name,
  candidate_count: count,
  strategy_version: '1.0.0+test',
  operational_status: 'healthy',
  effectiveness_status: 'not_validated',
  execution_authority: 'observe_only_no_auto_order',
  effectiveness_evidence: {
    sample_trade_days: 0,
    failed_gates: ['insufficient_matured_trade_days']
  },
  candidates: Array.from({ length: count }, (_, index) => ({
    rank: index + 1,
    ts_code: `${String(index + 1).padStart(6, '0')}.SZ`,
    name: `股票${index + 1}`,
    industry_name: `行业${(index % 5) + 1}`,
    score: 90 - index,
    settlement_status: 'pending_settlement',
    planned_entry_time: '2026-08-12T09:30:00+08:00',
    used_proxy: false,
    rank_change: 0
  }))
}));

const data = {
  runManifest: { trade_date: '20260811', run_id: 'dual-test' },
  systemVerdict: { dates: { decision_trade_date: '20260811' }, gates: {} },
  prebreakoutShadowWatch: {
    contract_version: 'dual_track_v1',
    generated_at: '2026-08-11T20:30:00+08:00',
    trade_date: '20260811',
    title: '双轨策略观察与验证',
    flow_status: 'healthy',
    effectiveness_status: 'not_validated',
    decision: 'observe_only',
    execution_authority: 'observe_only_no_auto_order',
    honesty_banner: '流程运行正常不代表策略有效。三组短线与公告事件策略均处于前瞻观察，未接自动下单。',
    evaluation_integrity: {
      total_rows: 1980,
      settlement_counts: { settled: 1350, pending_settlement: 100, data_missing: 530 },
      fake_or_impossible_return_count: 0,
      proxy_rows: 0,
      rank_changed_rows: 0,
      ai_effectiveness_eligible_rows: 0
    },
    short_track_strategies: strategies,
    event_track: {
      strategy_id: 'event_quality_drift_v1',
      display_name: '公告事件质量漂移',
      operational_status: 'healthy_no_eligible_candidates',
      effectiveness_status: 'not_applicable_no_eligible_events',
      execution_authority: 'observe_only_no_auto_order',
      signal_date: '20260810',
      new_announcement_event_count: 1,
      eligible_event_count: 0,
      rejection_reason: 'no eligible PIT security'
    },
    promotion_rules: {
      short_track: '至少 60 个新成熟交易日。',
      event_track: '至少 12 个月与 100 个有效公告。',
      concentration: '收益集中则不通过。'
    }
  }
};

const model = buildModel(data, [], Date.UTC(2026, 7, 11, 12, 30, 0));
const html = renderPrebreakoutShadow(model);

for (const text of [
  '流程正常',
  '策略未验证',
  'prebreakout_v43_control',
  'prebreakout_v43_top15',
  'prebreakout_v44_balanced',
  'event_quality_drift_v1',
  '20 只',
  '15 只',
  '未接自动下单'
]) {
  assert.ok(html.includes(text), `missing rendered text: ${text}`);
}
assert.ok(!html.includes('因子二次加工厂'), 'dual-track contract must not render the retired factory view');
assert.ok(!html.includes('undefined'));
assert.ok(!html.includes('NaN'));

console.log('dual-track render: ok');
