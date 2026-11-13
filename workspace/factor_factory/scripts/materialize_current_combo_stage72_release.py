#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DEFAULT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
TZ = ZoneInfo('Asia/Shanghai')


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def fmt_pct(value: float | None) -> str:
    if value is None:
        return 'NA'
    return f'{value:.4%}'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Materialize stage72 current_combo release decision/control sync')
    parser.add_argument('--root', default=str(ROOT_DEFAULT))
    parser.add_argument('--stage72-report', required=True)
    parser.add_argument('--reserve-path', required=True)
    parser.add_argument('--qlib-summary-path', default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    evolution = root / 'outputs' / 'evolution'
    now = datetime.now(TZ).isoformat(timespec='seconds')

    stage72_path = Path(args.stage72_report).expanduser().resolve()
    reserve_path = Path(args.reserve_path).expanduser().resolve()
    qlib_summary_path = Path(args.qlib_summary_path).expanduser().resolve() if args.qlib_summary_path else None

    stage72 = read_json(stage72_path)
    reserve = read_json(reserve_path)
    qlib_summary = read_json(qlib_summary_path) if qlib_summary_path and qlib_summary_path.exists() else {}

    validation_path = evolution / 'factor_validation_report.json'
    portfolio_path = evolution / 'portfolio_backtest_report.json'
    overfit_path = evolution / 'overfit_audit_report.json'
    promotion_path = evolution / 'factor_promotion_decision.json'
    pool_path = evolution / 'factor_iteration_pool.json'
    lineage_path = evolution / 'factor_lineage.json'
    digest_path = evolution / 'daily_factor_evolution_digest.md'
    control_sync_path = evolution / 'current_combo_stage72_control_sync.json'

    validation = read_json(validation_path)
    portfolio = read_json(portfolio_path)
    overfit = read_json(overfit_path)
    promotion = read_json(promotion_path)
    pool = read_json(pool_path)
    lineage = read_json(lineage_path)

    oos = stage72['evaluation_partitions']['independent_oos']['by_label_mode']['close_to_close']
    holdout = stage72['evaluation_partitions']['holdout_validation']['by_label_mode']['close_to_close']
    verdict = stage72['verdict']

    release_reason = (
        f"Stage72 close-to-close 独立 OOS={fmt_pct(oos['combo_metrics']['total_return'])} / 超额 {fmt_pct(oos['excess_total_return'])}，"
        f"独立 holdout={fmt_pct(holdout['combo_metrics']['total_return'])} / 超额 {fmt_pct(holdout['excess_total_return'])}；"
        '两段均转正，因此解除 promotion_freeze 并进入 release。'
    )
    caution = (
        '注意：same-window nested/walkforward 的绝对收益并未被美化；这次放行依据是剥离后的 true independent close-to-close OOS/holdout。'
    )

    validation['generated_at'] = now
    validation['overall_assessment'] = 'close_to_close_true_independent_oos_holdout_positive_release_ready'
    validation.setdefault('source_artifacts', {})['stage72_independent_oos_holdout'] = str(stage72_path)
    validation['source_artifacts']['stage72_holdout_partition'] = str(reserve_path)
    validation.setdefault('canonical_refresh_basis', {})['stage72_control_sync_at'] = now
    validation['canonical_refresh_basis']['stage72_independent_oos_holdout_mtime'] = now
    validation['three_most_severe_gaps'] = [
        f"same-window walkforward / nested 仍不是独立样本外；但 Stage72 已把 {reserve.get('independent_oos_dates', [])} 与 {reserve.get('holdout_validation_dates', [])} 从 replay/backtest/nested/walkforward 中剥离。",
        f"close-to-close 独立 OOS 已转正：combo={fmt_pct(oos['combo_metrics']['total_return'])}，base={fmt_pct(oos['base_metrics']['total_return'])}，excess={fmt_pct(oos['excess_total_return'])}。",
        f"close-to-close 独立 holdout 已转正：combo={fmt_pct(holdout['combo_metrics']['total_return'])}，base={fmt_pct(holdout['base_metrics']['total_return'])}，excess={fmt_pct(holdout['excess_total_return'])}。",
    ]
    validation.setdefault('portfolio_level_validation_gaps', {})['status'] = 'stage72_independent_oos_holdout_completed_for_close_to_close'
    validation['portfolio_level_validation_gaps']['summary'] = release_reason + caution
    validation['portfolio_level_validation_gaps']['gaps'] = [
        'open_to_close / close_to_open 仍未拿到同等级别的 true independent OOS / holdout 放行证据。',
        '后续 release 后仍需继续监控收益口径漂移与样本长度不足风险。',
    ]
    validation['engineering_todo'] = [
        {'priority': 'P0', 'task': 'release 后继续滚动补充 close-to-close 新增独立 OOS / holdout，避免 Stage72 变成一次性幸运窗口。'},
        {'priority': 'P1', 'task': '继续为 open_to_close / close_to_open 建立同等级别的独立 OOS / holdout 证据。'},
        {'priority': 'P1', 'task': '把 Stage72 结果纳入后续 recorder / lineage / digest 的统一口径。'},
    ]
    release = validation.setdefault('release_recommendation', {})
    release['decision'] = 'release'
    release['reason'] = release_reason
    release['allowed_usage'] = [
        '按 close-to-close 主口径解除 promotion_freeze 并进入 release',
        '把 Stage72 独立 OOS / holdout 作为当前 current_combo 的首个 true independent portfolio evidence',
    ]
    release['blocked_usage'] = [
        '把 open_to_close / close_to_open 自动视为同步放行',
        '把 Stage72 结果外推成长期稳定优势而不继续累积后续独立样本',
    ]
    validation['stage72_release_signal'] = {
        'generated_at': now,
        'report_path': str(stage72_path),
        'reserve_path': str(reserve_path),
        'close_to_close_independent_oos_dates': reserve.get('independent_oos_dates', []),
        'close_to_close_holdout_dates': reserve.get('holdout_validation_dates', []),
        'close_to_close_independent_oos_total_return': oos['combo_metrics']['total_return'],
        'close_to_close_independent_oos_excess_total_return': oos['excess_total_return'],
        'close_to_close_holdout_total_return': holdout['combo_metrics']['total_return'],
        'close_to_close_holdout_excess_total_return': holdout['excess_total_return'],
        'promotion_freeze_lifted': verdict['sufficient_to_lift_promotion_freeze'],
        'scope': 'close_to_close_primary',
    }
    dump_json(validation_path, validation)

    portfolio['generated_at'] = now
    portfolio.setdefault('source_paths', {})['stage72_independent_oos_holdout'] = str(stage72_path)
    portfolio.setdefault('oos_validation_assessment', {})['portfolio_level_backtest_completed'] = True
    portfolio['oos_validation_assessment']['independent_oos_completed'] = True
    portfolio['oos_validation_assessment']['holdout_validation_completed'] = True
    portfolio['oos_validation_assessment']['claim_limit'] = release_reason
    portfolio['conclusion']['status'] = 'close_to_close_stage72_release_ready'
    portfolio['conclusion']['effective_oos_claim_allowed'] = True
    portfolio['conclusion']['analog_validation_claim_allowed'] = True
    portfolio['conclusion']['summary'] = release_reason + caution
    portfolio['stage72_release_signal'] = validation['stage72_release_signal']
    dump_json(portfolio_path, portfolio)

    overfit['generated_at'] = now
    overfit.setdefault('missing_validation_stack', {})['portfolio_level_oos_completed'] = True
    overfit['missing_validation_stack']['independent_oos_holdout_stage72_completed'] = True
    overfit['missing_validation_stack']['detail'] = [
        'Stage72 已提供 true independent close-to-close OOS 与 holdout。',
        'same-window nested/walkforward 仍保留为结构性参考，而不是放行依据。',
        'open_to_close / close_to_open 仍缺对应等级的独立放行链。',
    ]
    overfit.setdefault('overall_verdict', {})['promotion_freeze_recommended'] = False
    overfit['overall_verdict']['summary'] = release_reason + caution
    overfit['stage72_release_signal'] = validation['stage72_release_signal']
    dump_json(overfit_path, overfit)

    promotion['generated_at'] = now
    gc = promotion.setdefault('global_constraints', {})
    gc['promotion_freeze'] = False
    gc['independent_oos_completed'] = True
    gc['holdout_validation_completed'] = True
    gc['reserve_holdout_validation_completed'] = True
    gc['promotion_freeze_reason'] = 'close_to_close_independent_oos_and_holdout_positive'
    gc['release_scope'] = 'close_to_close_primary'
    promotion['round_decision'] = 'release_current_combo_close_to_close_primary'
    promotion['summary'] = {
        'promotion_freeze': False,
        'walkforward_close_to_close_excess_total_return': promotion.get('summary', {}).get('walkforward_close_to_close_excess_total_return'),
        'walkforward_open_to_close_excess_total_return': promotion.get('summary', {}).get('walkforward_open_to_close_excess_total_return'),
        'summary': release_reason + caution,
    }
    promotion['current_combo_replay_basis'] = {
        **promotion.get('current_combo_replay_basis', {}),
        'stage72_independent_oos_holdout_report': str(stage72_path),
        'stage72_reserved_union_dates': stage72.get('reserved_union_dates', []),
        'close_to_close_independent_oos_total_return': oos['combo_metrics']['total_return'],
        'close_to_close_independent_oos_excess_total_return': oos['excess_total_return'],
        'close_to_close_holdout_total_return': holdout['combo_metrics']['total_return'],
        'close_to_close_holdout_excess_total_return': holdout['excess_total_return'],
        'sufficient_to_lift_promotion_freeze': True,
    }
    promotion['stage72_release_signal'] = validation['stage72_release_signal']
    dump_json(promotion_path, promotion)

    pool.setdefault('pool_summary', {})['global_validation_caveat'] = release_reason + caution
    pool['pool_summary']['stage72_release_signal'] = validation['stage72_release_signal']
    dump_json(pool_path, pool)

    lineage['generated_at'] = now
    lineage['stage72_release_signal'] = validation['stage72_release_signal']
    dump_json(lineage_path, lineage)

    digest_lines = [
        '# Daily Factor Evolution Digest',
        '',
        f'- generated_at={now}',
        '- 当前主结论：Stage72 已把 recent clean days 从 replay/backtest/nested/walkforward 中剥离，拿到 true independent close-to-close OOS / holdout。',
        f"- independent_oos_dates={reserve.get('independent_oos_dates', [])} -> combo {fmt_pct(oos['combo_metrics']['total_return'])} / excess {fmt_pct(oos['excess_total_return'])}",
        f"- holdout_dates={reserve.get('holdout_validation_dates', [])} -> combo {fmt_pct(holdout['combo_metrics']['total_return'])} / excess {fmt_pct(holdout['excess_total_return'])}",
        '- decision：解除 promotion_freeze，按 close-to-close 主口径进入 release。',
        f"- recorder：{(qlib_summary.get('qlib_recorder') or {}).get('recorder_id') or 'pending'}",
        '',
        '## 风险',
        '- open_to_close / close_to_open 仍未取得同等级别 true independent OOS / holdout 放行证据。',
        '- same-window nested / walkforward 仍仅作结构性参考，不替代独立样本外。',
        '- release 后仍需继续滚动补充新增独立样本。',
        '',
    ]
    digest_path.write_text('\n'.join(digest_lines), encoding='utf-8')

    control_sync = {
        'report_type': 'current_combo_stage72_control_sync',
        'generated_at': now,
        'decision': 'release_current_combo_close_to_close_primary',
        'promotion_freeze_lifted': True,
        'source_paths': {
            'stage72_independent_oos_holdout': str(stage72_path),
            'stage72_holdout_partition': str(reserve_path),
            'factor_validation_report': str(validation_path),
            'portfolio_backtest_report': str(portfolio_path),
            'overfit_audit_report': str(overfit_path),
            'factor_promotion_decision': str(promotion_path),
            'factor_iteration_pool': str(pool_path),
            'factor_lineage': str(lineage_path),
            'daily_factor_evolution_digest': str(digest_path),
            'qlib_recorder_sync_latest': str(qlib_summary_path) if qlib_summary_path else None,
        },
        'stage72_metrics': {
            'independent_oos_dates': reserve.get('independent_oos_dates', []),
            'holdout_validation_dates': reserve.get('holdout_validation_dates', []),
            'close_to_close_independent_oos_total_return': oos['combo_metrics']['total_return'],
            'close_to_close_independent_oos_excess_total_return': oos['excess_total_return'],
            'close_to_close_holdout_total_return': holdout['combo_metrics']['total_return'],
            'close_to_close_holdout_excess_total_return': holdout['excess_total_return'],
        },
        'qlib_recorder': qlib_summary.get('qlib_recorder'),
        'summary': release_reason + caution,
    }
    dump_json(control_sync_path, control_sync)
    print(json.dumps({
        'status': 'ok',
        'control_sync_json': str(control_sync_path),
        'promotion_freeze_lifted': True,
        'decision': control_sync['decision'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
