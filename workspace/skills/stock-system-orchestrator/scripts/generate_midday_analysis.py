#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen

from python_env_bootstrap import bootstrap_openclaw_venv

ENV_BOOTSTRAP = bootstrap_openclaw_venv()

try:
    import akshare as ak
except Exception:  # pragma: no cover - optional dependency
    ak = None

try:
    import yfinance as yf
    YF_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - optional dependency
    yf = None
    YF_IMPORT_ERROR = f'{type(exc).__name__}: {exc}'

from orchestrator_common import PUBLISHED_REPO, WORKSPACE, git_status, resolve_effective_trade_date, run_git, sync_git_tracking_branch
import exchange_calendar
import domestic_index_close

PUBLISHED_ROOT = PUBLISHED_REPO / 'data' / 'recommendation_analytics'
WORKING_ROOT = WORKSPACE / 'stock_data/03-working/recommendation_analytics'
MORNING = PUBLISHED_ROOT / 'market_morning_brief_latest.json'
MARKET = PUBLISHED_ROOT / 'market_industry_heatmap.json'
UNIFIED = PUBLISHED_ROOT / 'unified_decision_payload.json'
DETAIL = PUBLISHED_ROOT / 'prebreakout_recommendations.json'
STRATEGY_JSON = PUBLISHED_REPO / 'data' / 'strategy_backtests.json'
OUT = WORKING_ROOT / 'midday_analysis_latest.json'
PUBLISHED_OUT = PUBLISHED_ROOT / 'midday_analysis_latest.json'
LATEST_DIR = PUBLISHED_REPO / 'data' / 'latest'
STATE_GENERATOR = Path(__file__).resolve().parent / 'generate_latest_states.py'
V2_PUBLISH_SCRIPT = Path(__file__).resolve().parent / 'publish_stock_report_v2.sh'
FX_HISTORY = PUBLISHED_ROOT / 'overnight_fx_history.json'


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def safe_float(value, default: float = -999.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def confidence_rank(value: str | None) -> int:
    mapping = {'高': 0, '中': 1, '低': 2}
    return mapping.get(str(value or '').strip(), 3)


def unique_rows(rows: list[dict], key_fields: tuple[str, ...]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = '::'.join(str(row.get(field) or '') for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def latest_recommend_date(detail: dict) -> str | None:
    rows = detail.get('rows', []) or []
    dates = sorted(
        {
            str(row.get('recommend_date') or '').strip()
            for row in rows
            if str(row.get('recommend_date') or '').strip()
        }
    )
    return dates[-1] if dates else None


def find_matching_row(frame, keywords: list[str]) -> dict | None:
    if frame is None or getattr(frame, 'empty', True):
        return None
    cleaned = [re.escape(str(item).strip()) for item in keywords if str(item or '').strip()]
    if not cleaned:
        return None
    pattern = '|'.join(cleaned)
    text_frame = frame.astype(str)
    mask = None
    for column in text_frame.columns:
        column_mask = text_frame[column].str.contains(pattern, case=False, na=False, regex=True)
        mask = column_mask if mask is None else (mask | column_mask)
    matches = frame[mask] if mask is not None else frame.iloc[0:0]
    if matches.empty:
        return None
    return matches.iloc[0].to_dict()


def build_snapshot(
    *,
    key: str,
    label: str,
    symbol: str,
    source_kind: str,
    provider: str,
    close: float | None,
    prev_close: float | None,
    as_of: str | None,
    bar_count: int | None = None,
    raw_change_pct: float | None = None,
    source_error: str | None = None,
) -> dict:
    change_pct = None
    if close is not None and prev_close not in (None, 0):
        change_pct = round((float(close) / float(prev_close) - 1.0) * 100.0, 4)
    elif raw_change_pct is not None:
        change_pct = round(float(raw_change_pct), 4)
    return {
        'key': key,
        'symbol': symbol,
        'label': label,
        'source_kind': source_kind,
        'provider': provider,
        'close': close,
        'prev_close': prev_close,
        'change_pct': change_pct,
        'as_of': as_of,
        'bar_count': bar_count or 0,
        'source_error': source_error if change_pct is None else None,
    }


_TS_PRO = None


def _tushare_pro():
    """Reuse the configured primary/fallback client for market indices."""
    global _TS_PRO
    if _TS_PRO is not None:
        return _TS_PRO
    try:
        import sys as _sys
        _sys.path.insert(0, str(WORKSPACE / "skills" / "stock-analyzer"))
        from pipeline import init_tushare
        _TS_PRO = init_tushare()
    except Exception:
        _TS_PRO = False
    return _TS_PRO


def _configured_provider_label(client: object) -> str:
    """Return the real provider when an adapter omits row-level provenance."""
    for attr in ("provider_name", "source_provider", "provider"):
        value = str(getattr(client, attr, "") or "").strip().lower()
        if value:
            return value
    if getattr(client, "rds_key", None):
        return "rds"
    if getattr(client, "promax_key", None):
        return "promax"
    return "tushare"


_DOMESTIC_TS_CODE = {'shanghai': '000001.SH', 'shenzhen': '399001.SZ', 'chinext': '399006.SZ'}


def fetch_tushare_domestic_index(key: str, label: str, symbol: str, trade_date: str | None) -> tuple[dict | None, str | None]:
    """权威收盘: tushare index_daily 取【结算后收盘价】。盘中(该日尚无结算数据)返回空→自动回退现价源。
    20260703事故修复: 原用 akshare 新浪现价('最新价'),收盘后跑取到盘中陈旧/高点值却标exact,指数虚高。"""
    ts_code = _DOMESTIC_TS_CODE.get(key)
    if not ts_code or not trade_date:
        return None, 'no ts_code / trade_date for tushare index'
    pro = _tushare_pro()
    if not pro:
        return None, 'tushare unavailable'
    try:
        d = pro.index_daily(ts_code=ts_code, trade_date=str(trade_date))
        if d is None or len(d) == 0:
            return None, f'tushare index_daily empty for {ts_code}@{trade_date}'
        r = d.iloc[0]
        return build_snapshot(
            key=key, label=label, symbol=symbol,
            source_kind='exact_close', provider=str(r.get('source_provider') or _configured_provider_label(pro)),
            close=safe_float(r.get('close'), default=None),
            prev_close=safe_float(r.get('pre_close'), default=None),
            as_of=str(trade_date), bar_count=1,
            raw_change_pct=safe_float(r.get('pct_chg'), default=None),
        ), None
    except Exception as exc:
        return None, f'tushare.index_daily: {type(exc).__name__}: {exc}'


def fetch_akshare_domestic_index(key: str, label: str, symbol: str, keywords: list[str]) -> tuple[dict | None, str | None]:
    if ak is None:
        return None, 'akshare unavailable'
    try:
        frame = ak.stock_zh_index_spot_sina()
        row = find_matching_row(frame, keywords)
        if row is None:
            return None, f'no match for {symbol} in stock_zh_index_spot_sina'
        close = safe_float(row.get('最新价'))
        prev = safe_float(row.get('昨收'))
        as_of = str(row.get('最新行情时间') or '').strip() or None
        return build_snapshot(
            key=key,
            label=label,
            symbol=symbol,
            # 诚实标注: 新浪'最新价'是现价/盘中快照,非结算收盘(收盘后跑可能取到盘中陈旧值)。
            source_kind='intraday_spot',
            provider='akshare',
            close=None if close == -999.0 else close,
            prev_close=None if prev == -999.0 else prev,
            as_of=as_of,
            raw_change_pct=safe_float(row.get('涨跌幅'), default=None),
        ), None
    except Exception as exc:
        return None, f'akshare.stock_zh_index_spot_sina: {type(exc).__name__}: {exc}'


def fetch_akshare_global_index(key: str, label: str, symbol: str, keywords: list[str]) -> tuple[dict | None, str | None]:
    if ak is None:
        return None, 'akshare unavailable'
    try:
        frame = ak.index_global_spot_em()
        row = find_matching_row(frame, keywords)
        if row is None:
            return None, f'no match for {symbol} in index_global_spot_em'
        close = safe_float(row.get('最新价'))
        prev = safe_float(row.get('昨收价'))
        as_of = str(row.get('最新行情时间') or '').strip() or None
        return build_snapshot(
            key=key,
            label=label,
            symbol=symbol,
            source_kind='exact',
            provider='akshare',
            close=None if close == -999.0 else close,
            prev_close=None if prev == -999.0 else prev,
            as_of=as_of,
            raw_change_pct=safe_float(row.get('涨跌幅'), default=None),
        ), None
    except Exception as exc:
        return None, f'akshare.index_global_spot_em: {type(exc).__name__}: {exc}'


def fetch_yfinance_snapshot(
    key: str,
    label: str,
    symbol: str,
    *,
    source_kind: str = 'exact',
) -> tuple[dict | None, str | None]:
    if yf is None:
        return None, f'yfinance unavailable ({YF_IMPORT_ERROR or "import failed"})'
    try:
        ticker = yf.Ticker(symbol)
        daily = ticker.history(period='5d', interval='1d', auto_adjust=False)
        if daily is None or daily.empty:
            return None, f'insufficient daily data for {symbol}'
        daily_close = daily['Close'].dropna()
        if len(daily_close) < 2:
            return None, f'insufficient daily bars for {symbol}'

        intraday = ticker.history(period='1d', interval='5m', auto_adjust=False)
        intraday_close = None if intraday is None or intraday.empty else intraday['Close'].dropna()

        prev_close = float(daily_close.iloc[-2])
        if prev_close == 0:
            return None, f'zero prev_close for {symbol}'

        if intraday_close is not None and len(intraday_close) >= 1:
            last_close = float(intraday_close.iloc[-1])
            return build_snapshot(
                key=key,
                label=label,
                symbol=symbol,
                source_kind=source_kind,
                provider='yfinance',
                close=round(last_close, 4),
                prev_close=round(prev_close, 4),
                as_of=str(intraday.index[-1]),
                bar_count=int(len(intraday_close)),
            ), None

        last_close = float(daily_close.iloc[-1])
        return build_snapshot(
            key=key,
            label=label,
            symbol=symbol,
            source_kind=f'{source_kind}_daily_fallback',
            provider='yfinance_daily',
            close=round(last_close, 4),
            prev_close=round(prev_close, 4),
            as_of=str(daily.index[-1]),
            bar_count=int(len(daily_close)),
        ), f'intraday missing for {symbol}; used daily fallback'
    except Exception as exc:
        return None, f'{symbol}: {type(exc).__name__}: {exc}'


def build_hot_sectors(market: dict, limit: int = 6) -> list[dict]:
    latest = market.get('latest_trade_date')
    rows = [r for r in market.get('rows', []) if r.get('trade_date') == latest]
    rows = sorted(rows, key=lambda r: safe_float(r.get('market_heat_ema_5')), reverse=True)
    result = []
    for row in rows[:limit]:
        result.append({
            'industry_name': row.get('industry_name'),
            'market_heat_rank': row.get('market_heat_rank'),
            'market_heat_ema_5': row.get('market_heat_ema_5'),
            'trend_signal': row.get('trend_signal'),
            'avg_pct_chg': row.get('avg_pct_chg'),
            'stock_count': row.get('stock_count'),
            'up_ratio': row.get('up_ratio'),
            'strong_ratio': row.get('strong_ratio'),
            'hot_stocks': row.get('hot_stocks') or [],
        })
    return result


def build_industry_actions(unified: dict, limit: int = 6) -> list[dict]:
    actions = list(unified.get('industry_actions', []) or [])
    action_rank = {'增配': 0, '观察': 1, '回避': 2}

    def rank_value(row: dict) -> tuple[int, float, float]:
        primary = action_rank.get(str(row.get('action') or '').strip(), 9)
        market_rank = safe_float(row.get('market_rank'), 999.0)
        strategy_rank = safe_float(row.get('strategy_rank'), 999.0)
        return primary, market_rank, strategy_rank

    actions = sorted(actions, key=rank_value)
    return [{
        'industry': row.get('industry'),
        'action': row.get('action'),
        'reason': row.get('reason'),
        'market_rank': row.get('market_rank'),
        'strategy_rank': row.get('strategy_rank'),
        'market_heat': row.get('market_heat'),
        'strategy_heat': row.get('strategy_heat'),
        'stock_layer_summary': row.get('stock_layer_summary'),
        'action_summary': row.get('action_summary'),
        'market_hot_stocks': row.get('market_hot_stocks') or [],
        'strategy_candidates': row.get('strategy_candidates') or [],
    } for row in actions[:limit]]


def build_recommended_stocks(industry_actions: list[dict], limit: int = 12) -> list[dict]:
    rows: list[dict] = []
    for action in industry_actions:
        for candidate in action.get('strategy_candidates') or []:
            rows.append({
                'industry': action.get('industry'),
                'action': action.get('action'),
                'source_pool': 'strategy_candidates',
                'stock_code': candidate.get('stock_code'),
                'ts_code': candidate.get('ts_code'),
                'stock_name': candidate.get('stock_name'),
                'ai_view': candidate.get('ai_view'),
                'ai_score': candidate.get('ai_score'),
                'ai_confidence': candidate.get('ai_confidence'),
                'recommend_price': candidate.get('recommend_price'),
            })
    rows = unique_rows(rows, ('ts_code', 'stock_code'))
    rows = sorted(
        rows,
        key=lambda r: (
            -safe_float(r.get('ai_score')),
            confidence_rank(r.get('ai_confidence')),
            str(r.get('stock_code') or ''),
        ),
    )
    return rows[:limit]


def build_recommended_stocks_from_detail(detail: dict, trade_date: str | None, limit: int = 12) -> list[dict]:
    source_rows = [row for row in (detail.get('rows', []) or []) if str(row.get('recommend_date') or '') == str(trade_date or '')]
    rows: list[dict] = []
    for row in source_rows:
        ai_view = str(row.get('ai_view') or '')
        ai_decision = str(row.get('ai_decision') or '').lower()
        if ai_view not in {'买入', '观望'} and ai_decision not in {'buy', 'hold'}:
            continue
        rows.append({
            'industry': row.get('sector_name'),
            'action': row.get('ai_view'),
            'source_pool': 'prebreakout_detail',
            'recommend_date': row.get('recommend_date'),
            'stock_code': row.get('stock_code'),
            'ts_code': row.get('ts_code'),
            'stock_name': row.get('stock_name'),
            'ai_view': row.get('ai_view'),
            'ai_score': row.get('ai_score'),
            'ai_confidence': row.get('ai_confidence'),
            'recommend_price': row.get('recommend_price'),
            'recommend_reason': row.get('recommend_reason'),
            'ai_summary': row.get('ai_summary'),
        })
    if not rows:
        for row in source_rows:
            rows.append({
                'industry': row.get('sector_name'),
                'action': row.get('ai_view'),
                'source_pool': 'prebreakout_detail_all',
                'recommend_date': row.get('recommend_date'),
                'stock_code': row.get('stock_code'),
                'ts_code': row.get('ts_code'),
                'stock_name': row.get('stock_name'),
                'ai_view': row.get('ai_view'),
                'ai_score': row.get('ai_score'),
                'ai_confidence': row.get('ai_confidence'),
                'recommend_price': row.get('recommend_price'),
                'recommend_reason': row.get('recommend_reason'),
                'ai_summary': row.get('ai_summary'),
            })
    rows = unique_rows(rows, ('ts_code', 'stock_code'))
    rows = sorted(
        rows,
        key=lambda r: (
            -safe_float(r.get('ai_score')),
            confidence_rank(r.get('ai_confidence')),
            str(r.get('stock_code') or ''),
        ),
    )
    return rows[:limit]


def load_fx_history() -> dict:
    if not FX_HISTORY.exists():
        return {}
    try:
        raw = json.loads(FX_HISTORY.read_text(encoding='utf-8'))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def fetch_cnh_snapshot_with_fallback() -> tuple[dict | None, str | None]:
    result, error = fetch_yfinance_snapshot('cnh', 'USD/CNH', 'USDCNH=X', source_kind='exact')
    if result is not None:
        return result, error

    today = datetime.now().strftime('%Y%m%d')
    history = load_fx_history()
    record = history.get(today, {}) if isinstance(history, dict) else {}
    current = record.get('cnh')
    source_error = error

    if current is None:
        try:
            with urlopen('https://open.er-api.com/v6/latest/USD', timeout=15) as r:
                payload = json.loads(r.read().decode('utf-8'))
            current = payload.get('rates', {}).get('CNH')
            if current is not None:
                current = round(float(current), 6)
        except Exception as exc:
            source_error = f'{source_error}; open.er-api.com: {type(exc).__name__}: {exc}' if source_error else f'open.er-api.com: {type(exc).__name__}: {exc}'

    prev = None
    prev_dates = sorted(k for k in history.keys() if isinstance(k, str) and k < today)
    if prev_dates:
        prev_record = history.get(prev_dates[-1], {}) or {}
        prev = prev_record.get('cnh') or prev_record.get('close')

    if current is None:
        return None, source_error or 'cnh unavailable'

    return build_snapshot(
        key='cnh',
        label='USD/CNH',
        symbol='USDCNH=X',
        source_kind='fx_api_fallback',
        provider='open.er-api.com',
        close=round(float(current), 6),
        prev_close=None if prev is None else round(float(prev), 6),
        as_of=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        source_error=source_error,
    ), source_error


def build_watchlist(detail: dict, limit: int = 10, trade_date: str | None = None, strategy_doc: dict | None = None) -> list[dict]:
    watch_date = latest_recommend_date(detail)
    rows = [r for r in detail.get('rows', []) if str(r.get('recommend_date') or '') == str(watch_date or '')]
    if trade_date and str(watch_date or '') != str(trade_date or ''):
        rows = []
    rows = sorted(
        rows,
        key=lambda r: (
            -safe_float(r.get('ai_score')),
            confidence_rank(r.get('ai_confidence')),
            str(r.get('stock_code') or ''),
        ),
    )
    result = []
    for row in rows[:limit]:
        result.append({
            'rank_no': row.get('rank_no'),
            'recommend_date': row.get('recommend_date'),
            'stock_code': row.get('stock_code'),
            'ts_code': row.get('ts_code'),
            'stock_name': row.get('stock_name'),
            'sector_name': row.get('sector_name'),
            'ai_view': row.get('ai_view'),
            'ai_score': row.get('ai_score'),
            'ai_confidence': row.get('ai_confidence'),
            'recommend_price': row.get('recommend_price'),
            'recommend_reason': row.get('recommend_reason'),
            'ai_summary': row.get('ai_summary'),
        })
    if result or not strategy_doc:
        return result
    strategy_date = latest_strategy_trade_date(strategy_doc)
    fallback_rows = strategy_top20(strategy_doc)
    fallback_rows = sorted(
        fallback_rows,
        key=lambda r: (
            -safe_float(r.get('score')),
            -safe_float(r.get('ai_score')),
            safe_float(r.get('rank'), 9999),
            str(r.get('code') or ''),
        ),
    )
    for idx, row in enumerate(fallback_rows[:limit], start=1):
        ts_code = row.get('ts_code') or row.get('code')
        stock_code = str(ts_code or '').split('.')[0]
        result.append({
            'rank_no': row.get('rank') or idx,
            'recommend_date': strategy_date,
            'stock_code': stock_code,
            'ts_code': ts_code,
            'stock_name': row.get('name') or row.get('stock_name'),
            'sector_name': row.get('industry_name') or row.get('sector_name'),
            'ai_view': row.get('ai_advice') or row.get('ai_view'),
            'ai_score': row.get('ai_score') or row.get('score'),
            'ai_confidence': row.get('ai_confidence'),
            'recommend_price': row.get('close') or row.get('recommend_price'),
            'recommend_reason': row.get('ai_points') or row.get('recommend_reason'),
            'ai_summary': row.get('ai_summary'),
            'source_pool': 'strategy_backtests_top20_fallback',
        })
    return result


def _settled_domestic(key: str, trade_date: str):
    def resolve() -> tuple[dict | None, str | None]:
        snap = domestic_index_close.resolve_settled_close(key, trade_date, pro=_tushare_pro(), ak_module=ak)
        return snap, None  # 结算收盘或显式 unavailable, 均为终态, 不再回退现价
    return resolve


def fetch_session_snapshot(trade_date: str | None = None, *, now: datetime | None = None) -> tuple[dict, list[str]]:
    snapshot: dict[str, dict] = {}
    warnings: list[str] = []
    # 域内三大指数: tushare index_daily 结算收盘【首选】(盘中无结算数据自动回退现价),
    # akshare 新浪现价降为回退(标 intraday_spot)。20260703事故修复。
    # 20261009修: 收盘后(交易日已结算)只取结算收盘, 禁止回退现价/yfinance; 取不到则标 unavailable。
    # 盘中 yfinance 回退取的是 5 分钟现价, 如实标 intraday_spot(原误标 exact)。
    if trade_date and domestic_index_close.is_post_close(str(trade_date), now):
        domestic = {key: [_settled_domestic(key, str(trade_date))] for key in ('shanghai', 'shenzhen', 'chinext')}
    else:
        domestic = {
            'shanghai': [
                lambda: fetch_tushare_domestic_index('shanghai', '上证指数', 'sh000001', trade_date),
                lambda: fetch_akshare_domestic_index('shanghai', '上证指数', 'sh000001', ['sh000001', '上证指数', '000001']),
                lambda: fetch_yfinance_snapshot('shanghai', '上证指数', '^SSEC', source_kind='intraday_spot'),
            ],
            'shenzhen': [
                lambda: fetch_tushare_domestic_index('shenzhen', '深证成指', 'sz399001', trade_date),
                lambda: fetch_akshare_domestic_index('shenzhen', '深证成指', 'sz399001', ['sz399001', '深证成指', '399001']),
                lambda: fetch_yfinance_snapshot('shenzhen', '深证成指', '^SZI', source_kind='intraday_spot'),
            ],
            'chinext': [
                lambda: fetch_tushare_domestic_index('chinext', '创业板指', 'sz399006', trade_date),
                lambda: fetch_akshare_domestic_index('chinext', '创业板指', 'sz399006', ['sz399006', '创业板指', '399006']),
                lambda: fetch_yfinance_snapshot('chinext', '创业板指', '^CN100', source_kind='intraday_spot'),
            ],
        }
    attempts = {
        **domestic,
        'a50': [
            lambda: fetch_akshare_global_index('a50', '富时A50实时', 'XIN9.FS', ['XIN9.FS', 'XIN9', '富时中国A50', 'A50']),
            lambda: fetch_yfinance_snapshot('a50', '富时A50夜盘', 'XIN9.FGI', source_kind='exact'),
            lambda: fetch_yfinance_snapshot('a50', '富时A50参考代理', 'ASHR', source_kind='proxy'),
            lambda: fetch_yfinance_snapshot('a50', '富时A50参考代理', 'FXI', source_kind='proxy'),
        ],
        'golden_dragon': [
            lambda: fetch_yfinance_snapshot('golden_dragon', '纳斯达克中国金龙指数', '^HXC', source_kind='exact'),
            lambda: fetch_yfinance_snapshot('golden_dragon', '金龙参考代理', 'KWEB', source_kind='proxy'),
            lambda: fetch_yfinance_snapshot('golden_dragon', '金龙参考代理', 'CQQQ', source_kind='proxy'),
            lambda: fetch_yfinance_snapshot('golden_dragon', '金龙参考代理', 'PGJ', source_kind='proxy'),
        ],
        'spx': [
            lambda: fetch_yfinance_snapshot('spx', '标普500', '^GSPC', source_kind='exact'),
        ],
        'nasdaq': [
            lambda: fetch_yfinance_snapshot('nasdaq', '纳斯达克', '^IXIC', source_kind='exact'),
        ],
        'cnh': [
            lambda: fetch_cnh_snapshot_with_fallback(),
        ],
    }

    for key, funcs in attempts.items():
        last_error = None
        for func in funcs:
            try:
                result, error = func()
            except Exception as exc:
                result, error = None, f'{type(exc).__name__}: {exc}'
            if result is not None:
                snapshot[key] = result
                last_error = None
                break
            last_error = error
        if key not in snapshot:
            fallback_label = {
                'shanghai': '上证指数',
                'shenzhen': '深证成指',
                'chinext': '创业板指',
                'a50': '富时A50实时',
                'golden_dragon': '纳斯达克中国金龙指数',
                'spx': '标普500',
                'nasdaq': '纳斯达克',
                'cnh': 'USD/CNH',
            }[key]
            fallback_symbol = {
                'shanghai': 'sh000001',
                'shenzhen': 'sz399001',
                'chinext': 'sz399006',
                'a50': 'XIN9.FS',
                'golden_dragon': '^HXC',
                'spx': '^GSPC',
                'nasdaq': '^IXIC',
                'cnh': 'USDCNH=X',
            }[key]
            snapshot[key] = build_snapshot(
                key=key,
                label=fallback_label,
                symbol=fallback_symbol,
                source_kind='unavailable' if key != 'a50' else 'fallback',
                provider='unavailable',
                close=None,
                prev_close=None,
                as_of=None,
                source_error=last_error,
            )
            warnings.append(f'{key}: {last_error}')
    return snapshot, warnings


# ---------------------------------------------------------------------------
# Stub interfaces for midday volume-ratio and fund-flow (Phase 3 placeholder)
# ---------------------------------------------------------------------------

def fetch_midday_volume_ratio(codes: list[str]) -> dict[str, float | None]:
    """Fetch half-day volume ratio for given stock codes.

    Returns {code: volume_ratio} where volume_ratio is the morning session
    turnover divided by the same-period average of the past 5 days.
    Returns None for codes whose data is unavailable.

    TODO: implement with akshare real-time volume data once stable source
    is identified (e.g. ak.stock_zh_a_tick_163 or tushare minute bars).
    """
    return {code: None for code in codes}


def fetch_midday_fund_flow(codes: list[str]) -> dict[str, dict | None]:
    """Fetch half-day main-force fund flow for given stock codes.

    Returns {code: {"net_inflow": float, "big_order_ratio": float} | None}.
    net_inflow is in units of 万元 (10k CNY).
    big_order_ratio = big_buy / (big_buy + big_sell).

    TODO: implement with akshare ak.stock_individual_fund_flow or
    tushare moneyflow once intraday endpoint is verified.
    """
    return {code: None for code in codes}


def run_state_generator() -> tuple[bool, str]:
    python_exec = str(PREFERRED_PYTHON if 'PREFERRED_PYTHON' in globals() and PREFERRED_PYTHON.exists() else Path(sys.executable))
    proc = __import__('subprocess').run([python_exec, str(STATE_GENERATOR)], cwd=PUBLISHED_REPO.parent, capture_output=True, text=True)
    output = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
    return proc.returncode == 0, output


def latest_strategy_trade_date(strategy_doc: dict) -> str | None:
    return str(strategy_doc.get('latest_trade_date') or '').strip() or None


def strategy_top20(strategy_doc: dict) -> list[dict]:
    strategies = strategy_doc.get('strategies') or []
    for item in strategies:
        if str(item.get('strategy_id') or item.get('id') or item.get('name') or '') in {
            'prebreakout_v41', 'prebreakout_v43_control', '启动前夕 v4.1', '启动前夕 v4.3 对照'
        }:
            rows = item.get('top20') or item.get('stocks') or []
            return rows if isinstance(rows, list) else []
    if strategies:
        rows = strategies[0].get('top20') or strategies[0].get('stocks') or []
        return rows if isinstance(rows, list) else []
    return []


def assert_midday_freshness(trade_date: str, morning: dict, market: dict, unified: dict, detail_trade_date: str | None, strategy_doc: dict | None = None) -> str | None:
    """Raise on incoherent market inputs; return the selection date when only it lags."""
    strategy_doc = strategy_doc or {}
    strategy_date = latest_strategy_trade_date(strategy_doc)
    watchlist_date = detail_trade_date if detail_trade_date == trade_date else strategy_date
    session_date = datetime.now().strftime('%Y%m%d')
    source_dates = {
        'morning_trade_date': str(morning.get('trade_date') or ''),
        'market_latest_trade_date': str(market.get('latest_trade_date') or ''),
        'strategy_latest_recommend_date': str((unified.get('strategy_heatmap') or {}).get('latest_recommend_date') or strategy_date or ''),
        'watchlist_recommend_date': str(watchlist_date or ''),
    }
    # 如果当前日期就是 trade_date（即午盘当天），允许上游数据仍是前一交易日（尚未刷新）。
    # 此时只检查 watchlist 是否至少跟得上 morning/market；如果 trade_date 不是今天，
    # 则严格要求所有 source_dates 一致。
    if session_date == str(trade_date or ''):
        upstream_dates = {
            'morning_trade_date': source_dates['morning_trade_date'],
            'market_latest_trade_date': source_dates['market_latest_trade_date'],
            'strategy_latest_recommend_date': source_dates['strategy_latest_recommend_date'],
        }
        non_empty = {k: v for k, v in upstream_dates.items() if v}
        market_side = {v for k, v in non_empty.items() if k != 'strategy_latest_recommend_date'}
        strategy_date_seen = non_empty.get('strategy_latest_recommend_date')
        # 2026-10-23: a failed evening chain leaves only the selection behind;
        # the market analysis is still coherent, so degrade instead of aborting.
        if len(market_side) == 1 and strategy_date_seen and strategy_date_seen < next(iter(market_side)):
            return strategy_date_seen
        if non_empty and len(set(non_empty.values())) != 1:
            raise RuntimeError(
                f'DATA_STALE: upstream source dates disagree on session date={trade_date}: {non_empty}'
            )
    else:
        stale = {key: value for key, value in source_dates.items() if value != str(trade_date or '')}
        if stale:
            raise RuntimeError(f'DATA_STALE: midday source dates mismatch trade_date={trade_date}: {stale}')
    return None


def publish_midday_files(trade_date: str) -> dict:
    result = {'published': False}
    repo_state = git_status(PUBLISHED_REPO)
    result['pre_publish_repo_state'] = repo_state
    if not (repo_state.get('clean') or repo_state.get('allowed_output_changes_only')):
        result['error'] = 'published repo dirty before midday publish'
        return result

    remote_sync = sync_git_tracking_branch(PUBLISHED_REPO)
    result['remote_sync'] = remote_sync
    if not remote_sync.get('ok'):
        result['error'] = remote_sync.get('error') or 'git pull --rebase failed'
        return result

    tracked_targets = [
        'data/recommendation_analytics/midday_analysis_latest.json',
        'data/latest/run_manifest.json',
        'data/latest/market_state.json',
        'data/latest/strategy_state.json',
        'data/latest/candidate_state.json',
        'data/latest/review_state.json',
        'data/latest/research_state.json',
    ]
    status_proc = run_git(['status', '--short', '--', *tracked_targets], cwd=PUBLISHED_REPO)
    result['git_status_returncode'] = status_proc.returncode
    if status_proc.returncode != 0:
        result['error'] = 'git status failed'
        return result
    changed = []
    for line in (status_proc.stdout or '').splitlines():
        if len(line) >= 4:
            changed.append(line[3:].strip())
    if not changed:
        result['published'] = True
        result['changed'] = []
        result['commit'] = 'unchanged'
        return result

    if not V2_PUBLISH_SCRIPT.exists():
        result['error'] = f'v2 publisher missing: {V2_PUBLISH_SCRIPT}'
        return result
    proc = subprocess.run(
        ['bash', str(V2_PUBLISH_SCRIPT)],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=300,
    )
    result['publisher'] = 'publish_stock_report_v2.sh'
    result['publisher_returncode'] = proc.returncode
    result['publisher_stdout_tail'] = '\n'.join((proc.stdout or '').splitlines()[-20:])
    result['publisher_stderr_tail'] = '\n'.join((proc.stderr or '').splitlines()[-20:])
    if proc.returncode != 0:
        result['error'] = 'v2 publisher failed'
        return result
    result['changed'] = changed
    result['published'] = True
    return result


def build_midday_advice(
    morning: dict,
    hot_sectors: list[dict],
    watchlist: list[dict],
    session_snapshot: dict,
) -> str:
    regime = str(morning.get('regime') or '中性')
    focus_names = [s.get('industry_name') for s in hot_sectors[:3] if s.get('industry_name')]
    focus_text = '、'.join(focus_names) if focus_names else '主线行业'
    watch_names = [r.get('stock_name') for r in watchlist[:3] if r.get('stock_name')]
    watch_text = '、'.join(watch_names) if watch_names else '启动前夕候选'
    snapshot_lines = []
    for key in ('shanghai', 'shenzhen', 'chinext', 'spx', 'nasdaq', 'cnh'):
        item = session_snapshot.get(key) or {}
        if item.get('change_pct') is not None:
            snapshot_lines.append(f"{item.get('label') or key} {item['change_pct']:+.2f}%")
    snapshot_text = '；'.join(snapshot_lines[:4]) if snapshot_lines else '盘中快照暂不可用'

    if regime == '高风险':
        return f'午盘盘中快照显示 {snapshot_text}。继续以防守为主，优先确认指数与防御板块是否稳住；启动前夕重点只看 {watch_text} 是否守住关键位，不做追高。'
    if regime == '谨慎':
        return f'午盘盘中快照显示 {snapshot_text}。以观察和择强为主，先看 {focus_text} 是否延续热度；启动前夕候选只关注 {watch_text} 的承接、回踩和失效位，不追高。'
    if regime == '偏积极':
        return f'午盘盘中快照显示 {snapshot_text}。可围绕 {focus_text} 的龙头和扩散股继续跟踪；启动前夕候选以 {watch_text} 为主，重点看分时承接、量能和回踩是否满足介入条件。'
    return f'午盘盘中快照显示 {snapshot_text}。维持中性偏积极的跟踪节奏，先看 {focus_text} 是否形成明确主线，再从 {watch_text} 中挑选确认度更高的品种。'


def main() -> int:
    morning = load_json(MORNING)
    market = load_json(MARKET)
    unified = load_json(UNIFIED)
    detail = load_json(DETAIL)
    strategy_doc = load_json(STRATEGY_JSON)
    # 午盘必须使用当前交易日日期。
    # 原逻辑优先取 morning_brief.trade_date，而晨判是昨晚生成、记录的是前一交易日，
    # 导致午盘 trade_date 错误地沿用前一交易日。
    session_trade_date = datetime.now().strftime('%Y%m%d')
    # 2026-10-03: the wall-clock date is only a candidate session.  It must be
    # a confirmed open exchange day; a holiday or unusable calendar is an error,
    # never a reason to relabel older data as a new session.
    exchange_calendar.require_open_session(session_trade_date)
    trade_date = resolve_effective_trade_date(
        session_trade_date,
        morning.get('trade_date'),
        market.get('latest_trade_date'),
        unified.get('morning_brief', {}).get('trade_date'),
        fallback_now=True,
    )
    # 域内指数快照按已解析的交易日取 tushare 结算收盘(收盘后跑=真收盘;盘中=回退现价)。
    session_snapshot, session_warnings = fetch_session_snapshot(trade_date)
    as_of_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    detail_trade_date = latest_recommend_date(detail)
    strategy_trade_date = latest_strategy_trade_date(strategy_doc)
    strategy_lagging = assert_midday_freshness(trade_date, morning, market, unified, detail_trade_date, strategy_doc)

    hot_sectors = build_hot_sectors(market, limit=6)
    industry_actions = build_industry_actions(unified, limit=6)
    recommended_stocks = []
    if strategy_lagging:
        # Never show an old selection as today's watchlist.
        watchlist = []
        session_warnings.append(f'选股结果仍停在 {strategy_lagging}（前一晚主管线未完成），本次午盘不展示候选。')
    else:
        watchlist = build_watchlist(detail, limit=10, trade_date=trade_date, strategy_doc=strategy_doc)
    advice = build_midday_advice(morning, hot_sectors, watchlist, session_snapshot)

    market_view_midday = {
        'regime': morning.get('regime'),
        'risk_score': morning.get('risk_score'),
        'summary': morning.get('ai_summary'),
        'action_advice': morning.get('ai_action_advice'),
        'focus_sectors': (morning.get('focus_sectors') or [])[:5],
        'key_drivers': (morning.get('key_drivers') or [])[:3],
        'opening_playbook': (morning.get('opening_playbook') or [])[:3],
        'note': '午盘剖析优先使用 AkShare 实时指数快照，并保留 yfinance 兜底；A50 与金龙按真实源优先、代理备份。',
    }

    payload = {
        'generated_at': as_of_time,
        'session_trade_date': session_trade_date,
        'trade_date': trade_date,
        'as_of_time': as_of_time,
        'source_dates': {
            'session_trade_date': session_trade_date,
            'morning_trade_date': morning.get('trade_date'),
            'market_latest_trade_date': market.get('latest_trade_date'),
            'strategy_latest_recommend_date': unified.get('strategy_heatmap', {}).get('latest_recommend_date'),
            'watchlist_recommend_date': detail_trade_date if detail_trade_date == trade_date else strategy_trade_date,
            'detail_recommend_date': detail_trade_date,
            'strategy_trade_date': strategy_trade_date,
            'strategy_lagging_trade_date': strategy_lagging,
        },
        'market_view_midday': market_view_midday,
        'session_snapshot': session_snapshot,
        'hot_sectors_topN': hot_sectors,
        'industry_actions_topN': industry_actions,
        'recommended_stocks_topN': recommended_stocks,
        'prebreakout_watchlist_topN': watchlist,
        'midday_action_advice': advice,
        'session_warnings': session_warnings,
        'notes': [
            '午盘剖析不再输出推荐个股列表；启动前夕候选优先使用同交易日 prebreakout 明细，明细滞后时回退到 strategy_backtests top20。',
            '指数层仅保留对盘面判断真正有帮助的来源，不再依赖 A50 代理作为主参考。',
            '如果后续补入更细的分时板块和成交结构，可升级为更强的午盘热度引擎。',
        ],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    PUBLISHED_OUT.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    OUT.write_text(encoded, encoding='utf-8')
    PUBLISHED_OUT.write_text(encoded, encoding='utf-8')

    state_ok, state_output = run_state_generator()
    publish_result = None
    if state_ok:
        publish_result = publish_midday_files(trade_date)

    print(f'created={OUT}')
    print(f'published_copy={PUBLISHED_OUT}')
    print(json.dumps({
        'trade_date': trade_date,
        'as_of_time': as_of_time,
        'regime': market_view_midday['regime'],
        'risk_score': market_view_midday['risk_score'],
        'detail_trade_date': detail_trade_date,
        'strategy_trade_date': strategy_trade_date,
        'hot_sectors': [row.get('industry_name') for row in hot_sectors[:3]],
        'recommended_stocks': [row.get('stock_name') for row in recommended_stocks[:3]],
        'watchlist': [row.get('stock_name') for row in watchlist[:3]],
        'state_generator_ok': state_ok,
        'state_generator_output': state_output[-800:] if state_output else '',
        'publish_result': publish_result,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
