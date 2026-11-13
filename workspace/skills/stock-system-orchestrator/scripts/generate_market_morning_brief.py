#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
import os
from pathlib import Path
from urllib.request import urlopen

from python_env_bootstrap import bootstrap_openclaw_venv
from orchestrator_common import HEALTH_DIR, PUBLISHED_REPO, resolve_effective_trade_date
import exchange_calendar

ENV_BOOTSTRAP = bootstrap_openclaw_venv()

ANALYTICS_DIR = PUBLISHED_REPO / 'data' / 'recommendation_analytics'
MARKET = ANALYTICS_DIR / 'market_industry_heatmap.json'
OUT = ANALYTICS_DIR / 'market_morning_brief_latest.json'
FX_HISTORY = ANALYTICS_DIR / 'overnight_fx_history.json'
OVERNIGHT_INDEX_CACHE = HEALTH_DIR / 'overnight_index_cache.json'


def pct_change_from_history(hist):
    if hist is None or len(hist) < 2:
        return None
    closes = hist['Close'].dropna()
    if len(closes) < 2:
        return None
    prev = float(closes.iloc[-2])
    last = float(closes.iloc[-1])
    if prev == 0:
        return None
    return round((last / prev - 1.0) * 100.0, 4)


def normalize_trade_date_key(value: str) -> str:
    text = str(value or '').strip()
    if len(text) == 10 and text[4] == '-' and text[7] == '-':
        return text.replace('-', '')
    return text


def fetch_symbol_history(yf, symbol: str, period: str = '1mo'):
    try:
        hist = yf.Ticker(symbol).history(period=period, interval='1d', auto_adjust=False)
        if hist is not None and not hist.empty and len(hist['Close'].dropna()) >= 2:
            return hist, None
        return None, f'insufficient history for {symbol}'
    except Exception as e:
        return None, f'{symbol}: {type(e).__name__}: {e}'


