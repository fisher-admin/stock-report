from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core7p_native_scorer as cn  # noqa: E402
import mandate_guard as mg  # noqa: E402


class FakeStore:
    def __init__(self, bad_indices: set[int] | None = None, ind_map: dict[str, str] | None = None):
        self.bad_indices = bad_indices or set()
        self.ind_map = dict(ind_map or {})
        self.last_diag = {"n_em_R_rows_used": 0, "n_em_forecast_rows": 0}

    def industry_map(self, d_target=None):
        return dict(self.ind_map)

    def pit_fundamentals(self, d_target, codes, cal_pit, total_mv_d, pb_d, ind_names=None, mv_pb_hist=None):
        n = len(codes)
        base_n = min(n, 120)
        rng = np.random.default_rng(42)
        ep_b = rng.uniform(0.01, 0.10, size=base_n)
        roe_b = rng.uniform(0.02, 0.25, size=base_n)
        dedt_b = rng.uniform(1e7, 1e9, size=base_n)
        jump_b = rng.normal(size=base_n)
        rjump_b = rng.normal(size=base_n)
        sue_b = rng.normal(size=base_n)
        suemv_b = rng.normal(size=base_n)
        dedt_yoy_b = rng.uniform(-0.2, 0.5, size=base_n)
        rev_yoy_b = rng.uniform(-0.1, 0.4, size=base_n)
        ocf_b = rng.uniform(1e7, 1e9, size=base_n)
        extra = max(0, n - base_n)
        pad = lambda a, v=0.0: np.concatenate([a, np.full(extra, v)]) if extra else a[:n]  # noqa: E731
        ep, roe, dedt = pad(ep_b, 0.05), pad(roe_b, 0.1), pad(dedt_b, 1e8)
        dsrc = np.array(["seed_profit_dedt"] * n, dtype=object)
        for j in self.bad_indices:
            ep[j] = np.nan
            dedt[j] = np.nan
            dsrc[j] = "missing"
        return {
            "ep_dedt": ep,
            "roe_dedt": roe,
            "jump": pad(jump_b),
            "rev_jump": pad(rjump_b),
            "sue": pad(sue_b),
            "sue_mv": pad(suemv_b),
            "ev_age": np.full(n, 5.0),
            "dedt_ttm": dedt,
            "np_ttm": dedt,
            "rev_ttm": dedt * 2.0,
            "dedt_yoy": pad(dedt_yoy_b),
            "rev_yoy": pad(rev_yoy_b),
            "ocf_ttm": pad(ocf_b, 1e8),
            "ocf_applicable": np.ones(n, dtype=bool),
            "dedt_source": dsrc,
            "latest_report_date": np.array(["20260930"] * n, dtype=object),
            "latest_ann_date": np.array(["20261030"] * n, dtype=object),
        }


def _synth_inputs(codes: list[str], cal: list[str], rogue_ret: float = 0.0):
    rng = np.random.default_rng(17)
    T, N = len(cal), len(codes)
    base_n = min(N, 120)
    r_b = rng.normal(0.0, 0.015, size=(T, base_n))
    O_b = rng.uniform(10.0, 40.0, size=(T, base_n))
    V_b = rng.uniform(1e5, 1e7, size=(T, base_n))
    tmv_b = rng.uniform(5e5, 5e6, size=base_n)
    cmv_b = rng.uniform(5e5, 5e6, size=base_n)
    pb_b = rng.uniform(1.0, 6.0, size=base_n)
    pe_b = rng.uniform(8.0, 40.0, size=base_n)
    dv_b = rng.uniform(0.0, 0.05, size=base_n)
    extra = max(0, N - base_n)
    if extra:
        r = np.hstack([r_b, np.zeros((T, extra))])
        O = np.hstack([O_b, np.full((T, extra), 20.0)])
        V = np.hstack([V_b, np.full((T, extra), 1e6)])
    else:
        r, O, V = r_b[:, :N], O_b[:, :N], V_b[:, :N]
    C = O * (1.0 + r)
    PC = np.vstack([O[:1], C[:-1]])
    aC = np.cumprod(1.0 + r, axis=0) * 10.0
    traded = np.ones((T, N), dtype=bool)
    U = np.ones((T, N), dtype=bool)
    for j, c in enumerate(codes):
        if not mg.is_allowed_mandate_code(c):
            r[:, j] = rogue_ret
            C[:, j] = O[:, j] * (1.0 + rogue_ret)
    mats = {"O": O, "C": C, "PC": PC, "V": V, "aC": aC, "r": r, "traded": traded, "U": U}
    ind_names = [f"IND{i % 6}" for i in range(6)]
    ind_vec = np.array([i % 6 for i in range(N)], dtype=np.int16)
    pad1 = lambda a, v: np.concatenate([a, np.full(extra, v)]) if extra else a[:N]  # noqa: E731
    val_data = {
        "total_mv": pad1(tmv_b, 1e6),
        "circ_mv": pad1(cmv_b, 1e6),
        "pb": pad1(pb_b, 2.0),
        "pe_ttm": pad1(pe_b, 20.0),
        "dv_lag1": pad1(dv_b, 0.01),
    }
    return mats, ind_vec, ind_names, val_data


