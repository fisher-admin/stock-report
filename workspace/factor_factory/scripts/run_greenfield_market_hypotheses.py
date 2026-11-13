#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from greenfield_multifactor_lib import load_config, load_json, dump_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build greenfield market-state hypotheses for the new A-share multi-factor mainline')
    parser.add_argument('--config', default='/Users/fisher/.openclaw/workspace/factor_factory/config/greenfield_multifactor_v1.yaml')
    return parser.parse_args()


def choose_family_biases(state: dict, cfg) -> tuple[dict, list[dict]]:
    regime_id = state.get('regime_id') or 'shrink_uptrend'
    family_budget = cfg.family_budgets.get(regime_id) or cfg.family_budgets['shrink_uptrend']
    breadth_ratio = float(state.get('breadth_ratio') or 0.0)
    amount_percentile = float(state.get('amount_total_percentile_80d') or 0.0)
    close_pct_1y = float(state.get('close_percentile_1y') or 0.0)
    focus_sectors = state.get('focus_sectors') or []
    style_regime = state.get('style_regime_id') or 'large_cap_rotation'
    sector_regime = state.get('sector_regime_id') or 'sector_dispersion'

    hypotheses = [
        {
            'hypothesis_id': 'gf_h01_liquid_leadership_over_pure_breakout',
            'priority': 'P0',
            'family': 'liquid_leadership',
            'action': 'increase',
            'rationale': f"当前 primary regime={regime_id}、style={style_regime}，且成交强度位于近80日分位 {amount_percentile:.2f}；更适合做高流动性龙头延续，而不是继续把单一 prebreakout / gap 事件当主线。",
            'validation': '看 walk-forward 中 liquid leadership 家族在 c2c / o2c 双口径下是否同时保留正 Sharpe。',
        },
        {
            'hypothesis_id': 'gf_h02_sector_rotation_should_follow_heatmap_not_single_theme',
            'priority': 'P0',
            'family': 'sector_rotation',
            'action': 'increase',
            'rationale': f"当前 focus sectors={focus_sectors}，sector regime={sector_regime}；应显式引入行业热度与行业相对强弱，而不是用旧启动前夕叙事隐式承担板块轮动。",
            'validation': '在相似市场窗口中验证 sector_rotation 家族是否持续优于等权基线。',
        },
        {
            'hypothesis_id': 'gf_h03_trend_quality_better_than_gap_chasing_under_weak_breadth',
            'priority': 'P0',
            'family': 'trend_quality',
            'action': 'increase_selectively',
            'rationale': f"当前 breadth_ratio={breadth_ratio:.3f} 偏弱，说明不是全面普涨；应偏向趋势质量与相对强度，而非继续放大开盘缺口/事件脉冲。",
            'validation': '重点观察趋势质量家族在低广度日是否仍能维持正超额。',
        },
        {
            'hypothesis_id': 'gf_h04_high_position_requires_downside_control',
            'priority': 'P0',
            'family': 'downside_control',
            'action': 'increase',
            'rationale': f"当前市场代理 1y 位置分位={close_pct_1y:.3f}，处于高位区域；新主线必须把回撤约束和低波领涨纳入主结构，而不是事后再补风险过滤。",
            'validation': 'walk-forward 中观察 max_drawdown / calmar 是否较等权基线改善。',
        },
        {
            'hypothesis_id': 'gf_h05_chip_factors_demoted_to_confirmation_layer',
            'priority': 'P1',
            'family': 'chip_resilience',
            'action': 'keep_as_confirmation',
            'rationale': '筹码结构保留，但只作为回踩支撑/拥挤过滤的确认层，不再作为主线中心。',
            'validation': '只允许与 trend/liquid/sector 家族联合入选，不单独主导组合。',
        },
        {
            'hypothesis_id': 'gf_h06_prebreakout_and_gap_only_are_reference_not_core',
            'priority': 'P0',
            'family': 'legacy_reference',
            'action': 'decrease_to_reference_only',
            'rationale': 'prebreakout_v41 / 启动前夕逻辑仅保留为旧系统基准、候选因子和对照项；不再作为新体系的结构中心，也不再优先消耗主算力修 benchmark。',
            'validation': '新组合训练与验证不得依赖 legacy selection_history 或 prebreakout 权重模板。',
        },
    ]
    return family_budget, hypotheses


