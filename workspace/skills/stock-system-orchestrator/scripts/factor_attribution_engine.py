#!/usr/bin/env python3
"""Historical replay engine for factor IC attribution (diagnostic_only)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import factor_attribution as fa
import short_track_shadow as sts

try:
    import pit_market_snapshot as pms
except Exception:  # pragma: no cover
    pms = None


def _workspace() -> Path:
    return Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path(__file__).resolve().parents[3]))).resolve()


def discover_stk_factor_dates(cache_dir: Path) -> list[str]:
    return sorted(
        p.stem.replace("stk_factor_", "")
        for p in cache_dir.glob("stk_factor_*.parquet")
        if p.stem.startswith("stk_factor_")
    )


def _local_daily_basic_paths(workspace: Path, day: str, attr_cache: Path) -> list[Path]:
    return [
        workspace / f"stock_data/03-working/strategy_research/short_track/materialized/daily_basic/daily_basic_{day}.parquet",
        # Same-night full-field downloads by the shortTrackGeneration stage.
        workspace / f"stock_data/03-working/strategy_research/short_track/input_cache/daily_basic_{day}.parquet",
        workspace / f"stock_data/03-working/fundamental_cache/pit_market/daily_basic_{day}.parquet",
        workspace
        / f"stock_data/03-working/strategy_research/event_quality_drift_v1/materialized/valuation/daily_basic_{day}.parquet",
        attr_cache / f"daily_basic_{day}.parquet",
    ]


def _local_universe_paths(workspace: Path, day: str, attr_cache: Path) -> list[Path]:
    return [
        workspace
        / f"stock_data/03-working/strategy_research/short_track/materialized/pit_universe/universe_{day}.parquet",
        workspace / f"stock_data/03-working/fundamental_cache/pit_market/universe_{day}.parquet",
        attr_cache / f"universe_{day}.parquet",
    ]


def _normalize_daily_basic(df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    out = df.copy()
    out["trade_date"] = str(trade_date)
    if "ts_code" in out.columns:
        out["ts_code"] = out["ts_code"].astype(str)
    for col in ("turnover_rate", "circ_mv", "total_mv"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out["used_proxy"] = False
    out["completeness"] = "complete"
    return out


class PitFetchSession:
    """Session-scoped PIT fetch with static SW2021/stock_basic reuse and retries."""

    def __init__(self, client: Any, attr_cache: Path):
        self.client = client
        self.attr_cache = attr_cache
        self.attr_cache.mkdir(parents=True, exist_ok=True)
        self._stock_dim: pd.DataFrame | None = None
        self._sw_map_cache: dict[str, dict[str, str | None]] = {}
        self._index_classify: pd.DataFrame | None = None
        self._index_member_all: pd.DataFrame | None = None
        # Negative lookups only: never cache scores, panels or successful data
        # here. Recheck local sources first on every call, and retry the remote
        # after five minutes or immediately in the next research session.
        self._daily_basic_retry_after: dict[str, float] = {}

    def _retry(self, fn, *, attempts: int = 4, sleep_s: float = 0.8):
        last = None
        for i in range(attempts):
            try:
                return fn()
            except Exception as exc:  # pragma: no cover
                last = exc
                time.sleep(sleep_s * (i + 1))
        if last is not None:
            raise last
        return None

    def load_daily_basic(self, trade_date: str, paths: list[Path]) -> pd.DataFrame | None:
        for path in paths:
            if path.exists():
                result = _normalize_daily_basic(pd.read_parquet(path), trade_date)
                self._daily_basic_retry_after.pop(trade_date, None)
                return result
        cached = self.attr_cache / f"daily_basic_{trade_date}.parquet"
        if cached.exists():
            result = _normalize_daily_basic(pd.read_parquet(cached), trade_date)
            self._daily_basic_retry_after.pop(trade_date, None)
            return result
        if self.client is None:
            return None

        if time.monotonic() < self._daily_basic_retry_after.get(trade_date, 0):
            return None

        def _call():
            # fields=None: the provider rejects narrowed projections that drop
            # its required daily_basic columns (close/pe_ttm/pb), after ~30s per
            # provider. A narrow request therefore never succeeded (2026-10-13 timeout).
            df = self.client.daily_basic(trade_date=trade_date, fields=None)
            if df is None or len(df) == 0:
                raise RuntimeError(f"empty daily_basic {trade_date}")
            return df

        try:
            df = self._retry(_call)
        except Exception:
            self._daily_basic_retry_after[trade_date] = time.monotonic() + 300
            return None
        self._daily_basic_retry_after.pop(trade_date, None)
        out = _normalize_daily_basic(df, trade_date)
        out.to_parquet(cached, index=False)
        return out

    def _ensure_static_dim(self) -> bool:
        if self._stock_dim is not None and self._index_member_all is not None:
            return True
        if self.client is None or pms is None:
            return False
        static_path = self.attr_cache / "pit_static_bundle.parquet"
        mem_path = self.attr_cache / "sw2021_member_all.parquet"
        cls_path = self.attr_cache / "sw2021_index_classify.parquet"
        try:
            if self._stock_dim is None:
                if static_path.exists():
                    self._stock_dim = pd.read_parquet(static_path)
                else:
                    frames = {
                        status: pms._normalize_stock_basic(
                            pms._empty_or_frame(
                                self._retry(lambda s=status: self.client.stock_basic(list_status=s, fields=None)),
                                f"stock_basic[{status}]",
                            ),
                            status,
                        )
                        for status in ("L", "D", "P")
                    }
                    stock_dim = pd.concat(frames.values(), ignore_index=True)
                    stock_dim = (
                        stock_dim.sort_values(["ts_code", "current_list_status"])
                        .drop_duplicates("ts_code", keep="first")
                        .reset_index(drop=True)
                    )
                    stock_dim.to_parquet(static_path, index=False)
                    self._stock_dim = stock_dim
            if self._index_classify is None:
                if cls_path.exists():
                    self._index_classify = pd.read_parquet(cls_path)
                else:
                    self._index_classify = pms._empty_or_frame(
                        self._retry(lambda: self.client.index_classify(src="SW2021", level="L1")),
                        "index_classify",
                    )
                    self._index_classify.to_parquet(cls_path, index=False)
            if self._index_member_all is None:
                if mem_path.exists():
                    self._index_member_all = pd.read_parquet(mem_path)
                else:
                    self._index_member_all = pms._fetch_sw2021_membership(self.client, self._index_classify)
                    self._index_member_all.to_parquet(mem_path, index=False)
            return True
        except Exception:
            return False

    def load_universe(self, trade_date: str, paths: list[Path]) -> pd.DataFrame | None:
        for path in paths:
            if path.exists():
                return pd.read_parquet(path)
        cached = self.attr_cache / f"universe_{trade_date}.parquet"
        if cached.exists():
            return pd.read_parquet(cached)
        if self.client is None or pms is None:
            return None
        # Prefer lightweight assembly from cached static + day-specific ST/suspend/daily_basic.
        uni = self._assemble_universe(trade_date)
        if uni is not None:
            uni.to_parquet(cached, index=False)
            return uni
        # Fallback: full snapshot (slow; also caches daily_basic).
        try:
            snap = self._retry(lambda: pms.collect_pit_market_snapshot(self.client, trade_date), attempts=2, sleep_s=1.5)
            uni = snap["universe"]
            uni.to_parquet(cached, index=False)
            db = snap.get("daily_basic")
            if isinstance(db, pd.DataFrame) and not db.empty:
                db_path = self.attr_cache / f"daily_basic_{trade_date}.parquet"
                if not db_path.exists():
                    _normalize_daily_basic(db, trade_date).to_parquet(db_path, index=False)
            return uni
        except Exception:
            return None

    def _assemble_universe(self, trade_date: str) -> pd.DataFrame | None:
        if not self._ensure_static_dim():
            return None
        assert self._stock_dim is not None
        assert self._index_classify is not None
        assert self._index_member_all is not None
        try:
            stock_st = pms._empty_or_frame(
                self._retry(lambda: self.client.stock_st(trade_date=trade_date)),
                "stock_st",
            )
            suspend_d = pms._empty_or_frame(
                self._retry(lambda: self.client.suspend_d(trade_date=trade_date)),
                "suspend_d",
            )
            daily_basic = self.load_daily_basic(trade_date, [self.attr_cache / f"daily_basic_{trade_date}.parquet"])
            if daily_basic is None or daily_basic.empty:
                raise RuntimeError("daily_basic required for universe")
        except Exception:
            return None

        stock_dim = self._stock_dim.copy()
        stock_dim["trade_date"] = trade_date
        stock_dim["listing_days"] = (
            pd.to_datetime(trade_date) - pd.to_datetime(stock_dim["list_date"], format="%Y%m%d", errors="coerce")
        ).dt.days
        # Match pit_market_snapshot list/delist reconstruction (no back-projection of current status).
        list_date = stock_dim["list_date"].fillna("").astype(str)
        delist_date = stock_dim["delist_date"].fillna("").astype(str)
        listed_by_date = list_date.str.fullmatch(r"\d{8}") & (list_date <= trade_date)
        delisted_by_date = delist_date.str.fullmatch(r"\d{8}") & (delist_date <= trade_date)
        stock_dim["list_status_at_date"] = np.where(
            ~listed_by_date,
            "not_listed",
            np.where(delisted_by_date, "D", "L"),
        )
        stock_dim["list_status"] = stock_dim["list_status_at_date"]

        st_codes = set(stock_st.get("ts_code", pd.Series(dtype=str)).astype(str))
        sus_codes = set(suspend_d.get("ts_code", pd.Series(dtype=str)).astype(str))
        stock_dim["is_st"] = stock_dim["ts_code"].astype(str).isin(st_codes)
        stock_dim["is_suspended"] = stock_dim["ts_code"].astype(str).isin(sus_codes)

        if trade_date not in self._sw_map_cache:
            self._sw_map_cache[trade_date] = pms._active_sw2021_membership(
                self._index_classify, self._index_member_all, trade_date
            )
        sw_map = self._sw_map_cache[trade_date]
        stock_dim["sw2021_l1_name"] = stock_dim["ts_code"].astype(str).map(sw_map)
        stock_dim["sw2021_l1_name"] = stock_dim["sw2021_l1_name"].astype(object).where(
            stock_dim["sw2021_l1_name"].notna(), None
        )
        if "industry_name" not in stock_dim.columns:
            stock_dim["industry_name"] = ""
        stock_dim["industry_name"] = stock_dim["industry_name"].fillna("").astype(str)
        # Prefer SW L1 as industry_name when blank (production uses both fields)
        blank = stock_dim["industry_name"].eq("") | stock_dim["industry_name"].eq("nan")
        stock_dim.loc[blank, "industry_name"] = stock_dim.loc[blank, "sw2021_l1_name"].fillna("").astype(str)

        daily_codes = set(daily_basic["ts_code"].astype(str))
        stock_dim["has_daily_basic"] = stock_dim["ts_code"].astype(str).isin(daily_codes)
        stock_dim["universe_flag"] = (
            (stock_dim["list_status_at_date"].astype(str) == "L")
            & (~stock_dim["is_st"])
            & (~stock_dim["is_suspended"])
            & (stock_dim["listing_days"].fillna(-1) >= 0)
            & stock_dim["has_daily_basic"]
        ).astype(int)
        stock_dim["tradable"] = stock_dim["universe_flag"]
        stock_dim["avg_amount_lookback"] = np.nan
        stock_dim["trading_ratio_lookback"] = np.nan
        stock_dim["liquidity_threshold"] = np.nan
        for col in pms.META_COLUMNS:
            stock_dim[col] = False if col == "used_proxy" else ("complete" if col == "completeness" else "tushare_pit")
        stock_dim["provenance"] = "factor_attr_session_static+stock_st+suspend_d+SW2021"
        keep = [
            "trade_date",
            "ts_code",
            "name",
            "avg_amount_lookback",
            "trading_ratio_lookback",
            "listing_days",
            "liquidity_threshold",
            "universe_flag",
            "list_status",
            "list_status_at_date",
            "is_st",
            "is_suspended",
            "sw2021_l1_name",
            "industry_name",
            "used_proxy",
            "completeness",
            "source",
            "provenance",
        ]
        return stock_dim[keep].sort_values("ts_code").reset_index(drop=True)

    def prefetch_daily_basic(self, workspace: Path, days: list[str]) -> dict[str, int]:
        ok = miss = 0
        for day in days:
            df = self.load_daily_basic(day, _local_daily_basic_paths(workspace, day, self.attr_cache))
            if df is None:
                miss += 1
            else:
                ok += 1
        return {"ok": ok, "miss": miss}


def build_day_factor_panel(
    *,
    trade_date: str,
    calendar: list[str],
    price_history: pd.DataFrame,
    workspace: Path,
    session: PitFetchSession | None,
    attr_cache: Path,
) -> tuple[pd.DataFrame | None, str | None]:
    """Return (panel, skip_reason). panel has ts_code + factor columns."""
    if trade_date not in calendar:
        return None, "signal_date_not_in_calendar"
    idx = calendar.index(trade_date)
    if idx < 20:
        return None, "insufficient_price_history_window"
    hist_dates = calendar[idx - 20 : idx + 1]
    prices = fa.slice_prices_for_signal(price_history, trade_date=trade_date, calendar=calendar, lookback_sessions=20)
    if prices.empty:
        return None, "missing_stk_factor_history"
    present = set(prices["trade_date"].astype(str))
    if str(trade_date) not in present:
        return None, f"missing_stk_factor:{trade_date}"

    basics = []
    for day in hist_dates:
        day_paths = _local_daily_basic_paths(workspace, day, attr_cache)
        if session is not None:
            df = session.load_daily_basic(day, day_paths)
        else:
            df = None
            for path in day_paths:
                if path.exists():
                    df = _normalize_daily_basic(pd.read_parquet(path), day)
                    break
        if df is None:
            return None, f"missing_daily_basic:{day}"
        basics.append(df)
    daily_basic_history = pd.concat(basics, ignore_index=True)
    uni_paths = _local_universe_paths(workspace, trade_date, attr_cache)
    if session is not None:
        universe = session.load_universe(trade_date, uni_paths)
    else:
        universe = None
        for path in uni_paths:
            if path.exists():
                universe = pd.read_parquet(path)
                break
    if universe is None:
        return None, "missing_pit_universe"

    prices = prices.copy()
    prices["used_proxy"] = False
    prices["completeness"] = "complete"
    daily_basic_history = daily_basic_history.copy()
    daily_basic_history["used_proxy"] = False
    daily_basic_history["completeness"] = "complete"

    try:
        features = sts.build_balanced_feature_frame(
            price_history=prices,
            daily_basic_history=daily_basic_history,
            pit_universe=universe,
            trade_date=trade_date,
        )
        validated = sts._require_balanced_frame(features, trade_date=trade_date)
        panel = fa.daily_factor_panel_from_frame(validated)
        try:
            v45 = fa.daily_v45_panel_from_frame(validated)
            panel = panel.merge(v45, on="ts_code", how="left")
        except Exception as exc:
            panel["v45_skip_reason"] = f"{type(exc).__name__}:{exc}"
        panel["trade_date"] = trade_date
        return panel, None
    except sts.ShortTrackInputError as exc:
        return None, f"balanced_frame_failed:{exc}"
    except Exception as exc:  # pragma: no cover
        return None, f"unexpected:{type(exc).__name__}:{exc}"


def _load_official_calendar(workspace: Path, *, as_of: str | None, override: list[str] | None) -> list[str]:
    if override:
        dates = [str(d) for d in override]
    else:
        from trading_calendar_store import load_open_trade_dates

        dates = load_open_trade_dates(workspace / "stock_data/03-working/health/trading_calendar.json")
    dates = [d for d in dates if len(d) == 8 and d.isdigit()]
    dates = sorted(dict.fromkeys(dates))
    if as_of and as_of != "latest":
        dates = [d for d in dates if d <= as_of]
    if not dates:
        raise fa.AttributionError("official trading calendar is empty or missing")
    return dates


def run_attribution(
    *,
    workspace: Path | None = None,
    as_of: str | None = None,
    refresh_days: int | None = None,
    max_days: int | None = None,
    fetch_missing: bool = True,
    official_calendar: list[str] | None = None,
    write_latest: bool = True,
) -> dict[str, Any]:
    workspace = Path(workspace or _workspace()).resolve()
    paths = fa.build_paths(workspace)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.cache.mkdir(parents=True, exist_ok=True)
    fa.seed_cache_from_previous_config(paths)

    cache_dir = workspace / "stock_data/03-working/backtest_cache"
    stk_dates = discover_stk_factor_dates(cache_dir)
    if not stk_dates:
        raise fa.AttributionError("no stk_factor cache dates")
    if as_of and as_of != "latest":
        stk_dates = [d for d in stk_dates if d <= as_of]
    as_of_date = stk_dates[-1]

    calendar = _load_official_calendar(workspace, as_of=as_of_date if as_of == "latest" or not as_of else as_of, override=official_calendar)
    # Keep official sessions inside the price-cache span so T+N cannot jump to a later file.
    calendar = [d for d in calendar if stk_dates[0] <= d <= stk_dates[-1]]
    if len(calendar) < 22:
        raise fa.AttributionError("official calendar too short inside stk_factor span")
    missing_stk_dates = [d for d in calendar if d not in set(stk_dates)]

    # 21-session lookback on the official calendar (not on the file-name calendar).
    signal_universe = calendar[20:]
    # refresh_days/max_days only control which day *panels* to rebuild — never the IC universe.
    # None = rebuild all panels; 0 = fill missing panels only; N>0 = last N plus missing.
    refresh_window = refresh_days if refresh_days is not None else max_days

    client = None
    if fetch_missing:
        try:
            import sys

            sys.path.insert(0, str(workspace / "skills/stock-analyzer"))
            import pipeline as pl

            client = pl.init_tushare()
        except Exception:
            client = None

    attr_cache = paths.cache / "pit_fetch"
    attr_cache.mkdir(parents=True, exist_ok=True)
    # Session always constructed so local-path loads work even without client.
    session = PitFetchSession(client, attr_cache)

    day_cache_dir = paths.cache / "day_panels"
    day_cache_dir.mkdir(parents=True, exist_ok=True)
    existing_panels = {p.stem.replace("panel_", "") for p in day_cache_dir.glob("panel_*.parquet")}
    # First run for this config hash: refresh the whole universe so latest is never a 15-day stub.
    if not existing_panels:
        refresh_set = set(signal_universe)
    elif refresh_window is None:
        refresh_set = set(signal_universe)
    elif int(refresh_window) <= 0:
        refresh_set = {d for d in signal_universe if d not in existing_panels}
    else:
        refresh_set = set(signal_universe[-int(refresh_window) :])
        refresh_set.update(d for d in signal_universe if d not in existing_panels)

    # Prefetch daily_basic for refresh window only (fail-soft).
    if fetch_missing and client is not None and refresh_set:
        first_refresh = min(refresh_set)
        first_idx = calendar.index(first_refresh) if first_refresh in calendar else 0
        session.prefetch_daily_basic(workspace, calendar[max(0, first_idx - 20) :])

    # Load full price panel once (only needed dates)
    needed_price_dates = calendar  # keep simple
    prices = fa.load_price_panel(cache_dir, needed_price_dates)

    v44_names = fa.FACTOR_NAMES + ["composite", "composite_reversed"]
    v45_names = fa.V45_FACTOR_NAMES + ["v45_composite", "v45_composite_reversed"]
    v43_names = ["v43_composite", "v43_composite_reversed"]
    all_score_names = v44_names + v45_names + v43_names
    ic_store: dict[str, dict[int, list[float]]] = {name: {h: [] for h in fa.HORIZONS} for name in all_score_names}
    spread_store: dict[str, dict[int, list[float]]] = {name: {h: [] for h in fa.HORIZONS} for name in all_score_names}
    mono_store: dict[str, dict[int, list[float]]] = {
        name: {h: [] for h in fa.HORIZONS} for name in fa.FACTOR_NAMES + ["composite"] + fa.V45_FACTOR_NAMES + ["v45_composite", "v43_composite"]
    }
    decile_store: dict[str, dict[int, list[list[float]]]] = {name: {h: [] for h in fa.HORIZONS} for name in all_score_names}
    corr_mats: list[np.ndarray] = []
    composite_corr: list[list[float]] = []
    skipped: list[dict[str, str]] = []
    coverage_days: list[dict[str, Any]] = []
    missing_price_counts: dict[int, int] = {h: 0 for h in fa.HORIZONS}
    v43_restricted_days = 0
    v43_missing_days = 0
    v43_full_days = 0
    v45_days = 0
    v45_start = fa.first_v45_production_date(workspace)
    ok_days = 0

    retry_missing = bool(fetch_missing and client is not None)

    for trade_date in signal_universe:
        day_path = day_cache_dir / f"panel_{trade_date}.parquet"
        skip_path = day_cache_dir / f"skip_{trade_date}.json"
        panel = None
        skip_reason = None
        should_refresh = trade_date in refresh_set
        if day_path.exists() and not should_refresh:
            panel = pd.read_parquet(day_path)
            if v45_start and trade_date >= v45_start and fa.panel_missing_v45_columns(panel):
                should_refresh = True
                panel = None
        if panel is not None:
            pass
        elif skip_path.exists() and not should_refresh:
            meta = json.loads(skip_path.read_text(encoding="utf-8"))
            skip_reason = meta.get("reason")
        else:
            panel, skip_reason = build_day_factor_panel(
                trade_date=trade_date,
                calendar=calendar,
                price_history=prices,
                workspace=workspace,
                session=session,
                attr_cache=attr_cache,
            )
            if panel is not None:
                panel.to_parquet(day_path, index=False)
                if skip_path.exists():
                    try:
                        skip_path.unlink()
                    except OSError:
                        pass
            elif day_path.exists():
                # Refresh failed: keep the last good panel so a miss cannot wipe history.
                panel = pd.read_parquet(day_path)
                skip_reason = None
            else:
                skip_path.write_text(json.dumps({"reason": skip_reason}, ensure_ascii=False), encoding="utf-8")

        if panel is None:
            skipped.append({"trade_date": trade_date, "reason": str(skip_reason or "unknown")})
            continue

        ok_days += 1
        coverage_days.append({"trade_date": trade_date, "n_names": int(len(panel))})

        v43_art = fa.load_v43_composite_artifact(workspace, trade_date)
        v43_panel = None
        if v43_art.get("status") != "ok":
            v43_missing_days += 1
        elif v43_art.get("excluded_from_ic"):
            v43_restricted_days += 1
        else:
            v43_full_days += 1
            v43_panel = v43_art["panel"]

        include_v45 = bool(v45_start and trade_date >= v45_start and all(c in panel.columns for c in fa.V45_FACTOR_NAMES + ["v45_composite"]))
        if include_v45:
            v45_days += 1

        # correlation matrix among five v44 factors
        mat = fa.pairwise_spearman_matrix(panel, fa.FACTOR_NAMES)
        if mat is not None:
            corr_mats.append(mat)
            row = []
            for name in fa.FACTOR_NAMES:
                row.append(fa.spearman_corr(panel[name].to_numpy(dtype=float), panel["composite"].to_numpy(dtype=float)))
            composite_corr.append(row)

        for h in fa.HORIZONS:
            rets = fa.forward_close_returns(prices, signal_date=trade_date, horizon=h, calendar=calendar)
            if rets.empty:
                continue
            day = panel.set_index("ts_code")
            common = day.index.intersection(rets.index)
            if len(common) < 30:
                continue
            r = rets.loc[common]
            missing_price_counts[h] += int(r.isna().sum())
            r = r.dropna()
            if len(r) < 30:
                continue
            excess = r - float(r.mean())
            day2 = day.loc[r.index]
            codes = pd.Series(day2.index.astype(str), index=day2.index)
            day_names = list(v44_names)
            if include_v45:
                day_names.extend(v45_names)
            for name in day_names:
                if name not in day2.columns:
                    continue
                f = day2[name]
                ic = fa.ic_for_day(f, excess)
                if np.isfinite(ic):
                    ic_store[name][h].append(float(ic))
                stats = fa.decile_stats_for_day(f, excess, codes)
                if stats.get("decile_means"):
                    decile_store[name][h].append(list(stats["decile_means"]))
                if np.isfinite(stats.get("top_minus_bottom", float("nan"))):
                    spread_store[name][h].append(float(stats["top_minus_bottom"]))
                if name in mono_store and np.isfinite(stats.get("monotonicity", float("nan"))):
                    mono_store[name][h].append(float(stats["monotonicity"]))
            if v43_panel is not None:
                v43 = v43_panel.set_index("ts_code")
                common43 = v43.index.intersection(excess.index)
                if len(common43) >= 30:
                    e43 = excess.loc[common43]
                    e43 = e43 - float(e43.mean())
                    d43 = v43.loc[e43.index]
                    c43 = pd.Series(d43.index.astype(str), index=d43.index)
                    for name in v43_names:
                        f = d43[name]
                        ic = fa.ic_for_day(f, e43)
                        if np.isfinite(ic):
                            ic_store[name][h].append(float(ic))
                        stats = fa.decile_stats_for_day(f, e43, c43)
                        if stats.get("decile_means"):
                            decile_store[name][h].append(list(stats["decile_means"]))
                        if np.isfinite(stats.get("top_minus_bottom", float("nan"))):
                            spread_store[name][h].append(float(stats["top_minus_bottom"]))

    # Aggregate
    per_factor: dict[str, Any] = {}
    for name in all_score_names:
        per_factor[name] = {}
        for h in fa.HORIZONS:
            ic_sum = fa.summarize_ic_series(ic_store[name][h], horizon=h)
            spr = fa.summarize_spread_series(spread_store[name][h], horizon=h)
            mono = fa.summarize_spread_series(mono_store.get(name, {}).get(h, []), horizon=h) if name in mono_store else None
            deciles = fa.summarize_decile_means(decile_store[name][h])
            entry = {
                "ic": ic_sum,
                "top_minus_bottom": spr,
                "decile_mean_excess": deciles,
            }
            if mono is not None and not name.endswith("_reversed"):
                entry["decile_monotonicity_mean"] = mono.get("mean")
            if name in {"composite", "v45_composite", "v43_composite"}:
                bot_top = fa.summarize_spread_series(
                    [-x for x in spread_store[name][h] if np.isfinite(x)],
                    horizon=h,
                )
                entry["bottom_minus_top"] = bot_top
            if name.endswith("_reversed"):
                entry["interpretation"] = "long high reversed-score = long low original composite"
            per_factor[name][str(h)] = entry

    if corr_mats:
        mean_corr = np.nanmean(np.stack(corr_mats, axis=0), axis=0)
        corr_json = {
            "factors": fa.FACTOR_NAMES,
            "matrix": mean_corr.tolist(),
            "mean_pairwise_abs": float(
                np.mean(
                    [
                        abs(mean_corr[i, j])
                        for i in range(len(fa.FACTOR_NAMES))
                        for j in range(i + 1, len(fa.FACTOR_NAMES))
                    ]
                )
            ),
            "mean_pairwise": float(
                np.mean(
                    [
                        mean_corr[i, j]
                        for i in range(len(fa.FACTOR_NAMES))
                        for j in range(i + 1, len(fa.FACTOR_NAMES))
                    ]
                )
            ),
        }
        if composite_corr:
            cc = np.nanmean(np.asarray(composite_corr, dtype=float), axis=0)
            corr_json["corr_with_composite"] = {
                fa.FACTOR_NAMES[i]: float(cc[i]) for i in range(len(fa.FACTOR_NAMES))
            }
    else:
        corr_json = {"factors": fa.FACTOR_NAMES, "matrix": None, "mean_pairwise": None, "mean_pairwise_abs": None}

    def _reverse_block(score_name: str) -> dict[str, Any]:
        rows = []
        for h in fa.HORIZONS:
            bt = (per_factor.get(score_name) or {}).get(str(h), {}).get("bottom_minus_top") or {}
            rows.append(
                {
                    "horizon": h,
                    "bottom_minus_top_mean": bt.get("mean"),
                    "t_nw": bt.get("t_nw"),
                    "n_days": bt.get("n_days"),
                    "significant_nw_t_gt_2": fa.significant_nw_t(t_nw=bt.get("t_nw"), n_days=bt.get("n_days")),
                }
            )
        sig_horizons = [r["horizon"] for r in rows if r["significant_nw_t_gt_2"]]
        return {
            "score": score_name,
            "hypothesis": "long bottom composite / short top composite (mean-reversion after strong scores)",
            "status": "candidate_hypothesis_only",
            "min_inference_days": fa.MIN_INFERENCE_DAYS,
            "significant_horizons_nw_t_gt_2": sig_horizons,
            "meets_phase2_trigger": len(sig_horizons) >= 2,
            "wording": (
                "反向 Bottom−Top 在部分持有期上显著为正时，仅作为「候选假设，待预注册实验验证」，"
                "不得表述为已发现有效策略。"
                if sig_horizons
                else f"反向检验未在 ≥2 个持有期上达到 NW-t>2 且 n_days≥{fa.MIN_INFERENCE_DAYS}；不触发反转影子预注册。"
            ),
            "rows": rows,
        }

    reverse_conclusion = _reverse_block("composite")
    homology_abs = corr_json.get("mean_pairwise_abs")
    homology = {
        "mean_pairwise_spearman": corr_json.get("mean_pairwise"),
        "mean_pairwise_abs": homology_abs,
        "flag_same_factor_family": bool(homology_abs is not None and float(homology_abs) > 0.5),
        "note": (
            "五因子平均绝对两两相关 >0.5，更像同一价量因子的多种写法，而非五个正交信息源。"
            if homology_abs is not None and abs(float(homology_abs)) > 0.5
            else "五因子平均绝对两两相关 ≤0.5，同源性证据不足。"
        ),
    }

    result = {
        "schema_version": fa.SCHEMA_VERSION,
        "evidence_scope": fa.EVIDENCE_SCOPE,
        "not_for_promotion": True,
        "returns_definition": "close_qfq(T)->close_qfq(official_T+N); excess vs equal-weight domain mean; no costs (non-executable diagnostic)",
        "repro_metadata": fa.repro_metadata(
            as_of=as_of_date,
            n_signal_days=ok_days,
            extra={
                "refresh_days": refresh_window,
                "refresh_dates": sorted(refresh_set),
                "aggregation_scope": "full_history",
            },
        ),
        "coverage": {
            "as_of": as_of_date,
            "aggregation_scope": "full_history",
            "calendar_source": "official_sse_trade_cal",
            "official_calendar_start": calendar[0],
            "official_calendar_end": calendar[-1],
            "official_sessions": len(calendar),
            "stk_factor_dates": len(stk_dates),
            "missing_stk_factor_dates": missing_stk_dates,
            "signal_days_ok": ok_days,
            "signal_days_considered": len(signal_universe),
            "signal_days_skipped": len(skipped),
            "skipped_sample": skipped[:50],
            "skipped_reason_counts": (
                pd.Series([s["reason"].split(":")[0] for s in skipped]).value_counts().to_dict()
                if skipped
                else {}
            ),
            "names_per_day_sample": coverage_days[:10],
            "missing_forward_price_counts": missing_price_counts,
            "v43": {
                "full_domain_days": v43_full_days,
                "range_restricted_days": v43_restricted_days,
                "missing_artifact_days": v43_missing_days,
                "full_domain_min_names": fa.V43_FULL_DOMAIN_MIN_NAMES,
            },
            "v45": {
                "first_production_snapshot": v45_start,
                "days_included": v45_days,
            },
        },
        "per_factor": per_factor,
        "correlation": corr_json,
        "homology": homology,
        "reverse_test": reverse_conclusion,
        "reverse_test_v43": _reverse_block("v43_composite"),
        "reverse_test_v45": _reverse_block("v45_composite"),
        "questions": {
            "q1_single_factor_effectiveness": {
                key: (
                    None
                    if row is None
                    else {
                        "factor": row[0],
                        "mean_ic": row[1],
                        "icir": row[2],
                        "t_nw": row[3],
                        "n_days": row[4],
                    }
                )
                for key, row in fa.pick_signed_ic_extremes(per_factor).items()
            },
            "q2_reverse_hypothesis": reverse_conclusion,
            "q3_factor_homology": homology,
            "q4_ic_half_life": {
                "summary": fa.describe_ic_half_life(per_factor),
                "by_factor": {
                    name: {str(h): per_factor[name][str(h)]["ic"] for h in fa.HORIZONS}
                    for name in fa.FACTOR_NAMES + ["composite"]
                },
            },
        },
    }

    if write_latest:
        paths.latest_json.parent.mkdir(parents=True, exist_ok=True)
        fa.archive_previous_markdown_report(paths, incoming_as_of=str(as_of_date))
        fa.write_text_atomic(
            paths.latest_json,
            json.dumps(fa.json_safe(result), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        )
        fa.write_text_atomic(paths.latest_md, render_markdown_report(result))
        fa.write_public_factor_attribution_state(result, workspace=workspace, paths=paths)
    return result


def render_markdown_report(result: dict[str, Any]) -> str:
    rev = result["reverse_test"]
    hom = result["homology"]
    cov = result["coverage"]
    lines = [
        "# 因子级 IC 归因诊断报告（diagnostic_only）",
        "",
        f"- schema: `{result['schema_version']}`",
        f"- evidence_scope: **`{result['evidence_scope']}`**（禁止用于晋级）",
        f"- as_of: `{cov['as_of']}` · 聚合范围 **`{cov.get('aggregation_scope', 'full_history')}`** · 有效信号日 `{cov['signal_days_ok']}` / 考虑 `{cov.get('signal_days_considered', cov['signal_days_ok'])}` · 跳过 `{cov['signal_days_skipped']}`",
        f"- 收益口径: `{result['returns_definition']}`",
        f"- 日历: `{cov.get('calendar_source', 'official')}` {cov.get('official_calendar_start', cov.get('stk_factor_calendar_start'))} → {cov.get('official_calendar_end', cov.get('stk_factor_calendar_end'))}；缺失 stk_factor {len(cov.get('missing_stk_factor_dates') or [])} 日",
        f"- config_hash: `{result['repro_metadata']['config_hash']}` · min_inference_days={result['repro_metadata'].get('min_inference_days', fa.MIN_INFERENCE_DAYS)}",
        "",
        "## 四个问题的直接答案",
        "",
        "### 1. 单因子有效性",
    ]
    extremes = fa.pick_signed_ic_extremes(result["per_factor"], horizon="5")
    strongest_pos = extremes["positive"]
    strongest_neg = extremes["negative"]
    q1_bits: list[str] = []
    if strongest_pos:
        q1_bits.append(
            f"最强正 IC 为 `{strongest_pos[0]}`（mean IC={fa.fmt4(strongest_pos[1])}, "
            f"ICIR={fa.fmt4(strongest_pos[2])}, NW-t={fa.fmt4(strongest_pos[3])}, n={strongest_pos[4]}）"
        )
    else:
        q1_bits.append("无满足样本门槛的正 IC 因子")
    if strongest_neg:
        q1_bits.append(
            f"最强负 IC 为 `{strongest_neg[0]}`（mean IC={fa.fmt4(strongest_neg[1])}, "
            f"ICIR={fa.fmt4(strongest_neg[2])}, NW-t={fa.fmt4(strongest_neg[3])}, n={strongest_neg[4]}）"
        )
    else:
        q1_bits.append("无满足样本门槛的负 IC 因子")
    lines.append("在 T+5 上，" + "；".join(q1_bits) + "。")
    if strongest_pos:
        mono = (
            (result["per_factor"].get(strongest_pos[0]) or {})
            .get("5", {})
            .get("decile_monotonicity_mean")
        )
        if mono is not None and float(mono) < 0.5:
            lines.append(
                f"`{strongest_pos[0]}` 的十分位单调性为 {fa.fmt4(mono)}，"
                "IC 为正不等于最高十分位最优，不可直接做多最高分组。"
            )
    lines.append(
        f"仅 `status=ok` 且 n_days≥{fa.MIN_INFERENCE_DAYS} 的单元格进入本结论；其余标 insufficient_sample。"
    )
    lines += [
        "",
        "### 2. 反向假设（composite 取反 / Bottom−Top）",
        rev["wording"]
        + (
            f" v43 全域 IC 日 `{cov.get('v43', {}).get('full_domain_days')}`，"
            "不足门槛时仅覆盖 v44 composite。"
        ),
        f"NW-t>2 的持有期: {rev['significant_horizons_nw_t_gt_2'] or '无'}；"
        f"是否触及 Phase2 触发（≥2 期）: **{rev['meets_phase2_trigger']}**。",
        "结论性质: **候选假设，待预注册实验验证**（禁止「发现有效策略」叙事）。",
        "",
        "### 3. 因子同源性",
        hom["note"],
        f"平均绝对两两 Spearman = {fa.fmt4(hom.get('mean_pairwise_abs', hom.get('mean_pairwise_spearman')))}",
        "",
        "### 4. 信息半衰期（IC 随持有期）",
        (result.get("questions") or {}).get("q4_ic_half_life", {}).get("summary")
        if isinstance((result.get("questions") or {}).get("q4_ic_half_life"), dict)
        else fa.describe_ic_half_life(result.get("per_factor") or {}),
        "",
        "## 反向检验明细（composite Bottom−Top）",
        "",
        "| N | mean Bottom−Top | NW-t | n_days | sig(t>2) |",
        "|---:|---:|---:|---:|:---:|",
    ]
    for row in rev["rows"]:
        lines.append(
            f"| {row['horizon']} | {fa.fmt4(row['bottom_minus_top_mean'])} | {fa.fmt4(row['t_nw'])} | {row['n_days']} | {row['significant_nw_t_gt_2']} |"
        )
    lines += [
        "",
        f"- v43 全域 IC 日: `{cov.get('v43', {}).get('full_domain_days')}`；Top20 受限（排除 IC）: `{cov.get('v43', {}).get('range_restricted_days')}`；无工件: `{cov.get('v43', {}).get('missing_artifact_days')}`",
        f"- v45 自首个生产快照 `{cov.get('v45', {}).get('first_production_snapshot')}` 起纳入，日数 `{cov.get('v45', {}).get('days_included')}`",
        "",
        "## 五因子 × 持有期 mean IC / ICIR / NW-t",
        "",
    ]
    header = "| factor | " + " | ".join(f"h={h}" for h in fa.HORIZONS) + " |"
    sep = "|---|" + "|".join(["---:" for _ in fa.HORIZONS]) + "|"
    lines.append(header)
    lines.append(sep)
    for name in fa.FACTOR_NAMES + ["composite"]:
        cells = []
        for h in fa.HORIZONS:
            ic = result["per_factor"][name][str(h)]["ic"]
            if not ic.get("n_days"):
                cells.append("pending")
            elif ic.get("status") == "insufficient_sample":
                cells.append(f"{ic['mean_ic']:.4f}/{fa.fmt4(ic.get('icir'))}/{fa.fmt4(ic.get('t_nw'))}†")
            else:
                cells.append(f"{ic['mean_ic']:.4f}/{fa.fmt4(ic.get('icir'))}/{fa.fmt4(ic.get('t_nw'))}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## 相关矩阵（日均截面 Spearman）", ""]
    mat = result["correlation"].get("matrix")
    if mat:
        lines.append("| | " + " | ".join(fa.FACTOR_NAMES) + " |")
        lines.append("|---|" + "|".join(["---:" for _ in fa.FACTOR_NAMES]) + "|")
        for i, name in enumerate(fa.FACTOR_NAMES):
            row = " | ".join(f"{mat[i][j]:.3f}" for j in range(len(fa.FACTOR_NAMES)))
            lines.append(f"| {name} | {row} |")
    else:
        lines.append("_相关矩阵样本不足_")
    lines += ["", "## 十分位平均超额（组1=最低分 … 组10=最高分）", ""]
    for h in fa.HORIZONS:
        lines.append(f"### 持有期 T+{h}")
        lines.append("| factor | " + " | ".join(str(i) for i in range(1, 11)) + " | Top−Bottom |")
        lines.append("|---|" + "|".join(["---:" ] * 11) + "|")
        for name in fa.FACTOR_NAMES + ["composite", "v45_composite", "v43_composite"]:
            block = (result["per_factor"].get(name) or {}).get(str(h)) or {}
            dec = (block.get("decile_mean_excess") or {}).get("mean_excess") or [None] * 10
            if not any(v is not None for v in dec):
                continue
            tb = (block.get("top_minus_bottom") or {}).get("mean")
            cells = [("" if v is None else f"{v:.4f}") for v in dec]
            lines.append(f"| {name} | " + " | ".join(cells) + f" | {fa.fmt4(tb)} |")
        lines.append("")
    lines += [
        "",
        "## 覆盖与跳过",
        f"- 官方日历: {cov.get('official_calendar_start')} → {cov.get('official_calendar_end')}（{cov.get('official_sessions')} 日）",
        f"- 缺失 stk_factor: `{cov.get('missing_stk_factor_dates')}`",
        f"- skip 原因计数: `{cov.get('skipped_reason_counts')}`",
        f"- 前向缺价计数: `{cov.get('missing_forward_price_counts')}`",
        f"- † 表示该单元格 n_days < min_inference_days={fa.MIN_INFERENCE_DAYS}，不进入问题 1 / Phase2。",
        "",
        "---",
        "_本报告为 diagnostic_only，不得写入 strategy_evaluation 晋级逻辑。_",
        "",
    ]
    return "\n".join(lines) + "\n"
