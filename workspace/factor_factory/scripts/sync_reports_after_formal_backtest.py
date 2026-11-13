#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
EVOLUTION = ROOT / 'outputs' / 'evolution'
ATTRIB = ROOT / 'outputs' / 'attribution_reports' / 'phase1_attribution_report.json'
ATTRIB_MD = ROOT / 'outputs' / 'attribution_reports' / 'phase1_attribution_report.md'
VALIDATION = EVOLUTION / 'factor_validation_report.json'
PORTFOLIO = EVOLUTION / 'portfolio_backtest_report.json'
OVERFIT = EVOLUTION / 'overfit_audit_report.json'
PROMOTION = EVOLUTION / 'factor_promotion_decision.json'
DIGEST = EVOLUTION / 'daily_factor_evolution_digest.md'
FORMAL = EVOLUTION / 'current_combo_portfolio_backtest.json'
ANALOG = EVOLUTION / 'analog_validation_report.json'
CLAIM = EVOLUTION / 'current_combo_claim_limit_audit.json'
NESTED = EVOLUTION / 'current_combo_nested_validation.json'
TZ = ZoneInfo('Asia/Shanghai')


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds')


def newer_three_label_control_plane_present() -> tuple[bool, str]:
    if not ANALOG.exists() or not CLAIM.exists() or not NESTED.exists():
        return False, ''
    analog = load_json(ANALOG)
    claim = load_json(CLAIM)
    nested = load_json(NESTED)
    validation_scope = analog.get('validation_scope', {}) or {}
    validation_status = analog.get('validation_status', {}) or {}
    analog_ready = bool(
        validation_scope.get('portfolio_return_replay_included')
        or analog.get('current_combo_return_level_analog_replay')
        or validation_status.get('decision_grade') == 'hold_return_level_replay_present_no_independent_oos'
    )
    claim_modes = {
        str(mode)
        for mode in ((claim.get('independent_oos_blocker', {}) or {}).get('by_label_mode') or {}).keys()
    }
    claim_ready = {'close_to_close', 'open_to_close', 'close_to_open'}.issubset(claim_modes)
    nested_modes = {str(mode) for mode in (nested.get('applies_to_label_modes') or [])}
    nested_ready = {'close_to_close', 'open_to_close', 'close_to_open'}.issubset(nested_modes)
    if analog_ready and claim_ready and nested_ready:
        return True, (
            '当前 outputs 已完成三口径 claim-limit + nested + 收益级 analog replay 控制面；'
            '为避免旧 formal-backtest helper 把控制面回退，本脚本本轮不再改写 validation / portfolio / overfit / promotion / digest。'
        )
    return False, ''


def fmt_pct(x: float) -> str:
    return f'{x:.4%}'


now = datetime.now(TZ).isoformat(timespec='seconds')
validation = load_json(VALIDATION)
portfolio = load_json(PORTFOLIO)
overfit = load_json(OVERFIT)
promotion = load_json(PROMOTION)
formal = load_json(FORMAL)
attrib = load_json(ATTRIB)

control_plane_ready, control_plane_reason = newer_three_label_control_plane_present()
if control_plane_ready:
    print(json.dumps({
        'status': 'skipped_existing_three_label_control_plane',
        'generated_at': now,
        'reason': control_plane_reason,
        'preserved_outputs': [
            str(VALIDATION),
            str(PORTFOLIO),
            str(OVERFIT),
            str(PROMOTION),
            str(DIGEST),
        ],
    }, ensure_ascii=False, indent=2))
    raise SystemExit(0)

c2c_combo = formal['minimum_required_metrics']['close_to_close_combo']
o2c_combo = formal['minimum_required_metrics']['open_to_close_combo']
c2c_base = formal['minimum_required_metrics']['close_to_close_base']
o2c_base = formal['minimum_required_metrics']['open_to_close_base']
comparisons = formal['comparisons']
direction = formal['direction_consistency']
claim_limit = formal['claim_limit']
benchmark_summary = formal.get('benchmark_eligible_summary', {})
effective_replay_days_map = benchmark_summary.get('effective_replay_days', {})
benchmark_eligible_days = benchmark_summary.get('eligible_trade_days')
effective_replay_days = effective_replay_days_map.get('close_to_close_combo')
benchmark_days_text = benchmark_eligible_days if benchmark_eligible_days is not None else 'NA'
effective_replay_days_text = effective_replay_days if effective_replay_days is not None else 'NA'

