"""Capacity-aware impact cost (square-root rule) for research stress tests.

The frozen cost models (CostModel in research_labels_v2, 0.3% round trip in tier_monitor_v1) charge a flat
slippage that ignores order size. Small, illiquid names look best in a flat-cost backtest and worst in
practice. This module adds a stress overlay, never replacing the frozen specs:

  participation p = order_yuan / ADV_yuan
  one-way impact  = K_BPS * sqrt(p / 1%) bps, capped at CAP_BPS       (10 bp at 1% of ADV)
  round trip      = both legs, as a fraction
"""
from __future__ import annotations

import numpy as np
import pandas as pd

K_BPS = 10.0
CAP_BPS = 150.0
MAX_PARTICIPATION = 0.05  # orders above 5% of ADV are flagged as capacity-breaching


def impact_bps(participation):
    p = np.asarray(participation, dtype=float)
    return np.minimum(K_BPS * np.sqrt(np.maximum(p, 0.0) / 0.01), CAP_BPS)


def round_trip_impact(order_yuan: float, adv_yuan: pd.Series) -> pd.Series:
    """Round-trip impact as a fraction, per code; NaN where ADV is unknown."""
    p = order_yuan / adv_yuan.where(adv_yuan > 0)
    return pd.Series(2 * impact_bps(p) / 1e4, index=adv_yuan.index).where(p.notna())


def breaches(order_yuan: float, adv_yuan: pd.Series) -> pd.Series:
    return (order_yuan / adv_yuan.where(adv_yuan > 0)) > MAX_PARTICIPATION
