#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DEFAULT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
TZ = ZoneInfo('Asia/Shanghai')
FOCUS_FACTORS = [
    'f_chip_support_proxy',
    'f_gap_return',
    'f_price_vs_ma20',
    'f_atr_proxy_14',
    'f_volume_ratio_5',
    'f_volume_ratio_20',
    'f_intraday_reversal',
]
MODE_SUFFIXES = {
    'open_to_close': 'open_to_close_focus7',
    'close_to_open': 'close_to_open_focus7',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build focus-factor dual-label comparison artifacts')
    parser.add_argument('--root', default=str(ROOT_DEFAULT))
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def newer_three_label_control_plane_present(root: Path) -> tuple[bool, str]:
    evolution = root / 'outputs' / 'evolution'
    analog_path = evolution / 'analog_validation_report.json'
    claim_path = evolution / 'current_combo_claim_limit_audit.json'
    nested_path = evolution / 'current_combo_nested_validation.json'
    if not analog_path.exists() or not claim_path.exists() or not nested_path.exists():
        return False, ''
    analog = load_json(analog_path)
    claim = load_json(claim_path)
    nested = load_json(nested_path)
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
            'current outputs 已完成三口径 downstream + 收益级 analog replay 控制面；'
            '继续运行 focus7 helper 会把 validation / pool / lineage / digest 回写成低阶 focus7 双口径口径。'
        )
    return False, ''


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds')


def fmt_pct(value: float | None) -> str:
    if value is None:
        return 'NA'
    return f'{float(value):.4%}'


def sign_of(value: float | None) -> str:
    if value is None:
        return 'na'
    value = float(value)
    if value > 0:
        return 'positive'
    if value < 0:
        return 'negative'
    return 'flat'


def latest_plan_factor_ids(weight: dict) -> list[str]:
    return [str(item.get('factor_id')) for item in (weight.get('latest_weight_plan') or {}).get('selected_factors', []) if item.get('factor_id')]


def union_factor_ids(weight: dict) -> list[str]:
    out: set[str] = set()
    for row in weight.get('regime_weight_map', []):
        for item in row.get('selected_factors', []):
            fid = str(item.get('factor_id') or '').strip()
            if fid:
                out.add(fid)
    return sorted(out)


def selection_sources_for_factor(weight: dict, factor_id: str) -> list[str]:
    out: set[str] = set()
    for row in weight.get('regime_weight_map', []):
        for item in row.get('selected_factors', []):
            if str(item.get('factor_id')) == factor_id and item.get('selection_source'):
                out.add(str(item.get('selection_source')))
    return sorted(out)


def compare_lists(base: list[str], alt: list[str]) -> dict:
    base_set = set(base)
    alt_set = set(alt)
    return {
        'added_vs_close_to_close': sorted(alt_set - base_set),
        'removed_vs_close_to_close': sorted(base_set - alt_set),
        'shared_with_close_to_close': sorted(base_set & alt_set),
    }


def report_map(audit: dict) -> dict[str, dict]:
    return {str(item.get('factor_id')): item for item in audit.get('reports', []) if item.get('factor_id')}


