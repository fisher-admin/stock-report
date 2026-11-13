#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
OUTPUTS = ROOT / 'outputs'
AUDITS = OUTPUTS / 'audit_reports'
WEIGHTS = OUTPUTS / 'weight_maps'
EVOLUTION = OUTPUTS / 'evolution'
TZ = ZoneInfo('Asia/Shanghai')

C2C_AUDIT = AUDITS / 'factor_audit_phase1.json'
O2C_AUDIT = AUDITS / 'factor_audit_phase1.o2c_full45_33d.json'
C2O_AUDIT = AUDITS / 'factor_audit_phase1.c2o_full45_33d.json'
C2C_WEIGHT = WEIGHTS / 'regime_weight_map_phase1.json'
O2C_WEIGHT = WEIGHTS / 'regime_weight_map_phase1.o2c_full45_33d.json'
C2O_WEIGHT = WEIGHTS / 'regime_weight_map_phase1.c2o_full45_33d.json'

COMPARISON_JSON = EVOLUTION / 'multi_label_canonical_comparison.json'
COMPARISON_MD = EVOLUTION / 'multi_label_canonical_comparison.md'
LEGACY_COMPARISON_JSON = EVOLUTION / 'label_mode_canonical_comparison.json'
LEGACY_COMPARISON_MD = EVOLUTION / 'label_mode_canonical_comparison.md'
ITERATION_POOL = EVOLUTION / 'factor_iteration_pool.json'
FACTOR_LINEAGE = EVOLUTION / 'factor_lineage.json'
VALIDATION = EVOLUTION / 'factor_validation_report.json'
DIGEST = EVOLUTION / 'daily_factor_evolution_digest.md'
ANALOG_REPORT = EVOLUTION / 'analog_validation_report.json'
CLAIM_LIMIT = EVOLUTION / 'current_combo_claim_limit_audit.json'
NESTED = EVOLUTION / 'current_combo_nested_validation.json'


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def newer_three_label_control_plane_present() -> tuple[bool, str]:
    if not ANALOG_REPORT.exists() or not CLAIM_LIMIT.exists() or not NESTED.exists():
        return False, ''
    analog = load_json(ANALOG_REPORT)
    claim = load_json(CLAIM_LIMIT)
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
            'current outputs 已完成三口径 downstream + 收益级 analog replay 控制面；'
            '继续运行 multi-label canonical context helper 会把 validation / pool / lineage / digest 回写成旧阶段文案。'
        )
    return False, ''


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec='seconds')


def sign_of(value: float | int | None) -> str:
    if value is None:
        return 'na'
    val = float(value)
    if val > 0:
        return 'positive'
    if val < 0:
        return 'negative'
    return 'flat'


def pct(value: float | None) -> str:
    if value is None:
        return 'NA'
    return f'{float(value):.4%}'


def build_label_payload(label_name: str, audit_path: Path, weight_path: Path) -> dict:
    audit = load_json(audit_path)
    weight = load_json(weight_path)
    latest_weight_plan = weight.get('latest_weight_plan') or {}
    latest_selected = latest_weight_plan.get('selected_factors') or []
    factor_ids = [str(item.get('factor_id')) for item in latest_selected if item.get('factor_id')]
    return {
        'label_name': label_name,
        'audit_path': str(audit_path),
        'weight_path': str(weight_path),
        'audit_mtime': iso_mtime(audit_path),
        'weight_mtime': iso_mtime(weight_path),
        'label_mode': audit.get('label_mode'),
        'label_column': audit.get('label_column'),
        'benchmark_eligible_trade_days': audit.get('benchmark_eligible_trade_days'),
        'benchmark_eligible_row_count': audit.get('benchmark_eligible_row_count'),
        'factor_count': audit.get('factor_count'),
        'whitelist_count': audit.get('whitelist_count'),
        'coldlist_count': audit.get('coldlist_count'),
        'support_pool_count': weight.get('support_pool_count'),
        'fallback_pool_count': weight.get('fallback_pool_count'),
        'selection_mode': weight.get('selection_mode'),
        'latest_regime': weight.get('latest_regime'),
        'latest_weight_plan_factor_ids': factor_ids,
        'latest_weight_plan': latest_weight_plan,
        'reports': {item.get('factor_id'): item for item in audit.get('reports', []) if item.get('factor_id')},
    }


def factor_snapshot(payload: dict, factor_id: str) -> dict:
    item = payload['reports'].get(factor_id, {})
    return {
        'status': item.get('status'),
        'support_pool_candidate': item.get('support_pool_candidate'),
        'rank_ic_mean': item.get('rank_ic_mean'),
        'rank_ic_abs': item.get('rank_ic_abs'),
        'sign': sign_of(item.get('rank_ic_mean')),
    }