def main() -> int:
    args = parse_args()
    cfg = load_config(Path(args.config))
    fingerprint = load_json(cfg.current_market_fingerprint_path)
    analog = load_json(cfg.regime_analog_windows_path)
    market_state = load_json(cfg.market_state_path)
    market_snapshot = load_json(cfg.market_snapshot_path)
    morning = load_json(cfg.market_morning_brief_path)

    broad = (fingerprint.get('style_and_sector') or {}).get('broad_market_proxy') or {}
    state = {
        'trade_date': fingerprint.get('trade_date'),
        'regime_id': broad.get('regime_id'),
        'regime_confidence': broad.get('regime_confidence'),
        'style_regime_id': (broad.get('regime_components') or {}).get('style', {}).get('id'),
        'sector_regime_id': (broad.get('regime_components') or {}).get('sector', {}).get('id'),
        'breadth_ratio': (fingerprint.get('trend_state') or {}).get('broad_market_proxy', {}).get('breadth_ratio'),
        'amount_total_percentile_80d': (fingerprint.get('turnover_and_liquidity') or {}).get('broad_market_proxy', {}).get('amount_total_percentile_80d'),
        'close_percentile_1y': (fingerprint.get('index_positions') or {}).get('broad_market_equal_weight_proxy', {}).get('close_percentile_1y'),
        'focus_sectors': (fingerprint.get('style_and_sector') or {}).get('focus_sectors') or morning.get('focus_sectors') or [],
    }
    family_budget, hypotheses = choose_family_biases(state, cfg)
    top_analogs = analog.get('analog_windows', [])[: cfg.analog_max_windows]
    analog_summary = {
        'count': len(top_analogs),
        'top_trade_dates': [item.get('trade_date') for item in top_analogs[:5]],
        'same_primary_regime_count': sum(bool((item.get('similar_reasons') or {}).get('same_primary_regime')) for item in top_analogs),
        'same_style_regime_count': sum(bool((item.get('similar_reasons') or {}).get('same_style_regime')) for item in top_analogs),
        'method_caveat': (analog.get('methodology') or {}).get('caveat'),
    }

    payload = {
        'schema_version': '2026-04-19.greenfield_market_hypotheses.v1',
        'trade_date': state['trade_date'],
        'generated_from': {
            'current_market_fingerprint': str(cfg.current_market_fingerprint_path),
            'regime_analog_windows': str(cfg.regime_analog_windows_path),
            'market_state': str(cfg.market_state_path),
            'market_snapshot': str(cfg.market_snapshot_path),
            'market_morning_brief': str(cfg.market_morning_brief_path),
        },
        'legacy_role': {
            'prebreakout_v41': 'reference_only',
            'legacy_system_role': ['historical_reference', 'old_system_baseline', 'candidate_factor_optional_control'],
            'mainline_training_optimization': 'disabled',
        },
        'current_market_state': {
            'trade_date': state['trade_date'],
            'primary_regime': state['regime_id'],
            'primary_regime_confidence': state['regime_confidence'],
            'style_regime': state['style_regime_id'],
            'sector_regime': state['sector_regime_id'],
            'breadth_ratio': state['breadth_ratio'],
            'amount_total_percentile_80d': state['amount_total_percentile_80d'],
            'close_percentile_1y': state['close_percentile_1y'],
            'focus_sectors': state['focus_sectors'],
            'morning_brief_regime': morning.get('regime'),
            'morning_brief_risk_score': morning.get('risk_score'),
            'data_quality': market_state.get('data_preparation'),
            'overnight_proxies': market_snapshot.get('overnight_indices'),
        },
        'analog_summary': analog_summary,
        'family_budgets': family_budget,
        'hypotheses': hypotheses,
        'execution_principles': [
            '新组合训练与验证默认不读取 prebreakout selection_history。',
            'gap / prebreakout 相关逻辑只可作为参考列或惩罚项，不可做新主线核心。',
            '优先验证 trend_quality + liquid_leadership + sector_rotation + downside_control 的独立组合产物。',
            '新验证至少输出 walk-forward OOS 与 analog window replay 两类证据。',
            'P1 fixed-long 选择器必须在训练窗 trailing holdout 中记录 baseline_close_to_close_total_return 与 close_to_close_excess_total_return。',
            '当 trailing holdout 供给足够时，至少两个独立 c2c-positive 且 baseline-beating families 才能进入正式选择；early folds 供给不足保留为 blocker。',
        ],
        'guardrail_objective_constraints': {
            'direction_mode': (cfg.scoring_controls or {}).get('direction_mode'),
            'holdout_baseline_objective': 'strict_positive_excess_over_equal_weight',
            'min_c2c_excess_total_return_vs_baseline': ((cfg.selection_controls or {}).get('holdout_confirmation') or {}).get('min_c2c_excess_total_return_vs_baseline'),
            'promotion_freeze_lifted': False,
        },
    }
    out_json = cfg.output_root / 'market_state_hypotheses.json'
    dump_json(out_json, payload)

    md = cfg.output_root / 'market_state_hypotheses.md'
    md.write_text(
        '\n'.join([
            '# Greenfield 多因子主线：市场状态与因子假设',
            '',
            f"- 交易日：{state['trade_date']}",
            f"- 主 regime：{state['regime_id']}（置信度 {float(state.get('regime_confidence') or 0):.3f}）",
            f"- 风格：{state['style_regime_id']}｜行业状态：{state['sector_regime_id']}",
            f"- 广度：{float(state.get('breadth_ratio') or 0):.3f}｜成交强度分位：{float(state.get('amount_total_percentile_80d') or 0):.3f}",
            f"- 关注行业：{', '.join(state['focus_sectors'])}",
            '',
            '## 主线纠偏',
            '- prebreakout_v41 仅保留为历史参考 / 对照项，不再作为当前主线。',
            '- 新主线聚焦：liquid leadership、sector rotation、trend quality、downside control。',
            '',
            '## 家族预算',
            *[f"- {k}: {v:.2%}" for k, v in family_budget.items()],
            '',
            '## 核心假设',
            *[f"- [{item['priority']}] {item['hypothesis_id']}：{item['rationale']}" for item in hypotheses],
            '',
            '## Analog 提示',
            f"- top analog 数量：{analog_summary['count']}，前五个窗口：{', '.join(analog_summary['top_trade_dates'])}",
            f"- same primary regime={analog_summary['same_primary_regime_count']} / {analog_summary['count']}，same style regime={analog_summary['same_style_regime_count']} / {analog_summary['count']}",
            f"- caveat：{analog_summary['method_caveat']}",
            '',
        ]) + '\n',
        encoding='utf-8',
    )
    print(out_json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
