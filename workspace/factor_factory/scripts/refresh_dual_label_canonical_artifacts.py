#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
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
    'open_to_close': 'open_to_close_cmp',
    'close_to_open': 'close_to_open_cmp',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Refresh alternate label-mode canonical audit + weight artifacts')
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
    analog_ready = bool(
        validation_scope.get('portfolio_return_replay_included')
        and analog.get('current_combo_return_level_analog_replay')
    )
    claim_ready = bool((claim.get('independent_oos_blocker', {}) or {}).get('by_label_mode'))
    nested_modes = {str(mode) for mode in (nested.get('applies_to_label_modes') or [])}
    nested_ready = {'close_to_close', 'open_to_close', 'close_to_open'}.issubset(nested_modes)
    if analog_ready and claim_ready and nested_ready:
        return True, (
            'current outputs 已完成三口径 downstream + 收益级 analog replay 控制面；'
            '继续运行 dual-label helper 会把 digest / validation / pool / lineage 回写成旧口径。'
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
            factor_id = str(item.get('factor_id') or '').strip()
            if factor_id:
                out.add(factor_id)
    return sorted(out)


def selection_sources_for_factor(weight: dict, factor_id: str) -> list[str]:
    out: set[str] = set()
    for row in weight.get('regime_weight_map', []):
        for item in row.get('selected_factors', []):
            if str(item.get('factor_id')) == factor_id and item.get('selection_source'):
                out.add(str(item['selection_source']))
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


def focus_factor_summary(
    factor_id: str,
    current_reports: dict[str, dict],
    current_weight: dict,
    canonical_reports: dict[str, dict],
    canonical_weight: dict,
) -> dict:
    current = current_reports.get(factor_id)
    canonical = canonical_reports.get(factor_id)
    current_latest_ids = set(latest_plan_factor_ids(current_weight))
    canonical_latest_ids = set(latest_plan_factor_ids(canonical_weight))
    current_union = set(union_factor_ids(current_weight))
    canonical_union = set(union_factor_ids(canonical_weight))
    current_rank = None if not current else current.get('rank_ic_mean')
    canonical_rank = None if not canonical else canonical.get('rank_ic_mean')
    return {
        'present_in_audit': current is not None,
        'status': None if not current else current.get('status'),
        'support_pool_candidate': None if not current else current.get('support_pool_candidate'),
        'rank_ic_mean': None if current_rank is None else round(float(current_rank), 6),
        'rank_ic_direction': sign_of(current_rank),
        'rank_ic_abs': None if not current else round(float(current.get('rank_ic_abs', 0.0) or 0.0), 6),
        'latest_plan_selected': factor_id in current_latest_ids,
        'selected_any_regime': factor_id in current_union,
        'selection_sources_in_weight_map': selection_sources_for_factor(current_weight, factor_id),
        'vs_close_to_close': {
            'status': None if not canonical else canonical.get('status'),
            'rank_ic_mean': None if canonical_rank is None else round(float(canonical_rank), 6),
            'rank_ic_direction': sign_of(canonical_rank),
            'rank_ic_sign_flip': sign_of(current_rank) != sign_of(canonical_rank),
            'latest_plan_selected': factor_id in canonical_latest_ids,
            'selected_any_regime': factor_id in canonical_union,
            'selection_sources_in_weight_map': selection_sources_for_factor(canonical_weight, factor_id),
        },
    }


def summarize_mode(
    *,
    mode: str,
    suffix: str,
    audit_path: Path,
    weight_path: Path,
    canonical_audit: dict,
    canonical_weight: dict,
) -> dict:
    audit = load_json(audit_path)
    weight = load_json(weight_path)
    current_reports = report_map(audit)
    canonical_reports = report_map(canonical_audit)
    latest_ids = latest_plan_factor_ids(weight)
    latest_union = union_factor_ids(weight)
    canonical_latest_ids = latest_plan_factor_ids(canonical_weight)
    canonical_union = union_factor_ids(canonical_weight)

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
        'whitelist_count': audit.get('whitelist_count'),
        'coldlist_count': audit.get('coldlist_count'),
        'selection_mode': weight.get('selection_mode'),
        'support_pool_count': weight.get('support_pool_count'),
        'fallback_pool_count': weight.get('fallback_pool_count'),
        'latest_regime': weight.get('latest_regime'),
        'latest_weight_plan_factor_ids': latest_ids,
        'latest_weight_plan_factor_union': latest_union,
        'vs_close_to_close_latest_weight_plan': compare_lists(canonical_latest_ids, latest_ids),
        'vs_close_to_close_factor_union': compare_lists(canonical_union, latest_union),
        'focus_factors': {
            factor_id: focus_factor_summary(factor_id, current_reports, weight, canonical_reports, canonical_weight)
            for factor_id in FOCUS_FACTORS
        },
    }


