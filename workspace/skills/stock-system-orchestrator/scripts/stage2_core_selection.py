#!/usr/bin/env python3
"""Stage 2 wrapper: run the two retained strategies through the existing pipeline."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from orchestrator_common import WORKSPACE, HEALTH_DIR, PREFERRED_PYTHON, latest_common_trade_date, write_health_snapshot

SKILL_DIR = WORKSPACE / 'skills/stock-system-orchestrator'
DEFAULT_CONFIG = SKILL_DIR / 'references/config.example.json'
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
PIPELINE = WORKSPACE / 'skills/stock-analyzer/pipeline.py'


def load_config() -> dict:
    return json.loads(DEFAULT_CONFIG.read_text(encoding='utf-8'))


def run_strategy(arg: str) -> dict:
    cmd = [str(PREFERRED_PYTHON), str(PIPELINE), '--strategy', arg]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {
        'cmd': cmd,
        'returncode': proc.returncode,
        'stdout_tail': '\n'.join(proc.stdout.splitlines()[-30:]),
        'stderr_tail': '\n'.join(proc.stderr.splitlines()[-30:]),
    }


def main() -> int:
    _ = load_config()
    latest_trade_date, _ = latest_common_trade_date()
    baseline = run_strategy('v4.0')
    prebreakout = run_strategy('v4.1')
    ok = baseline['returncode'] == 0 and prebreakout['returncode'] == 0
    payload = {
        'stage': 'coreSelection',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'policy': {'mode': 'opt-in'},
        'target_trade_date': latest_trade_date,
        'retained_strategies': ['baseline_v4', 'prebreakout_v41'],
        'runs': {
            'baseline_v4': baseline,
            'prebreakout_v41': prebreakout,
        },
        'ok': ok,
    }
    out = HEALTH_DIR / 'core_selection_run.json'
    write_health_snapshot(out, payload, trade_date=latest_trade_date)
    print(f'[stage2] wrote run file: {out}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