def run_checked(cmd: list[str], cwd: Path) -> dict:
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    payload = {'cmd': cmd, 'exit_code': res.returncode, 'stdout': res.stdout, 'stderr': res.stderr}
    if res.returncode != 0:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def build_mode_summary(mode: str, suffix: str, audit_path: Path, weight_path: Path, canonical_audit: dict, canonical_weight: dict) -> dict:
    audit = load_json(audit_path)
    weight = load_json(weight_path)
    current_reports = report_map(audit)
    canonical_reports = report_map(canonical_audit)
    latest_ids = latest_plan_factor_ids(weight)
    latest_union = union_factor_ids(weight)
    canonical_latest_ids = latest_plan_factor_ids(canonical_weight)
    canonical_union = union_factor_ids(canonical_weight)
    focus = {}
    for fid in FOCUS_FACTORS:
        cur = current_reports.get(fid)
        base = canonical_reports.get(fid)
        focus[fid] = {
            'present_in_focus_audit': cur is not None,
            'status': None if not cur else cur.get('status'),
            'support_pool_candidate': None if not cur else cur.get('support_pool_candidate'),
            'rank_ic_mean': None if not cur else round(float(cur.get('rank_ic_mean', 0.0) or 0.0), 6),
            'rank_ic_direction': sign_of(None if not cur else cur.get('rank_ic_mean')),
            'latest_plan_selected': fid in latest_ids,
            'selected_any_regime': fid in latest_union,
            'selection_sources': selection_sources_for_factor(weight, fid),
            'vs_close_to_close': {
                'status': None if not base else base.get('status'),
                'rank_ic_mean': None if not base else round(float(base.get('rank_ic_mean', 0.0) or 0.0), 6),
                'rank_ic_direction': sign_of(None if not base else base.get('rank_ic_mean')),
                'rank_ic_sign_flip': sign_of(None if not cur else cur.get('rank_ic_mean')) != sign_of(None if not base else base.get('rank_ic_mean')),
                'latest_plan_selected': fid in canonical_latest_ids,
                'selected_any_regime': fid in canonical_union,
                'selection_sources': selection_sources_for_factor(canonical_weight, fid),
            },
        }
    return {
        'label_mode': mode,
        'output_suffix': suffix,
        'audit_path': str(audit_path),
        'weight_path': str(weight_path),
        'audit_mtime': iso_mtime(audit_path),
        'weight_mtime': iso_mtime(weight_path),
        'label_column': audit.get('label_column'),
        'benchmark_eligible_trade_days': audit.get('benchmark_eligible_trade_days'),
        'benchmark_eligible_row_count': audit.get('benchmark_eligible_row_count'),
        'factor_count': audit.get('factor_count'),
        'requested_factor_ids': audit.get('requested_factor_ids'),
        'selection_mode': weight.get('selection_mode'),
        'support_pool_count': weight.get('support_pool_count'),
        'fallback_pool_count': weight.get('fallback_pool_count'),
        'latest_weight_plan_factor_ids': latest_ids,
        'latest_weight_plan_factor_union': latest_union,
        'vs_close_to_close_latest_weight_plan': compare_lists(canonical_latest_ids, latest_ids),
        'vs_close_to_close_factor_union': compare_lists(canonical_union, latest_union),
        'focus_factors': focus,
    }


