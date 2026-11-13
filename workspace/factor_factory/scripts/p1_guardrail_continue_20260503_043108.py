#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
ROOT=Path('/Users/fisher/.openclaw/workspace/factor_factory')
OUT=ROOT/'outputs/evolution'
LOG=OUT/'logs/p1_greenfield_guardrail_audit_20260503_043108.log'
PROGRESS_JSON=OUT/'p1_auto_intervention_progress_20260503_043108.json'
PROGRESS_MD=OUT/'p1_auto_intervention_progress_20260503_043108.md'
BLOCKER_IN=OUT/'p1_long_only_infeasible_blocker.transformed_20260503_040049.json'
BLOCKER_OUT=OUT/'p1_long_only_infeasible_blocker.transformed_20260503_043108.json'
BACKTEST_LOG=OUT/'logs/p1_greenfield_backtest_20260503_043108.log'

def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')

def load(p):
    return json.loads(p.read_text()) if p.exists() else None

def dump(p,obj):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')

def write_progress(stage, commands, guardrail=None, blocker=None):
    payload={
      'schema_version':'2026-05-03.p1_auto_intervention_progress.v1',
      'generated_at':now(), 'stage':stage,
      'read_blocker_path':str(BLOCKER_IN),
      'read_blocker_decision': (load(BLOCKER_IN) or {}).get('decision'),
      'backup_dir':str(OUT/'backup_20260503_043108'),
      'modified_files':[
        str(ROOT/'scripts/greenfield_multifactor_lib.py'),
        str(ROOT/'scripts/run_greenfield_multifactor_backtest.py'),
        str(ROOT/'config/greenfield_multifactor_v1.yaml')],
      'change_summary':[
        '新增 g_long_c2c_calm_rebound / g_long_c2c_intraday_followthrough / g_long_c2c_defensive_momentum 三个历史可回测 long-side C2C companion factors。',
        'selector holdout confirmation 增加 equal-weight baseline excess 记录与 min_c2c_excess_total_return_vs_baseline 约束。',
        'train_score 对 holdout c2c excess 加正向 objective boost；config 切到 v7 scenario 并同步 factor_registry / shrink_uptrend budgets。',
        '已重建 factor pool 与 market_state_hypotheses，使派生 family_budgets 跟随 config。'],
      'commands':commands,
      'artifacts':{
        'backtest_log':str(BACKTEST_LOG),'guardrail_audit_log':str(LOG),
        'factor_pool_snapshot':str(ROOT/'outputs/greenfield_multifactor/factor_pool_snapshot.json'),
        'market_state_hypotheses':str(ROOT/'outputs/greenfield_multifactor/market_state_hypotheses.json'),
        'walkforward_report':str(ROOT/'outputs/greenfield_multifactor/walkforward_report.json'),
        'analog_validation':str(ROOT/'outputs/greenfield_multifactor/analog_validation.json'),
        'portfolio_spec':str(ROOT/'outputs/greenfield_multifactor/portfolio_spec.json'),
        'guardrail_audit':str(OUT/'p1_greenfield_guardrail_audit.json'),
        'transformed_blocker':str(BLOCKER_OUT)},
      'latest_guardrail_status':guardrail,
      'remaining_blocker':blocker,
      'promotion_freeze_lifted': False,
    }
    dump(PROGRESS_JSON,payload)
    md=['# P1 auto intervention progress 20260503_043108','',f'- generated_at: {payload["generated_at"]}',f'- stage: {stage}',f'- blocker: {payload["read_blocker_decision"]}','','## 修改','']
    md += [f'- {x}' for x in payload['change_summary']]
    md += ['','## Commands','']+[f'- `{c.get("cmd")}` -> exit={c.get("exit_code")}, log={c.get("log") or c.get("output_tail","")[:160]}' for c in commands]
    md += ['','## Guardrail','',json.dumps(guardrail,ensure_ascii=False,indent=2) if guardrail else 'pending']
    md += ['','## Remaining blocker','',json.dumps(blocker,ensure_ascii=False,indent=2) if blocker else 'pending']
    PROGRESS_MD.write_text('\n'.join(md)+'\n')