class Core7PNativeScorerTests(unittest.TestCase):
    def _run_score(self, codes, rogue_ret=0.0, bad_indices=None):
        cal = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-03-01", periods=135)]
        sig = cal[-1]
        mats, ind_vec, ind_names, val_data = _synth_inputs(codes, cal[-130:], rogue_ret=rogue_ret)
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            cfg = mock.MagicMock()
            cfg.workspace = ws
            cfg.window_sessions = 130
            cfg.panel_path = ws / "no_panel.parquet"
            ind_map = {c: ind_names[v] for c, v in zip(codes, ind_vec) if v >= 0}
            with (
                mock.patch.object(cn.ag, "get_official_calendar", return_value=cal),
                mock.patch.object(cn.ag, "load_window_prices_and_universe", return_value=(mats, codes, {}, {"universe_degraded_mode": "none", "source": None, "mode": "test"})),
                mock.patch.object(cn.ag, "load_pit_sw1_industries", side_effect=AssertionError("load_pit_sw1_industries must not be called")),
                mock.patch.object(cn.ag, "load_valuation_and_cyq", return_value=(val_data, {})),
                mock.patch.object(cn, "load_mv_pb_hist", return_value=pd.DataFrame(columns=["ts_code", "trade_date", "total_mv", "pb"])),
                mock.patch.object(cn.ag, "_load_pit_snapshot_flexible", return_value=(None, None, {})),
            ):
                return cn.score_date(sig, ws=ws, store=FakeStore(bad_indices=bad_indices, ind_map=ind_map), cfg=cfg)

    def test_strict_mandate_applied_before_mret_neutralisation_and_coverage(self):
        # 120 valid codes + 3 rogue codes (600000.SZ, 600001.SH\n, 60０002.SH) that pass loose prefix checks or sneak in
        valid = [f"60{i:04d}.SH" for i in range(60)] + [f"00{i:04d}.SZ" for i in range(60)]
        rogues = ["600000.SZ", "600001.SH\n", "60０002.SH"]
        df_clean, meta_clean = self._run_score(valid)
        df_rogue, meta_rogue = self._run_score(valid + rogues, rogue_ret=0.95, bad_indices={120, 121, 122})

        # Rogue codes completely excluded from universe, m_ret, neutralisation, and coverage
        self.assertEqual(len(df_rogue), 120)
        self.assertEqual(meta_rogue["universe"]["rows_aegis_rank_mask"], 123)
        self.assertEqual(meta_rogue["universe"]["rows_strict_mandate_mask"], 120)
        self.assertEqual(meta_rogue["coverage"]["n_mask"], 120)
        self.assertEqual(meta_rogue["coverage"]["qarp_real_dedt_cov"], 1.0)
        pd.testing.assert_frame_equal(df_clean[["ts_code", "core7p_native", "ivol_60_si", "qarp_pit_si"]],
                                      df_rogue[["ts_code", "core7p_native", "ivol_60_si", "qarp_pit_si"]])
        self.assertEqual(meta_clean["scores_sha256_16"], meta_rogue["scores_sha256_16"])

    def test_product_rows_constrained_rank_handling(self):
        codes = [f"60{i:04d}.SH" for i in range(30)]
        df = pd.DataFrame({
            "ts_code": codes,
            "name": [f"N{i}" for i in range(30)],
            "ind_sw1_name": [f"IND{i % 5}" for i in range(30)],
            "core7p_native_pct": np.linspace(1.0, 0.1, 30),
            "dedt_ttm": [1e8] * 30,
            "dedt_yoy": [0.1] * 30,
            "rev_yoy": [0.1] * 30,
            "roe_dedt": [0.15] * 30,
            "ocf_ttm": [1e8] * 30,
            "ocf_applicable": [True] * 30,
            "pe_ttm": [20.0] * 30,
            "pb": [2.0] * 30,
            "dedt_source": ["seed_profit_dedt"] * 30,
            "latest_report_date": ["20260930"] * 30,
            "latest_ann_date": ["20261030"] * 30,
        })
        sel = [codes[0], codes[5], codes[10]]
        rows_plain = cn.product_rows(df, sel, buys={codes[5]})
        self.assertEqual([r["rank"] for r in rows_plain], [1, 2, 3])
        self.assertNotIn("constrained_rank", rows_plain[0])

        crmap = {codes[0]: 1, codes[5]: 4, codes[10]: 9}
        rows_map = cn.product_rows(df, sel, buys={codes[5]}, constrained_rank_map=crmap)
        self.assertEqual([r["rank"] for r in rows_map], [1, 2, 3])
        self.assertEqual([r["constrained_rank"] for r in rows_map], [1, 4, 9])

        df["constrained_rank"] = list(range(101, 131))
        rows_col = cn.product_rows(df, sel)
        self.assertEqual([r["constrained_rank"] for r in rows_col], [101, 106, 111])


    def test_causal_industry_map_excludes_future_and_expired_and_keeps_unknown(self):
        cal = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-05-28", periods=130)]
        sig = cal[-1]
        codes = [f"60{i:04d}.SH" for i in range(60)] + [f"00{i:04d}.SZ" for i in range(60)]
        mats, _, _, val_data = _synth_inputs(codes, cal)
        future_code = codes[116]
        expired_code = codes[117]
        reclass_code = codes[118]
        absent_code = codes[119]
        sw_rows = [
            {"ts_code": c, "l1_name": f"IND{i % 6}", "in_date": "20200101", "out_date": None}
            for i, c in enumerate(codes[:116])
        ]
        # Future-only entry (in_date > sig) must NOT leak backward
        sw_rows.append({"ts_code": future_code, "l1_name": "银行", "in_date": "20990101", "out_date": None})
        # Expired entry (out_date == sig) must NOT be active (interval is [in_date, out_date))
        sw_rows.append({"ts_code": expired_code, "l1_name": "非银金融", "in_date": "20200101", "out_date": sig})
        # Reclassified entry: old interval expired, new interval active
        sw_rows.append({"ts_code": reclass_code, "l1_name": "电子", "in_date": "20200101", "out_date": cal[-5]})
        sw_rows.append({"ts_code": reclass_code, "l1_name": "银行", "in_date": cal[-5], "out_date": None})

        seen_ind_names: list[list[str]] = []
        fake = FakeStore()

        def cap_pit(d_target, c_list, cal_pit, total_mv_d, pb_d, ind_names=None, mv_pb_hist=None):
            seen_ind_names.append(list(ind_names or []))
            return fake.pit_fundamentals(d_target, c_list, cal_pit, total_mv_d, pb_d, ind_names=ind_names, mv_pb_hist=mv_pb_hist)

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sw_p = ws / "sw_member.parquet"
            pd.DataFrame(sw_rows).to_parquet(sw_p, index=False)
            store = cn.ZeroPaidFinStoreV2(workspace=ws, sw_path=sw_p)
            cfg = mock.MagicMock()
            cfg.workspace = ws
            cfg.window_sessions = 130
            cfg.panel_path = ws / "no_panel.parquet"
            with (
                mock.patch.object(cn.ag, "get_official_calendar", return_value=cal),
                mock.patch.object(cn.ag, "load_window_prices_and_universe", return_value=(mats, codes, {}, {"universe_degraded_mode": "none", "source": None, "mode": "test"})),
                mock.patch.object(cn.ag, "load_pit_sw1_industries", side_effect=AssertionError("must not call load_pit_sw1_industries")),
                mock.patch.object(cn.ag, "load_valuation_and_cyq", return_value=(val_data, {})),
                mock.patch.object(cn, "load_mv_pb_hist", return_value=pd.DataFrame(columns=["ts_code", "trade_date", "total_mv", "pb"])),
                mock.patch.object(cn.ag, "_load_pit_snapshot_flexible", return_value=(None, None, {})),
                mock.patch.object(store, "pit_fundamentals", side_effect=cap_pit),
            ):
                df, _ = cn.score_date(sig, ws=ws, store=store, cfg=cfg)

        by_code = df.set_index("ts_code")
        for c in (future_code, expired_code, absent_code):
            self.assertEqual(by_code.at[c, "ind_sw1_name"], "UNKNOWN")
            self.assertTrue(pd.isna(by_code.at[c, "core7p_native"]))
        self.assertEqual(by_code.at[reclass_code, "ind_sw1_name"], "银行")
        self.assertEqual(seen_ind_names[0][116], "")
        self.assertEqual(seen_ind_names[0][117], "")
        self.assertEqual(seen_ind_names[0][118], "银行")
        self.assertEqual(seen_ind_names[0][119], "")


if __name__ == "__main__":
    unittest.main()
