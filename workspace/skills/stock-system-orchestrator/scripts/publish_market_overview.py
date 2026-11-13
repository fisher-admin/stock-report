#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestrator_common import PREFERRED_PYTHON, PUBLISHED_REPO, WORKSPACE
from python_env_bootstrap import bootstrap_openclaw_venv

ENV_BOOTSTRAP = bootstrap_openclaw_venv()

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = PREFERRED_PYTHON
ANALYTICS_DIR = PUBLISHED_REPO / 'data' / 'recommendation_analytics'
V2_PUBLISH_SCRIPT = SCRIPT_DIR / 'publish_stock_report_v2.sh'

SCRIPTS = [
    SCRIPT_DIR / 'generate_market_industry_heatmap.py',
    SCRIPT_DIR / 'generate_market_morning_brief.py',
    SCRIPT_DIR / 'generate_latest_states.py',
    SCRIPT_DIR / 'generate_unified_decision_payload.py',
]


def run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def ensure_ok(proc: subprocess.CompletedProcess[str], step: str) -> None:
    if proc.returncode != 0:
        detail = (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')
        raise RuntimeError(f'{step} failed\n{detail.strip()}')


def run_generators() -> None:
    for script in SCRIPTS:
        proc = run_cmd([str(PYTHON), str(script)], cwd=WORKSPACE)
        ensure_ok(proc, f'run {script.name}')
        print(proc.stdout.strip())


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def validate_generated_dates(
    morning: dict,
    heatmap: dict,
    unified: dict,
    run_manifest: dict,
) -> dict:
    """Reject a publication bundle whose derived date fields disagree."""

    morning_trade_date = str(morning.get('trade_date') or '')
    market_trade_date = str(morning.get('market_data_trade_date') or '')
    heatmap_trade_date = str(heatmap.get('latest_trade_date') or '')
    manifest_trade_date = str(run_manifest.get('trade_date') or '')
    unified_morning_trade_date = str(
        (unified.get('morning_brief') or {}).get('trade_date') or ''
    )
    dates = {
        'morning': morning_trade_date,
        'market': market_trade_date,
        'heatmap': heatmap_trade_date,
        'manifest': manifest_trade_date,
        'unified morning brief': unified_morning_trade_date,
    }
    missing = [name for name, value in dates.items() if not value]
    if missing:
        raise RuntimeError(
            'ENVIRONMENT_DRIFT: missing analytics trade dates '
            + ', '.join(missing)
        )
    market_side = {name: value for name, value in dates.items() if name != 'manifest'}
    # Only the selection manifest lagging means last evening's chain did not
    # finish; the market-side brief is still coherent and worth delivering.
    strategy_lagging = len(set(market_side.values())) == 1 and manifest_trade_date < morning_trade_date
    if len(set(dates.values())) != 1 and not strategy_lagging:
        detail = ' '.join(f'{name}={value}' for name, value in dates.items())
        raise RuntimeError(f'ENVIRONMENT_DRIFT: analytics trade dates mismatch {detail}')
    return {
        'morning_trade_date': morning_trade_date,
        'market_data_trade_date': market_trade_date,
        'heatmap_latest_trade_date': heatmap_trade_date,
        'manifest_trade_date': manifest_trade_date,
        'unified_morning_trade_date': unified_morning_trade_date,
        'strategy_lagging_trade_date': manifest_trade_date if strategy_lagging else None,
    }


def verify_outputs() -> dict:
    morning = read_json(ANALYTICS_DIR / 'market_morning_brief_latest.json')
    heatmap = read_json(ANALYTICS_DIR / 'market_industry_heatmap.json')
    unified = read_json(ANALYTICS_DIR / 'unified_decision_payload.json')
    run_manifest = read_json(PUBLISHED_REPO / 'data/latest/run_manifest.json')
    if not morning.get('trade_date'):
        raise RuntimeError('morning brief missing trade_date')
    if not heatmap.get('latest_trade_date'):
        raise RuntimeError('market heatmap missing latest_trade_date')
    if not unified.get('generated_at'):
        raise RuntimeError('unified decision payload missing generated_at')
    if not run_manifest.get('trade_date'):
        raise RuntimeError('run manifest missing trade_date')
    dates = validate_generated_dates(morning, heatmap, unified, run_manifest)
    return {
        **dates,
        'regime': morning.get('regime'),
        'risk_score': morning.get('risk_score'),
        'focus_sectors': (morning.get('focus_sectors') or [])[:3],
        'unified_generated_at': unified.get('generated_at'),
        'run_id': run_manifest.get('run_id'),
    }


def publish_via_v2() -> dict:
    if not V2_PUBLISH_SCRIPT.exists():
        raise RuntimeError(f'v2 publisher missing: {V2_PUBLISH_SCRIPT}')
    proc = run_cmd(['bash', str(V2_PUBLISH_SCRIPT)], cwd=WORKSPACE)
    if proc.returncode != 0:
        tail = '\n'.join(((proc.stdout or '') + '\n' + (proc.stderr or '')).splitlines()[-20:])
        raise RuntimeError(f'v2 publisher failed\n{tail}')
    return {
        'publisher': 'publish_stock_report_v2.sh',
        'returncode': proc.returncode,
        'stdout_tail': '\n'.join((proc.stdout or '').splitlines()[-20:]),
    }


def main() -> int:
    run_generators()
    summary = verify_outputs()
    lagging = summary.get('strategy_lagging_trade_date')
    if lagging:
        # Publishing would pair today's brief with a stale selection page; keep
        # the page coherent and let the evening publish pick the brief up.
        git_info = {'skipped': True, 'reason': f'selection still at {lagging}; page left unchanged'}
    else:
        git_info = publish_via_v2()
    payload = {
        'ok': True,
        'published': not lagging,
        'summary': summary,
        'git': git_info,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise
