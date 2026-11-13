#!/usr/bin/env python3
"""Phase 2 candidate factors (wide session x code matrices on Research Panel v2 inputs).

Sources, all local:
- research_matrices_v2.npz: adjusted close, c2c returns, volume (full, uncensored history)
- daily_basic_hist: valuation, size, free-float turnover (whole market, exact date)
- research_panel_v2.parquet: cyq chip fields, amount, industry (universe rows only)
Every factor is a value known at the close of T (no forward information).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import gauntlet_v2 as gv
import research_panel_v2 as rp


def rows_to_wide(ctx: gv.Context, values: np.ndarray) -> np.ndarray:
    w = np.full(ctx.mats["adj_close"].shape, np.nan)
    t = ctx.panel["trade_date"].map(ctx.s_idx).to_numpy()
    j = ctx.panel["ts_code"].map(ctx.c_idx).to_numpy()
    w[t, j] = values
    return w


_DB_CACHE: dict[str, pd.DataFrame] = {}


def daily_basic_wide(ctx: gv.Context, col: str) -> pd.DataFrame:
    if not _DB_CACHE:
        cols = ["ts_code", "trade_date", "pe_ttm", "pb", "circ_mv", "turnover_rate_f", "dv_ttm"]
        frames = [pd.read_parquet(rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet", columns=cols)
                  for d in ctx.sessions if (rp.DAILY_BASIC_DIR / f"daily_basic_{d}.parquet").exists()]
        long = pd.concat(frames, ignore_index=True)
        long["trade_date"] = long["trade_date"].astype(str)
        for c in cols[2:]:
            _DB_CACHE[c] = long.pivot(index="trade_date", columns="ts_code", values=c).reindex(
                index=ctx.sessions, columns=ctx.codes).astype(float)
    return _DB_CACHE[col]


def _ret(ctx: gv.Context) -> pd.DataFrame:
    return pd.DataFrame(ctx.mats["c2c"])


def _mkt(ctx: gv.Context) -> pd.Series:
    return _ret(ctx).mean(axis=1, skipna=True)


def _ind_rank(ctx: gv.Context, wide: np.ndarray) -> np.ndarray:
    """Within-industry percentile rank of a factor on each date (universe rows)."""
    v = ctx.wide_to_rows(wide)
    df = pd.DataFrame({"d": ctx.panel["trade_date"].to_numpy(), "i": ctx.panel["industry"].fillna("UNKNOWN").to_numpy(),
                       "v": v})
    return df.groupby(["d", "i"])["v"].rank(pct=True).to_numpy()


# ---------------------------------------------------------------- Family 1: valuation & size

def ep(ctx):
    pe = daily_basic_wide(ctx, "pe_ttm")
    pb = daily_basic_wide(ctx, "pb")
    out = 1.0 / pe.where(pe > 0)
    # The vendor leaves pe_ttm blank for TTM loss-makers: rank them below every profitable name
    # (EP = -1e-6) whenever the security has a valuation row that day; otherwise NaN.
    loss = pe.isna() & pb.notna()
    return out.mask(loss, -1e-6).to_numpy()


def bp(ctx):
    pb = daily_basic_wide(ctx, "pb")
    return (1.0 / pb.where(pb > 0)).to_numpy()


def ln_circ_mv(ctx):
    return np.log(daily_basic_wide(ctx, "circ_mv").clip(lower=1)).to_numpy()


def dv_ttm_lag1(ctx):
    dv = daily_basic_wide(ctx, "dv_ttm").fillna(0.0)  # no dividend -> 0 yield
    return dv.shift(1).to_numpy()  # lag-1 session rule (vendor-revised field)


def val_comp_indneu(ctx):
    comp = (_ind_rank(ctx, ep(ctx)) + _ind_rank(ctx, bp(ctx))) / 2.0
    return rows_to_wide(ctx, comp)


# ---------------------------------------------------------------- Family 2: liquidity & turnover

def _turn(ctx):
    return daily_basic_wide(ctx, "turnover_rate_f")


def turn_20(ctx):
    return _turn(ctx).rolling(20, min_periods=15).mean().to_numpy()


def turn_abn_5_20(ctx):
    t = _turn(ctx)
    return (t.rolling(5, min_periods=4).mean() / t.rolling(20, min_periods=15).mean()).to_numpy()


def turn_abn_5_60(ctx):
    t = _turn(ctx)
    return (t.rolling(5, min_periods=4).mean() / t.rolling(60, min_periods=45).mean()).to_numpy()


def amihud_20(ctx):
    amt = pd.DataFrame(rows_to_wide(ctx, ctx.panel_amount))  # 千元, universe rows
    ill = _ret(ctx).abs() / amt.where(amt > 0)
    return np.log1p(1e6 * ill.rolling(20, min_periods=15).mean()).to_numpy()


def turn_cv_20(ctx):
    t = _turn(ctx)
    return (t.rolling(20, min_periods=15).std() / t.rolling(20, min_periods=15).mean()).to_numpy()


# ---------------------------------------------------------------- Family 3: risk, volatility, lottery

def vol_20(ctx):
    return _ret(ctx).rolling(20, min_periods=15).std().to_numpy()


def ivol_60(ctx):
    r, m = _ret(ctx), _mkt(ctx)
    cov = r.rolling(60, min_periods=45).cov(m)
    var_m = m.rolling(60, min_periods=45).var()
    beta = cov.div(var_m, axis=0)
    resid = r - beta.mul(m, axis=0)
    return resid.rolling(20, min_periods=15).std().to_numpy()


def max_ret_20(ctx):
    return _ret(ctx).rolling(20, min_periods=15).max().to_numpy()


def skew_20(ctx):
    return _ret(ctx).rolling(20, min_periods=15).skew().to_numpy()


def down_semidev_20(ctx):
    r = _ret(ctx)
    neg = r.clip(upper=0.0).where(r.notna())
    return np.sqrt((neg ** 2).rolling(20, min_periods=15).mean()).to_numpy()


def down_beta_60(ctx):
    r, m = _ret(ctx), _mkt(ctx)
    dm = m.where(m < 0)
    x = r.mul(dm.notna().astype(float), axis=0).where(r.notna())  # stock return on down-market days, else 0
    mm = dm.fillna(0.0)
    n = dm.notna().astype(float).rolling(60, min_periods=45).sum()
    sxy = x.mul(mm, axis=0).rolling(60, min_periods=45).sum()
    sx = x.rolling(60, min_periods=45).sum()
    sy = mm.rolling(60, min_periods=45).sum()
    syy = (mm ** 2).rolling(60, min_periods=45).sum()
    cov = sxy.sub(sx.mul(sy / n, axis=0))
    var = (syy - sy ** 2 / n)
    return cov.div(var.where(var > 0), axis=0).to_numpy()


# ---------------------------------------------------------------- Family 4: price-volume & chips

def _vpd(ctx, n):
    ac = pd.DataFrame(ctx.mats["adj_close"])
    vol = pd.DataFrame(ctx.mats["vol"]).where(ac.notna())
    return -(ac.rolling(n, min_periods=int(n * 0.75)).corr(vol)).to_numpy()


def vpd_10(ctx):
    return _vpd(ctx, 10)


def vpd_60(ctx):
    return _vpd(ctx, 60)


def vpd_ret_20(ctx):
    """Return-volume divergence: -corr(daily return, volume change, 20d)."""
    r = _ret(ctx)
    dv = np.log(pd.DataFrame(ctx.mats["vol"]).where(lambda x: x > 0)).diff()
    return -(r.rolling(20, min_periods=15).corr(dv)).to_numpy()


def winner_rate(ctx):
    return rows_to_wide(ctx, ctx.panel_chip["winner_rate"])


def chip_width(ctx):
    c = ctx.panel_chip
    return rows_to_wide(ctx, (c["cost_95pct"] - c["cost_5pct"]) / c["cost_50pct"].where(c["cost_50pct"] > 0))


def price_to_chip(ctx):
    c = ctx.panel_chip
    return rows_to_wide(ctx, c["close"] / c["weight_avg"].where(c["weight_avg"] > 0) - 1.0)


# ---------------------------------------------------------------- pre-registered specs

# (factor_fn, family, direction, hypothesis, definition)
BATCH1 = [
    (ep, "F1_valuation_size", 1, "Cheap on earnings outperforms (value).", "1/pe_ttm for pe>0; blank pe_ttm with a valuation row (TTM loss) -> -1e-6 (ranked lowest)"),
    (bp, "F1_valuation_size", 1, "Cheap on book outperforms (value).", "1/pb, pb>0"),
    (ln_circ_mv, "F1_valuation_size", -1, "Small free-float cap outperforms (A-share size premium).", "ln(circ_mv)"),
    (dv_ttm_lag1, "F1_valuation_size", 1, "High trailing dividend yield outperforms.", "dv_ttm (NaN->0) lagged 1 session"),
    (val_comp_indneu, "F1_valuation_size", 1, "Industry-relative cheapness outperforms.",
     "mean of within-industry pct ranks of EP and BP"),
    (turn_20, "F2_liquidity_turnover", -1, "Low free-float turnover outperforms (attention/illiquidity).",
     "mean(turnover_rate_f, 20d)"),
    (turn_abn_5_20, "F2_liquidity_turnover", -1, "Abnormal recent turnover predicts underperformance.",
     "mean5(turnover_rate_f)/mean20"),
    (turn_abn_5_60, "F2_liquidity_turnover", -1, "Abnormal recent turnover vs 60d predicts underperformance.",
     "mean5(turnover_rate_f)/mean60"),
    (amihud_20, "F2_liquidity_turnover", 1, "Illiquid names earn a premium.", "log1p(1e6*mean(|r|/amount, 20d))"),
    (turn_cv_20, "F2_liquidity_turnover", -1, "Unstable turnover predicts underperformance.",
     "std20(turnover_rate_f)/mean20"),
]
BATCH2 = [
    (vol_20, "F3_risk_lottery", -1, "Low realized volatility outperforms.", "std(c2c, 20d)"),
    (ivol_60, "F3_risk_lottery", -1, "Low idiosyncratic volatility outperforms.",
     "std20 of residual vs EW market, beta from 60d"),
    (max_ret_20, "F3_risk_lottery", -1, "Lottery-like names (high max return) underperform.", "max(c2c, 20d)"),
    (skew_20, "F3_risk_lottery", -1, "Positively skewed names underperform.", "skew(c2c, 20d)"),
    (down_semidev_20, "F3_risk_lottery", -1, "High downside semideviation underperforms.", "sqrt(mean(min(r,0)^2, 20d))"),
    (down_beta_60, "F3_risk_lottery", -1, "High downside beta underperforms.", "beta to EW market on down days, 60d"),
    (vpd_10, "F4_price_volume_chip", 1, "Price-volume divergence (10d) outperforms.", "-corr(adj_close, vol, 10d)"),
    (vpd_60, "F4_price_volume_chip", 1, "Price-volume divergence (60d) outperforms.", "-corr(adj_close, vol, 60d)"),
    (vpd_ret_20, "F4_price_volume_chip", 1, "Return/volume-change divergence outperforms.",
     "-corr(c2c, dlog(vol), 20d)"),
    (winner_rate, "F4_price_volume_chip", -1, "High chip winner-rate predicts profit-taking.", "cyq winner_rate"),
    (chip_width, "F4_price_volume_chip", -1, "Wide (dispersed) chip distribution underperforms.",
     "(cost_95pct-cost_5pct)/cost_50pct"),
    (price_to_chip, "F4_price_volume_chip", -1, "Price far above average chip cost underperforms.", "close/weight_avg-1"),
]


def specs_for(batch: list, horizons=(5, 20)) -> list[tuple[gv.FactorSpec, callable]]:
    out = []
    for fn, fam, direction, hyp, definition in batch:
        for h in horizons:
            out.append((gv.FactorSpec(f"p2_{fn.__name__}_h{h}", fam, hyp, direction, h, "research", definition), fn))
    return out