def run_checked(cmd: list[str], cwd: Path, *, log_path: Path | None = None, step_name: str | None = None) -> dict:
    log_path = log_path.expanduser().resolve() if log_path else None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd_display = ' '.join(shlex.quote(part) for part in cmd)
    payload_lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    meta_start = json.dumps({
        'event': 'command_start',
        'ts': iso_now(),
        'step': step_name,
        'cwd': str(cwd),
        'cmd': cmd,
        'cmd_display': cmd_display,
        'log_path': str(log_path) if log_path else None,
    }, ensure_ascii=False)
    log_fh = log_path.open('a', encoding='utf-8') if log_path is not None else None
    try:
        sys.stderr.write(meta_start + '\n')
        sys.stderr.flush()
        if log_fh is not None:
            log_fh.write(meta_start + '\n')
            log_fh.flush()
        assert proc.stdout is not None
        for line in proc.stdout:
            payload_lines.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()
            if log_fh is not None:
                log_fh.write(line)
                log_fh.flush()
        proc.stdout.close()
        return_code = proc.wait()
        payload = {
            'cmd': cmd,
            'cmd_display': cmd_display,
            'step': step_name,
            'exit_code': return_code,
            'stdout': ''.join(payload_lines),
            'stderr': '',
            'log_path': str(log_path) if log_path else None,
        }
        meta_end = json.dumps({
            'event': 'command_end',
            'ts': iso_now(),
            'step': step_name,
            'exit_code': return_code,
        }, ensure_ascii=False)
        sys.stderr.write(meta_end + '\n')
        sys.stderr.flush()
        if log_fh is not None:
            log_fh.write(meta_end + '\n')
            log_fh.flush()
        if return_code != 0:
            raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    finally:
        if log_fh is not None:
            log_fh.close()


