import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import style_attribution as sa  # noqa: E402


def _universe(n=800, seed=0, size_beta=0.01, alpha_boost=0.0, group_size=30, random_group=False):
    rng = np.random.default_rng(seed)
    idx = [f"{i:06d}.SZ" for i in range(n)]
    st = pd.DataFrame(rng.normal(size=(n, len(sa.STYLES))), index=idx, columns=[k for k, _ in sa.STYLES])
    ind = pd.Series(rng.choice(list("ABCDE"), n), index=idx)
    fwd = size_beta * st["size"] + 0.002 * (ind == "B") + rng.normal(0, 0.02, n)
    group = (list(rng.choice(idx, group_size, replace=False)) if random_group
             else list(st["size"].sort_values(ascending=False).index[:group_size]))  # large-cap-tilted group
    fwd.loc[group] += alpha_boost
    return pd.Series(fwd, index=idx), st, ind, group


def test_decomposition_identity_and_style_vs_alpha():
    fwd, st, ind, group = _universe()
    r = sa.decompose(fwd, st, ind, group)
    assert abs(r["check"]) < 1e-12                                   # excess == style + industry + alpha, exactly
    assert r["style"]["size"] > 0.005 and r["exposure"]["size"] > 1.5  # return explained by the size tilt
    assert abs(r["alpha"]) < 0.01                                      # no genuine alpha injected


def test_injected_alpha_lands_in_alpha_not_style():
    fwd, st, ind, group = _universe(size_beta=0.0, alpha_boost=0.03, random_group=True)
    r = sa.decompose(fwd, st, ind, group)
    assert abs(r["check"]) < 1e-12 and r["alpha"] > 0.02 and abs(r["style"]["size"]) < 0.005


def test_small_or_empty_inputs_return_none():
    fwd, st, ind, group = _universe(n=200)
    assert sa.decompose(fwd, st, ind, group) is None                  # below MIN_UNIVERSE
    fwd, st, ind, _ = _universe()
    assert sa.decompose(fwd, st, ind, ["NOPE"]) is None


def test_redundancy_flags_correlated_pair_and_neff():
    rng = np.random.default_rng(2)
    base = rng.normal(size=1500)
    sc = pd.DataFrame({"ivol_60": -base + rng.normal(0, .3, 1500), "turn_20": -base + rng.normal(0, .3, 1500),
                       "turn_abn_5_60": -rng.normal(size=1500), "val_comp_indneu": rng.normal(size=1500),
                       "vpd_10": rng.normal(size=1500), "turn_cv_20": -rng.normal(size=1500)})
    r = sa.redundancy([sc, sc])
    assert r["high_pairs"][0]["a"] == "ivol_60" and r["high_pairs"][0]["b"] == "turn_20" and r["high_pairs"][0]["rho"] > 0.8
    assert 4.5 < r["n_eff"] < 6.0