def build_comparison() -> tuple[dict, str]:
    now = datetime.now(TZ).isoformat(timespec='seconds')
    c2c = build_label_payload('close_to_close', C2C_AUDIT, C2C_WEIGHT)
    o2c = build_label_payload('open_to_close', O2C_AUDIT, O2C_WEIGHT)
    c2o = build_label_payload('close_to_open', C2O_AUDIT, C2O_WEIGHT)
    labels = {
        'close_to_close': c2c,
        'open_to_close': o2c,
        'close_to_open': c2o,
    }

    latest_sets = {name: set(payload['latest_weight_plan_factor_ids']) for name, payload in labels.items()}
    common_latest = sorted(set.intersection(*latest_sets.values()))
    union_latest = sorted(set.union(*latest_sets.values()))

    factor_table = {}
    for factor_id in union_latest:
        factor_table[factor_id] = {
            label: factor_snapshot(payload, factor_id)
            for label, payload in labels.items()
        }

    c2c_only = sorted(latest_sets['close_to_close'] - latest_sets['open_to_close'] - latest_sets['close_to_open'])
    o2c_only = sorted(latest_sets['open_to_close'] - latest_sets['close_to_close'] - latest_sets['close_to_open'])
    c2o_only = sorted(latest_sets['close_to_open'] - latest_sets['close_to_close'] - latest_sets['open_to_close'])
    unstable = []
    for factor_id, snapshots in factor_table.items():
        signs = {snapshots[label]['sign'] for label in snapshots}
        if len(signs - {'na'}) > 1:
            unstable.append(factor_id)
    unstable.sort()
    latest_regime_id = (c2c.get('latest_regime') or {}).get('regime_id') or '当前regime'

    summary = {
        'generated_at': now,
        'schema_version': '2026-04-19.multi_label_canonical_comparison.v1',
        'labels': {
            name: {
                key: value
                for key, value in payload.items()
                if key not in {'reports'}
            }
            for name, payload in labels.items()
        },
        'latest_weight_plan_overlap': {
            'common_factor_ids': common_latest,
            'union_factor_ids': union_latest,
            'close_to_close_only': c2c_only,
            'open_to_close_only': o2c_only,
            'close_to_open_only': c2o_only,
        },
        'factor_sign_table_for_latest_union': factor_table,
        'sign_unstable_factor_ids': unstable,
        'takeaways': [
            f"三套独立 canonical audit 均覆盖 {c2c['benchmark_eligible_trade_days']} 个 benchmark eligible days / {c2c['benchmark_eligible_row_count']} 行 clean window。",
            f"latest {latest_regime_id} 计划分别为 c2c={c2c['latest_weight_plan_factor_ids']}、o2c={o2c['latest_weight_plan_factor_ids']}、c2o={c2o['latest_weight_plan_factor_ids']}。",
            f"三口径共同保留的 latest_weight_plan 因子={common_latest or '[]'}；仅某一口径才出现的因子：c2c_only={c2c_only or '[]'}，o2c_only={o2c_only or '[]'}，c2o_only={c2o_only or '[]'}。",
            f"latest 计划并集中的符号不稳定因子={unstable or '[]'}；这些因子不能在没有组合级独立 replay/backtest 的前提下直接 promote。",
        ],
    }

    md_lines = [
        '# Multi-label Canonical Comparison',
        '',
        f'- generated_at: {now}',
        '',
        '## Latest weight plan by label',
        f"- close_to_close: {', '.join(c2c['latest_weight_plan_factor_ids']) or 'NA'}",
        f"- open_to_close: {', '.join(o2c['latest_weight_plan_factor_ids']) or 'NA'}",
        f"- close_to_open: {', '.join(c2o['latest_weight_plan_factor_ids']) or 'NA'}",
        '',
        '## Overlap',
        f"- common_latest: {', '.join(common_latest) or 'NA'}",
        f"- c2c_only: {', '.join(c2c_only) or 'NA'}",
        f"- o2c_only: {', '.join(o2c_only) or 'NA'}",
        f"- c2o_only: {', '.join(c2o_only) or 'NA'}",
        '',
        '## Sign instability inside latest-plan union',
        f"- unstable_factor_ids: {', '.join(unstable) or 'NA'}",
        '',
        '## Key takeaways',
        *[f'- {item}' for item in summary['takeaways']],
        '',
    ]
    return summary, '\n'.join(md_lines) + '\n'