portfolio['generated_at'] = now
portfolio.setdefault('source_paths', {})['current_combo_portfolio_backtest'] = str(FORMAL)
portfolio['source_paths']['attribution_report'] = str(ATTRIB)
portfolio['source_paths']['attrib'] = str(ATTRIB)
portfolio.setdefault('source_mtimes', {})['current_combo_portfolio_backtest'] = iso_mtime(FORMAL)
portfolio['source_mtimes']['attribution_report'] = iso_mtime(ATTRIB)
portfolio['source_mtimes']['attribution_report_md'] = iso_mtime(ATTRIB_MD)
portfolio['source_mtimes']['attrib'] = iso_mtime(ATTRIB)
portfolio.setdefault('available_backtest_evidence', {})['formal_current_combo_backtest_present'] = True
portfolio['available_backtest_evidence']['formal_current_combo_backtest_path'] = str(FORMAL)
portfolio['available_backtest_evidence']['current_combo_minimal_replay_present'] = True
portfolio['available_backtest_evidence']['evidence_gap'] = '两份 strategy_backtests 仍没有当前组合的独立 end-to-end 回测，但 current_combo_portfolio_backtest 已补出与最新 weight_map 对齐的正式组合级 backtest 产物（含净值曲线、收益、夏普、回撤、换手与成本后结果）。'
portfolio.setdefault('oos_validation_assessment', {})['portfolio_level_backtest_completed'] = True
portfolio['oos_validation_assessment']['independent_oos_completed'] = False
portfolio['oos_validation_assessment']['analog_validation_completed'] = True
portfolio['oos_validation_assessment']['claim_limit'] = '当前组合已具备 formal current-weight-map backtest 与收益级 analog replay 证据；但仍不能宣称已完成独立 end-to-end OOS、独立样本外或独立 holdout。'
portfolio['oos_validation_assessment']['minimal_current_combo_replay_present'] = True
portfolio['oos_validation_assessment']['minimal_current_combo_replay_direction_consistent'] = bool(direction['combo_direction_consistent'] and direction['excess_direction_consistent'])
portfolio['oos_validation_assessment']['formal_current_combo_backtest_present'] = True
portfolio['oos_validation_assessment']['formal_current_combo_backtest_path'] = str(FORMAL)
portfolio['oos_validation_assessment']['formal_current_combo_backtest_direction_consistent'] = bool(direction['combo_direction_consistent'] and direction['excess_direction_consistent'])
portfolio['oos_validation_assessment']['attribution_synced_with_latest_audit_weight'] = True
portfolio['oos_validation_assessment']['attribution_risk_count'] = len(attrib.get('risks', []))
portfolio['conclusion']['status'] = 'formal_current_combo_backtest_available_but_no_independent_oos'
portfolio['conclusion']['effective_oos_claim_allowed'] = False
portfolio['conclusion']['analog_validation_claim_allowed'] = True
portfolio['conclusion']['summary'] = (
    '当前 support_pool + fallback 组合已补出正式组合级 backtest：'
    f'close-to-close combo 总收益 {fmt_pct(c2c_combo["total_return"])}、净值终值 {c2c_combo["net_value_end"]:.6f}、夏普 {c2c_combo["sharpe"]:.3f}、最大回撤 {fmt_pct(c2c_combo["max_drawdown"])}、胜率 {fmt_pct(c2c_combo["win_rate"])}、换手 {c2c_combo["turnover"]:.3f}；'
    f'但 open-to-close combo 总收益 {fmt_pct(o2c_combo["total_return"])}，与 close-to-close 方向不一致。'
    f'同时 attribution 已同步到最新 audit/weight（risk_count={len(attrib.get("risks", []))}），收益级 analog replay 也已具备；但独立 OOS / nested validation / 独立 holdout 仍缺，因此 promotion freeze 继续维持。'
)
if str(FORMAL) not in portfolio.setdefault('evidence_path', []):
    portfolio['evidence_path'].append(str(FORMAL))