commands=[
 {'cmd':'python -m py_compile scripts/greenfield_multifactor_lib.py scripts/run_greenfield_multifactor_backtest.py scripts/run_greenfield_market_hypotheses.py scripts/run_p1_greenfield_guardrail_audit.py','exit_code':0},
 {'cmd':'python scripts/build_greenfield_multifactor_pool.py --config config/greenfield_multifactor_v1.yaml','exit_code':0,'output_tail':'completed before cron timeout; factor_pool_snapshot mtime updated'},
 {'cmd':'python scripts/run_greenfield_market_hypotheses.py --config config/greenfield_multifactor_v1.yaml','exit_code':0},
 {'cmd':'python scripts/run_greenfield_multifactor_backtest.py --config config/greenfield_multifactor_v1.yaml','exit_code':0,'log':str(BACKTEST_LOG)},
]
write_progress('backtest_completed_running_guardrail_audit', commands)
with LOG.open('w') as f:
    proc=subprocess.run([sys.executable,'scripts/run_p1_greenfield_guardrail_audit.py'],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,text=True)
commands.append({'cmd':'python scripts/run_p1_greenfield_guardrail_audit.py','exit_code':proc.returncode,'log':str(LOG)})
audit=load(OUT/'p1_greenfield_guardrail_audit.json') or {}
failed=audit.get('failed_guardrails') or []
blocker={
 'status':'promotion_blocked' if failed else 'guardrails_passed_needs_manual_promotion_review',
 'failed_guardrails':failed,
 'next_step':'Continue P1; do not promote while fold_concentration or walkforward_absolute_and_excess_c2c fail. If still failed, inspect v7 selected families by fold and add/strengthen long-side c2c factors or baseline objective without short-side inversion.' if failed else 'Guardrails no longer list fold_concentration/walkforward c2c failures; require manual review before lifting freeze.'
}
write_progress('guardrail_audit_completed',commands,guardrail={'decision':audit.get('decision'),'promotion_freeze_lifted':audit.get('promotion_freeze_lifted'),'failed_guardrails':failed,'generated_at':audit.get('generated_at')},blocker=blocker)
orig=load(BLOCKER_IN) or {}
if failed:
    newb={'schema_version':'2026-05-03.p1_long_only_infeasible_blocker.transformed.v7','generated_at':now(),'status':'blocker_transformed','previous_decision':orig.get('decision'),'decision':'v7_baseline_beating_c2c_supply_repair_ran_but_guardrails_still_blocked','promotion_freeze_lifted':False,'failed_guardrails':failed,'evidence':{'progress':str(PROGRESS_JSON),'guardrail_audit':str(OUT/'p1_greenfield_guardrail_audit.json'),'walkforward_report':str(ROOT/'outputs/greenfield_multifactor/walkforward_report.json'),'analog_validation':str(ROOT/'outputs/greenfield_multifactor/analog_validation.json'),'backtest_log':str(BACKTEST_LOG),'guardrail_log':str(LOG)},'next_executable_path':['Inspect v7 per-fold holdout confirmations and strengthen/add genuine long-side close-to-close factor families or stricter baseline-beating objective until at least two independent holdout-confirmed families beat equal-weight across every walkforward fold. Do not promote while fold_concentration or walkforward_absolute_and_excess_c2c fail.'],'original_blocker':orig}
else:
    newb={'schema_version':'2026-05-03.p1_long_only_infeasible_blocker.transformed.v7','generated_at':now(),'status':'guardrail_repair_completed_pending_manual_review','previous_decision':orig.get('decision'),'decision':'v7_baseline_beating_c2c_supply_repair_removed_p1_failed_guardrails','promotion_freeze_lifted':False,'evidence':{'progress':str(PROGRESS_JSON),'guardrail_audit':str(OUT/'p1_greenfield_guardrail_audit.json')},'original_blocker':orig}
dump(BLOCKER_OUT,newb)
write_progress('completed',commands,guardrail={'decision':audit.get('decision'),'promotion_freeze_lifted':audit.get('promotion_freeze_lifted'),'failed_guardrails':failed,'generated_at':audit.get('generated_at')},blocker=blocker)
