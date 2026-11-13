#!/usr/bin/env python3
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
ROOT=Path('/Users/fisher/.openclaw/workspace/factor_factory'); EV=ROOT/'outputs'/'evolution'; TZ=ZoneInfo('Asia/Shanghai')
def read(p:Path)->dict[str,Any]: return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
def dump(p:Path,o:dict[str,Any])->None: p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(o,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def mt(p:Path)->str|None: return datetime.fromtimestamp(p.stat().st_mtime,TZ).isoformat(timespec='seconds') if p.exists() else None
def val(d:Any,*ks,default=None):
    c=d
    for k in ks:
        if not isinstance(c,dict): return default
        c=c.get(k)
    return c if c is not None else default
def pct(x:Any)->str:
    try: return f'{float(x):.4%}'
    except Exception: return 'NA'
def main()->int:
    now=datetime.now(TZ).isoformat(timespec='seconds')
    paths={'checkpoint_started':EV/'stage77_checkpoint_started.json','factor_iteration_pool_delta':EV/'stage77_factor_iteration_pool_delta.json','factor_lineage_delta':EV/'stage77_factor_lineage_delta.json','combo_weight_evidence':EV/'stage77_combo_weight_evidence.json','replay':EV/'stage77_current_combo_replay.json','portfolio':EV/'stage77_current_combo_portfolio.json','nested':EV/'stage77_current_combo_nested.json','walkforward':EV/'stage77_current_combo_walkforward.json','independent_oos_holdout':EV/'stage77_current_combo_independent_oos_holdout.json','qlib_recorder_sync_latest':EV/'qlib_recorder_sync_latest.json','stage77_qlib_recorder_sync':EV/'stage77_qlib_recorder_sync.json','factor_promotion_decision':EV/'factor_promotion_decision.json','factor_validation_report':EV/'factor_validation_report.json','portfolio_backtest_report':EV/'portfolio_backtest_report.json','overfit_audit_report':EV/'overfit_audit_report.json','factor_iteration_pool':EV/'factor_iteration_pool.json','factor_lineage':EV/'factor_lineage.json'}
    portfolio=read(paths['portfolio']); nested=read(paths['nested']); walkforward=read(paths['walkforward']); oos=read(paths['independent_oos_holdout']); qlib=read(paths['qlib_recorder_sync_latest']); weight=read(paths['combo_weight_evidence'])
    independent=val(oos,'evaluation_partitions','independent_oos','by_label_mode','close_to_close',default={}); holdout=val(oos,'evaluation_partitions','holdout_validation','by_label_mode','close_to_close',default={}); recorder=qlib.get('qlib_recorder') or {}
    signal={'generated_at':now,'stage':'stage77','decision':'release_continue_after_stage77_factor_weight_oos_closure','promotion_freeze_lifted':bool(val(oos,'verdict','sufficient_to_lift_promotion_freeze',default=False)),'canonical_weight_factor_ids':weight.get('selected_factor_ids',[]),'greenfield_watchlist_factor_ids':weight.get('greenfield_watchlist_factor_ids',[]),'metrics':{'portfolio_primary_combo_total_return':portfolio.get('primary_combo_total_return'),'nested_primary_excess_total_return':nested.get('primary_excess_total_return'),'walkforward_primary_excess_total_return':walkforward.get('primary_excess_total_return'),'independent_oos_total_return':val(independent,'combo_metrics','total_return'),'independent_oos_excess_total_return':independent.get('excess_total_return'),'holdout_total_return':val(holdout,'combo_metrics','total_return'),'holdout_excess_total_return':holdout.get('excess_total_return')},'qlib_recorder':recorder}
    for key in ['factor_promotion_decision','factor_validation_report','portfolio_backtest_report','overfit_audit_report','factor_iteration_pool','factor_lineage']:
        payload=read(paths[key]); payload['generated_at']=now; payload['stage77_closure_signal']=signal; payload.setdefault('source_artifacts',{})['stage77_factor_iteration_pool_delta']=str(paths['factor_iteration_pool_delta']); payload['source_artifacts']['stage77_factor_lineage_delta']=str(paths['factor_lineage_delta']); payload['source_artifacts']['stage77_combo_weight_evidence']=str(paths['combo_weight_evidence']); payload['source_artifacts']['stage77_independent_oos_holdout']=str(paths['independent_oos_holdout']); payload['source_artifacts']['stage77_qlib_recorder_sync_latest']=str(paths['qlib_recorder_sync_latest'])
        if key=='factor_promotion_decision':
            gc=payload.setdefault('global_constraints',{}); gc['promotion_freeze']=not signal['promotion_freeze_lifted']; gc['stage77_factor_weight_refresh_completed']=True; gc['stage77_independent_oos_holdout_completed']=True; gc['stage77_qlib_recorder_completed']=bool(recorder.get('recorder_id')); payload['round_decision']=signal['decision']
        dump(paths[key],payload)
    control={'report_type':'current_combo_stage77_control_sync','generated_at':now,'decision':signal['decision'],'promotion_freeze_lifted':signal['promotion_freeze_lifted'],'stage77_signal':signal,'source_paths':{k:str(v) for k,v in paths.items()},'source_mtimes':{k:mt(v) for k,v in paths.items()},'closure_checks':{'checkpoint_exists':paths['checkpoint_started'].exists(),'factor_iteration_pool_delta_exists':paths['factor_iteration_pool_delta'].exists(),'factor_lineage_delta_exists':paths['factor_lineage_delta'].exists(),'combo_weight_evidence_exists':paths['combo_weight_evidence'].exists(),'stage77_replay_exists':paths['replay'].exists(),'stage77_portfolio_exists':paths['portfolio'].exists(),'stage77_nested_exists':paths['nested'].exists(),'stage77_walkforward_exists':paths['walkforward'].exists(),'stage77_independent_oos_holdout_exists':paths['independent_oos_holdout'].exists(),'qlib_recorder_id':recorder.get('recorder_id')},'summary':f"Stage77 closed factor/lineage/new weight evidence + combo validation + qlib recorder={recorder.get('recorder_id')}; independent OOS {pct(signal['metrics']['independent_oos_total_return'])}/excess {pct(signal['metrics']['independent_oos_excess_total_return'])}; holdout {pct(signal['metrics']['holdout_total_return'])}/excess {pct(signal['metrics']['holdout_excess_total_return'])}."}
    out=EV/'current_combo_stage77_control_sync.json'; dump(out,control); print(json.dumps({'status':'ok','control_sync':str(out),'recorder_id':recorder.get('recorder_id'),'promotion_freeze_lifted':signal['promotion_freeze_lifted']},ensure_ascii=False,indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
