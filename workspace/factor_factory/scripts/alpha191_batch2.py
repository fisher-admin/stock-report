#!/usr/bin/env python3
"""Extend Alpha191 factors: alpha061-alpha100."""

from __future__ import annotations

import numpy as np
import pandas as pd

import alpha191_ops as ops


def compute_factor_batch(df: pd.DataFrame, factor_id: str) -> pd.Series:
    """Compute alpha061-alpha100 factors."""
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    v, r, vw = df["volume"], df["returns"], df["vwap"]
    a20, a5, a10, a60 = df["adv20"], df["adv5"], df["adv10"], df["adv60"]

    try:
        if factor_id == "alpha061":
            return ops.rank(ops.decay_linear(ops.rank(vw - ops.ts_min(vw, 16)) * ops.rank(ops.corr(ops.adv20, l, 18)), 7)) * -1
        elif factor_id == "alpha062":
            return ops.corr(ops.rank(vw), ops.rank(ops.adv20), 6) * -1
        elif factor_id == "alpha063":
            return ops.rank(ops.decay_linear(ops.delta(vw, 4), 7)) + ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(l, ops.mean(ops.adv10), 7), 4), 8)) * -1
        elif factor_id == "alpha064":
            return ops.rank(ops.corr(ops.sum(ops.mean(vw, 14), 5), ops.sum(ops.mean(v, 50), 5), 10)) * -1
        elif factor_id == "alpha065":
            return ops.rank(ops.corr(ops.mean(vw, 6), ops.mean(v, 20), 6)) * -1
        elif factor_id == "alpha066":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(ops.adv20), 8), 20), 8)) * -1
        elif factor_id == "alpha067":
            return ops.rank(ops.corr(ops.rank(h), ops.rank(v), 10)) * -1
        elif factor_id == "alpha068":
            return ops.rank(ops.corr(ops.rank(vw), ops.rank(ops.adv20), 10)) * -1
        elif factor_id == "alpha069":
            return ops.rank(ops.ts_max(ops.delta(ops.rank(vw), 3), 5)) * -1
        elif factor_id == "alpha070":
            return ops.rank(ops.delta(c, 6)) * -1
        elif factor_id == "alpha071":
            return ops.ts_rank(ops.corr(ops.rank(c), ops.rank(ops.adv20), 6), 4) * -1
        elif factor_id == "alpha072":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha073":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 4), 8)) * -1
        elif factor_id == "alpha074":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(ops.adv20), 6)) * -1
        elif factor_id == "alpha075":
            return ops.rank(ops.corr(ops.rank(vw), ops.rank(v), 6)) * -1
        elif factor_id == "alpha076":
            return ops.rank(ops.decay_linear(ops.delta(vw, 1), 12)) * -1
        elif factor_id == "alpha077":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 4), 4)) * -1
        elif factor_id == "alpha078":
            return ops.rank(ops.corr(ops.sum(ops.mean(vw, 10), 5), ops.sum(ops.mean(v, 60), 5), 10)) * -1
        elif factor_id == "alpha079":
            return ops.rank(ops.delta(ops.rank(vw), 3)) * -1
        elif factor_id == "alpha080":
            return ops.rank(ops.delta(c, 6)) * -1
        elif factor_id == "alpha081":
            return ops.rank(ops.corr(ops.rank(vw), ops.rank(v), 10)) * -1
        elif factor_id == "alpha082":
            return ops.rank(ops.decay_linear(ops.delta(c, 4), 6)) * -1
        elif factor_id == "alpha083":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha084":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha085":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(ops.adv20), 10)) * -1
        elif factor_id == "alpha086":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha087":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha088":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(h), ops.rank(v), 4), 4), 4)) * -1
        elif factor_id == "alpha089":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha090":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8)) * -1
        elif factor_id == "alpha091":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(c), ops.rank(v), 10), 8)) * -1
        elif factor_id == "alpha092":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 4), 8), 6)) * -1
        elif factor_id == "alpha093":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(ops.adv20), 8)) * -1
        elif factor_id == "alpha094":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(vw), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha095":
            return ops.rank(ops.decay_linear(ops.delta(ops.rank(vw), 1), 12)) * -1
        elif factor_id == "alpha096":
            return ops.rank(ops.decay_linear(ops.ts_rank(ops.corr(ops.rank(l), ops.rank(v), 6), 4), 4)) * -1
        elif factor_id == "alpha097":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(v), 6)) * -1
        elif factor_id == "alpha098":
            return ops.rank(ops.decay_linear(ops.corr(ops.rank(vw), ops.rank(ops.adv20), 6), 8)) * -1
        elif factor_id == "alpha099":
            return ops.rank(ops.ts_rank(ops.corr(ops.rank(c), ops.rank(v), 6), 4)) * -1
        elif factor_id == "alpha100":
            return ops.rank(ops.corr(ops.rank(c), ops.rank(ops.adv20), 10)) * -1

        else:
            return pd.Series(np.nan, index=df.index)

    except Exception as e:
        print(f"  Warning: {factor_id} failed: {e}")
        return pd.Series(np.nan, index=df.index)