def write_comparison_artifacts(comparison: dict, md: str) -> list[str]:
    dump_json(COMPARISON_JSON, comparison)
    COMPARISON_MD.write_text(md, encoding='utf-8')
    dump_json(LEGACY_COMPARISON_JSON, comparison)
    LEGACY_COMPARISON_MD.write_text(md, encoding='utf-8')
    return [
        str(COMPARISON_JSON),
        str(COMPARISON_MD),
        str(LEGACY_COMPARISON_JSON),
        str(LEGACY_COMPARISON_MD),
    ]


def sync_iteration_pool(comparison: dict) -> None:
    payload = load_json(ITERATION_POOL)
    payload.setdefault('pool_summary', {})['multi_label_canonical_summary'] = {
        'generated_at': comparison['generated_at'],
        'available_label_modes': {
            label: {
                'audit_path': info['audit_path'],
                'weight_path': info['weight_path'],
                'label_column': info['label_column'],
                'latest_weight_plan_factor_ids': info['latest_weight_plan_factor_ids'],
            }
            for label, info in comparison['labels'].items()
        },
        'common_latest_weight_plan_factor_ids': comparison['latest_weight_plan_overlap']['common_factor_ids'],
        'sign_unstable_factor_ids': comparison['sign_unstable_factor_ids'],
    }
    payload['pool_summary']['global_validation_caveat'] = (
        '当前已补出 close_to_close / open_to_close / close_to_open 三套独立 canonical audit + weight map，'
        '但 latest_weight_plan 仍随标签口径发生分歧；组合级收益验证与放行仍需以下游 current_combo 控制面统一判定；'
        '因此 promotion freeze 继续维持。'
    )
    dump_json(ITERATION_POOL, payload)


def sync_factor_lineage(comparison: dict) -> None:
    payload = load_json(FACTOR_LINEAGE)
    payload['multi_label_canonical_context'] = {
        'generated_at': comparison['generated_at'],
        'label_modes': {
            label: {
                'audit_mtime': info['audit_mtime'],
                'weight_mtime': info['weight_mtime'],
                'latest_weight_plan_factor_ids': info['latest_weight_plan_factor_ids'],
                'support_pool_count': info['support_pool_count'],
                'fallback_pool_count': info['fallback_pool_count'],
            }
            for label, info in comparison['labels'].items()
        },
        'common_latest_weight_plan_factor_ids': comparison['latest_weight_plan_overlap']['common_factor_ids'],
        'label_specific_latest_weight_plan_factor_ids': {
            'close_to_close_only': comparison['latest_weight_plan_overlap']['close_to_close_only'],
            'open_to_close_only': comparison['latest_weight_plan_overlap']['open_to_close_only'],
            'close_to_open_only': comparison['latest_weight_plan_overlap']['close_to_open_only'],
        },
        'sign_unstable_factor_ids': comparison['sign_unstable_factor_ids'],
    }
    payload.setdefault('canonical_refresh_basis', {})['multi_label_audit_mtimes'] = {
        label: info['audit_mtime'] for label, info in comparison['labels'].items()
    }
    payload['canonical_refresh_basis']['multi_label_weight_mtimes'] = {
        label: info['weight_mtime'] for label, info in comparison['labels'].items()
    }
    dump_json(FACTOR_LINEAGE, payload)


