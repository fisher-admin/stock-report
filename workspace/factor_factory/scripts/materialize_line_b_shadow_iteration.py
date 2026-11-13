#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from run_greenfield_final_scoring_recovery import (
    line_concentration_stats,
    next_round_guidance,
    overfit_risk_label,
)
from sync_greenfield_mainline_outputs import build_overfit_audit_report, selection_counts

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory')
PARALLEL_ROOT = ROOT / 'outputs' / 'evolution' / 'parallel_lines'
RUN_NAME = '20260422_line_b_shadow_iteration_001'
RUN_DIR = PARALLEL_ROOT / RUN_NAME
LINE_NAME = 'line_b_downside_rotation_c2c'
LINE_DIR = RUN_DIR / LINE_NAME
SOURCE_ROOT = ROOT / 'outputs' / 'greenfield_multifactor' / 'parallel_lines' / LINE_NAME
LATEST_PATH = PARALLEL_ROOT / 'line_b_shadow_iteration_latest.json'
LATEST_LOG_PATH = PARALLEL_ROOT / 'line_b_shadow_iteration_latest.log'
TZ = ZoneInfo('Asia/Shanghai')

SOURCE_JSONS = {
    'portfolio_spec': SOURCE_ROOT / 'portfolio_spec.json',
    'current_selection_snapshot': SOURCE_ROOT / 'current_selection_snapshot.json',
    'validation_summary': SOURCE_ROOT / 'validation_summary.json',
    'walkforward_report': SOURCE_ROOT / 'walkforward_report.json',
    'analog_validation': SOURCE_ROOT / 'analog_validation.json',
    'market_state_hypotheses': SOURCE_ROOT / 'market_state_hypotheses.json',
    'current_factor_leaders': SOURCE_ROOT / 'current_factor_leaders.json',
    'factor_pool_snapshot': SOURCE_ROOT / 'factor_pool_snapshot.json',
}
SOURCE_TEXTS = {
    'market_state_hypotheses_md': SOURCE_ROOT / 'market_state_hypotheses.md',
    'factor_pool_engineering_audit_md': SOURCE_ROOT / 'factor_pool_engineering_audit.md',
}


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def append_log(line: str) -> None:
    LATEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LATEST_LOG_PATH.open('a', encoding='utf-8') as fh:
        fh.write(line.rstrip() + '\n')


def enrich_with_shadow_meta(payload: dict[str, Any], *, kind: str, now: str) -> dict[str, Any]:
    enriched = copy.deepcopy(payload)
    enriched['shadow_iteration'] = {
        'run_name': RUN_NAME,
        'run_dir': str(RUN_DIR),
        'line_name': LINE_NAME,
        'materialized_at': now,
        'materialization_mode': 'official_artifact_rehydration_from_source_line_output',
        'status': 'artifacts_materialized',
        'promotion': False,
        'legacy_reference_only': ['prebreakout_v41'],
        'source_output_root': str(SOURCE_ROOT),
        'artifact_kind': kind,
    }
    return enriched


def ensure_source_exists() -> None:
    missing = [str(path) for path in list(SOURCE_JSONS.values()) + list(SOURCE_TEXTS.values()) if not path.exists()]
    if missing:
        raise FileNotFoundError('missing source artifacts: ' + ', '.join(missing))


def build_shadow_gate(validation_summary: dict[str, Any], concentration: dict[str, Any], risk_level: str) -> dict[str, Any]:
    blocked_by: list[str] = []
    max_weight = concentration.get('max_weight')
    selected_factor_count = int(concentration.get('selected_factor_count') or 0)
    effective_factor_count = float(concentration.get('effective_factor_count') or 0.0)
    if risk_level == 'high':
        blocked_by.append('overfit_risk_level=high')
    if max_weight is not None and float(max_weight) >= 0.45:
        blocked_by.append('max_single_factor_weight>=0.45')
    if selected_factor_count < 3:
        blocked_by.append('selected_factor_count<3')
    if effective_factor_count < 2.5:
        blocked_by.append('effective_factor_count<2.5')
    if float(validation_summary.get('walkforward_close_to_close_excess_total_return') or 0.0) <= 0:
        blocked_by.append('walkforward_close_to_close_excess_total_return<=0')
    if float(validation_summary.get('analog_close_to_close_total_return') or 0.0) <= 0:
        blocked_by.append('analog_close_to_close_total_return<=0')
    return {
        'shadow_candidate': True,
        'promotion_allowed': False,
        'blocked_by': blocked_by,
    }


