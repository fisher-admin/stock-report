#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path('/Users/fisher/.openclaw/workspace/factor_factory/outputs/evolution')
FILES = [
    'current_market_fingerprint.json',
    'regime_analog_windows.json',
    'candidate_factor_hypotheses.json',
    'factor_iteration_pool.json',
    'factor_validation_report.json',
    'portfolio_backtest_report.json',
    'overfit_audit_report.json',
    'factor_promotion_decision.json',
    'daily_factor_evolution_digest.md',
]


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def main() -> None:
    summary = {'workspace': str(ROOT.parent.parent), 'files': []}
    for name in FILES:
        path = ROOT / name
        item = {'name': name, 'exists': path.exists(), 'path': str(path)}
        if path.exists():
            item['mtime'] = path.stat().st_mtime
            item['size'] = path.stat().st_size
            if path.suffix == '.json':
                payload = load_json(path)
                if name == 'current_market_fingerprint.json':
                    item['trade_date'] = payload.get('trade_date')
                    item['regime'] = (payload.get('market_regime_summary') or {}).get('current_proxy_regime')
                if name == 'factor_validation_report.json':
                    item['overall_assessment'] = payload.get('overall_assessment')
                if name == 'factor_promotion_decision.json':
                    item['promotion_freeze'] = (payload.get('global_constraints') or {}).get('promotion_freeze')
        summary['files'].append(item)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