def load_fx_history() -> dict:
    if FX_HISTORY.exists():
        try:
            raw = json.loads(FX_HISTORY.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                return {}
            return {normalize_trade_date_key(key): value for key, value in raw.items()}
        except Exception:
            return {}
    return {}


def save_fx_history(history: dict) -> None:
    normalized = {normalize_trade_date_key(key): value for key, value in history.items()}
    FX_HISTORY.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def fetch_cnh_with_fallback(trade_date: str):
    history = load_fx_history()
    record = history.get(trade_date, {})
    current = record.get('cnh')
    source_error = None
    if current is None:
        try:
            with urlopen('https://open.er-api.com/v6/latest/USD', timeout=15) as r:
                payload = json.loads(r.read().decode('utf-8'))
            current = payload.get('rates', {}).get('CNH')
            if current is not None:
                current = round(float(current), 6)
                history[trade_date] = {'cnh': current, 'source': 'open.er-api.com'}
                save_fx_history(history)
        except Exception as e:
            source_error = f'open.er-api.com: {type(e).__name__}: {e}'

    prev_dates = sorted(d for d in history.keys() if d < trade_date)
    prev = None
    if prev_dates:
        prev_record = history.get(prev_dates[-1], {}) or {}
        prev = prev_record.get('cnh')
        if prev is None:
            prev = prev_record.get('close')
    change_pct = None
    if current is not None and prev not in (None, 0):
        change_pct = round((float(current) / float(prev) - 1.0) * 100.0, 4)
    return {
        'symbol': 'USD/CNH',
        'change_pct': change_pct,
        'close': current,
        'source_error': source_error if change_pct is None else None,
        'prev_close': prev,
    }


def load_overnight_cache() -> dict:
    """Load persisted overnight index cache."""
    if OVERNIGHT_INDEX_CACHE.exists():
        try:
            return json.loads(OVERNIGHT_INDEX_CACHE.read_text())
        except Exception:
            pass
    return {}


def save_overnight_cache(a50_pct, dragon_pct, trade_date: str):
    """Persist valid overnight index values for future fallback."""
    cache = load_overnight_cache()
    if a50_pct is not None:
        cache['a50'] = {'change_pct': a50_pct, 'trade_date': trade_date}
    if dragon_pct is not None:
        cache['golden_dragon'] = {'change_pct': dragon_pct, 'trade_date': trade_date}
    try:
        OVERNIGHT_INDEX_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception:
        pass


def fetch_overnight_inputs(trade_date: str):
    import yfinance as yf
    inputs = {}
    history = load_fx_history()
    mapping = {
        'a50': [
            {'symbol': 'XIN9.FGI', 'label': '富时A50夜盘', 'source_kind': 'exact'},
            {'symbol': 'ASHR', 'label': '富时A50参考代理', 'source_kind': 'proxy'},
            {'symbol': 'FXI', 'label': '富时A50参考代理', 'source_kind': 'proxy'},
        ],
        'golden_dragon': [
            {'symbol': '^HXC', 'label': '纳斯达克中国金龙指数', 'source_kind': 'exact'},
            {'symbol': 'KWEB', 'label': '金龙参考代理', 'source_kind': 'proxy'},
            {'symbol': 'CQQQ', 'label': '金龙参考代理', 'source_kind': 'proxy'},
            {'symbol': 'PGJ', 'label': '金龙参考代理', 'source_kind': 'proxy'},
        ],
        'spx': [{'symbol': '^GSPC', 'label': '标普500', 'source_kind': 'exact'}],
        'nasdaq': [{'symbol': '^IXIC', 'label': '纳斯达克', 'source_kind': 'exact'}],
    }
    for key, candidates in mapping.items():
        chosen = None
        last_error = None
        for spec in candidates:
            hist, error = fetch_symbol_history(yf, spec['symbol'])
            if hist is not None:
                chosen = {
                    'symbol': spec['symbol'],
                    'source_kind': spec['source_kind'],
                    'display_name': spec['label'],
                    'change_pct': pct_change_from_history(hist),
                    'close': round(float(hist['Close'].dropna().iloc[-1]), 4),
                    'source_error': None,
                }
                if chosen['close'] is not None:
                    history[trade_date] = {**history.get(trade_date, {}), key: chosen['close'], 'source': spec['symbol']}
                    save_fx_history(history)
                break
            last_error = error
        if chosen is None:
            prev_dates = sorted(d for d in history.keys() if d < trade_date)
            fallback_close = None
            if prev_dates:
                prev_record = history.get(prev_dates[-1], {}) or {}
                fallback_close = prev_record.get(key) or prev_record.get('close')
            chosen = {
                'symbol': candidates[0]['symbol'],
                'source_kind': 'history_carry_forward',
                'display_name': candidates[0]['label'],
                'change_pct': None,
                'close': fallback_close,
                'source_error': last_error,
            }
        inputs[key] = chosen
    inputs['cnh'] = fetch_cnh_with_fallback(trade_date)
    return inputs


def derive_regime(data):
    a50 = data.get('a50_change_pct')
    dragon = data.get('golden_dragon_change_pct')
    cnh = data.get('cnh_change_pct')
    score = 50
    drivers = []
    if a50 is not None:
        if a50 <= -1.0:
            score += 20
            drivers.append(f'A50隔夜偏弱 {a50:+.2f}%')
        elif a50 >= 0.8:
            score -= 15
            drivers.append(f'A50隔夜偏强 {a50:+.2f}%')
    if dragon is not None:
        if dragon <= -2.0:
            score += 15
            drivers.append(f'金龙指数走弱 {dragon:+.2f}%')
        elif dragon >= 1.5:
            score -= 10
            drivers.append(f'金龙指数走强 {dragon:+.2f}%')
    if cnh is not None:
        if cnh >= 0.2:
            score += 10
            drivers.append(f'离岸人民币走弱 {cnh:+.2f}%')
        elif cnh <= -0.2:
            score -= 8
            drivers.append(f'离岸人民币回稳 {cnh:+.2f}%')
    score = max(0, min(100, score))
    if score >= 75:
        regime = '高风险'
    elif score >= 55:
        regime = '谨慎'
    elif score <= 35:
        regime = '偏积极'
    else:
        regime = '中性'
    return regime, score, drivers


def build_ai_summary(regime: str, score: int, drivers: list[str], top: list[dict], opening_playbook: list[str]):
    top_names = [r.get('industry_name') for r in top[:3] if r.get('industry_name')]
    sector_text = '、'.join(top_names) if top_names else '热点主线'
    driver_text = '；'.join(drivers[:3]) if drivers else '外盘与汇率没有形成明显单边冲击'
    if regime == '高风险':
        summary = f'今晨市场偏防守，风险分数 {score:.0f}，隔夜扰动主要来自{driver_text}。开盘更适合先看权重与防御板块能否稳住，再决定是否参与题材扩散。当前优先观察 {sector_text} 是否还能独立走强，若高开无承接，先降仓位预期。'
        advice = '操作建议：先控仓，前 30 分钟不追高；只有当指数、量能、主线三者同步转强时，才考虑回补进攻仓位。'
    elif regime == '谨慎':
        summary = f'今晨市场偏谨慎，风险分数 {score:.0f}，主要信号是{driver_text}。盘面不算极弱，但开盘阶段更像是先做确认、再决定是否进攻。热点仍集中在 {sector_text}，重点看龙头是否继续放量领涨，而不是只看情绪高开。'
        advice = '操作建议：仓位保持中性偏谨慎，先盯开盘 30 分钟主线强度；强者恒强再跟，分歧走弱就等二次确认。'
    elif regime == '偏积极':
        summary = f'今晨市场偏积极，风险分数 {score:.0f}，隔夜环境对 A 股偏友好，核心驱动是{driver_text}。如果开盘后量价继续配合，{sector_text} 更容易成为今天的主动进攻方向，可以重点看龙头带动和板块扩散节奏。'
        advice = '操作建议：可适度提高进攻仓位，优先做最强主线的龙头与前排扩散，但仍要用开盘后量价确认来决定是否追击。'
    else:
        summary = f'今晨市场整体偏中性，风险分数 {score:.0f}，当前没有看到足以单边定调的隔夜冲击，主要线索是{driver_text}。这意味着今天更像是盘中择强，不适合一开盘就重仓押方向，先看 {sector_text} 能否形成清晰主线。'
        advice = '操作建议：维持中性仓位，先观察再出手；如果热点行业持续强化，就跟随最强主线，否则以等待和低吸为主。'
    if opening_playbook:
        advice = advice + '｜执行要点：' + '；'.join(opening_playbook[:3])
    return summary, advice


def _load_market_snapshot() -> dict:
    """Try to load the unified market snapshot. Returns empty dict if unavailable."""
    from orchestrator_common import HEALTH_DIR as _HD
    snapshot_latest = _HD / 'market_snapshot_latest.json'
    if snapshot_latest.exists():
        try:
            return json.loads(snapshot_latest.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def main() -> int:
    market = json.loads(MARKET.read_text(encoding='utf-8'))
    latest_market_trade_date = market.get('latest_trade_date')
    trade_date = resolve_effective_trade_date(
        os.environ.get('MORNING_TRADE_DATE'),
        latest_market_trade_date,
        fallback_now=True,
    )
    rows = [r for r in (market.get('rows') or []) if r.get('trade_date') == latest_market_trade_date]
    rows = sorted(rows, key=lambda r: float(r.get('market_heat_ema_5') or -999), reverse=True)
    top = rows[:5]

    # Prefer unified market snapshot for overnight indices (avoids redundant fetches)
    _snapshot = _load_market_snapshot()
    _snap_overnight = (_snapshot.get('overnight_indices') or {})
    _snap_trade_date = _snapshot.get('trade_date')
    _use_snapshot = bool(_snap_trade_date and _snap_trade_date == trade_date)

    if _use_snapshot and _snap_overnight.get('a50', {}).get('change_pct') is not None:
        print(f'[morning_brief] using market snapshot for overnight indices (trade_date={_snap_trade_date})')
        overnight = {
            'a50': _snap_overnight.get('a50', {}),
            'golden_dragon': _snap_overnight.get('golden_dragon', {}),
            'cnh': _snap_overnight.get('cnh', {}),
        }
    else:
        overnight = fetch_overnight_inputs(trade_date)

    a50 = overnight['a50']['change_pct']
    dragon = overnight['golden_dragon']['change_pct']
    cnh_raw = overnight['cnh']['change_pct']
    if a50 is None and overnight['a50'].get('close') is not None:
        a50 = 0.0
    if dragon is None and overnight['golden_dragon'].get('close') is not None:
        dragon = 0.0
    # Cache fallback: use last known good values when live fetch returns None
    _overnight_cache = load_overnight_cache()
    if a50 is None:
        _cached = _overnight_cache.get('a50', {})
        if _cached.get('change_pct') is not None:
            a50 = _cached['change_pct']
            print(f"[morning_brief] A50 using cached value: {a50}% from {_cached.get('trade_date')}")
    if dragon is None:
        _cached = _overnight_cache.get('golden_dragon', {})
        if _cached.get('change_pct') is not None:
            dragon = _cached['change_pct']
            print(f"[morning_brief] Dragon using cached value: {dragon}% from {_cached.get('trade_date')}")
    # Persist valid values back to cache for next run
    save_overnight_cache(a50, dragon, trade_date)
    regime, score, drivers = derive_regime({
        'a50_change_pct': a50,
        'golden_dragon_change_pct': dragon,
        'cnh_change_pct': cnh_raw,
    })
    if regime == '高风险':
        opening_playbook = ['开盘前30分钟不追高', '优先观察防御板块与权重是否稳盘', '高弹性题材先降预期']
    elif regime == '谨慎':
        opening_playbook = ['等主线确认后再加仓', '优先看市场最热行业是否延续', '避免开盘即追高']
    elif regime == '偏积极':
        opening_playbook = ['优先跟踪最热行业龙头与扩散股', '可适度提高进攻仓位', '仍需观察开盘30分钟资金确认']
    else:
        opening_playbook = ['保持中性仓位', '观察市场最热行业是否有持续性', '等待开盘后主线明朗']
    ai_summary, ai_action_advice = build_ai_summary(regime, score, drivers, top, opening_playbook)
    session_trade_date = str(os.environ.get('OPENCLAW_TARGET_TRADE_DATE') or datetime.now().strftime('%Y%m%d'))
    # 2026-10-03: the morning brief legitimately uses the previous trading day's
    # close (trade_date / market_data_trade_date), but the session it is written
    # for must be a confirmed open exchange day.
    exchange_calendar.require_open_session(session_trade_date)
    payload = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'session_trade_date': session_trade_date,
        'trade_date': trade_date,
        'market_data_trade_date': latest_market_trade_date,
        'data_time_label': f"晨判生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜市场数据交易日 {latest_market_trade_date}",
        'regime': regime,
        'risk_score': score,
        'a50_change_pct': a50,
        'golden_dragon_change_pct': dragon,
        'cnh_change_pct': cnh_raw,
        'offset_score': None,
        'offset_reason': 'v1 暂未接入结构化政策/新闻对冲打分，先按外盘与汇率风险做基础晨判。',
        'key_drivers': drivers,
        'focus_sectors': [r.get('industry_name') for r in top if r.get('industry_name')],
        'opening_playbook': opening_playbook,
        'ai_summary': ai_summary,
        'ai_action_advice': ai_action_advice,
        'top_sector_snapshots': top,
        'external_inputs': overnight,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(f'created={OUT}')
    print(json.dumps({
        'trade_date': trade_date,
        'regime': regime,
        'risk_score': score,
        'a50_change_pct': a50,
        'golden_dragon_change_pct': dragon,
        'cnh_change_pct': cnh_raw,
        'focus_sectors': payload['focus_sectors'][:5],
        'sources': {
            k: (v.get('symbol') or v.get('label') or k) if isinstance(v, dict) else k
            for k, v in overnight.items()
        },
        'market_ref_trade_date': latest_market_trade_date,
    }, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