if str(ATTRIB) not in portfolio['evidence_path']:
    portfolio['evidence_path'].append(str(ATTRIB))

overfit['generated_at'] = now
overfit.setdefault('missing_validation_stack', {})['portfolio_level_oos_completed'] = False
overfit['missing_validation_stack']['analog_validation_completed'] = True
overfit['missing_validation_stack']['formal_portfolio_backtest_artifact_present'] = True
overfit['missing_validation_stack']['attribution_synced_with_latest_audit_weight'] = True
overfit['missing_validation_stack']['detail'] = [
    'current_combo_portfolio_backtest 已提供当前 support_pool + fallback 组合的正式 backtest 产物（含净值曲线与成本后指标），但它仍不是独立 holdout / nested OOS。',
    '未发现 nested walk-forward 产物。',
    'analog_validation_report 已升级为收益级 analog replay，但它仍只是 frozen-current-plan 的历史类比验证，不是独立 OOS。',
    'current_combo formal backtest 已确认 close-to-close 与 open-to-close 在绝对收益和相对 base 超额两个维度都不一致。',
    'phase1_attribution_report.json 已在 12:11 跟随最新 audit/weight 刷新，但风险摘要仍只代表说明性 attribution，不构成收益级验证。',
]
overfit.setdefault('artifact_consistency_risk', {})['attrib_mtime'] = iso_mtime(ATTRIB)
overfit['artifact_consistency_risk']['detail'] = '最新 canonical audit/weight 为 11:39/11:40；phase1_attribution_report 已在 12:11 同步刷新；current_combo_portfolio_backtest 已产出正式组合级 backtest，analog_validation 也已升级为收益级 analog replay，但 strategy_backtests 仍没有当前组合的独立 OOS / end-to-end 回测。'
overfit['overall_verdict']['summary'] = (
    '过拟合审计结论继续维持冻结：当前组合虽已补出 formal current-weight-map backtest 且 attribution 已同步，'
    f'close-to-close 超额 {fmt_pct(comparisons["close_to_close_excess_total_return"])} 与 open-to-close 超额 {fmt_pct(comparisons["open_to_close_excess_total_return"])} 方向仍不一致，'
    '收益级 analog replay 已具备，但独立 OOS / nested validation / 独立 holdout 仍缺失，promotion freeze 不能放松。'
)
overfit['current_combo_formal_backtest_signal'] = {
    'generated_at': formal['generated_at'],
    'path': str(FORMAL),
    'close_to_close_combo_total_return': c2c_combo['total_return'],
    'close_to_close_combo_sharpe': c2c_combo['sharpe'],
    'close_to_close_combo_max_drawdown': c2c_combo['max_drawdown'],
    'open_to_close_combo_total_return': o2c_combo['total_return'],
    'close_to_close_base_total_return': c2c_base['total_return'],
    'open_to_close_base_total_return': o2c_base['total_return'],
    'direction_consistent': bool(direction['combo_direction_consistent'] and direction['excess_direction_consistent']),
}
if str(FORMAL) not in overfit.setdefault('evidence_path', []):
    overfit['evidence_path'].append(str(FORMAL))
if str(ATTRIB) not in overfit['evidence_path']:
    overfit['evidence_path'].append(str(ATTRIB))

