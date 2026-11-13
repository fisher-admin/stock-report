#!/usr/bin/env python3
"""Colab 产出接收 + 发布（VPS 只留发布）。

Usage:
  python3 colab_publish_receiver.py --staging /srv/hermes-stock/colab-inbox/20261110 --trade-date 20261110 [--publish] [--dry-run]

Staging dir must contain (written by the Colab notebook):
  strategy_backtests.json, data.json, latest_summary.txt, manifest.json

Validation gates (same contract as the Colab notebook):
  - manifest.trade_date == strategy_backtests.latest_trade_date == data.json update_time/latest_trade_date
  - retained strategies baseline_v4 + prebreakout_v41 present, each top20 == 20
  - prebreakout_v41 top20 AI fields present, or manifest.degraded == true
  - JSON parses with allow_nan=False (no NaN/Inf smuggled in)

On --publish: copy validated files into stock-report-repo, then run
public_dashboard/publish_release.py to build + promote a release and verify
the public API serves it. Without --publish (or with --dry-run) it only
validates and writes a receipt to health/colab_publish_<date>.json.
"""
from __future__ import annotations
import argparse, json, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

WS = Path('/srv/hermes-stock/workspace')
REPO = WS / 'stock_data/03-working/stock-report-repo'
HEALTH = WS / 'stock_data/03-working/health'
PUBLISH = Path('/srv/hermes-stock/public/public_dashboard/publish_release.py')
RELEASE_ROOT = Path('/srv/hermes-stock/releases')
AI_FIELDS = ['ai_score', 'ai_advice', 'ai_conclusion', 'ai_points', 'ai_trend',
             'ai_ma', 'ai_volume', 'ai_confidence', 'ai_summary', 'ai_risk_warning']

def load_json_strict(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f'non-finite constant {x}')))
    except Exception as exc:
        raise SystemExit(f'JSON 无效 {path}: {exc}')

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--staging', required=True)
    ap.add_argument('--trade-date', required=True)
    ap.add_argument('--publish', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    staging = Path(args.staging)
    date = args.trade_date
    errors, checks = [], {}
    for name in ['strategy_backtests.json', 'data.json', 'manifest.json']:
        if not (staging / name).exists():
            errors.append(f'missing {name}')
    if errors:
        print('校验失败:', errors); return 1
    sb = load_json_strict(staging / 'strategy_backtests.json')
    dj = load_json_strict(staging / 'data.json')
    mf = load_json_strict(staging / 'manifest.json')
    checks['manifest_trade_date'] = mf.get('trade_date')
    checks['sb_trade_date'] = sb.get('latest_trade_date')
    checks['data_trade_date'] = dj.get('latest_trade_date') or dj.get('update_time')
    if not (mf.get('trade_date') == date == sb.get('latest_trade_date') == checks['data_trade_date']):
        errors.append(f"trade date mismatch: manifest={mf.get('trade_date')} sb={sb.get('latest_trade_date')} data={checks['data_trade_date']} expected={date}")
    ids = [s.get('id') for s in sb.get('strategies', [])]
    checks['strategy_ids'] = ids
    for want in ['baseline_v4', 'prebreakout_v41']:
        if want not in ids:
            errors.append(f'retained strategy missing: {want}')
    for s in sb.get('strategies', []):
        n = len(s.get('top20', []))
        checks[f"top20_{s.get('id')}"] = n
        if s.get('id') in ('baseline_v4', 'prebreakout_v41') and n != 20:
            errors.append(f"top20 != 20 for {s.get('id')}: {n}")
    pb = next((s for s in sb.get('strategies', []) if s.get('id') == 'prebreakout_v41'), None)
    if pb:
        filled = sum(1 for it in pb.get('top20', []) if all(k in it and it[k] not in (None, '') for k in AI_FIELDS))
        checks['prebreakout_ai_filled'] = filled
        if filled < 20 and not mf.get('degraded'):
            errors.append(f'AI fields incomplete ({filled}/20) but manifest not marked degraded')
    receipt = {'trade_date': date, 'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'source': 'colab', 'checks': checks, 'errors': errors, 'ok': not errors}
    HEALTH.mkdir(parents=True, exist_ok=True)
    (HEALTH / f'colab_publish_{date}.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    if errors:
        print('校验失败，拒绝发布'); return 1
    if args.dry_run or not args.publish:
        print('校验通过（未发布，dry-run / 未加 --publish）'); return 0
    # publish: stage into repo, then build + promote release
    (REPO / 'data').mkdir(parents=True, exist_ok=True)
    shutil.copy2(staging / 'strategy_backtests.json', REPO / 'data/strategy_backtests.json')
    shutil.copy2(staging / 'data.json', REPO / 'data.json')
    if (staging / 'latest_summary.txt').exists():
        shutil.copy2(staging / 'latest_summary.txt', REPO / 'data/latest_summary.txt')
    r = subprocess.run([sys.executable, str(PUBLISH), '--source', str(REPO),
                        '--release-root', str(RELEASE_ROOT),
                        '--expected-trade-date', date,
                        '--verify-url', 'http://127.0.0.1:9120'],
                       capture_output=True, text=True, timeout=600)
    print(r.stdout[-2000:]); print(r.stderr[-1000:])
    print('publish 返回码:', r.returncode)
    return r.returncode

if __name__ == '__main__':
    raise SystemExit(main())
