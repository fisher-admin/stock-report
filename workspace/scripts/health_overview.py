#!/usr/bin/env python3
"""Build a compact health overview across stock and cron subsystems."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

WORKSPACE = Path('/Users/fisher/.openclaw/workspace')
HEALTH = WORKSPACE / 'stock_data/03-working/health'
OUT = HEALTH / 'system_health_overview.json'

FILES = {
    'env_drift_probe': HEALTH / 'env_drift_probe.json',
    'cron_delivery_audit': HEALTH / 'cron_delivery_audit.json',
    'orchestrator_run': HEALTH / 'orchestrator_run.json',
    'ai_publish_readiness': HEALTH / 'ai_publish_readiness.json',
    'validation_report': HEALTH / 'validation_report.json',
    'recommendation_db_sync': HEALTH / 'recommendation_db_sync.json',
}


def now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def load(path: Path) -> dict:
    if not path.exists():
        return {'exists': False}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            data['exists'] = True
            return data
        return {'exists': True, 'raw_type': type(data).__name__}
    except Exception as exc:
        return {'exists': True, 'error': f'{type(exc).__name__}: {exc}'}


def main() -> int:
    docs = {name: load(path) for name, path in FILES.items()}
    summary = {
        'generated_at': now_str(),
        'components': {},
        'risks': [],
        'ok': True,
    }

    env_probe = docs['env_drift_probe']
    summary['components']['env_drift_probe'] = {
        'ok': bool(env_probe.get('ok')),
        'python_executable': env_probe.get('python_executable'),
        'venv_active': env_probe.get('venv_active'),
    }
    if not env_probe.get('ok'):
        summary['risks'].append('ENVIRONMENT_DRIFT')

    cron_audit = docs['cron_delivery_audit']
    summary['components']['cron_delivery_audit'] = {
        'ok': bool(cron_audit.get('ok')),
        'problematic': ((cron_audit.get('summary') or {}).get('problematic')),
        'total': ((cron_audit.get('summary') or {}).get('total')),
    }
    if not cron_audit.get('ok'):
        summary['risks'].append('CRON_DELIVERY_RISK')

    validation = docs['validation_report']
    summary['components']['validation_report'] = {
        'ok': bool(validation.get('ok')),
        'publish_ready': validation.get('publish_ready'),
        'ai_complete': validation.get('ai_complete'),
    }
    if validation.get('exists') and not validation.get('ok'):
        summary['risks'].append('VALIDATION_FAILED')

    ai_publish = docs['ai_publish_readiness']
    summary['components']['ai_publish_readiness'] = {
        'base_ok': ai_publish.get('base_ok'),
        'published': ai_publish.get('published'),
        'ai_complete': ai_publish.get('ai_complete'),
        'publish_mode': ai_publish.get('publish_mode'),
    }
    if ai_publish.get('exists') and not ai_publish.get('published'):
        summary['risks'].append('PUBLISH_NOT_READY')

    recommendation_db = docs['recommendation_db_sync']
    summary['components']['recommendation_db_sync'] = {
        'ok': recommendation_db.get('ok'),
        'latest_recommend_date': recommendation_db.get('latest_recommend_date'),
        'latest_date_row_count': recommendation_db.get('latest_date_row_count'),
    }
    if recommendation_db.get('exists') and not recommendation_db.get('ok'):
        summary['risks'].append('RECOMMENDATION_DB_FAILED')

    orchestrator = docs['orchestrator_run']
    summary['components']['orchestrator_run'] = {
        'ok': orchestrator.get('ok'),
        'run_id': orchestrator.get('run_id'),
        'trade_date': orchestrator.get('trade_date'),
        'warnings': orchestrator.get('warnings'),
    }
    if orchestrator.get('warnings'):
        summary['risks'].append('ORCHESTRATOR_WARNINGS')

    summary['risks'] = list(dict.fromkeys(summary['risks']))
    summary['ok'] = len(summary['risks']) == 0
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(OUT))
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