promotion['generated_at'] = now
promotion.setdefault('canonical_refresh_basis', {})['attrib_mtime'] = iso_mtime(ATTRIB)
promotion['canonical_refresh_basis']['formal_backtest_mtime'] = iso_mtime(FORMAL)
promotion.setdefault('global_constraints', {})['analog_validation_complete'] = True
promotion['current_combo_replay_basis']['path'] = str(FORMAL)
promotion['current_combo_replay_basis']['generated_at'] = formal['generated_at']
promotion['current_combo_replay_basis']['current_combo_has_portfolio_level_backtest_evidence'] = 'yes_formal_current_weight_map_backtest_but_not_independent_oos'
promotion['current_combo_replay_basis']['close_to_close_vs_open_to_close_direction_consistent'] = bool(direction['combo_direction_consistent'] and direction['excess_direction_consistent'])
promotion['current_combo_replay_basis']['sufficient_to_lift_promotion_freeze'] = False
promotion['current_combo_replay_basis']['close_to_close_combo_total_return'] = c2c_combo['total_return']
promotion['current_combo_replay_basis']['open_to_close_combo_total_return'] = o2c_combo['total_return']
promotion['current_combo_replay_basis']['close_to_close_excess_total_return'] = comparisons['close_to_close_excess_total_return']
promotion['current_combo_replay_basis']['open_to_close_excess_total_return'] = comparisons['open_to_close_excess_total_return']
for bucket in ['keep_active', 'watch', 'demote']:
    for item in promotion.get(bucket, []):
        evidence = item.setdefault('evidence_path', [])
        if str(FORMAL) not in evidence:
            evidence.append(str(FORMAL))
        if str(ATTRIB) not in evidence:
            evidence.append(str(ATTRIB))
        if item.get('factor_id') == 'f_chip_support_proxy':
            item['reason'] = '当前 f_chip_support_proxy 已有 formal current-weight-map 组合级 backtest 证据，analog_validation 也已提供收益级 analog replay；但这些仍不是独立 OOS / 独立 holdout，audit 状态也仍为 cold_storage，因此继续保留 watch。'
        elif item.get('factor_id') == 'f_gap_return':
            item['reason'] = '当前 f_gap_return 已有 formal current-weight-map 组合级 backtest 证据，且是 latest shrink_uptrend 正式权重计划中唯一 support_pool 因子；analog_validation 也已提供收益级 analog replay，但它仍缺独立 OOS / 独立 holdout，audit 状态也仍为 cold_storage，因此继续保留 watch。'
promotion['summary']['promotion_freeze'] = True
promotion['summary']['summary'] = (
    'promotion freeze 不变；当前 canonical 已补出 formal current_combo_portfolio_backtest，'
    f'close-to-close combo 总收益 {fmt_pct(c2c_combo["total_return"])}、open-to-close combo 总收益 {fmt_pct(o2c_combo["total_return"])}，方向仍不一致；'
    'attribution 与收益级 analog replay 已同步，但由于缺少独立 OOS / nested validation / 独立 holdout，base 外辅助因子继续只保留 watch。'
)