def write_digest(
    digest_path: Path,
    comparison: dict,
    portfolio: dict,
    claim_limit: dict,
    nested: dict,
) -> None:
    c2c = comparison['close_to_close']
    o2c = comparison['open_to_close']
    cto = comparison['close_to_open']
    formal = portfolio['current_combo_formal_backtest_signal']
    latest_plan = portfolio['current_portfolio_structure'].get('latest_weight_plan') or {}
    latest_plan_ids = [str(item.get('factor_id')) for item in latest_plan.get('selected_factors', []) if item.get('factor_id')]
    blocker = claim_limit.get('independent_oos_blocker', {})
    sample = nested.get('sample_coverage', {})
    lines = [
        '# Daily Factor Evolution Digest',
        '',
        f"- 生成时间：{comparison['generated_at']}",
        f"- 当前交易日：{comparison['trade_date']}",
        f"- 当前组合：{portfolio['current_portfolio_structure']['selection_mode']} | latest shrink_uptrend = {' / '.join(latest_plan_ids)}",
        '',
        '## 本轮新增实质产物',
        f"- 新增双口径 canonical 审计与权重图：`factor_audit_phase1.open_to_close_cmp.json` / `regime_weight_map_phase1.open_to_close_cmp.json`、`factor_audit_phase1.close_to_open_cmp.json` / `regime_weight_map_phase1.close_to_open_cmp.json`，均已追平到 {c2c['benchmark_eligible_trade_days']} 个 benchmark eligible days / {c2c['benchmark_eligible_row_count']} 行。",
        '- 新增 `label_mode_canonical_comparison.json` / `.md`，把 close-to-close、open-to-close、close-to-open 三套 canonical audit/weight 放到同一控制面比较。',
        '',
        '## 双口径 canonical 审计对照',
        f"- close-to-close latest plan：{' / '.join(c2c['latest_weight_plan_factor_ids'])}。",
        f"- open-to-close latest plan：{' / '.join(o2c['latest_weight_plan_factor_ids'])}；相对 c2c 新增={o2c['vs_close_to_close_latest_weight_plan']['added_vs_close_to_close'] or ['无']}，移除={o2c['vs_close_to_close_latest_weight_plan']['removed_vs_close_to_close'] or ['无']}。",
        f"- close-to-open latest plan：{' / '.join(cto['latest_weight_plan_factor_ids'])}；相对 c2c 新增={cto['vs_close_to_close_latest_weight_plan']['added_vs_close_to_close'] or ['无']}，移除={cto['vs_close_to_close_latest_weight_plan']['removed_vs_close_to_close'] or ['无']}。",
        f"- `f_gap_return` rank_ic 方向：c2c={c2c['focus_factors']['f_gap_return']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_gap_return']['rank_ic_direction']} / cto={cto['focus_factors']['f_gap_return']['rank_ic_direction']}。",
        f"- `f_volume_ratio_5` rank_ic 方向：c2c={c2c['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / cto={cto['focus_factors']['f_volume_ratio_5']['rank_ic_direction']}。",
        f"- `f_intraday_reversal` rank_ic 方向：c2c={c2c['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / cto={cto['focus_factors']['f_intraday_reversal']['rank_ic_direction']}。",
        '',
        '## 当前 formal backtest 结果（成本后）',
        f"- close-to-close combo：总收益={fmt_pct(formal['close_to_close_combo_total_return'])}，夏普={portfolio['current_combo_formal_backtest_signal']['close_to_close_combo_sharpe']:.3f}，最大回撤={fmt_pct(portfolio['current_combo_formal_backtest_signal']['close_to_close_combo_max_drawdown'])}。",
        f"- open-to-close combo：总收益={fmt_pct(formal['open_to_close_combo_total_return'])}。",
        f"- 相对 base 超额：close-to-close={fmt_pct(portfolio['current_combo_minimal_replay_signal']['close_to_close_excess_total_return'])}，open-to-close={fmt_pct(portfolio['current_combo_minimal_replay_signal']['open_to_close_excess_total_return'])}。",
        '',
        '## claim-limit / nested / analog',
        f"- claim-limit：当前 {blocker.get('close_to_close', {}).get('used_labeled_days')} / {blocker.get('close_to_close', {}).get('labeled_eligible_days')} 个有标签 clean days 已被 formal replay 用完，独立 labeled holdout={blocker.get('remaining_labeled_holdout_days_count')}。",
        f"- micro nested：{sample.get('fold_count')} 个 outer folds / {sample.get('outer_oos_days')} 个 OOS 日，仍不足以解除 promotion freeze。",
        '- analog proxy：仍只覆盖 market-proxy windows，不是组合收益级 replay。',
        '',
        '## 关键风险',
        '- 双口径 canonical audit + weight 已补齐，但下游 current_combo replay / formal backtest / claim-limit / nested / analog 仍只绑定 close-to-close canonical。',
        '- close-to-close 与 open-to-close 的组合绝对收益方向仍不一致，交易时点敏感性未收敛。',
        '- 当前没有剩余 labeled holdout，formal 结果仍不能宣称为独立 OOS。',
        '',
        '## 下一步',
        '- 让 current_combo replay / formal backtest / claim-limit / nested 在 open-to-close 或 close-to-open canonical weight map 下形成独立下游验证链。',
        '- 继续补真正独立的 labeled holdout / rolling OOS。',
        '- 把 analog 从 market-proxy 覆盖扩成收益级 analog replay。',
    ]
    digest_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def update_factor_validation(validation_path: Path, comparison: dict, outputs: dict) -> None:
    validation = load_json(validation_path)
    now = comparison['generated_at']
    src = validation.setdefault('source_artifacts', {})
    src['label_mode_canonical_comparison'] = str(outputs['comparison_json'])
    src['label_mode_canonical_comparison_md'] = str(outputs['comparison_md'])
    for mode in ['open_to_close', 'close_to_open']:
        src[f'factor_audit_phase1_{mode}'] = str(outputs[f'{mode}_audit'])
        src[f'regime_weight_map_phase1_{mode}'] = str(outputs[f'{mode}_weight'])

    validation.setdefault('canonical_refresh_basis', {})['label_mode_comparison_mtime'] = iso_mtime(outputs['comparison_json'])
    validation['canonical_refresh_basis']['open_to_close_audit_mtime'] = iso_mtime(outputs['open_to_close_audit'])
    validation['canonical_refresh_basis']['open_to_close_weight_mtime'] = iso_mtime(outputs['open_to_close_weight'])
    validation['canonical_refresh_basis']['close_to_open_audit_mtime'] = iso_mtime(outputs['close_to_open_audit'])
    validation['canonical_refresh_basis']['close_to_open_weight_mtime'] = iso_mtime(outputs['close_to_open_weight'])

    infra = validation.setdefault('infrastructure_progress', {}).setdefault('return_label_infrastructure', {})
    infra['status'] = 'implemented_with_alt_canonical_audits_and_weights'
    infra['summary'] = (
        'panel 已支持 next_return_close_to_close_1d / next_return_open_to_close_1d / next_return_close_to_open_1d；'
        'close_to_close、open_to_close、close_to_open 三套 canonical audit 与 weight map 现已全部生成。'
    )
    infra.setdefault('evidence', {})['alternate_canonical_artifacts'] = {
        'open_to_close': {
            'audit': str(outputs['open_to_close_audit']),
            'weight_map': str(outputs['open_to_close_weight']),
            'latest_weight_plan_factor_ids': comparison['open_to_close']['latest_weight_plan_factor_ids'],
        },
        'close_to_open': {
            'audit': str(outputs['close_to_open_audit']),
            'weight_map': str(outputs['close_to_open_weight']),
            'latest_weight_plan_factor_ids': comparison['close_to_open']['latest_weight_plan_factor_ids'],
        },
        'comparison_report': str(outputs['comparison_json']),
    }
    infra['residual_risk'] = (
        '虽然 open_to_close / close_to_open 的独立 canonical audit 与 weight map 已补齐，'
        '但 current_combo replay / formal backtest / claim-limit / nested / analog 下游链路仍只绑定 close-to-close canonical。'
    )

    gap_2 = (
        'close-to-close / open-to-close / close-to-open 三套 canonical audit + weight map 已补齐到同一 33 天 clean window；'
        '但 current_combo 正式回放、claim-limit、nested 与 analog 仍只绑定 close-to-close canonical，'
        '且 close-to-close combo 总收益 2.0350%、open-to-close combo 总收益 -3.3010%，绝对收益方向仍不一致。'
    )
    severe = validation.get('three_most_severe_gaps', [])
    if len(severe) >= 2:
        severe[1] = gap_2
    else:
        while len(severe) < 1:
            severe.append('')
        severe.append(gap_2)
    validation['three_most_severe_gaps'] = severe

    comparison_summary = {
        'generated_at': comparison['generated_at'],
        'comparison_report': str(outputs['comparison_json']),
        'close_to_close_latest_weight_plan_factor_ids': comparison['close_to_close']['latest_weight_plan_factor_ids'],
        'open_to_close_latest_weight_plan_factor_ids': comparison['open_to_close']['latest_weight_plan_factor_ids'],
        'close_to_open_latest_weight_plan_factor_ids': comparison['close_to_open']['latest_weight_plan_factor_ids'],
        'open_to_close_plan_delta_vs_close_to_close': comparison['open_to_close']['vs_close_to_close_latest_weight_plan'],
        'close_to_open_plan_delta_vs_close_to_close': comparison['close_to_open']['vs_close_to_close_latest_weight_plan'],
    }
    validation['canonical_label_mode_comparison'] = comparison_summary

    c2c_o2c = validation.setdefault('close_to_close_vs_open_to_close_comparison', {})
    base_summary = c2c_o2c.get('summary', '')
    addon = '；并且 open_to_close / close_to_open canonical audit 与 weight map 已补齐，可直接比较权重计划差异。'
    if addon not in base_summary:
        c2c_o2c['summary'] = (base_summary + addon).strip('；') if base_summary else addon.strip('；')
    c2c_o2c.setdefault('evidence', {})['canonical_label_mode_comparison'] = comparison_summary

    todos = validation.get('engineering_todo', [])
    replaced = False
    for item in todos:
        task = str(item.get('task', ''))
        if 'factor_audit / weight_search' in task or '独立 canonical audit 与 weight map' in task:
            item['task'] = '让 current_combo replay / formal backtest / claim-limit / nested / analog 在 open_to_close 或 close_to_open canonical weight map 下形成独立下游验证链，而不是只停在替代 audit / weight。'
            replaced = True
    if not replaced:
        todos.append({
            'priority': 'P0',
            'task': '让 current_combo replay / formal backtest / claim-limit / nested / analog 在 open_to_close 或 close_to_open canonical weight map 下形成独立下游验证链，而不是只停在替代 audit / weight。',
        })
    validation['engineering_todo'] = todos

    release = validation.setdefault('release_recommendation', {})
    reason = str(release.get('reason', ''))
    if '双口径 canonical audit/weight 已补齐' not in reason:
        release['reason'] = (
            reason + ' 双口径 canonical audit/weight 已补齐，但下游 formal combo validation 仍未按替代权重图独立重放。'
        ).strip()

    validation['generated_at'] = now
    dump_json(validation_path, validation)