def sync_validation(comparison: dict) -> None:
    payload = load_json(VALIDATION)
    payload['generated_at'] = comparison['generated_at']
    payload.setdefault('canonical_refresh_basis', {})['multi_label_audit_mtimes'] = {
        label: info['audit_mtime'] for label, info in comparison['labels'].items()
    }
    payload['canonical_refresh_basis']['multi_label_weight_mtimes'] = {
        label: info['weight_mtime'] for label, info in comparison['labels'].items()
    }
    payload['multi_label_canonical_audit'] = {
        'status': 'completed_for_close_open_labels',
        'summary': (
            f'open_to_close 与 close_to_open 已补出与当前 {benchmark_days} 天 clean window / {benchmark_rows} 行对齐的独立 canonical audit + weight map；'
            '但 latest_weight_plan 随标签口径发生分歧，组合级收益验证与放行仍需以下游 current_combo 控制面统一判定。'
        ),
        'latest_weight_plan_by_label': {
            label: info['latest_weight_plan_factor_ids']
            for label, info in comparison['labels'].items()
        },
        'common_latest_weight_plan_factor_ids': comparison['latest_weight_plan_overlap']['common_factor_ids'],
        'sign_unstable_factor_ids': comparison['sign_unstable_factor_ids'],
        'evidence_paths': [
            comparison['labels']['close_to_close']['audit_path'],
            comparison['labels']['close_to_close']['weight_path'],
            comparison['labels']['open_to_close']['audit_path'],
            comparison['labels']['open_to_close']['weight_path'],
            comparison['labels']['close_to_open']['audit_path'],
            comparison['labels']['close_to_open']['weight_path'],
            str(COMPARISON_JSON),
        ],
    }
    infra = payload.setdefault('infrastructure_progress', {}).setdefault('return_label_infrastructure', {})
    infra['status'] = 'implemented_and_canonicalized_for_close_open_labels'
    infra['summary'] = 'panel 与 factor_audit 已支持 c2c/o2c/c2o；本轮进一步补出 open_to_close / close_to_open 独立 canonical audit 与 weight map。'
    infra['residual_risk'] = '三套 canonical 权重图已齐，但多口径 latest_weight_plan 之间仍有分歧；组合级收益验证与放行仍需以下游 current_combo 控制面统一判定。'
    infra.setdefault('evidence', {})['alternate_label_artifacts'] = {
        'open_to_close_audit': comparison['labels']['open_to_close']['audit_path'],
        'open_to_close_weight': comparison['labels']['open_to_close']['weight_path'],
        'close_to_open_audit': comparison['labels']['close_to_open']['audit_path'],
        'close_to_open_weight': comparison['labels']['close_to_open']['weight_path'],
    }
    payload['three_most_severe_gaps'] = [
        f'当前 clean window 仍只有 {benchmark_days} 个 benchmark eligible days，其中 formal current combo 只有 {max(int(benchmark_days) - 1, 0) if str(benchmark_days).isdigit() else "NA"} 个具备 next-day 标签可用于完整回放，独立 labeled holdout 仍为 0。',
        (
            '虽然 open_to_close / close_to_open 已补出独立 canonical audit + weight map，但 latest_weight_plan 仍分歧：'
            f"c2c={comparison['labels']['close_to_close']['latest_weight_plan_factor_ids']}，"
            f"o2c={comparison['labels']['open_to_close']['latest_weight_plan_factor_ids']}，"
            f"c2o={comparison['labels']['close_to_open']['latest_weight_plan_factor_ids']}。"
        ),
        'current combo 的 formal backtest 仍显示 close-to-close 为正、open-to-close 为负，且独立 labeled holdout / rolling OOS 仍缺，因此 promotion freeze 不能解除。',
    ]
    payload['engineering_todo'] = [
        {
            'priority': 'P0',
            'task': '继续校准 multi-label canonical 与 current combo replay / formal backtest / claim-limit / nested / walkforward 的 shared 控制面，避免旧 helper 回灌旧口径。',
        },
        {
            'priority': 'P0',
            'task': '为 current combo 预留真正独立的 labeled holdout / rolling OOS，不要再让 formal replay 吃满全部 clean-window 标签。',
        },
        {
            'priority': 'P1',
            'task': '在已有收益级 analog replay 基础上，继续补独立 labeled holdout / rolling OOS，并视需要接历史权重重建与真实交易约束。',
        },
        {
            'priority': 'P1',
            'task': '把 micro nested / same-window walkforward 扩成真正独立的 nested walk-forward / rolling template selection，减少对既有全样本模板的复用。',
        },
    ]
    payload.setdefault('release_recommendation', {})['reason'] = (
        '多口径 canonical audit / weight 已补齐，基础设施进一步收口；'
        '但 latest_weight_plan 仍分歧、current combo formal backtest 仍出现 c2c/o2c 绝对收益方向不一致、独立 OOS 仍缺，因此继续 hold。'
    )
    dump_json(VALIDATION, payload)