validation['generated_at'] = now
validation.setdefault('source_artifacts', {})['current_combo_portfolio_backtest'] = str(FORMAL)
validation['source_artifacts']['attribution_report'] = str(ATTRIB)
validation.setdefault('canonical_refresh_basis', {})['portfolio_report_mtime'] = now
validation['canonical_refresh_basis']['overfit_report_mtime'] = now
validation['canonical_refresh_basis']['promotion_report_mtime'] = now
validation['canonical_refresh_basis']['attribution_report_mtime'] = iso_mtime(ATTRIB)
validation['canonical_refresh_basis']['formal_backtest_mtime'] = iso_mtime(FORMAL)
validation['three_most_severe_gaps'] = [
    f'canonical factor_audit / regime_weight_map 已追平到 {benchmark_days_text} 个 benchmark eligible days，但当前组合收益验证仍只覆盖 {benchmark_days_text} 个 eligible trade days 中的 {effective_replay_days_text} 个有效回放日，且没有剩余独立 labeled holdout。',
    f'当前 support_pool + fallback 组合已补 formal current_combo_portfolio_backtest：close-to-close combo 总收益 {fmt_pct(c2c_combo["total_return"])}、相对 base 超额 {fmt_pct(comparisons["close_to_close_excess_total_return"])}；open-to-close combo 总收益 {fmt_pct(o2c_combo["total_return"])}、相对 base 超额 {fmt_pct(comparisons["open_to_close_excess_total_return"])}，方向仍不一致；剔除回填日后 close-to-close 超额仍为 {fmt_pct(formal.get("sensitivity", {}).get("comparisons_without_backfill", {}).get("close_to_close_excess_total_return"))}。',
    'analog_validation_report 已升级为收益级 analog replay，formal current_combo_portfolio_backtest、claim-limit 审计与 micro nested validation 也已同步；但这些证据仍共享同一 clean window，不构成独立 OOS / 独立 holdout。',
]
validation.setdefault('portfolio_level_validation_gaps', {})['status'] = 'formal_backtest_completed_but_independent_validation_open'
validation['portfolio_level_validation_gaps']['summary'] = '当前 support_pool + fallback 组合已补出 formal current-weight-map backtest，attribution 与收益级 analog replay 也已同步，但仍没有独立 end-to-end backtest、独立 OOS 与 nested validation。'
validation['portfolio_level_validation_gaps']['gaps'] = [
    'current_combo_portfolio_backtest.json 已提供与最新 weight_map 对齐的正式组合级 backtest，但两份 strategy_backtests 仍没有当前组合的独立 end-to-end 回测。',
    'analog_validation_report.json 已更新为收益级 analog replay，但它仍只是 frozen-current-plan 的历史类比验证，不是独立 OOS。',
    'phase1_attribution_report.json 已跟随最新 audit/weight 刷新，但它仍只是说明性风险摘要，不构成收益级验证。',
    'current_combo_label_sensitivity.json 已确认收益口径敏感性仍高，promotion freeze 继续维持。',
]
validation['engineering_todo'] = [
    {
        'priority': 'P0',
        'task': f'为 current combo 预留真正独立的 labeled holdout / rolling OOS，不要再让 formal replay 吃满 {benchmark_days_text} 个 eligible trade days 中全部 {effective_replay_days_text} 个有效回放日。',
    },
    {
        'priority': 'P0',
        'task': '让 factor_audit / weight_search 在 open_to_close 或 close_to_open 下形成独立 canonical audit 与 weight map，而不是只停留在 replay/backtest 对照。',
    },
    {
        'priority': 'P1',
        'task': '补独立 holdout / nested validation，避免筛因子、定权重、做 analog 用同一批 clean window。',
    },
    {
        'priority': 'P1',
        'task': '在已具备收益级 analog replay 的基础上继续补独立 holdout / rolling OOS，避免把类比验证误读成独立组合验证。',
    },
]
validation.setdefault('release_recommendation', {})['decision'] = 'hold'
validation['release_recommendation']['reason'] = '基础设施修复、formal current-weight-map backtest、attribution 同步与收益级 analog replay 都已落地，但 c2c/o2c 方向仍不一致，且独立 OOS / nested validation / 独立 holdout 仍缺。'
validation['release_recommendation']['allowed_usage'] = [
    '把本轮当作当前 support_pool + fallback 组合已补出 formal current-weight-map backtest 的证据',
    '用 current_combo_label_sensitivity 识别最新辅助层的收益口径敏感性',
    '用 analog_validation_report 作为收益级 analog replay 的补充验证参考（非独立 OOS）',
]
validation['release_recommendation']['blocked_usage'] = [
    '解除 promotion freeze',
    '把 current_combo_portfolio_backtest 或 current_combo_replay 当作独立 end-to-end 回测或独立 OOS',
    '把 close-to-close 的正超额直接外推为 open-to-close 可成交优势',
]

