import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import multiple_testing_audit as mt  # noqa: E402


def test_adjustments_match_hand_values():
    a = mt.adjust({"a": 0.001, "b": 0.02, "c": 0.5, "d": 0.9})
    assert abs(a["a"]["bonferroni_p"] - 0.004) < 1e-12 and abs(a["b"]["bonferroni_p"] - 0.08) < 1e-12
    assert abs(a["a"]["holm_p"] - 0.004) < 1e-12 and abs(a["b"]["holm_p"] - 0.06) < 1e-12   # 0.02*3
    assert abs(a["a"]["bh_q"] - 0.004) < 1e-12 and abs(a["b"]["bh_q"] - 0.04) < 1e-12
    assert a["a"]["bh_q"] <= a["b"]["bh_q"] <= a["c"]["bh_q"] <= a["d"]["bh_q"]            # monotone q-values
    assert a["a"]["breakeven_family"] == int(1 * 0.10 / 0.001) and a["b"]["breakeven_family"] == int(2 * 0.10 / 0.02)


def test_breakeven_is_the_edge_of_bh_survival():
    p = 0.002
    be = mt.adjust({"x": p})["x"]["breakeven_family"]
    others = {f"o{i}": 0.5 for i in range(be - 1)}          # family of size be: x still survives
    assert mt.adjust({"x": p, **others})["x"]["bh_q"] <= mt.Q
    others = {f"o{i}": 0.5 for i in range(be + 5)}          # clearly past the edge: no longer survives
    assert mt.adjust({"x": p, **others})["x"]["bh_q"] > mt.Q


def test_deflated_sharpe_falls_with_more_trials_and_rises_with_sr():
    base = mt.deflated_sharpe(0.5, 50, 0.0, 3.0, 1)
    many = mt.deflated_sharpe(0.5, 50, 0.0, 3.0, 500)
    assert base > 0.99 and many < base
    assert mt.deflated_sharpe(0.9, 50, 0.0, 3.0, 500) > many
    assert mt.deflated_sharpe(0.5, 2, 0.0, 3.0, 5) is None
