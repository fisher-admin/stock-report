#!/usr/bin/env python3
"""Generate the unified daily market snapshot.

This script aggregates all daily-level market state into a single
`market_snapshot_YYYYMMDD.json` file. All downstream analysis scripts
(morning brief, midday analysis, unified decision payload) should read
from this snapshot instead of fetching data independently.

Outputs:
  stock_data/03-working/health/market_snapshot_YYYYMMDD.json  (date-stamped)
  stock_data/03-working/health/market_snapshot_latest.json    (always-latest symlink copy)

Designed to run after stage1_data_prepare.py succeeds.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from python_env_bootstrap import bootstrap_openclaw_venv
from orchestrator_common import HEALTH_DIR, PUBLISHED_REPO

ENV_BOOTSTRAP = bootstrap_openclaw_venv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PUBLISHED_ROOT = PUBLISHED_REPO / 'data' / 'recommendation_analytics'
OVERNIGHT_INDEX_CACHE = HEALTH_DIR / 'overnight_index_cache.json'
MARKET_HEATMAP = PUBLISHED_ROOT / 'market_industry_heatmap.json'
FX_HISTORY = PUBLISHED_ROOT / 'overnight_fx_history.json'

HEALTH_DIR.mkdir(parents=True, exist_ok=True)


def now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        val = json.loads(path.read_text(encoding='utf-8'))
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------------------------------------------------------------------------
# Overnight index helpers (reuse cache from generate_market_morning_brief.py)
# ---------------------------------------------------------------------------

def load_overnight_cache() -> dict:
    return load_json(OVERNIGHT_INDEX_CACHE)


def fetch_overnight_indices(trade_date: str) -> dict:
    """Fetch A50 and Golden Dragon. Falls back to cache on failure."""
    result = {
        'a50': {'change_pct': None, 'close': None, 'source': None},
        'golden_dragon': {'change_pct': None, 'close': None, 'source': None},
    }
    try:
        import yfinance as yf
        specs = [
            ('a50', [('XIN9.FGI', 'exact'), ('ASHR', 'proxy'), ('FXI', 'proxy')]),
            ('golden_dragon', [('^HXC', 'exact'), ('KWEB', 'proxy'), ('CQQQ', 'proxy')]),
        ]
        for key, candidates in specs:
            for symbol, kind in candidates:
                try:
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period='5d')
                    if hist is not None and not hist.empty and len(hist) >= 2:
                        closes = hist['Close'].dropna().tolist()
                        if len(closes) >= 2:
                            prev, last = closes[-2], closes[-1]
                            change_pct = round((last - prev) / prev * 100, 4) if prev else None
                            result[key] = {
                                'change_pct': change_pct,
                                'close': round(last, 4),
                                'source': symbol,
                                'source_kind': kind,
                            }
                            break
                except Exception:
                    continue
    except ImportError:
        pass

    # Cache fallback
    cache = load_overnight_cache()
    for key in ('a50', 'golden_dragon'):
        if result[key]['change_pct'] is None:
            cached = cache.get(key, {})
            if cached.get('change_pct') is not None:
                result[key] = {
                    'change_pct': cached['change_pct'],
                    'close': cached.get('close'),
                    'source': 'cache',
                    'cache_trade_date': cached.get('trade_date'),
                }
                print(f'[snapshot] {key} using cached value: {cached["change_pct"]}% from {cached.get("trade_date")}')

    # Persist valid values to cache
    for key in ('a50', 'golden_dragon'):
        pct = result[key].get('change_pct')
        if pct is not None and result[key].get('source') != 'cache':
            cache[key] = {'change_pct': pct, 'trade_date': trade_date,
                          'close': result[key].get('close')}
    if any(result[k].get('change_pct') is not None and result[k].get('source') != 'cache'
           for k in ('a50', 'golden_dragon')):
        try:
            OVERNIGHT_INDEX_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Industry heat state
# ---------------------------------------------------------------------------

def load_industry_state(trade_date: str) -> dict:
    """Read industry heat from the published heatmap file."""
    heatmap = load_json(MARKET_HEATMAP)
    latest_td = heatmap.get('latest_trade_date')
    rows = heatmap.get('rows') or []
    if latest_td:
        rows = [r for r in rows if r.get('trade_date') == latest_td]
    # Use avg_pct_chg (primary) or market_heat_ema_5 (secondary) for ranking
    def _sort_key(r):
        for field in ('avg_pct_chg', 'net_pct', 'market_heat_ema_5'):
            v = r.get(field)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0
    top = sorted(rows, key=_sort_key, reverse=True)[:5]
    bottom = sorted(rows, key=_sort_key)[:5]
    return {
        'heatmap_trade_date': latest_td,
        'top_industries': [r.get('industry_name') for r in top],
        'bottom_industries': [r.get('industry_name') for r in bottom],
        'industry_count': len(rows),
    }


# ---------------------------------------------------------------------------
# Stage1 health read
# ---------------------------------------------------------------------------

def load_stage1_health(trade_date: str) -> dict:
    path = HEALTH_DIR / f'data_ready_{trade_date}.json'
    if not path.exists():
        # Try latest data_preparation_run.json
        path = HEALTH_DIR / 'data_preparation_run.json'
    data = load_json(path)
    run = data.get('run') or {}
    return {
        'data_ok': data.get('ok', False),
        'trade_date': run.get('trade_date') or data.get('target_trade_date'),
        'run_id': run.get('run_id'),
        'cache_quality': data.get('quality_ok'),
    }


# ---------------------------------------------------------------------------
# CNH / FX
# ---------------------------------------------------------------------------

def load_cnh(trade_date: str) -> dict:
    """Read CNH from fx history file."""
    if not FX_HISTORY.exists():
        return {'change_pct': None, 'source': 'unavailable'}
    try:
        raw = json.loads(FX_HISTORY.read_text(encoding='utf-8'))
        record = raw.get(trade_date, {})
        return {
            'change_pct': record.get('cnh'),
            'source': record.get('source', 'fx_history'),
        }
    except Exception:
        return {'change_pct': None, 'source': 'error'}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    trade_date = os.environ.get('SNAPSHOT_TRADE_DATE') or datetime.now().strftime('%Y%m%d')
    print(f'[snapshot] generating market snapshot for {trade_date}')

    stage1 = load_stage1_health(trade_date)
    # Use confirmed trade_date from stage1 if available
    if stage1.get('trade_date'):
        trade_date = stage1['trade_date']
        print(f'[snapshot] using confirmed trade_date from stage1: {trade_date}')

    overnight = fetch_overnight_indices(trade_date)
    industry = load_industry_state(trade_date)
    cnh = load_cnh(trade_date)

    payload = {
        'generated_at': now_str(),
        'trade_date': trade_date,
        'data_layer': {
            'stage1_ok': stage1.get('data_ok'),
            'run_id': stage1.get('run_id'),
            'cache_quality': stage1.get('cache_quality'),
        },
        'overnight_indices': {
            'a50': overnight['a50'],
            'golden_dragon': overnight['golden_dragon'],
            'cnh': cnh,
        },
        'industry_state': industry,
        'meta': {
            'schema_version': '1.0',
            'description': 'Unified daily market snapshot. All analysis scripts should read from this file.',
        },
    }

    # Write date-stamped and latest copies
    stamped = HEALTH_DIR / f'market_snapshot_{trade_date}.json'
    latest = HEALTH_DIR / 'market_snapshot_latest.json'
    write_json(stamped, payload)
    write_json(latest, payload)

    print(f'[snapshot] wrote {stamped}')
    print(f'[snapshot] wrote {latest}')
    print(f'[snapshot] a50={overnight["a50"].get("change_pct")} '
          f'dragon={overnight["golden_dragon"].get("change_pct")} '
          f'industry_top={industry.get("top_industries", [])[:3]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