def write_digest(digest_path: Path, comparison: dict, portfolio: dict, claim: dict, nested: dict) -> None:
    formal = portfolio['current_combo_formal_backtest_signal']
    current_plan = [str(item.get('factor_id')) for item in (portfolio['current_portfolio_structure'].get('latest_weight_plan') or {}).get('selected_factors', []) if item.get('factor_id')]
    blocker = claim['independent_oos_blocker']
    sample = nested['sample_coverage']
    lines = [
        '# Daily Factor Evolution Digest',
        '',
        f"- 生成时间：{comparison['generated_at']}",
        f"- 当前交易日：{comparison['trade_date']}",
        f"- 当前组合：{portfolio['current_portfolio_structure']['selection_mode']} | latest shrink_uptrend = {' / '.join(current_plan)}",
        '',
        '## 本轮新增实质产物',
        '- 新增 focus7 双口径最小审计与权重图：覆盖 `f_chip_support_proxy / f_gap_return / f_price_vs_ma20 / f_atr_proxy_14 / f_volume_ratio_5 / f_volume_ratio_20 / f_intraday_reversal`。',
        '- 新增 `label_mode_focus7_comparison.json` / `.md`，把 close-to-close 基线与 open-to-close / close-to-open 的关键因子最小权重计划放到同一控制面比较。',
        '',
        '## focus7 双口径最小对照',
        f"- open-to-close latest focus plan：{' / '.join(comparison['open_to_close']['latest_weight_plan_factor_ids'])}。",
        f"- close-to-open latest focus plan：{' / '.join(comparison['close_to_open']['latest_weight_plan_factor_ids'])}。",
        f"- `f_gap_return` rank_ic 方向：c2c={comparison['close_to_close']['focus_factors']['f_gap_return']['rank_ic_direction']} / o2c={comparison['open_to_close']['focus_factors']['f_gap_return']['rank_ic_direction']} / cto={comparison['close_to_open']['focus_factors']['f_gap_return']['rank_ic_direction']}。",
        f"- `f_volume_ratio_5` rank_ic 方向：c2c={comparison['close_to_close']['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / o2c={comparison['open_to_close']['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / cto={comparison['close_to_open']['focus_factors']['f_volume_ratio_5']['rank_ic_direction']}。",
        f"- `f_intraday_reversal` rank_ic 方向：c2c={comparison['close_to_close']['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / o2c={comparison['open_to_close']['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / cto={comparison['close_to_open']['focus_factors']['f_intraday_reversal']['rank_ic_direction']}。",
        '',
        '## 当前 formal backtest 结果（成本后）',
        f"- close-to-close combo：总收益={fmt_pct(formal['close_to_close_combo_total_return'])}。",
        f"- open-to-close combo：总收益={fmt_pct(formal['open_to_close_combo_total_return'])}。",
        f"- claim-limit：当前 {blocker['close_to_close']['used_labeled_days']} / {blocker['close_to_close']['labeled_eligible_days']} 个有标签 clean days 已用完，remaining_labeled_holdout_days_count={blocker['remaining_labeled_holdout_days_count']}。",
        f"- micro nested：fold_count={sample['fold_count']}，outer_oos_days={sample['outer_oos_days']}。",
        '',
        '## 关键风险',
        '- 本轮只补了 focus7 最小双口径审计，不是全 45 因子的完整替代 canonical。',
        '- 双口径最小权重计划已显示交易时点会改变因子筛选/定权，但组合级 replay/backtest 仍未在替代权重图下独立重放。',
        '- promotion freeze 仍不能解除。',
        '',
        '## 下一步',
        '- 若算力允许，继续把双口径审计从 focus7 扩到全因子池。',
        '- 先把 open-to-close / close-to-open 权重图推进到 current_combo replay + formal backtest，验证分歧是否因权重变化缓解。',
        '- 继续补独立 holdout / analog replay。',
    ]
    digest_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    scripts = root / 'scripts'
    factor_ids_arg = ','.join(FOCUS_FACTORS)

    control_plane_ready, control_plane_reason = newer_three_label_control_plane_present(root)
    if control_plane_ready:
        evolution = root / 'outputs' / 'evolution'
        print(json.dumps({
            'status': 'skipped_existing_three_label_control_plane',
            'generated_at': iso_now(),
            'reason': control_plane_reason,
            'preserved_outputs': [
                str(evolution / 'factor_validation_report.json'),
                str(evolution / 'factor_iteration_pool.json'),
                str(evolution / 'factor_lineage.json'),
                str(evolution / 'daily_factor_evolution_digest.md'),
            ],
            'skipped_focus7_outputs': [
                str(evolution / 'label_mode_focus7_comparison.json'),
                str(evolution / 'label_mode_focus7_comparison.md'),
                str(root / 'outputs' / 'audit_reports' / 'factor_audit_phase1.open_to_close_focus7.json'),
                str(root / 'outputs' / 'audit_reports' / 'factor_audit_phase1.close_to_open_focus7.json'),
                str(root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.open_to_close_focus7.json'),
                str(root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.close_to_open_focus7.json'),
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    commands = []
    output_paths: dict[str, Path] = {}
    for mode, suffix in MODE_SUFFIXES.items():
        commands.append(run_checked([
            sys.executable,
            str(scripts / 'run_factor_audit.py'),
            '--root', str(root),
            '--label-mode', mode,
            '--factor-ids', factor_ids_arg,
            '--output-suffix', suffix,
        ], root))
        commands.append(run_checked([
            sys.executable,
            str(scripts / 'run_weight_search.py'),
            '--root', str(root),
            '--output-suffix', suffix,
        ], root))
        output_paths[f'{mode}_audit'] = root / 'outputs' / 'audit_reports' / f'factor_audit_phase1.{suffix}.json'
        output_paths[f'{mode}_weight'] = root / 'outputs' / 'weight_maps' / f'regime_weight_map_phase1.{suffix}.json'

    c2c_audit_path = root / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json'
    c2c_weight_path = root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json'
    c2c_audit = load_json(c2c_audit_path)
    c2c_weight = load_json(c2c_weight_path)

    comparison = {
        'schema_version': '2026-04-19.label_mode_focus7_comparison.v1',
        'generated_at': iso_now(),
        'scope': 'focus7_minimal_dual_label_audit',
        'focus_factor_ids': FOCUS_FACTORS,
        'trade_date': str((c2c_weight.get('latest_regime') or {}).get('trade_date') or ''),
        'source_artifacts': {
            'close_to_close_audit': str(c2c_audit_path),
            'close_to_close_weight': str(c2c_weight_path),
            'open_to_close_audit': str(output_paths['open_to_close_audit']),
            'open_to_close_weight': str(output_paths['open_to_close_weight']),
            'close_to_open_audit': str(output_paths['close_to_open_audit']),
            'close_to_open_weight': str(output_paths['close_to_open_weight']),
        },
        'refresh_commands': commands,
        'close_to_close': build_mode_summary('close_to_close', 'canonical', c2c_audit_path, c2c_weight_path, c2c_audit, c2c_weight),
        'open_to_close': build_mode_summary('open_to_close', MODE_SUFFIXES['open_to_close'], output_paths['open_to_close_audit'], output_paths['open_to_close_weight'], c2c_audit, c2c_weight),
        'close_to_open': build_mode_summary('close_to_open', MODE_SUFFIXES['close_to_open'], output_paths['close_to_open_audit'], output_paths['close_to_open_weight'], c2c_audit, c2c_weight),
    }
    comparison['key_findings'] = [
        'focus7 最小双口径审计已可在同一 33 天 clean window 上复用。',
        '交易时点会改变 key factors 的 rank_ic 方向与最小权重计划。',
        '但这仍不是全量替代 canonical，也不是组合级替代 replay/backtest。',
    ]

    comparison_json = root / 'outputs' / 'evolution' / 'label_mode_focus7_comparison.json'
    comparison_md = root / 'outputs' / 'evolution' / 'label_mode_focus7_comparison.md'
    dump_json(comparison_json, comparison)
    comparison_md.write_text('\n'.join([
        '# Label Mode Focus7 Comparison',
        '',
        f"- 生成时间：{comparison['generated_at']}",
        f"- focus 因子：{', '.join(FOCUS_FACTORS)}",
        f"- open-to-close latest plan：{' / '.join(comparison['open_to_close']['latest_weight_plan_factor_ids'])}",
        f"- close-to-open latest plan：{' / '.join(comparison['close_to_open']['latest_weight_plan_factor_ids'])}",
        f"- f_gap_return 方向：c2c={comparison['close_to_close']['focus_factors']['f_gap_return']['rank_ic_direction']} / o2c={comparison['open_to_close']['focus_factors']['f_gap_return']['rank_ic_direction']} / cto={comparison['close_to_open']['focus_factors']['f_gap_return']['rank_ic_direction']}",
        f"- f_volume_ratio_5 方向：c2c={comparison['close_to_close']['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / o2c={comparison['open_to_close']['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / cto={comparison['close_to_open']['focus_factors']['f_volume_ratio_5']['rank_ic_direction']}",
        f"- f_intraday_reversal 方向：c2c={comparison['close_to_close']['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / o2c={comparison['open_to_close']['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / cto={comparison['close_to_open']['focus_factors']['f_intraday_reversal']['rank_ic_direction']}",
        '',
        '- 结论：已形成 key factors 的最小双口径审计与权重图，但仍需推进到组合级 replay/backtest。',
        '',
    ]) + '\n', encoding='utf-8')

    validation_path = root / 'outputs' / 'evolution' / 'factor_validation_report.json'
    pool_path = root / 'outputs' / 'evolution' / 'factor_iteration_pool.json'
    lineage_path = root / 'outputs' / 'evolution' / 'factor_lineage.json'
    digest_path = root / 'outputs' / 'evolution' / 'daily_factor_evolution_digest.md'
    portfolio = load_json(root / 'outputs' / 'evolution' / 'portfolio_backtest_report.json')
    claim = load_json(root / 'outputs' / 'evolution' / 'current_combo_claim_limit_audit.json')
    nested = load_json(root / 'outputs' / 'evolution' / 'current_combo_nested_validation.json')

    validation = load_json(validation_path)
    validation.setdefault('source_artifacts', {})['label_mode_focus7_comparison'] = str(comparison_json)
    validation['source_artifacts']['label_mode_focus7_comparison_md'] = str(comparison_md)
    validation['source_artifacts']['open_to_close_focus7_audit'] = str(output_paths['open_to_close_audit'])
    validation['source_artifacts']['open_to_close_focus7_weight'] = str(output_paths['open_to_close_weight'])
    validation['source_artifacts']['close_to_open_focus7_audit'] = str(output_paths['close_to_open_audit'])
    validation['source_artifacts']['close_to_open_focus7_weight'] = str(output_paths['close_to_open_weight'])
    validation['focus_label_mode_comparison'] = {
        'generated_at': comparison['generated_at'],
        'scope': comparison['scope'],
        'focus_factor_ids': FOCUS_FACTORS,
        'comparison_report': str(comparison_json),
        'open_to_close_latest_weight_plan_factor_ids': comparison['open_to_close']['latest_weight_plan_factor_ids'],
        'close_to_open_latest_weight_plan_factor_ids': comparison['close_to_open']['latest_weight_plan_factor_ids'],
    }
    validation.setdefault('close_to_close_vs_open_to_close_comparison', {}).setdefault('evidence', {})['focus_label_mode_comparison'] = validation['focus_label_mode_comparison']
    validation['generated_at'] = comparison['generated_at']
    dump_json(validation_path, validation)

    for path in [pool_path, lineage_path]:
        payload = load_json(path)
        payload.setdefault('source_artifacts', {})['label_mode_focus7_comparison'] = str(comparison_json)
        payload.setdefault('source_artifacts', {})['label_mode_focus7_comparison_md'] = str(comparison_md)
        payload['source_artifacts']['open_to_close_focus7_audit'] = str(output_paths['open_to_close_audit'])
        payload['source_artifacts']['open_to_close_focus7_weight'] = str(output_paths['open_to_close_weight'])
        payload['source_artifacts']['close_to_open_focus7_audit'] = str(output_paths['close_to_open_audit'])
        payload['source_artifacts']['close_to_open_focus7_weight'] = str(output_paths['close_to_open_weight'])
        payload['focus_label_mode_comparison'] = {
            'generated_at': comparison['generated_at'],
            'scope': comparison['scope'],
            'focus_factor_ids': FOCUS_FACTORS,
            'comparison_report': str(comparison_json),
            'open_to_close_latest_weight_plan_factor_ids': comparison['open_to_close']['latest_weight_plan_factor_ids'],
            'close_to_open_latest_weight_plan_factor_ids': comparison['close_to_open']['latest_weight_plan_factor_ids'],
            'f_gap_return_directions': {
                'close_to_close': comparison['close_to_close']['focus_factors']['f_gap_return']['rank_ic_direction'],
                'open_to_close': comparison['open_to_close']['focus_factors']['f_gap_return']['rank_ic_direction'],
                'close_to_open': comparison['close_to_open']['focus_factors']['f_gap_return']['rank_ic_direction'],
            },
        }
        payload['generated_at'] = comparison['generated_at']
        dump_json(path, payload)

    write_digest(digest_path, comparison, portfolio, claim, nested)

    print(json.dumps({
        'status': 'ok',
        'comparison_json': str(comparison_json),
        'comparison_md': str(comparison_md),
        'open_to_close_audit': str(output_paths['open_to_close_audit']),
        'open_to_close_weight': str(output_paths['open_to_close_weight']),
        'close_to_open_audit': str(output_paths['close_to_open_audit']),
        'close_to_open_weight': str(output_paths['close_to_open_weight']),
        'updated_files': [str(validation_path), str(pool_path), str(lineage_path), str(digest_path)],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