def write_materialized_artifacts() -> dict[str, Any]:
    ensure_source_exists()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LINE_DIR.mkdir(parents=True, exist_ok=True)

    now = iso_now()
    manifest = load_json(RUN_DIR / 'manifest.json')
    previous_progress = load_json(RUN_DIR / 'progress.json')

    portfolio = load_json(SOURCE_JSONS['portfolio_spec'])
    current_selection = load_json(SOURCE_JSONS['current_selection_snapshot'])
    validation_summary = load_json(SOURCE_JSONS['validation_summary'])
    walkforward = load_json(SOURCE_JSONS['walkforward_report'])
    analog = load_json(SOURCE_JSONS['analog_validation'])

    walkforward_counter, analog_counter, _ = selection_counts(walkforward, analog, current_selection)
    concentration = line_concentration_stats(portfolio)
    risk_level = overfit_risk_label(validation_summary, concentration)
    shadow_gate = build_shadow_gate(validation_summary, concentration, risk_level)
    guidance = next_round_guidance(LINE_NAME, {'timeout': 0}, concentration)
    if 'overfit_risk_level=high' in shadow_gate['blocked_by']:
        guidance.append('当前 overfit 仍为 high；继续做 shadow iterate，不进入 promotion。')
    if 'selected_factor_count<3' in shadow_gate['blocked_by']:
        guidance.append('把有效因子数从 2 提升到至少 3，优先补 downside_control 以外的非同质因子。')
    if 'max_single_factor_weight>=0.45' in shadow_gate['blocked_by']:
        guidance.append('继续把单因子权重上限压到 0.40 以下，先拆 g_liquid_leadership / g_drawdown_resilience 的 0.5-0.5 结构。')

    overfit_audit = build_overfit_audit_report(
        now,
        RUN_NAME,
        walkforward,
        analog,
        portfolio,
        walkforward_counter,
        analog_counter,
    )
    overfit_audit['schema_version'] = '2026-04-23.line_b_shadow_iteration.overfit_audit.v1'
    overfit_audit['run_name'] = RUN_NAME
    overfit_audit['run_dir'] = str(RUN_DIR)
    overfit_audit['line_name'] = LINE_NAME
    overfit_audit['source_output_root'] = str(SOURCE_ROOT)
    overfit_audit['overall_risk_level'] = risk_level
    overfit_audit['shadow_gate'] = shadow_gate
    overfit_audit['shadow_concentration'] = concentration
    overfit_audit['overall_verdict'] = {
        'promotion_freeze_recommended': True,
        'summary': 'line_b 已完成 shadow formalization，但当前仍是高 overfit：c2c 为正、analog 为正，然而 0.5/0.5 双因子结构导致集中度与有效因子数未过闸，继续 shadow iterate。',
    }

    portfolio_out = enrich_with_shadow_meta(portfolio, kind='portfolio_spec', now=now)
    current_selection_out = enrich_with_shadow_meta(current_selection, kind='current_selection_snapshot', now=now)
    validation_out = enrich_with_shadow_meta(validation_summary, kind='validation_summary', now=now)
    validation_out['shadow_iteration']['concentration'] = concentration
    validation_out['shadow_iteration']['shadow_gate'] = shadow_gate
    validation_out['shadow_iteration']['next_round_guidance'] = guidance

    # Self-contained source context for this official run dir.
    for key, source in SOURCE_JSONS.items():
        dest = RUN_DIR / source.name
        if key == 'portfolio_spec':
            dump_json(dest, portfolio_out)
        elif key == 'current_selection_snapshot':
            dump_json(dest, current_selection_out)
        elif key == 'validation_summary':
            dump_json(dest, validation_out)
        else:
            shutil.copy2(source, dest)
    for _, source in SOURCE_TEXTS.items():
        shutil.copy2(source, RUN_DIR / source.name)

    overfit_path = RUN_DIR / 'overfit_audit.json'
    dump_json(overfit_path, overfit_audit)

    report_json = {
        'schema_version': '2026-04-23.line_b_shadow_iteration.report.v1',
        'generated_at': now,
        'run_name': RUN_NAME,
        'run_dir': str(RUN_DIR),
        'status': 'ok',
        'line_name': LINE_NAME,
        'objective': manifest.get('objective'),
        'constraints': manifest.get('constraints') or {},
        'parent_references': manifest.get('parent_references') or {},
        'materialization_note': 'original shadow run only landed manifest/progress and then stalled; this run rehydrated formal artifacts from official line_b source outputs.',
        'baseline_inputs': {
            'source_output_root': str(SOURCE_ROOT),
            'portfolio_spec': str(SOURCE_JSONS['portfolio_spec']),
            'current_selection_snapshot': str(SOURCE_JSONS['current_selection_snapshot']),
            'validation_summary': str(SOURCE_JSONS['validation_summary']),
            'walkforward_report': str(SOURCE_JSONS['walkforward_report']),
            'analog_validation': str(SOURCE_JSONS['analog_validation']),
        },
        'selected_factor_ids': [str(item.get('factor_id')) for item in portfolio.get('selected_factors', []) or []],
        'selected_families': [str(item.get('family')) for item in portfolio.get('selected_factors', []) or []],
        'weights': portfolio.get('weights') or {},
        'validation_summary': {
            'walkforward_close_to_close_excess_total_return': validation_summary.get('walkforward_close_to_close_excess_total_return'),
            'walkforward_open_to_close_excess_total_return': validation_summary.get('walkforward_open_to_close_excess_total_return'),
            'analog_close_to_close_total_return': validation_summary.get('analog_close_to_close_total_return'),
            'analog_open_to_close_total_return': validation_summary.get('analog_open_to_close_total_return'),
            'overfit_risk_level': validation_summary.get('overfit_risk_level'),
        },
        'concentration': concentration,
        'shadow_gate': shadow_gate,
        'overfit_audit_summary': {
            'overall_risk_level': overfit_audit.get('overall_risk_level'),
            'summary': (overfit_audit.get('overall_verdict') or {}).get('summary'),
        },
        'next_round_guidance': guidance,
        'legacy_reference_only': ['prebreakout_v41'],
        'output_files': {
            'portfolio_spec': str(RUN_DIR / 'portfolio_spec.json'),
            'current_selection_snapshot': str(RUN_DIR / 'current_selection_snapshot.json'),
            'validation_summary': str(RUN_DIR / 'validation_summary.json'),
            'overfit_audit': str(overfit_path),
            'report_json': str(RUN_DIR / 'line_b_shadow_iteration_report.json'),
            'report_md': str(RUN_DIR / 'line_b_shadow_iteration_report.md'),
        },
    }
    dump_json(RUN_DIR / 'line_b_shadow_iteration_report.json', report_json)

    report_md = '\n'.join([
        '# 20260422 line_b shadow iteration formalization',
        '',
        f'- 生成时间: `{now}`',
        f'- Run: `{RUN_NAME}`',
        f'- Line: `{LINE_NAME}`',
        '- 说明: 原始 shadow run 只落了 manifest/progress，未生成正式产物；本次按官方 source line_b 输出补齐正式 artifacts。',
        '- legacy reference only: `prebreakout_v41`',
        '',
        '## 核心结论',
        f"- walkforward c2c excess: `{validation_summary.get('walkforward_close_to_close_excess_total_return')}`",
        f"- walkforward o2c excess: `{validation_summary.get('walkforward_open_to_close_excess_total_return')}`",
        f"- analog c2c total_return: `{validation_summary.get('analog_close_to_close_total_return')}`",
        f"- analog o2c total_return: `{validation_summary.get('analog_open_to_close_total_return')}`",
        f"- overfit 风险: `{overfit_audit.get('overall_risk_level')}`",
        f"- 最大单因子权重: `{concentration.get('max_weight')}` / 有效因子数: `{concentration.get('effective_factor_count')}` / 选中因子数: `{concentration.get('selected_factor_count')}`",
        f"- blocked_by: `{', '.join(shadow_gate.get('blocked_by') or [])}`",
        '',
        '## 当前组合',
        *[
            f"- `{item.get('factor_id')}` / family=`{item.get('family')}` / direction=`{item.get('direction_label')}` / weight=`{item.get('actual_weight')}`"
            for item in portfolio.get('selected_factors', []) or []
        ],
        '',
        '## 下一轮 shadow iterate 指引',
        *[f'- {item}' for item in guidance],
    ]) + '\n'
    (RUN_DIR / 'line_b_shadow_iteration_report.md').write_text(report_md, encoding='utf-8')

    line_payload = {
        'generated_at': now,
        'run_name': RUN_NAME,
        'line_name': LINE_NAME,
        'status': 'ok',
        'output_root': str(RUN_DIR),
        'source_output_root': str(SOURCE_ROOT),
        'validation_summary': report_json['validation_summary'],
        'selected_factor_ids': report_json['selected_factor_ids'],
        'selected_families': report_json['selected_families'],
        'weights': report_json['weights'],
        'concentration': concentration,
        'overfit_risk_level': risk_level,
        'next_round_guidance': guidance,
        'legacy_reference_only': ['prebreakout_v41'],
    }
    dump_json(RUN_DIR / f'{LINE_NAME}.json', line_payload)

    line_progress = {
        'schema_version': '2026-04-23.line_b_shadow_iteration.line_progress.v1',
        'generated_at': now,
        'run_name': RUN_NAME,
        'line_name': LINE_NAME,
        'status': 'ok',
        'stage': 'artifacts_materialized',
        'started_at': previous_progress.get('started_at') or manifest.get('started_at'),
        'materialized_at': now,
        'source_output_root': str(SOURCE_ROOT),
        'artifacts_completed': [
            'portfolio_spec.json',
            'current_selection_snapshot.json',
            'validation_summary.json',
            'overfit_audit.json',
            'line_b_shadow_iteration_report.json',
            'line_b_shadow_iteration_report.md',
        ],
        'artifacts_pending': [
            'qlib_recorder_sync.json',
            'factor_iteration_pool.json sync',
            'factor_lineage.json sync',
            'factor_promotion_decision.json sync',
        ],
        'notes': [
            'Official shadow iteration artifacts have been rehydrated from official line_b source outputs.',
            'No promotion will be executed in this run.',
            'prebreakout_v41 remains historical reference only.',
        ],
    }
    dump_json(LINE_DIR / 'progress.json', line_progress)

    top_progress = {
        'schema_version': '2026-04-23.line_b_shadow_iteration.progress.v1',
        'generated_at': now,
        'run_name': RUN_NAME,
        'run_dir': str(RUN_DIR),
        'status': 'running',
        'stage': 'control_plane_sync',
        'line_name': LINE_NAME,
        'heartbeat_seconds': 1800,
        'expected_next_update_before': (datetime.fromisoformat(now) if '+' in now or now.endswith('Z') else datetime.now(TZ)).isoformat(timespec='seconds'),
        'started_at': previous_progress.get('started_at') or manifest.get('started_at'),
        'materialized_at': now,
        'completed_steps': [
            'official_run_dir_created',
            'manifest_landed',
            'candidate_weight_reconstruction',
            'portfolio_spec',
            'current_selection_snapshot',
            'validation_summary',
            'overfit_audit',
            'shadow_iteration_report',
        ],
        'pending_steps': [
            'control_plane_sync',
            'qlib_recorder_sync',
        ],
        'candidate_search_scope': previous_progress.get('candidate_search_scope') or {},
        'lines': [line_payload],
    }
    dump_json(RUN_DIR / 'progress.json', top_progress)
    dump_json(LATEST_PATH, top_progress)

    append_log(f'[{now}] detected stalled shadow run; previous process exited before formal artifact landing.')
    append_log(f'[{now}] materialized portfolio/current_selection/validation/overfit/report artifacts from {SOURCE_ROOT}.')
    append_log(f'[{now}] pending control-plane sync + qlib recorder sync.')

    return {
        'generated_at': now,
        'run_dir': str(RUN_DIR),
        'report_path': str(RUN_DIR / 'line_b_shadow_iteration_report.json'),
        'overfit_path': str(overfit_path),
        'progress_path': str(RUN_DIR / 'progress.json'),
        'latest_path': str(LATEST_PATH),
        'blocked_by': shadow_gate['blocked_by'],
    }


if __name__ == '__main__':
    result = write_materialized_artifacts()
    print(json.dumps(result, ensure_ascii=False, indent=2))