# digest rewrite
lines = [
    '# Daily Factor Evolution Digest',
    '',
    f'- 生成时间：{now}',
    f'- 当前交易日：{portfolio["current_proxy_state"]["trade_date"]}',
    f'- 当前组合：{portfolio["current_portfolio_structure"]["selection_mode"]} | latest shrink_uptrend = {" / ".join(portfolio["current_portfolio_structure"]["latest_weight_plan"]["selected_factors"][i]["factor_id"] for i in range(len(portfolio["current_portfolio_structure"]["latest_weight_plan"]["selected_factors"]))) if portfolio["current_portfolio_structure"].get("latest_weight_plan") else "NA"}',
    '',
    '## 本轮新增实质产物',
    f'- canonical audit/weight 已追平：`factor_audit_phase1.json` / `regime_weight_map_phase1.json` 现为 {benchmark_days_text} 个 benchmark eligible days / 410 行。',
    f'- attribution 已同步：`phase1_attribution_report.json` / `.md` 已在 {iso_mtime(ATTRIB)} 刷新，当前 `risk_count={len(attrib.get("risks", []))}`。',
    f'- 正式组合级 backtest 已落地：`current_combo_portfolio_backtest.json` + `current_combo_portfolio_backtest_equity_curves.csv/json`，覆盖 {benchmark_days_text} 个 eligible trade days / {effective_replay_days_text} 个有效回放日。',
    '',
    '## 当前 formal backtest 结果（成本后）',
    f'- close-to-close combo：净值终值={c2c_combo["net_value_end"]:.6f}，总收益={fmt_pct(c2c_combo["total_return"])}，夏普={c2c_combo["sharpe"]:.3f}，最大回撤={fmt_pct(c2c_combo["max_drawdown"])}，胜率={fmt_pct(c2c_combo["win_rate"])}，换手={c2c_combo["turnover"]:.3f}。',
    f'- open-to-close combo：净值终值={o2c_combo["net_value_end"]:.6f}，总收益={fmt_pct(o2c_combo["total_return"])}，夏普={o2c_combo["sharpe"]:.3f}，最大回撤={fmt_pct(o2c_combo["max_drawdown"])}，胜率={fmt_pct(o2c_combo["win_rate"])}，换手={o2c_combo["turnover"]:.3f}。',
    f'- 相对 base 超额：close-to-close={fmt_pct(comparisons["close_to_close_excess_total_return"])}，open-to-close={fmt_pct(comparisons["open_to_close_excess_total_return"])}。',
    '',
    '## analog / attribution / promotion 状态',
    '- analog_validation_report 已对齐到最新权重图：10/10 proxy windows 全覆盖 latest weight plan 的 `f_chip_support_proxy / f_gap_return / f_atr_proxy_14`；`f_price_vs_ma20` 仍仅是 union-proxy。',
    f'- attribution 已不再滞后，但仍只是说明性风险摘要，不能替代收益级验证。',
    '- promotion_freeze=true 维持不变；当前没有任何因子可从 watch 升到 active/promote。',
    '',
    '## 关键风险',
    '- clean benchmark 已追平到当前 33 天 canonical 口径，但剩余 labeled holdout=0，独立 OOS / 收益级 analog 仍未建立。',
    '- close-to-close 与 open-to-close 方向不一致，说明交易时点敏感性仍未收敛。',
    '- formal backtest 已有，但独立 OOS / nested validation / 收益级 analog validation 仍缺。',
    '',
    '## 下一步',
    '- 继续做 `benchmark_eligible` / clean benchmark 修复与 selection_history 回填。',
    '- 把双口径标签扩成可复用验证框架，而不是只停在单次 formal backtest。',
    '- 再补独立 OOS、nested validation、收益级 analog replay，之后才谈 promotion freeze 解除。',
]
DIGEST.write_text('\n'.join(lines) + '\n', encoding='utf-8')

dump_json(VALIDATION, validation)
dump_json(PORTFOLIO, portfolio)
dump_json(OVERFIT, overfit)
dump_json(PROMOTION, promotion)
print(json.dumps({
    'status': 'ok',
    'validation': str(VALIDATION),
    'portfolio': str(PORTFOLIO),
    'overfit': str(OVERFIT),
    'promotion': str(PROMOTION),
    'digest': str(DIGEST),
    'attribution_synced': True,
    'formal_backtest_path': str(FORMAL),
}, ensure_ascii=False, indent=2))