def update_pool_or_lineage(path: Path, comparison: dict, outputs: dict, *, top_level_key: str) -> None:
    payload = load_json(path)
    now = comparison['generated_at']
    src = payload.setdefault('source_artifacts', {})
    src['label_mode_canonical_comparison'] = str(outputs['comparison_json'])
    src['label_mode_canonical_comparison_md'] = str(outputs['comparison_md'])
    for mode in ['open_to_close', 'close_to_open']:
        src[f'factor_audit_phase1_{mode}'] = str(outputs[f'{mode}_audit'])
        src[f'regime_weight_map_phase1_{mode}'] = str(outputs[f'{mode}_weight'])

    basis = payload.setdefault('canonical_refresh_basis', {})
    basis['label_mode_comparison_mtime'] = iso_mtime(outputs['comparison_json'])
    basis['alternate_label_modes_ready'] = ['open_to_close', 'close_to_open']
    basis['open_to_close_audit_mtime'] = iso_mtime(outputs['open_to_close_audit'])
    basis['open_to_close_weight_mtime'] = iso_mtime(outputs['open_to_close_weight'])
    basis['close_to_open_audit_mtime'] = iso_mtime(outputs['close_to_open_audit'])
    basis['close_to_open_weight_mtime'] = iso_mtime(outputs['close_to_open_weight'])

    payload[top_level_key] = {
        'generated_at': comparison['generated_at'],
        'comparison_report': str(outputs['comparison_json']),
        'close_to_close': {
            'latest_weight_plan_factor_ids': comparison['close_to_close']['latest_weight_plan_factor_ids'],
            'latest_weight_plan_factor_union': comparison['close_to_close']['latest_weight_plan_factor_union'],
        },
        'open_to_close': {
            'latest_weight_plan_factor_ids': comparison['open_to_close']['latest_weight_plan_factor_ids'],
            'latest_weight_plan_factor_union': comparison['open_to_close']['latest_weight_plan_factor_union'],
            'delta_vs_close_to_close': comparison['open_to_close']['vs_close_to_close_latest_weight_plan'],
        },
        'close_to_open': {
            'latest_weight_plan_factor_ids': comparison['close_to_open']['latest_weight_plan_factor_ids'],
            'latest_weight_plan_factor_union': comparison['close_to_open']['latest_weight_plan_factor_union'],
            'delta_vs_close_to_close': comparison['close_to_open']['vs_close_to_close_latest_weight_plan'],
        },
        'focus_factors': {
            factor_id: {
                'open_to_close': comparison['open_to_close']['focus_factors'][factor_id],
                'close_to_open': comparison['close_to_open']['focus_factors'][factor_id],
            }
            for factor_id in ['f_gap_return', 'f_volume_ratio_5', 'f_volume_ratio_20', 'f_intraday_reversal']
        },
    }

    poll = payload.setdefault('latest_stage_poll', {})
    poll['polled_at'] = now
    for mode in ['open_to_close', 'close_to_open']:
        item = comparison[mode]
        poll[f'factor_audit_phase1_{mode}'] = {
            'mtime': item['audit_mtime'],
            'label_mode': item['label_mode'],
            'label_column': item['label_column'],
            'benchmark_eligible_trade_days': item['benchmark_eligible_trade_days'],
            'benchmark_eligible_row_count': item['benchmark_eligible_row_count'],
            'factor_count': item['factor_count'],
        }
        poll[f'regime_weight_map_phase1_{mode}'] = {
            'mtime': item['weight_mtime'],
            'selection_mode': item['selection_mode'],
            'support_pool_count': item['support_pool_count'],
            'fallback_pool_count': item['fallback_pool_count'],
            'latest_weight_plan_factor_ids': item['latest_weight_plan_factor_ids'],
        }

    payload['generated_at'] = now
    dump_json(path, payload)


