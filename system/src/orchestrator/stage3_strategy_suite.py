#!/usr/bin/env python3
"""Stage 3 wrapper: rebuild strategy_backtests.json for the current retained strategies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from python_env_bootstrap import bootstrap_openclaw_venv
from orchestrator_common import (
    ARCHIVED_STRATEGIES,
    HEALTH_DIR,
    PREFERRED_PYTHON,
    RETAINED_STRATEGIES,
    WORKSPACE,
    load_json,
    top_industries,
    write_canonical_object,
    write_health_snapshot,
)

ENV_BOOTSTRAP = bootstrap_openclaw_venv()

HEALTH_DIR.mkdir(parents=True, exist_ok=True)
RUN_SUITE = WORKSPACE / 'skills/stock-analyzer/run_strategy_suite.py'
FACTOR_SCRIPTS = WORKSPACE / 'factor_factory/scripts'
BUILD_UNIVERSE = FACTOR_SCRIPTS / 'build_universe.py'
SHORT_TRACK_SHADOW = WORKSPACE / 'skills/stock-system-orchestrator/scripts/short_track_shadow_runner.py'
# T1 已退役（复盘无边际，冻结档案）：以下两个入口不再出现在每日 steps 序列，
# 仅保留常量供将来手动重建静态档案时引用。
BUILD_GTJA_ALPHA191 = FACTOR_SCRIPTS / 'build_gtja_alpha191_panel.py'
BUILD_T1_PORTFOLIO = FACTOR_SCRIPTS / 'build_t1_portfolio.py'
STRATEGY_PUBLICATION_LAYER = WORKSPACE / 'skills/stock-system-orchestrator/scripts/strategy_publication_layer.py'
QLIB_PYTHON = WORKSPACE / 'factor_factory/qlib_lab/venv/bin/python'
OUT_JSON = WORKSPACE / 'stock_data/03-working/stock-report-repo/data/strategy_backtests.json'
RETAINED = list(RETAINED_STRATEGIES)


def summarize_strategy(strategy: dict) -> dict:
    top20 = strategy.get('top20', [])
    return {
        'id': strategy.get('id'),
        'name': strategy.get('name'),
        'top20_count': len(top20),
        'top_industries': top_industries(top20, limit=5),
    }


def run_step(name: str, python_exec: str, script: Path, env: dict[str, str]) -> dict:
    proc = subprocess.run([python_exec, str(script)], cwd=WORKSPACE, capture_output=True, text=True, env=env)
    return {
        'name': name,
        'script': str(script),
        'python_exec': python_exec,
        'returncode': proc.returncode,
        'ok': proc.returncode == 0,
        'stdout_tail': '\n'.join(proc.stdout.splitlines()[-40:]),
        'stderr_tail': '\n'.join(proc.stderr.splitlines()[-40:]),
    }


def main() -> int:
    python_exec = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    qlib_python = str(QLIB_PYTHON if QLIB_PYTHON.exists() else Path(sys.executable))
    env = dict(**os.environ)
    env['PYTHONPATH'] = f"{WORKSPACE / 'skills/stock-system-orchestrator/scripts'}:{env.get('PYTHONPATH', '')}"
    steps = [
        run_step('prebreakout_suite', python_exec, RUN_SUITE, env),
        # Factor-factory universe rebuilding is manual research only. It now
        # fails closed unless exact-date PIT snapshots exist for every date.
        # O2C/T1 and other failed lanes remain available only as historical archives.
        run_step('strategy_publication_layer', python_exec, STRATEGY_PUBLICATION_LAYER, env),
        run_step('short_track_shadow', python_exec, SHORT_TRACK_SHADOW, env),
    ]
    # 按策略关键性隔离（2026-06 诚实化加固）：只有「主线」步骤失败才阻断当日；附属策略(O2C/T1)
    # 任一步失败只记 incomplete、不毙整条管线——「一个附属数据没到位也尽可能完成当日主线推荐」。
    # 主线 = 生成主策略 prebreakout 的 run_strategy_suite + 把结果落库发布的 strategy_publication_layer。
    CRITICAL_STEPS = {'prebreakout_suite', 'strategy_publication_layer'}
    critical_failed = [s['name'] for s in steps if s['name'] in CRITICAL_STEPS and not s['ok']]
    attached_failed = [s['name'] for s in steps if s['name'] not in CRITICAL_STEPS and not s['ok']]
    proc_returncode = next((step['returncode'] for step in steps if step['returncode'] != 0), 0)
    payload = {
        'stage': 'strategySuite',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'retained_strategies': RETAINED,
        'archived_strategies': ARCHIVED_STRATEGIES,
        'expected_strategy_count': len(RETAINED),
        'python_exec': python_exec,
        'qlib_python_exec': qlib_python,
        'steps': steps,
        'returncode': proc_returncode,
        'critical_failed_steps': critical_failed,
        'attached_failed_steps': attached_failed,
        'main_line_ok': not critical_failed,
        'stdout_tail': '\n\n'.join(f"[{step['name']}]\n{step['stdout_tail']}" for step in steps[-3:]),
        'stderr_tail': '\n\n'.join(f"[{step['name']}]\n{step['stderr_tail']}" for step in steps if step['stderr_tail']),
        'output_exists': OUT_JSON.exists(),
    }
    if OUT_JSON.exists():
        try:
            data = load_json(OUT_JSON)
            ids = [x.get('id') for x in data.get('strategies', [])]
            payload['strategy_ids'] = ids
            payload['retained_only'] = sorted(ids) == sorted(RETAINED)
            payload['latest_trade_date'] = str(data.get('latest_trade_date') or '')
            payload['strategy_summaries'] = [summarize_strategy(strategy) for strategy in data.get('strategies', [])]
            # 空 top20 不再被当「步骤成功」静默放行，而是如实记 incomplete（延后到 validation 才暴雷已根治）。
            empty_strategies = [s['id'] for s in payload['strategy_summaries'] if int(s.get('top20_count') or 0) == 0]
            payload['incomplete_strategies'] = sorted(set(attached_failed) | set(empty_strategies))
            payload['empty_top20_strategies'] = empty_strategies
            if payload['latest_trade_date']:
                write_canonical_object(
                    'strategy_alignment',
                    payload['latest_trade_date'],
                    {
                        'latest_trade_date': payload['latest_trade_date'],
                        'retained_only': payload['retained_only'],
                        'retained_strategies': payload['strategy_summaries'],
                    },
                )
        except Exception as e:
            payload['parse_error'] = str(e)
    out = HEALTH_DIR / 'strategy_suite_run.json'
    write_health_snapshot(out, payload, trade_date=payload.get('latest_trade_date') or None)
    print(f'[stage3] wrote run file: {out}')
    if critical_failed:
        print(f'[stage3] 主线步骤失败，阻断当日: {critical_failed}')
        return 1
    if attached_failed or payload.get('empty_top20_strategies'):
        print(
            f'[stage3] 主线成功；附属策略落后(不阻断): failed={attached_failed} '
            f'empty_top20={payload.get("empty_top20_strategies")}'
        )
    # 主线就绪即放行（return 0），附属策略缺失由 validation 的 per-strategy 隔离如实公示。
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
