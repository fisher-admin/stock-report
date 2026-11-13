#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render Phase 1 attribution report for factor_factory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Render phase1 attribution report')
    parser.add_argument('--root', default='/Users/fisher/.openclaw/workspace/factor_factory', help='factor_factory root')
    return parser.parse_args()


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def top_reports(items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    def score(item: dict[str, Any]) -> float:
        lift = item.get('lift') or {}
        return float((lift.get('sharpe_lift', 0.0) or 0.0) * 10 + item.get('rank_ic_abs', abs(item.get('rank_ic_mean', 0.0)) or 0.0))
    return sorted(items, key=score, reverse=True)[:limit]


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()

    audit = load_json(root / 'outputs' / 'audit_reports' / 'factor_audit_phase1.json') or {}
    regime = load_json(root / 'outputs' / 'regime_maps' / 'regime_label_phase1.json') or {}
    weight_map = load_json(root / 'outputs' / 'weight_maps' / 'regime_weight_map_phase1.json') or {}
    whitelist = load_json(root / 'outputs' / 'candidate_lists' / 'factor_whitelist_phase1.json') or []
    coldlist = load_json(root / 'outputs' / 'candidate_lists' / 'factor_coldlist_phase1.json') or []

    latest_regime = regime.get('latest') or {}
    latest_weight_plan = weight_map.get('latest_weight_plan') or {}
    selected_factors = []
    for row in weight_map.get('regime_weight_map', []):
        for item in row.get('selected_factors', []):
            selected_factors.append(item)

    latest_selected = list(latest_weight_plan.get('selected_factors', []) or [])
    unique_selected = list({item['factor_id']: item for item in selected_factors if item.get('factor_id')}.values())
    report = {
        'status': 'ok',
        'phase': 'phase1_plus',
        'audit_summary': {
            'factor_count': audit.get('factor_count', 0),
            'whitelist_count': audit.get('whitelist_count', 0),
            'coldlist_count': audit.get('coldlist_count', 0),
        },
        'regime_summary': {
            'regime_count': regime.get('regime_count', 0),
            'latest': latest_regime,
            'counts': regime.get('counts', {}),
        },
        'weight_plan_summary': {
            'selection_mode': weight_map.get('selection_mode'),
            'latest_weight_plan': latest_weight_plan,
            'latest_weight_plan_selected_factor_count': len(latest_selected),
            'regime_union_selected_factor_count': len(unique_selected),
        },
        'highlights': {
            'top_whitelist': top_reports(whitelist),
            'top_coldlist_watch': top_reports(coldlist),
            'selected_factor_blueprint': latest_selected,
            'regime_union_selected_factor_blueprint': unique_selected,
        },
        'risks': [],
        'next_actions': [
            '先看最新 regime 与对应权重图，再决定是否接受 fallback 因子分配。',
            '若 whitelist 仍为空，应优先回看 audit 门槛与 split 有效性，而不是直接放宽 base 约束。',
            '若 fallback 模式持续存在，下一阶段应优先加强 regime + attribution，而不是直接上 RL。',
        ],
    }

    if audit.get('whitelist_count', 0) == 0:
        report['risks'].append('Phase 1 whitelist 为空，当前 weight map 可能退化为 fallback_coldlist。')
    if latest_regime and float(latest_regime.get('regime_confidence', 0.0) or 0.0) < 0.5:
        report['risks'].append('最新 regime 置信度偏低，实盘解释力有限。')
    if not latest_weight_plan:
        report['risks'].append('未生成 latest_weight_plan，需核对 regime labeling 或 weight search 输出。')

    out_json = root / 'outputs' / 'attribution_reports' / 'phase1_attribution_report.json'
    out_md = root / 'outputs' / 'attribution_reports' / 'phase1_attribution_report.md'
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    lines = [
        '# Phase 1 Attribution Report',
        '',
        f"- factor_count: {report['audit_summary']['factor_count']}",
        f"- whitelist_count: {report['audit_summary']['whitelist_count']}",
        f"- coldlist_count: {report['audit_summary']['coldlist_count']}",
        f"- latest_regime: {latest_regime.get('regime_id', 'NA')} (confidence={latest_regime.get('regime_confidence', 'NA')})",
        f"- selection_mode: {weight_map.get('selection_mode', 'NA')}",
        '',
        '## Risks',
    ]
    for item in report['risks'] or ['- none']:
        lines.append(f'- {item}' if not str(item).startswith('- ') else str(item))
    lines.extend(['', '## Next Actions'])
    for item in report['next_actions']:
        lines.append(f'- {item}')
    out_md.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    print(json.dumps({'status': 'ok', 'json_path': str(out_json), 'markdown_path': str(out_md), 'risk_count': len(report['risks'])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