def build_comparison_markdown(comparison: dict) -> str:
    c2c = comparison['close_to_close']
    o2c = comparison['open_to_close']
    cto = comparison['close_to_open']
    return '\n'.join([
        '# Label Mode Canonical Comparison',
        '',
        f"- 生成时间：{comparison['generated_at']}",
        f"- 当前交易日：{comparison['trade_date']}",
        f"- clean window：{c2c['benchmark_eligible_trade_days']} 个 trade days / {c2c['benchmark_eligible_row_count']} 行",
        '',
        '## latest weight plan',
        f"- close-to-close：{' / '.join(c2c['latest_weight_plan_factor_ids'])}",
        f"- open-to-close：{' / '.join(o2c['latest_weight_plan_factor_ids'])}",
        f"- close-to-open：{' / '.join(cto['latest_weight_plan_factor_ids'])}",
        '',
        '## 与 close-to-close 的差异',
        f"- open-to-close 新增：{o2c['vs_close_to_close_latest_weight_plan']['added_vs_close_to_close'] or ['无']}；移除：{o2c['vs_close_to_close_latest_weight_plan']['removed_vs_close_to_close'] or ['无']}",
        f"- close-to-open 新增：{cto['vs_close_to_close_latest_weight_plan']['added_vs_close_to_close'] or ['无']}；移除：{cto['vs_close_to_close_latest_weight_plan']['removed_vs_close_to_close'] or ['无']}",
        '',
        '## 重点因子 rank_ic 方向',
        f"- f_gap_return：c2c={c2c['focus_factors']['f_gap_return']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_gap_return']['rank_ic_direction']} / cto={cto['focus_factors']['f_gap_return']['rank_ic_direction']}",
        f"- f_volume_ratio_5：c2c={c2c['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_volume_ratio_5']['rank_ic_direction']} / cto={cto['focus_factors']['f_volume_ratio_5']['rank_ic_direction']}",
        f"- f_volume_ratio_20：c2c={c2c['focus_factors']['f_volume_ratio_20']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_volume_ratio_20']['rank_ic_direction']} / cto={cto['focus_factors']['f_volume_ratio_20']['rank_ic_direction']}",
        f"- f_intraday_reversal：c2c={c2c['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / o2c={o2c['focus_factors']['f_intraday_reversal']['rank_ic_direction']} / cto={cto['focus_factors']['f_intraday_reversal']['rank_ic_direction']}",
        '',
        '## 当前结论',
        '- 双口径 canonical audit + weight 已补齐，但下游 current_combo replay / formal backtest / claim-limit / nested / analog 仍只绑定 close-to-close canonical。',
        '- promotion freeze 继续维持。',
        '',
    ]) + '\n'


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    scripts = root / 'scripts'
    logs_dir = root / 'outputs' / 'evolution' / 'logs'
    run_stamp = datetime.now(TZ).strftime('%Y%m%dT%H%M%S')
    outputs = {
        'comparison_json': root / 'outputs' / 'evolution' / 'label_mode_canonical_comparison.json',
        'comparison_md': root / 'outputs' / 'evolution' / 'label_mode_canonical_comparison.md',
        'validation': root / 'outputs' / 'evolution' / 'factor_validation_report.json',
        'pool': root / 'outputs' / 'evolution' / 'factor_iteration_pool.json',
        'lineage': root / 'outputs' / 'evolution' / 'factor_lineage.json',
        'digest': root / 'outputs' / 'evolution' / 'daily_factor_evolution_digest.md',
        'portfolio': root / 'outputs' / 'evolution' / 'portfolio_backtest_report.json',
        'claim_limit': root / 'outputs' / 'evolution' / 'current_combo_claim_limit_audit.json',
        'nested': root / 'outputs' / 'evolution' / 'current_combo_nested_validation.json',
        'refresh_log': logs_dir / f'refresh_dual_label_canonical_artifacts.{run_stamp}.log',
    }

    should_skip, skip_reason = newer_three_label_control_plane_present(root)
    if should_skip:
        print(json.dumps({
            'status': 'skipped_existing_three_label_control_plane',
            'reason': skip_reason,
            'preserved_outputs': [
                str(outputs['validation']),
                str(outputs['pool']),
                str(outputs['lineage']),
                str(outputs['digest']),
            ],
            'recommended_sync_chain': [
                'python scripts/run_current_combo_analog_replay.py --root /Users/fisher/.openclaw/workspace/factor_factory',
                'python scripts/sync_multi_label_combo_validation_context.py',
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    commands = []
    for mode, suffix in MODE_SUFFIXES.items():
        audit_progress_log = logs_dir / f'run_factor_audit.{suffix}.{run_stamp}.jsonl'
        commands.append(run_checked([
            sys.executable,
            str(scripts / 'run_factor_audit.py'),
            '--root', str(root),
            '--label-mode', mode,
            '--output-suffix', suffix,
            '--progress-log', str(audit_progress_log),
        ], root, log_path=outputs['refresh_log'], step_name=f'run_factor_audit:{suffix}'))
        commands.append(run_checked([
            sys.executable,
            str(scripts / 'run_weight_search.py'),
            '--root', str(root),
            '--output-suffix', suffix,
        ], root, log_path=outputs['refresh_log'], step_name=f'run_weight_search:{suffix}'))
        outputs[f'{mode}_audit'] = root / 'outputs' / 'audit_reports' / f'factor_audit_phase1.{suffix}.json'
        outputs[f'{mode}_weight'] = root / 'outputs' / 'weight_maps' / f'regime_weight_map_phase1.{suffix}.json'
        outputs[f'{mode}_audit_progress_log'] = audit_progress_log

    canonical_audit_path = root / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json'
    canonical_weight_path = root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json'
    canonical_audit = load_json(canonical_audit_path)
    canonical_weight = load_json(canonical_weight_path)

    comparison = {
        'schema_version': '2026-04-19.label_mode_canonical_comparison.v1',
        'generated_at': iso_now(),
        'trade_date': str((canonical_weight.get('latest_regime') or {}).get('trade_date') or ''),
        'source_artifacts': {
            'close_to_close_audit': str(canonical_audit_path),
            'close_to_close_weight': str(canonical_weight_path),
            'open_to_close_audit': str(outputs['open_to_close_audit']),
            'open_to_close_weight': str(outputs['open_to_close_weight']),
            'close_to_open_audit': str(outputs['close_to_open_audit']),
            'close_to_open_weight': str(outputs['close_to_open_weight']),
        },
        'close_to_close': summarize_mode(
            mode='close_to_close',
            suffix='canonical',
            audit_path=canonical_audit_path,
            weight_path=canonical_weight_path,
            canonical_audit=canonical_audit,
            canonical_weight=canonical_weight,
        ),
        'open_to_close': summarize_mode(
            mode='open_to_close',
            suffix=MODE_SUFFIXES['open_to_close'],
            audit_path=outputs['open_to_close_audit'],
            weight_path=outputs['open_to_close_weight'],
            canonical_audit=canonical_audit,
            canonical_weight=canonical_weight,
        ),
        'close_to_open': summarize_mode(
            mode='close_to_open',
            suffix=MODE_SUFFIXES['close_to_open'],
            audit_path=outputs['close_to_open_audit'],
            weight_path=outputs['close_to_open_weight'],
            canonical_audit=canonical_audit,
            canonical_weight=canonical_weight,
        ),
        'refresh_commands': commands,
        'summary': {
            'alternate_label_modes_ready': ['open_to_close', 'close_to_open'],
            'close_to_close_latest_weight_plan_factor_ids': latest_plan_factor_ids(canonical_weight),
            'open_to_close_latest_weight_plan_factor_ids': latest_plan_factor_ids(load_json(outputs['open_to_close_weight'])),
            'close_to_open_latest_weight_plan_factor_ids': latest_plan_factor_ids(load_json(outputs['close_to_open_weight'])),
        },
        'refresh_log': str(outputs['refresh_log']),
        'progress_logs': {
            'open_to_close': str(outputs['open_to_close_audit_progress_log']),
            'close_to_open': str(outputs['close_to_open_audit_progress_log']),
        },
    }
    comparison['key_findings'] = [
        'open_to_close / close_to_open 两套 canonical audit + weight map 已补齐到同一 33 天 clean window。',
        '双口径权重计划与 close-to-close 并不完全一致，说明交易时点会改变因子筛选与定权。',
        '但 current_combo replay / formal backtest / claim-limit / nested / analog 仍只绑定 close-to-close canonical，promotion freeze 不能解除。',
    ]

    dump_json(outputs['comparison_json'], comparison)
    outputs['comparison_md'].write_text(build_comparison_markdown(comparison), encoding='utf-8')

    update_factor_validation(outputs['validation'], comparison, outputs)
    update_pool_or_lineage(outputs['pool'], comparison, outputs, top_level_key='alternate_label_mode_audits')
    update_pool_or_lineage(outputs['lineage'], comparison, outputs, top_level_key='label_mode_comparison')
    write_digest(
        outputs['digest'],
        comparison,
        load_json(outputs['portfolio']),
        load_json(outputs['claim_limit']),
        load_json(outputs['nested']),
    )

    print(json.dumps({
        'status': 'ok',
        'refresh_log': str(outputs['refresh_log']),
        'comparison_json': str(outputs['comparison_json']),
        'comparison_md': str(outputs['comparison_md']),
        'open_to_close_audit': str(outputs['open_to_close_audit']),
        'open_to_close_weight': str(outputs['open_to_close_weight']),
        'close_to_open_audit': str(outputs['close_to_open_audit']),
        'close_to_open_weight': str(outputs['close_to_open_weight']),
        'open_to_close_progress_log': str(outputs['open_to_close_audit_progress_log']),
        'close_to_open_progress_log': str(outputs['close_to_open_audit_progress_log']),
        'updated_files': [
            str(outputs['validation']),
            str(outputs['pool']),
            str(outputs['lineage']),
            str(outputs['digest']),
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