def sync_digest(comparison: dict) -> None:
    validation = load_json(VALIDATION)
    common = comparison['latest_weight_plan_overlap']['common_factor_ids']
    c2c = comparison['labels']['close_to_close']['latest_weight_plan_factor_ids']
    o2c = comparison['labels']['open_to_close']['latest_weight_plan_factor_ids']
    c2o = comparison['labels']['close_to_open']['latest_weight_plan_factor_ids']
    current_regime = comparison['labels']['close_to_close'].get('latest_regime') or {}
    current_trade_date = current_regime.get('trade_date') or '未知'
    current_regime_id = current_regime.get('regime_id') or '未知'
    selection_mode = comparison['labels']['close_to_close'].get('selection_mode') or 'unknown'
    benchmark_days = comparison['labels']['close_to_close'].get('benchmark_eligible_trade_days', 'NA')
    benchmark_rows = comparison['labels']['close_to_close'].get('benchmark_eligible_row_count', 'NA')
    lines = [
        '# Daily Factor Evolution Digest',
        '',
        f"- 生成时间：{comparison['generated_at']}",
        f"- 当前 canonical regime trade_date：{current_trade_date}",
        f"- 当前组合：{selection_mode} | c2c latest {current_regime_id} = {' / '.join(c2c)}",
        '',
        '## 本轮新增实质产物',
        '- 新增 multi-label canonical：`factor_audit_phase1.o2c_full45_33d.json` / `regime_weight_map_phase1.o2c_full45_33d.json` 与 `factor_audit_phase1.c2o_full45_33d.json` / `regime_weight_map_phase1.c2o_full45_33d.json` 已落地。',
        f"- 三口径 latest_weight_plan：c2c={c2c}；o2c={o2c}；c2o={c2o}。",
        f"- 三口径共同保留因子：{common or '[]'}；符号不稳定因子：{comparison['sign_unstable_factor_ids'] or '[]'}。",
        '',
        '## 当前最小可信结论',
        f'- clean benchmark / selection_history 回填口径维持 {benchmark_days} 个 benchmark eligible days / {benchmark_rows} 行。',
        '- 现在不再是“没有 open_to_close / close_to_open canonical”，而是“多口径 canonical 已齐，但 latest_weight_plan 仍分歧”。',
        '- promotion freeze 继续维持；新增 evidence 只能证明口径分歧被显式量化，不能证明可交易优势已经稳定。',
        '',
        '## 当前关键风险',
        *[f'- {item}' for item in validation.get('three_most_severe_gaps', [])],
        '',
        '## 下一步',
        '- 把主阻塞收敛到独立 labeled holdout / rolling OOS。',
        '- 继续校准 multi-label canonical 与 current combo replay / formal backtest / claim-limit / nested / walkforward 的 shared 控制面。',
        '- 把 same-window walkforward / micro nested 扩成真正独立的 nested walk-forward / rolling template selection。',
        '',
    ]
    DIGEST.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description='Sync multi-label canonical context artifacts.')
    parser.add_argument(
        '--comparison-only',
        action='store_true',
        help='Only rebuild multi_label/legacy canonical comparison files from current audit + weight artifacts.',
    )
    args = parser.parse_args()
    control_plane_ready, control_plane_reason = newer_three_label_control_plane_present()
    if control_plane_ready and not args.comparison_only:
        print(json.dumps({
            'status': 'skipped_existing_three_label_control_plane',
            'generated_at': datetime.now(TZ).isoformat(timespec='seconds'),
            'reason': control_plane_reason,
            'preserved_outputs': [
                str(VALIDATION),
                str(ITERATION_POOL),
                str(FACTOR_LINEAGE),
                str(DIGEST),
            ],
            'recommended_sync_chain': [
                'python scripts/run_current_combo_analog_replay.py --root /Users/fisher/.openclaw/workspace/factor_factory',
                'python scripts/sync_multi_label_combo_validation_context.py',
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    for path in [C2C_AUDIT, O2C_AUDIT, C2O_AUDIT, C2C_WEIGHT, O2C_WEIGHT, C2O_WEIGHT, ITERATION_POOL, FACTOR_LINEAGE, VALIDATION]:
        if not path.exists():
            raise FileNotFoundError(path)
    comparison, md = build_comparison()
    comparison_outputs = write_comparison_artifacts(comparison, md)
    if args.comparison_only:
        print(json.dumps({
            'status': 'ok_comparison_only',
            'comparison_json': str(COMPARISON_JSON),
            'comparison_md': str(COMPARISON_MD),
            'legacy_comparison_json': str(LEGACY_COMPARISON_JSON),
            'legacy_comparison_md': str(LEGACY_COMPARISON_MD),
            'updated_files': comparison_outputs,
        }, ensure_ascii=False, indent=2))
        return 0
    sync_iteration_pool(comparison)
    sync_factor_lineage(comparison)
    sync_validation(comparison)
    sync_digest(comparison)
    print(json.dumps({
        'status': 'ok',
        'comparison_json': str(COMPARISON_JSON),
        'comparison_md': str(COMPARISON_MD),
        'legacy_comparison_json': str(LEGACY_COMPARISON_JSON),
        'legacy_comparison_md': str(LEGACY_COMPARISON_MD),
        'updated_files': comparison_outputs + [
            str(ITERATION_POOL),
            str(FACTOR_LINEAGE),
            str(VALIDATION),
            str(DIGEST),
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
