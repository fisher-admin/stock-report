"""Tests for forward_shadow_v2 and its Hermes shell wrappers (synthetic data only; no network, no real ledgers).

Run from the workspace root:  python -m unittest factor_factory/scripts/tests/test_forward_shadow_v2.py
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import forward_shadow_v2 as v2  # noqa: E402

TZ = v2.TZ


def synth_df(n: int = 240, seed: int = 7, top_same_industry: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    codes = [f"60{i:04d}.SH" for i in range(n // 2)] + [f"30{i:04d}.SZ" for i in range(n - n // 2)]
    score = rng.normal(size=n)
    dedt = rng.normal(loc=2e8, scale=3e8, size=n)
    dedt[::11] = np.nan
    ind = np.array([f"IND{i % 15}" for i in range(n)], dtype=object)
    df = pd.DataFrame({
        "ts_code": codes, "comp_rank_ew6": rng.uniform(size=n), "name": [f"N{i}" for i in range(n)], "ind_sw1_name": ind,
        "close": rng.uniform(3, 80, size=n), "core7p_native": score, "core7p_cgo_missing": score + rng.normal(scale=.1, size=n),
        "dedt_ttm": dedt, "dedt_source": "seed_profit_dedt", "dedt_yoy": rng.normal(size=n), "rev_yoy": rng.normal(size=n),
        "roe_dedt": rng.normal(size=n), "ocf_ttm": rng.normal(size=n) * 1e8, "ocf_applicable": True, "pe_ttm": 20.0, "pb": 2.0,
        "latest_report_date": "20260930", "latest_ann_date": "20261030"})
    df["core7p_native_pct"] = df["core7p_native"].rank(pct=True)
    df = df.sort_values(["core7p_native", "ts_code"], ascending=[False, True]).reset_index(drop=True)
    if top_same_industry:
        df.loc[:top_same_industry - 1, "ind_sw1_name"] = "SAME"
        df.loc[:top_same_industry - 1, "dedt_ttm"] = 1e8
    return df


def synth_meta(cov: float = 1.0, mode: str = "none") -> dict:
    return {"coverage": {"qarp_real_dedt_cov": cov, "acc_real_cov": cov, "ocf_nonfin_cov": cov, "n_mask": 240},
            "degraded_pillars": [], "universe": {"universe_degraded_mode": mode}, "scores_sha256_16": "abc",
            "variant_diag": {"spearman_native_vs_cgo_missing": 0.96, "top25_overlap": 18, "n": 230}, "native_coverage": 1.0,
            "cgo_missing_coverage": 1.0, "pillar_coverage": {}, "store_diag": {}, "keys": {}}


def synth_px(codes) -> pd.DataFrame:
    return pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "pre_close": 10.0, "vol": 1e6},
                        index=pd.Index(list(codes), name="ts_code"))


class PureLogic(unittest.TestCase):
    def test_strict_board_filter(self):
        codes = ["600000.SH", "688001.SH", "000001.SZ", "300750.SZ", "830000.BJ", "600000.SZ", "920001.BJ", "60000.SH",
                 "301001.SZ", "002001.SZ", "900901.SH", "200002.SZ", "600000.sh", "600000.SH\n", "60０000.SH"]
        ok = v2.board_ok(codes)
        self.assertEqual(ok.tolist(), [True, False, True, True, False, False, False, False, True, True, False, False, False, False, False])

    def test_fin_healthy_thresholds(self):
        self.assertTrue(v2.fin_healthy({"qarp_real_dedt_cov": .9, "acc_real_cov": .9, "ocf_nonfin_cov": .85})[0])
        ok, bad = v2.fin_healthy({"qarp_real_dedt_cov": .89, "acc_real_cov": .95, "ocf_nonfin_cov": .84})
        self.assertFalse(ok)
        self.assertEqual(sorted(bad), ["ocf_nonfin_cov", "qarp_real_dedt_cov"])
        self.assertFalse(v2.fin_healthy({"qarp_real_dedt_cov": float("nan"), "acc_real_cov": 1, "ocf_nonfin_cov": 1})[0])
        self.assertFalse(v2.fin_healthy({})[0])

    def test_official_base_fail_closed(self):
        v1 = {"official": True}
        self.assertTrue(v2.official_base(False, False, "none", v1, True))
        self.assertFalse(v2.official_base(True, False, "none", v1, True))
        self.assertFalse(v2.official_base(False, True, "none", v1, True))
        self.assertFalse(v2.official_base(False, False, "pit_ffill_1d", v1, True))
        self.assertFalse(v2.official_base(False, False, "none", {"official": False}, True))
        self.assertFalse(v2.official_base(False, False, "none", None, True))
        self.assertFalse(v2.official_base(False, False, "none", v1, False))
        self.assertFalse(v2.official_base(False, False, "none", v1, True, exdiv_healthy=False))
        self.assertTrue(v2.is_late(None, datetime.now(TZ)))

    def test_official_reason_and_wait(self):
        self.assertEqual(v2.official_reason(False, True, False, True), "late")
        self.assertEqual(v2.official_reason(False, False, False, True, exdiv_healthy=False), "exdiv_unavailable")
        self.assertEqual(v2.official_reason(False, False, False, True), "non_official_inputs")
        self.assertEqual(v2.official_reason(False, False, True, False), "fin_coverage_low")
        self.assertIsNone(v2.official_reason(False, False, True, True))
        dl = datetime(2026, 11, 26, 9, 30, tzinfo=TZ)
        night, early = datetime(2026, 11, 25, 23, 52, tzinfo=TZ), datetime(2026, 11, 26, 8, 50, tzinfo=TZ)
        self.assertEqual(v2.should_wait(False, False, False, False, dl, night), "waiting_for_fundamentals")
        self.assertIsNone(v2.should_wait(False, False, False, False, dl, early))
        self.assertEqual(v2.should_wait(False, False, True, True, dl, early), "waiting_for_pipelines")
        self.assertIsNone(v2.should_wait(True, False, True, False, dl, night))

    def test_pct_book_lists(self):
        df = synth_df()
        L = v2.book_lists(df, v2.PRIMARY_BOOK)
        s = df["core7p_native"].dropna()
        self.assertEqual(len(L["entry"]), int((s.rank(pct=True) >= 0.9 - 1e-12).sum()))
        self.assertEqual(len(L["hold"]), int((s.rank(pct=True) >= 0.7 - 1e-12).sum()))
        self.assertEqual(L["entry"][0], df.dropna(subset=["core7p_native"]).iloc[0]["ts_code"])
        self.assertTrue(set(L["entry"]) <= set(L["hold"]))

    def test_product_constraint_dedt_positive(self):
        df = synth_df()
        order = v2.profit_order(df)
        d = df.set_index("ts_code")["dedt_ttm"]
        self.assertTrue(all(np.isfinite(d[c]) and d[c] > 0 for c in order))
        neg = d[(d <= 0) | d.isna()].index
        self.assertFalse(set(order) & set(neg))
        L = v2.book_lists(df, v2.PRODUCT_BOOK)
        self.assertEqual(L["entry"], order[:v2.TOPK_KEEP])
        self.assertEqual(L["hold"], sorted(order[:75]))

    def test_industry_cap_and_size_including_retained_holdings(self):
        df = synth_df(top_same_industry=12)
        order = v2.profit_order(df)
        ind = df.set_index("ts_code")["ind_sw1_name"].to_dict()
        tgt = v2.select_product_target(order, ind, set())
        self.assertEqual(len(tgt), 25)
        counts = pd.Series([ind[c] for c in tgt]).value_counts()
        self.assertLessEqual(int(counts.max()), 4)
        self.assertEqual(int(counts["SAME"]), 4)
        self.assertEqual(tgt, sorted(tgt, key=order.index))

        # Retained holdings exceeding CAP=4 (e.g. industry reclassification) must be capped at 4 in rank order
        prev_excess = set(order[:6]) | set(order[12:31])  # 6 stocks in SAME
        tgt2 = v2.select_product_target(order, ind, prev_excess)
        self.assertEqual(len(tgt2), 25)
        counts2 = pd.Series([ind[c] for c in tgt2]).value_counts()
        self.assertEqual(int(counts2["SAME"]), 4)
        self.assertEqual([c for c in tgt2 if ind[c] == "SAME"], order[:4])

        # _targets in replay must also cap retained holdings at CAP=4 in entry rank order
        L = {"entry": order[:v2.TOPK_KEEP], "hold": sorted(order[:75]), "size": 25, "industry": ind}
        q = {c: (0.0, 0.001, 0.001, True, True) for c in order}
        t_replay = v2._targets(v2.PRODUCT_BOOK, L, set(order[:6]), q)
        self.assertEqual(len(t_replay), 25)
        self.assertEqual([c for c in t_replay if ind[c] == "SAME"], order[:4])

    def test_buffer_hold_rank_logic(self):
        df = synth_df(n=400)
        order = v2.profit_order(df)
        ind = {c: f"I{i % 20}" for i, c in enumerate(order)}
        first = v2.select_product_target(order, ind, set())
        self.assertEqual(first, order[:25])
        prev = set(first)
        keep_name, drop_name = order[60], order[80]
        prev = (prev - {order[0], order[1]}) | {keep_name, drop_name}
        new = v2.select_product_target(order, ind, prev)
        self.assertIn(keep_name, new)
        self.assertNotIn(drop_name, new)
        self.assertEqual(len(new), 25)
        self.assertTrue((set(first) - {order[0], order[1]}) <= set(new))
        self.assertEqual(v2.select_product_target(order, ind, set(first)), first)

    def test_friction_math_with_slippage_and_missing_prices(self):
        # Baseline zero-slippage sanity
        fr0 = v2.retail_friction({"a": 50.0, "b": 50.0, "c": 300.0, "d": 249.9}, accounts=(100000,), slippage_bps=0.0)["100000"]
        self.assertEqual(fr0["names_buyable"], 3)
        self.assertAlmostEqual(fr0["invested_rmb"], 64990.0, places=2)

        # Default slippage_bps=10.0: d (249.9 * 1.001 = 250.1499 -> 25014.99 > 25000 slice) becomes unbuyable!
        # Missing-price entry ("e": nan) remains in target_n=5 -> slice=20000
        fr_slip = v2.retail_friction(
            {"a": 50.0, "b": 50.0, "c": 300.0, "d": 249.9, "e": float("nan")},
            accounts=(100000,),
        )["100000"]
        # slice_cash = 20000; 50*1.001 = 50.05 -> 3 lots = 15015, fee = max(5, 15015*2.5e-4)=5 + 15015*0.1e-4=0.15015 -> 15020.15
        self.assertEqual(fr_slip["names_buyable"], 2)
        expected_notional = 2 * 300 * 50.0 * 1.001
        expected_fees = 2 * (5.0 + (300 * 50.0 * 1.001) * 0.1 / 1e4)
        self.assertAlmostEqual(fr_slip["invested_rmb"], round(expected_notional, 2), places=2)
        self.assertAlmostEqual(fr_slip["buy_fees_rmb"], round(expected_fees, 2), places=2)
        self.assertAlmostEqual(
            fr_slip["cash_drag_pct"],
            round(100.0 * (1.0 - (expected_notional + expected_fees) / 100000.0), 2),
            places=2,
        )

    def test_rollback_status(self):
        dates = [f"2027{m:02d}{d:02d}" for m in range(1, 5) for d in range(1, 29)][:70]
        rows = []
        for d in dates:
            rows.append({"book": v2.PRODUCT_BOOK, "date": d, "ret_9.6bp": -0.002})
            rows.append({"book": "bench_ew", "date": d, "ret_9.6bp": 0.0})
        r = v2.rollback_status(pd.DataFrame(rows))
        self.assertEqual(r["sessions"], 70)
        self.assertAlmostEqual(r["rel_excess_60d"], (1 - 0.002) ** 60 - 1, places=5)
        self.assertTrue(r["triggered"])
        mild = [{**x, "ret_9.6bp": (-0.001 if x["book"] == v2.PRODUCT_BOOK else 0.0)} for x in rows]
        r_mild = v2.rollback_status(pd.DataFrame(mild))
        self.assertFalse(r_mild["triggered"])
        self.assertAlmostEqual(r_mild["rel_excess_60d"], 0.999 ** 60 - 1, places=5)
        r2 = v2.rollback_status(pd.DataFrame(rows[:20]))
        self.assertIsNone(r2["rel_excess_60d"])
        self.assertFalse(r2["triggered"])
        self.assertEqual(v2.rollback_status(None)["sessions"], 0)

    def test_product_file_schema_and_validation(self):
        df = synth_df()
        order = v2.profit_order(df)
        ind = df.set_index("ts_code")["ind_sw1_name"].to_dict()
        tgt = v2.select_product_target(order, ind, set())
        fr = v2.retail_friction(df.set_index("ts_code")["close"].reindex(tgt).to_dict())
        rb = v2.rollback_status(None)
        pf = v2.build_product_file("20261125", df, synth_meta(), tgt, set(tgt[:3]), True, True, rb, fr, "2026-11-25T23:52:00+08:00")
        self.assertEqual(v2.validate_product_file(pf), [])
        self.assertEqual(pf["schema"], "core7p_top25_v1")
        self.assertEqual(pf["status"], "ok")
        self.assertEqual(pf["friction"]["slippage_bps"], 10)
        self.assertEqual(pf["friction"]["stamp_duty_sell_bps"], 5)
        self.assertEqual(set(pf["friction"]["accounts"]), {"100000", "300000", "1000000"})
        self.assertEqual(set(pf["friction"]["accounts"]["100000"]), {"names_buyable", "cash_drag_pct"})
        self.assertEqual(pf["rollback"]["rule"], v2.ROLLBACK_RULE)
        h = pf["holdings"]
        self.assertEqual([x["rank"] for x in h], list(range(1, 26)))
        self.assertEqual([x["constrained_rank"] for x in h], [order.index(x["ts_code"]) + 1 for x in h])
        self.assertEqual([x["action"] for x in h[:4]], ["BUY", "BUY", "BUY", "HOLD"])
        self.assertTrue(all(x["code"] == x["ts_code"][:6] and x["plan_status"] == "planned_T+1_open" for x in h))
        self.assertEqual(set(h[0]), {"rank", "constrained_rank", "ts_code", "code", "name", "industry", "score_pct", "dedt_ttm",
                                     "dedt_yoy", "rev_yoy", "roe_dedt", "ocf_ttm", "ocf_applicable", "pe_ttm", "pb",
                                     "dedt_source", "latest_report_date", "latest_ann_date", "action", "plan_status"})
        json.dumps(pf)
        pf2 = v2.build_product_file("20261125", df, synth_meta(0.5), tgt, set(), False, False, rb, fr, "x")
        self.assertEqual(pf2["status"], "degraded")
        self.assertFalse(pf2["official"])
        bad = json.loads(json.dumps(pf))
        bad["holdings"][0]["dedt_ttm"] = -1.0
        bad["holdings"][1]["ts_code"] = "688001.SH"
        bad["holdings"][2] = dict(bad["holdings"][3])
        errs = v2.validate_product_file(bad)
        self.assertTrue(any(e.startswith("dedt_ttm") for e in errs))
        self.assertIn("board", errs)
        self.assertIn("duplicate_codes", errs)
        short = json.loads(json.dumps(pf))
        short["holdings"] = short["holdings"][:10]
        self.assertTrue(any(e.startswith("n_holdings") for e in v2.validate_product_file(short)))
        priv = json.loads(json.dumps(pf))
        priv["rule"] = "/Users/x/y"
        self.assertIn("absolute_path", v2.validate_product_file(priv))

    def test_parity_ok_function(self):
        self.assertTrue(v2.parity_ok({"a": 0.9995, "b": 1.0}, 500))
        self.assertTrue(v2.parity_ok({"a": 0.9995, "b": 1.0}, 500, n_old=500, n_new=500, top25_overlap=25))
        self.assertFalse(v2.parity_ok({"a": 0.9995, "b": 1.0}, 499, n_old=500, n_new=499, top25_overlap=25))
        self.assertFalse(v2.parity_ok({"a": 0.9995, "b": 1.0}, 500, n_old=500, n_new=500, top25_overlap=24))
        self.assertFalse(v2.parity_ok({"a": 0.998}, 500))
        self.assertFalse(v2.parity_ok({"a": 1.0}, 50))
        self.assertFalse(v2.parity_ok({}, 500))

    def test_spec_hash_stable_and_contract_fields(self):
        self.assertEqual(v2.spec_hash(), v2.spec_hash())
        self.assertEqual(v2.SPEC["spec_id"], "forward_shadow_v2")
        self.assertEqual(v2.SPEC["costs_one_way"], {"9.6bp": 0.00096, "15.1bp": 0.00151, "20bp": 0.0020})
        self.assertEqual(v2.SPEC["evaluation"]["sequential_looks"],
                         {"2027-05-31": 3.46, "2027-11-30": 2.45, "2028-05-31": 2.0, "2028-11-30": 1.73})
        self.assertFalse(v2.BOOKS[v2.PRODUCT_BOOK]["judged"])
        self.assertTrue(v2.BOOKS[v2.PRIMARY_BOOK]["judged"])
        self.assertIn("23:40", v2.SPEC["schedule"])
        self.assertIn("08:40", v2.SPEC["schedule"])
        self.assertIn("09:35", v2.SPEC["schedule"])
        self.assertIn("rollback.triggered", v2.SPEC["product_file"]["rollback"])

    def test_pipelines_running_fail_closed(self):
        res_1 = mock.MagicMock(returncode=1)
        res_0 = mock.MagicMock(returncode=0)
        res_2 = mock.MagicMock(returncode=2)
        with mock.patch.object(v2.subprocess, "run", side_effect=[res_1, res_1]):
            self.assertFalse(v2.pipelines_running())
        with mock.patch.object(v2.subprocess, "run", side_effect=[res_0]):
            self.assertTrue(v2.pipelines_running())
        with mock.patch.object(v2.subprocess, "run", side_effect=[res_1, res_2]):
            self.assertTrue(v2.pipelines_running())
        with mock.patch.object(v2.subprocess, "run", side_effect=OSError("no pgrep")):
            self.assertTrue(v2.pipelines_running())


class Replay(unittest.TestCase):
    def test_replay_all_books_with_blocked_limit_up(self):
        df = synth_df(n=240)
        cal = [f"202611{d:02d}" for d in range(2, 12)]
        codes = df.ts_code.tolist()
        decs = []
        for i in range(5):
            books = {b: v2.book_lists(df, b) for b in v2.BOOKS}
            decs.append({"signal_date": cal[i], "anchor": cal[0], "subbook": i, "universe": codes, "books": books,
                         "official_books": {b: True for b in v2.BOOKS}, "degraded": False})
        blocked = set(v2.book_lists(df, v2.PRIMARY_BOOK)["entry"][:3])

        def quotes(d, st):
            return {c: (0.0, 0.001, 0.001, c not in blocked, True) for c in codes}

        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        self.assertEqual(set(out.book), set(v2.BOOKS) | {"bench_ew"})
        for b in v2.BOOKS:
            g = out[out.book == b]
            self.assertGreater(len(g), 0)
            self.assertTrue(g["ret_9.6bp"].notna().all())
            self.assertTrue((g["ret_20bp"] <= g["ret_9.6bp"] + 1e-12).all())
        prof = out[out.book == v2.PRODUCT_BOOK]
        self.assertLessEqual(prof["n_hold"].max(), 25 + 1e-9)
        pct = out[out.book == v2.PRIMARY_BOOK]
        self.assertTrue(pct["n_hold"].max() > 20)

    def test_replay_freezes_suspended_limit_up_and_limit_down_retained_holdings(self):
        # 6-day calendar: subbook 0 trades on day 1 (buys A, B, C, D) and rebalances on day 6
        cal = [f"202611{d:02d}" for d in range(2, 9)]
        codes = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH", "600006.SH"]
        ind = {c: f"I{i}" for i, c in enumerate(codes)}
        L1 = {
            v2.PRIMARY_BOOK: {"entry": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"], "hold": sorted(codes[:4])},
            v2.PRODUCT_BOOK: {"entry": codes[:4], "hold": sorted(codes[:4]), "size": 4, "industry": ind},
            v2.REF_BOOK: {"entry": codes[:4], "hold": sorted(codes[:4])},
        }
        # Rebalance 2 (subbook 0 on cal[5] -> executes cal[6]):
        # - 600001.SH surged (+50% on open) and is LIMIT-DOWN on cal[6] (cannot sell excess!)
        # - 600002.SH is SUSPENDED on cal[6] (absent from quotes -> cannot buy or sell!)
        # - 600003.SH dropped (-40% on open) and is LIMIT-UP on cal[6] (cannot buy more!)
        # - 600004.SH dropped and is normal; 600005.SH is a new buy (buyable)
        hold_l2 = sorted(["600001.SH", "600002.SH", "600003.SH", "600005.SH"])
        L2 = {
            v2.PRIMARY_BOOK: {"entry": ["600005.SH", "600001.SH", "600002.SH", "600003.SH"], "hold": hold_l2},
            v2.PRODUCT_BOOK: {"entry": ["600005.SH", "600001.SH", "600002.SH", "600003.SH"], "hold": hold_l2, "size": 4, "industry": ind},
            v2.REF_BOOK: {"entry": ["600005.SH", "600001.SH", "600002.SH", "600003.SH"], "hold": hold_l2},
        }
        decs = []
        for i in range(5):
            decs.append({"signal_date": cal[i], "anchor": cal[0], "subbook": i, "universe": codes, "books": L1,
                         "official_books": {b: True for b in v2.BOOKS}, "degraded": False})
        decs.append({"signal_date": cal[5], "anchor": cal[0], "subbook": 0, "universe": codes, "books": L2,
                     "official_books": {b: True for b in v2.BOOKS}, "degraded": False})

        def quotes(d, st):
            if d != cal[6]:
                return {c: (0.0, 0.0, 0.0, True, True) for c in codes}
            return {
                "600001.SH": (0.50, 0.0, 0.50, True, False),   # limit-down: cannot sell excess
                # 600002.SH suspended: omitted from q!
                "600003.SH": (-0.40, 0.0, -0.40, False, True), # limit-up: cannot buy more
                "600004.SH": (0.0, 0.0, 0.0, True, True),      # exited & sold
                "600005.SH": (0.0, 0.0, 0.0, True, True),      # new buy
                "600006.SH": (0.0, 0.0, 0.0, True, True),
            }

        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        # Verify turnover on cal[6] for PRODUCT_BOOK: ONLY 600004 sold (0.25) and 600005 bought!
        row = out[(out.book == v2.PRODUCT_BOOK) & (out.date == cal[6])].iloc[0]
        self.assertLess(row["turnover"], 0.06)
        self.assertGreater(row["turnover"], 0.04)

    def test_replay_cascading_freeze(self):
        # Day 1 buys A, B, C, D (each 0.25).
        # On Day 6 open: A is +50% (0.375) and limit-down; B is -12% (0.22) and ALSO limit-down; C & D stay at 0.25.
        # Iter 1: eq = 1.095 / 4 = 0.27375 -> A (0.375 > eq) freezes, B (0.22 < eq) wants to BUY so doesn't freeze yet!
        # Iter 2: avail = (1.095 - 0.375) = 0.72, flex = {B, C, D} -> eq = 0.24 -> B (0.22 < 0.24) still buys!
        # Actually make B +0% (0.25) and C -30% (0.175): iter 1 eq = 1.05 / 4 = 0.2625 -> A freezes at 0.375!
        # Iter 2: avail = 0.675 / 3 = 0.225 -> NOW B (0.25 > 0.225) wants to SELL and freezes at 0.25!
        cal = [f"202611{d:02d}" for d in range(2, 9)]
        codes = ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]
        ind = {c: f"I{i}" for i, c in enumerate(codes)}
        L1 = {
            v2.PRIMARY_BOOK: {"entry": codes, "hold": codes},
            v2.PRODUCT_BOOK: {"entry": codes, "hold": codes, "size": 4, "industry": ind},
            v2.REF_BOOK: {"entry": codes, "hold": codes},
        }
        decs = [{"signal_date": cal[i], "anchor": cal[0], "subbook": i % 5, "universe": codes, "books": L1,
                 "official_books": {b: True for b in v2.BOOKS}, "degraded": False} for i in range(6)]

        def quotes(d, st):
            if d != cal[6]:
                return {c: (0.0, 0.0, 0.0, True, True) for c in codes}
            return {
                "600001.SH": (0.50, 0.0, 0.50, True, False),   # 0.375, limit-down
                "600002.SH": (0.00, 0.0, 0.00, True, False),   # 0.250, limit-down (freezes only in iter 2!)
                "600003.SH": (-0.30, 0.0, -0.30, True, True),  # 0.175 -> buys to 0.2125
                "600004.SH": (0.00, 0.0, 0.00, True, True),    # 0.250 -> sells to 0.2125
            }

        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        row = out[(out.book == v2.PRODUCT_BOOK) & (out.date == cal[6])].iloc[0]
        # Only C buys 0.0375 and D sells 0.0375 -> subbook turnover ~ 0.0375 / 1.05 = 0.0357; / 5 ~ 0.0071
        self.assertLess(row["turnover"], 0.01)
        self.assertGreater(row["turnover"], 0.005)

    def test_replay_all_frozen_keeps_leftover_cash(self):
        # Day 1 buys 600001.SH + 600002.SH; Day 6 keeps 600001.SH (suspended) and sells 600002.SH -> flex_set empty, cash retained!
        cal = [f"202611{d:02d}" for d in range(2, 9)]
        codes = ["600001.SH", "600002.SH"]
        ind = {c: f"I{i}" for i, c in enumerate(codes)}
        L1 = {
            v2.PRIMARY_BOOK: {"entry": codes, "hold": codes},
            v2.PRODUCT_BOOK: {"entry": codes, "hold": codes, "size": 2, "industry": ind},
            v2.REF_BOOK: {"entry": codes, "hold": codes},
        }
        L2 = {
            v2.PRIMARY_BOOK: {"entry": ["600001.SH"], "hold": ["600001.SH"]},
            v2.PRODUCT_BOOK: {"entry": ["600001.SH"], "hold": ["600001.SH"], "size": 1, "industry": ind},
            v2.REF_BOOK: {"entry": ["600001.SH"], "hold": ["600001.SH"]},
        }
        decs = [{"signal_date": cal[i], "anchor": cal[0], "subbook": i, "universe": codes, "books": L1,
                 "official_books": {b: True for b in v2.BOOKS}, "degraded": False} for i in range(5)]
        decs.append({"signal_date": cal[5], "anchor": cal[0], "subbook": 0, "universe": codes, "books": L2,
                     "official_books": {b: True for b in v2.BOOKS}, "degraded": False})

        def quotes(d, st):
            if d != cal[6]:
                return {c: (0.0, 0.0, 0.0, True, True) for c in codes}
            return {"600002.SH": (0.0, 0.0, 0.0, True, True)}  # 600001.SH suspended

        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        # Portfolio return on cal[6] should only reflect transaction cost of selling 0.5 of V (around -0.0001), NOT losing half the portfolio!
        row = out[(out.book == v2.PRODUCT_BOOK) & (out.date == cal[6])].iloc[0]
        self.assertGreater(row["ret_9.6bp"], -0.001)
        self.assertLess(row["ret_9.6bp"], 0.0)

    def test_replay_pre_blocked_counts_toward_industry_cap_and_slots(self):
        cal = [f"202611{d:02d}" for d in range(2, 9)]
        # A, B, C, D in SAME; N is new top-ranked candidate in SAME; O1..O4 in OTHER industries
        codes = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH",
                 "600006.SH", "600007.SH", "600008.SH", "600009.SH"]
        ind = {c: ("SAME" if i < 5 else f"OTH{i}") for i, c in enumerate(codes)}
        L1 = {
            v2.PRIMARY_BOOK: {"entry": codes[:4], "hold": sorted(codes[:4])},
            v2.PRODUCT_BOOK: {"entry": codes[:4], "hold": sorted(codes[:4]), "size": 4, "industry": ind},
            v2.REF_BOOK: {"entry": codes[:4], "hold": sorted(codes[:4])},
        }
        # Day 6: 600001.SH (SAME) dropped from hold AND is suspended (pre_blocked!).
        # 600002..600004 (SAME) stay in hold; 600005 (SAME) is #1 new entry -> MUST NOT enter because SAME already has 1 pre_blocked + 3 kept = 4!
        hold2 = sorted(["600002.SH", "600003.SH", "600004.SH", "600005.SH", "600006.SH"])
        entry2 = ["600005.SH", "600002.SH", "600003.SH", "600004.SH", "600006.SH", "600007.SH"]
        L2 = {
            v2.PRIMARY_BOOK: {"entry": entry2, "hold": hold2},
            v2.PRODUCT_BOOK: {"entry": entry2, "hold": hold2, "size": 4, "industry": ind},
            v2.REF_BOOK: {"entry": entry2, "hold": hold2},
        }
        decs = [
            {"signal_date": cal[i], "anchor": cal[0], "subbook": i, "universe": codes, "books": L1,
             "official_books": {b: True for b in v2.BOOKS}, "degraded": False}
            for i in range(5)
        ]
        decs.append({"signal_date": cal[5], "anchor": cal[0], "subbook": 0, "universe": codes, "books": L2,
                     "official_books": {b: True for b in v2.BOOKS}, "degraded": False})

        def quotes(d, st):
            if d != cal[6]:
                return {c: (0.0, 0.0, 0.0, True, True) for c in codes}
            # 600001.SH suspended; all others tradeable
            return {c: (0.0, 0.0, 0.0, True, True) for c in codes if c != "600001.SH"}

        # Directly verify _targets reserves pre_blocked industry & slot budget:
        # Even with size=5 (1 free slot left after 1 pre_blocked + 3 kept), 600005.SH (SAME) MUST be skipped by CAP=4
        # and 600006.SH (OTH6) MUST be selected instead!
        q6 = quotes(cal[6], set())
        held_keep = {"600002.SH", "600003.SH", "600004.SH"}
        pre_blocked = {"600001.SH"}
        L_cap5 = {**L2[v2.PRODUCT_BOOK], "size": 5}
        tgt5 = v2._targets(v2.PRODUCT_BOOK, L_cap5, held_keep, q6, pre_blocked=pre_blocked)
        self.assertEqual(len(tgt5), 4)  # 5 total slots - 1 pre_blocked = 4
        self.assertNotIn("600005.SH", tgt5)  # SAME cap=4 already full (1 pre_blocked + 3 kept)
        self.assertIn("600006.SH", tgt5)     # filled from OTH6 instead
        self.assertNotIn("600001.SH", tgt5)  # stays out of newset so replay marks it pending sell

        tgt = v2._targets(v2.PRODUCT_BOOK, L2[v2.PRODUCT_BOOK], held_keep, q6, pre_blocked=pre_blocked)
        self.assertEqual(len(tgt), 3)  # 4 total slots - 1 pre_blocked = 3
        self.assertNotIn("600005.SH", tgt)
        self.assertNotIn("600001.SH", tgt)

        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        row = out[(out.book == v2.PRODUCT_BOOK) & (out.date == cal[6])].iloc[0]
        # Subbooks 1..4 hold 4 names; subbook 0 holds 1 pre_blocked + 3 kept = 4 -> average 4.0 (never >4)
        self.assertAlmostEqual(row["n_hold"], 4.0, places=9)

    def test_replay_post_fee_freeze_prevents_selling_limit_down_equal_weight_stock(self):
        cal = [f"202611{d:02d}" for d in range(2, 9)]
        codes = ["600001.SH", "600002.SH", "600003.SH"]
        ind = {c: f"I{i}" for i, c in enumerate(codes)}
        L1 = {
            v2.PRIMARY_BOOK: {"entry": codes[:2], "hold": codes[:2]},
            v2.PRODUCT_BOOK: {"entry": codes[:2], "hold": codes[:2], "size": 2, "industry": ind},
            v2.REF_BOOK: {"entry": codes[:2], "hold": codes[:2]},
        }
        # Day 6: keep 600001.SH (exact equal weight 0.49952, LIMIT-DOWN cannot sell!), sell 600002.SH, buy 600003.SH.
        # Post-fee target < 0.49952 must freeze 600001.SH so it is NOT trimmed by transaction costs!
        L2 = {
            v2.PRIMARY_BOOK: {"entry": ["600001.SH", "600003.SH"], "hold": ["600001.SH", "600003.SH"]},
            v2.PRODUCT_BOOK: {"entry": ["600001.SH", "600003.SH"], "hold": ["600001.SH", "600003.SH"], "size": 2, "industry": ind},
            v2.REF_BOOK: {"entry": ["600001.SH", "600003.SH"], "hold": ["600001.SH", "600003.SH"]},
        }
        decs = [
            {"signal_date": cal[i], "anchor": cal[0], "subbook": i, "universe": codes, "books": L1,
             "official_books": {b: True for b in v2.BOOKS}, "degraded": False}
            for i in range(5)
        ]
        decs.append({"signal_date": cal[5], "anchor": cal[0], "subbook": 0, "universe": codes, "books": L2,
                     "official_books": {b: True for b in v2.BOOKS}, "degraded": False})

        def quotes(d, st):
            if d != cal[6]:
                return {c: (0.0, 0.0, 0.0, True, True) for c in codes}
            return {
                "600001.SH": (0.0, 0.0, 0.0, True, False),   # limit-down: cannot sell!
                "600002.SH": (0.0, 0.0, 0.0, True, True),
                "600003.SH": (0.0, 0.0, 0.0, True, True),
            }

        # With 9.6bp (0.00096): day 1 buys 0.5 each -> w=0.49952 each, V=0.99904.
        # On day 6: 600001 freezes at 0.49952 (no sell!), 600002 sells 0.49952, 600003 buys (0.49952*(1-0.00096)/(1+0.00096)).
        # Compare turnover: only 600002 sold + 600003 bought (zero trade on 600001!).
        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        row = out[(out.book == v2.PRODUCT_BOOK) & (out.date == cal[6])].iloc[0]
        # Only subbook 0 trades: amt = 0.49952 + 0.49856184 = 0.99808184; to = (amt/2)/0.99904 / 5 = 0.099904...
        # Verify exact subbook 0 return reflects only 600002+600003 fees (not selling 600001):
        # V_end(sub0) = 0.49952 + 0.49856184 = 0.99808184 -> ret = -0.00095908 -> mean over 5 subbooks = -0.000191816
        self.assertAlmostEqual(row["ret_9.6bp"], ((0.49952 + 0.49952 * (1 - 0.00096) / (1 + 0.00096)) / 0.99904 - 1) / 5.0, places=9)

    def test_replay_empty_quotes_skips_bench_ew_and_marks_missed_subbooks_non_official(self):
        cal = [f"202611{d:02d}" for d in range(2, 11)]  # 9 sessions: cal[0..8]
        codes = ["600001.SH", "600002.SH", "600003.SH"]
        ind = {c: f"I{i}" for i, c in enumerate(codes)}
        L1 = {
            v2.PRIMARY_BOOK: {"entry": codes[:2], "hold": codes[:2]},
            v2.PRODUCT_BOOK: {"entry": codes[:2], "hold": codes[:2], "size": 2, "industry": ind},
            v2.REF_BOOK: {"entry": codes[:2], "hold": codes[:2]},
        }
        # Decisions on cal[0..4] (subbooks 0..4), cal[5] (subbook 0 -> executes cal[6]), cal[6] (subbook 1 -> executes cal[7])
        decs = [
            {"signal_date": cal[i], "anchor": cal[0], "subbook": i, "universe": codes, "books": L1,
             "official_books": {b: True for b in v2.BOOKS}, "degraded": False}
            for i in range(5)
        ]
        decs.append({"signal_date": cal[5], "anchor": cal[0], "subbook": 0, "universe": codes, "books": L1,
                     "official_books": {b: True for b in v2.BOOKS}, "degraded": False})
        decs.append({"signal_date": cal[6], "anchor": cal[0], "subbook": 1, "universe": codes, "books": L1,
                     "official_books": {b: True for b in v2.BOOKS}, "degraded": False})

        def quotes(d, st):
            if d == cal[6]:
                return {}
            return {c: (0.0, 0.01, 0.01, True, True) for c in codes}

        with mock.patch.object(v2.fsm, "day_quotes", quotes), mock.patch.object(v2.fsm, "_st", lambda d: set()):
            out = v2.replay(decs, cal, cal[-1])
        # Missing quotes on cal[6] must NOT emit any row (bench_ew or books) for cal[6]
        self.assertNotIn(cal[6], set(out["date"]))
        bench = out[out["book"] == "bench_ew"]
        self.assertIn(cal[5], set(bench["date"]))
        self.assertIn(cal[7], set(bench["date"]))
        for b in v2.BOOKS:
            sub_df = out[out["book"] == b]
            self.assertTrue(bool(sub_df.loc[sub_df["date"] == cal[5], "official_day"].iloc[0]))
            # Subbook 0 missed rebalance on cal[6] -> stays non-official on cal[7] and cal[8]
            self.assertFalse(bool(sub_df.loc[sub_df["date"] == cal[7], "official_day"].iloc[0]))
            self.assertFalse(bool(sub_df.loc[sub_df["date"] == cal[8], "official_day"].iloc[0]))


class Ledger(unittest.TestCase):
    """End-to-end run() with every external input mocked and OUT redirected to a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "v2"
        self.fs_v1 = self.root / "fs_v1"
        (self.fs_v1 / "scores").mkdir(parents=True)
        self.df = synth_df()
        self.df[["ts_code"]].assign(comp_rank_ew6=0.5).to_parquet(self.fs_v1 / "scores" / "scores_20261125.parquet", index=False)
        self.meta = synth_meta()
        self.spec_path = self.root / "forward_shadow_v2.json"
        self.spec_path.write_text(json.dumps({**v2.SPEC, "spec_hash": v2.spec_hash(), "frozen_at": "2026-11-26"}))
        self.cal = ["20261124", "20261125", "20261126"]
        self.px = synth_px(self.df.ts_code)
        bc = self.root / "backtest_cache"
        bc.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({
            "ts_code": self.df.ts_code.tolist(),
            "source_provider": ["tdx_api"] * len(self.df),
            "pre_close_source": ["tdx_raw"] * len(self.df),
        }).to_parquet(bc / "daily_20261125.parquet", index=False)
        cc_dir = self.root / "stock_data/03-working/health/price_crosscheck"
        cc_dir.mkdir(parents=True, exist_ok=True)
        (cc_dir / "crosscheck_20261125.json").write_text(
            json.dumps({"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 0}),
            encoding="utf-8",
        )
        self.patches = [
            mock.patch.object(v2, "OUT", self.out), mock.patch.object(v2, "FS_V1", self.fs_v1),
            mock.patch.object(v2, "SPEC_PATH", self.spec_path),
            mock.patch.object(v2, "WS", self.root),
            mock.patch.object(v2.fsm.rp, "BACKTEST_CACHE", self.root / "backtest_cache"),
            mock.patch.object(v2.fs, "official_sessions", lambda upto: list(self.cal)),
            mock.patch.object(v2, "v1_recorded", lambda d: {"signal_date": d, "official": True}),
            mock.patch.object(v2.fsm, "day_prices", lambda d: self.px),
            mock.patch.object(v2, "score_day", lambda d: (self.df.copy(), json.loads(json.dumps(self.meta)))),
            mock.patch.object(v2, "pipelines_running", lambda: False),
            mock.patch.object(v2.rp, "git_commit", lambda: "deadbeef"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def clock(self, *times):
        it = iter(times)
        last = times[-1]
        return mock.patch.object(v2, "_now", side_effect=lambda: next(it, last))

    T_NIGHT = datetime(2026, 11, 25, 23, 52, tzinfo=TZ)
    T_EARLY = datetime(2026, 11, 26, 8, 50, tzinfo=TZ)
    T_LATE = datetime(2026, 11, 26, 9, 31, tzinfo=TZ)

    def test_refuses_uncommitted_or_tampered_contract_and_write_spec_test_forbidden(self):
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                v2.run("20261125", False)
        self.assertIn("refusing", str(cm.exception))
        self.assertFalse(self.out.exists())

        # Hash matches spec_hash() on field, but saved spec body was mutated -> canonical verification refuses!
        mutated = {**v2.SPEC, "writes": "tampered/", "spec_hash": v2.spec_hash(), "frozen_at": "2026-11-26"}
        self.spec_path.write_text(json.dumps(mutated))
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True):
            with self.assertRaises(SystemExit):
                v2.run("20261125", False)

        # Bad spec_hash
        self.spec_path.write_text(json.dumps({**v2.SPEC, "spec_hash": "0" * 16}))
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True):
            with self.assertRaises(SystemExit):
                v2.run("20261125", False)

        # Missing spec
        self.spec_path.unlink()
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True):
            with self.assertRaises(SystemExit):
                v2.run("20261125", False)

        # --write-spec + --test forbidden
        with self.assertRaises(SystemExit):
            v2.main(["--write-spec", "--test"])

    def test_test_mode_writes_only_under_test_dir_and_uses_flock(self):
        with mock.patch.object(v2.fcntl, "flock", wraps=fcntl.flock) as m_flock, self.clock(self.T_NIGHT, self.T_NIGHT):
            r = v2.run("20261125", True)
        self.assertGreaterEqual(m_flock.call_count, 2)
        self.assertTrue(any(call.args[1] == fcntl.LOCK_EX for call in m_flock.call_args_list))
        self.assertTrue((self.out / "test/.v2.lock").exists())
        self.assertEqual(r["status"], "decided")
        self.assertFalse(r["official"])
        self.assertFalse(any(r["official_books"].values()))
        files = [p for p in self.out.rglob("*") if p.is_file() and p.name != ".v2.lock"]
        self.assertTrue(files)
        self.assertTrue(all((self.out / "test") in p.parents for p in files), [str(p) for p in files])
        self.assertTrue((self.out / "test/product/core7p_top25_20261125.json").exists())
        self.assertTrue((self.out / "test/scores/v2_scores_20261125.parquet").exists())
        self.assertFalse((self.out / "gaps.jsonl").exists())
        self.assertFalse((self.out / "product").exists() and any((self.out / "product").iterdir()))
        self.assertTrue(r["product_valid"])

    def test_official_when_before_deadline_and_healthy_and_self_heals(self):
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT, self.T_NIGHT):
            r = v2.run("20261125", False)
        self.assertTrue(r["official"])
        self.assertTrue(all(r["official_books"].values()))
        self.assertFalse((self.out / "gaps.jsonl").exists() and (self.out / "gaps.jsonl").read_text().strip())
        pp = self.out / "product/core7p_top25_20261125.json"
        pf = json.loads(pp.read_text())
        self.assertTrue(pf["official"])
        self.assertEqual(pf["spec_hash"], v2.spec_hash())
        state = json.loads((self.out / "state.json").read_text())
        self.assertEqual(state["coverage"]["qarp_real_dedt_cov"], 1.0)
        self.assertEqual(state["degraded_pillars"], [])
        self.assertTrue(state["official"])
        self.assertTrue(all(state["official_books"].values()))
        decs = v2.fs.read_jsonl(self.out / "decisions.jsonl")
        self.assertEqual(len(decs), 1)
        self.assertLessEqual(len(decs[0]["top_ranked"]), 400)

        # Simulate partial crash: remove product file and state.json, then rerun -> self-heals cleanly
        pp.unlink()
        (self.out / "state.json").unlink()
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT):
            r2 = v2.run("20261125", False)
        self.assertEqual(r2["status"], "already_decided")
        self.assertEqual(len(v2.fs.read_jsonl(self.out / "decisions.jsonl")), 1)
        self.assertTrue(pp.exists())
        self.assertTrue((self.out / "state.json").exists())
        pf_healed = json.loads(pp.read_text())
        self.assertIn("constrained_rank", pf_healed["holdings"][0])
        state_healed = json.loads((self.out / "state.json").read_text())
        self.assertEqual(state_healed["coverage"]["qarp_real_dedt_cov"], 1.0)

    def test_exdiv_unavailable_fails_official_base(self):
        bc = self.root / "backtest_cache"
        bc.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"ts_code": ["600000.SH"], "pre_close_source": ["unavailable"]}).to_parquet(bc / "daily_20261125.parquet")
        # 1) events parquet missing -> fails official_base with reason exdiv_unavailable
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT, self.T_NIGHT):
            r = v2.run("20261125", False)
        self.assertFalse(r["official"])
        self.assertFalse(any(r["official_books"].values()))
        gaps = v2.fs.read_jsonl(self.out / "gaps.jsonl")
        self.assertEqual([g["reason"] for g in gaps], ["exdiv_unavailable"])

        # 2) events parquet present with len > 0 -> still fails exdiv_ok
        ev_dir = self.root / "stock_data/03-working/fundamental_cache/ex_dividend"
        ev_dir.mkdir(parents=True)
        pd.DataFrame([{"ts_code": "600000.SH", "ex_date": "20261125"}]).to_parquet(ev_dir / "events_20261125_20261125.parquet")
        self.assertFalse(v2.exdiv_ok("20261125"))

        # 3) events parquet present with 0 rows -> passes exdiv_ok
        pd.DataFrame(columns=["ts_code", "ex_date"]).to_parquet(ev_dir / "events_20261125_20261125.parquet")
        self.assertTrue(v2.exdiv_ok("20261125"))

    def test_clock_reread_after_scoring_makes_late(self):
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_EARLY, self.T_LATE):
            r = v2.run("20261125", False)
        self.assertEqual(r["status"], "decided")
        self.assertFalse(r["official"])
        self.assertFalse(any(r["official_books"].values()))
        gaps = v2.fs.read_jsonl(self.out / "gaps.jsonl")
        self.assertEqual([g["reason"] for g in gaps], ["late"])
        dec = v2.fs.read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertTrue(dec["late"])

    def test_low_fin_coverage_waits_then_non_official(self):
        self.meta = synth_meta(0.5)
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT):
            r = v2.run("20261125", False)
        self.assertEqual(r["status"], "waiting_for_fundamentals")
        self.assertFalse((self.out / "decisions.jsonl").exists() and (self.out / "decisions.jsonl").read_text().strip())
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_EARLY, self.T_EARLY):
            r = v2.run("20261125", False)
        self.assertEqual(r["status"], "decided")
        self.assertFalse(r["official_books"][v2.PRIMARY_BOOK])
        self.assertFalse(r["official_books"][v2.PRODUCT_BOOK])
        self.assertTrue(r["official_books"][v2.REF_BOOK])
        self.assertTrue(r["degraded"])
        gaps = v2.fs.read_jsonl(self.out / "gaps.jsonl")
        self.assertEqual([g["reason"] for g in gaps], ["fin_coverage_low"])
        pf = json.loads((self.out / "product/core7p_top25_20261125.json").read_text())
        self.assertEqual(pf["status"], "degraded")
        self.assertFalse(pf["official"])

    def test_proxy_universe_is_non_official(self):
        self.meta = synth_meta(mode="pit_proxy_accepted")
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT, self.T_NIGHT):
            r = v2.run("20261125", False)
        self.assertFalse(r["official"])
        gaps = v2.fs.read_jsonl(self.out / "gaps.jsonl")
        self.assertEqual([g["reason"] for g in gaps], ["non_official_inputs"])

    def test_kline_gate_failure_writes_no_decision(self):
        self.px = self.px.copy()
        self.px.loc[self.px.index[:20], "close"] = np.nan
        self.px.loc[self.px.index[:20], "vol"] = 1e6
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT):
            r = v2.run("20261125", False)
        self.assertEqual(r["status"], "kline_gate_failed")
        self.assertFalse((self.out / "decisions.jsonl").exists() and (self.out / "decisions.jsonl").read_text().strip())

    def test_parity_function_roundtrip_and_strict_checks(self):
        with self.clock(self.T_NIGHT, self.T_NIGHT):
            v2.run("20261125", True)
        rec = v2.parity("20261125", True, recompute=lambda d: self.df.copy())
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["n_old"], rec["n_new"], rec["n_matched"], rec["top25_overlap"]), (240, 240, 240, 25))
        self.assertGreaterEqual(rec["spearman"]["core7p_native"], 0.999999)

        # Dropping 1 row fails full universe equality
        rec_drop = v2.parity("20261125", True, recompute=lambda d: self.df.iloc[:-1].copy())
        self.assertFalse(rec_drop["ok"])
        self.assertEqual((rec_drop["n_old"], rec_drop["n_new"], rec_drop["n_matched"]), (240, 239, 239))

        # Swapping top-25 rank #25 with #26 keeps Spearman >= 0.999 but fails top25_overlap == 25
        swapped = self.df.copy()
        s24 = float(swapped.loc[24, "core7p_native"])
        s25 = float(swapped.loc[25, "core7p_native"])
        swapped.loc[24, "core7p_native"] = s25
        swapped.loc[25, "core7p_native"] = s24
        rec_swap = v2.parity("20261125", True, recompute=lambda d: swapped)
        self.assertGreaterEqual(rec_swap["spearman"]["core7p_native"], 0.999)
        self.assertEqual(rec_swap["top25_overlap"], 24)
        self.assertFalse(rec_swap["ok"])

        shuffled = self.df.copy()
        shuffled["core7p_native"] = np.random.default_rng(1).permutation(shuffled["core7p_native"].values)
        rec2 = v2.parity("20261125", True, recompute=lambda d: shuffled)
        self.assertFalse(rec2["ok"])
        self.assertEqual(v2.parity("20250101", True)["status"], "no_stored_scores")
        self.assertEqual(len(v2.fs.read_jsonl(self.out / "test/parity.jsonl")), 4)

    def test_exdiv_ok_fail_closed_cases(self):
        dp = self.root / "backtest_cache/daily_20261125.parquet"
        cc_dir = self.root / "stock_data/03-working/health/price_crosscheck"
        cc_dir.mkdir(parents=True, exist_ok=True)
        cc_p = cc_dir / "crosscheck_20261125.json"

        # 1) clean daily + clean crosscheck -> True
        cc_p.write_text(
            json.dumps({"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 0}),
            encoding="utf-8",
        )
        self.assertTrue(v2.exdiv_ok("20261125"))

        # 2) fail-closed cases for has_tdx=True: empty {}, events_missing, None, False, error:*, mismatch, unavailable_rows > 0, bad types, missing file
        for bad_payload in (
            {},
            [],
            {"pre_close_exdiv_ok": None, "pre_close_exdiv_status": "events_missing", "pre_close_exdiv_unavailable_rows": 0},
            {"pre_close_exdiv_ok": None, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 0},
            {"pre_close_exdiv_ok": False, "pre_close_exdiv_status": "mismatch", "pre_close_exdiv_unavailable_rows": 0},
            {"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "mismatch", "pre_close_exdiv_unavailable_rows": 0},
            {"pre_close_exdiv_ok": None, "pre_close_exdiv_status": "error:RuntimeError", "pre_close_exdiv_unavailable_rows": 0},
            {"pre_close_exdiv_ok": "error:Boom", "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 0},
            {"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 1},
            {"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": True},
            {"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": False},
            {"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": "bad"},
        ):
            cc_p.write_text(json.dumps(bad_payload), encoding="utf-8")
            self.assertFalse(v2.exdiv_ok("20261125"), bad_payload)
        cc_p.write_text('{"pre_close_exdiv_ok": true, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": NaN}', encoding="utf-8")
        self.assertFalse(v2.exdiv_ok("20261125"))

        # 3) corrupt or missing crosscheck JSON when has_tdx=True -> False
        cc_p.write_text("{bad json", encoding="utf-8")
        self.assertFalse(v2.exdiv_ok("20261125"))
        cc_p.unlink()
        self.assertFalse(v2.exdiv_ok("20261125"))

        # 4) tdx_api missing pre_close_source -> False; non-tdx_api preserves existing crosscheck behavior
        pd.DataFrame({"ts_code": ["600000.SH"], "source_provider": ["tdx_api"]}).to_parquet(dp, index=False)
        self.assertFalse(v2.exdiv_ok("20261125"))
        pd.DataFrame({"ts_code": ["600000.SH"], "source_provider": ["tushare"]}).to_parquet(dp, index=False)
        self.assertTrue(v2.exdiv_ok("20261125"))
        cc_p.write_text(json.dumps({"pre_close_exdiv_ok": None, "pre_close_exdiv_status": "events_missing"}), encoding="utf-8")
        self.assertTrue(v2.exdiv_ok("20261125"))
        cc_p.write_text(json.dumps({"pre_close_exdiv_ok": False, "pre_close_exdiv_status": "mismatch"}), encoding="utf-8")
        self.assertFalse(v2.exdiv_ok("20261125"))

        # 6) corrupt or missing daily parquet -> False
        dp.write_bytes(b"not a parquet")
        self.assertFalse(v2.exdiv_ok("20261125"))
        dp.unlink()
        self.assertFalse(v2.exdiv_ok("20261125"))

    def test_git_commit_and_parquet_run_before_clock_reread_and_atomic_append(self):
        events = []
        now_calls = [self.T_EARLY]
        orig_write_pq = v2._write_parquet_atomic
        orig_append = v2._append_jsonl_atomic

        def fake_now():
            t = now_calls[-1]
            events.append(("now", t))
            return t

        def fake_git():
            events.append(("git_commit", None))
            return "abc123"

        def fake_pq(path, df):
            events.append(("write_parquet", Path(path).name))
            # Wall clock crosses 09:30 during slow parquet write!
            now_calls.append(self.T_LATE)
            return orig_write_pq(path, df)

        def fake_append(path, rec, **kwargs):
            events.append(("append_jsonl", Path(path).name))
            return orig_append(path, rec, **kwargs)

        with (
            mock.patch.object(v2.fs, "committed_and_clean", return_value=True),
            mock.patch.object(v2, "_now", side_effect=fake_now),
            mock.patch.object(v2.rp, "git_commit", side_effect=fake_git),
            mock.patch.object(v2, "_write_parquet_atomic", side_effect=fake_pq),
            mock.patch.object(v2, "_append_jsonl_atomic", side_effect=fake_append),
        ):
            r = v2.run("20261125", False)

        self.assertFalse(r["official"])
        self.assertFalse(any(r["official_books"].values()))
        dec = v2.fs.read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertTrue(dec["late"])
        self.assertEqual([g["reason"] for g in v2.fs.read_jsonl(self.out / "gaps.jsonl")], ["late"])
        names = [e[0] for e in events]
        # Order MUST be: initial now -> git_commit -> write_parquet -> second now -> append_jsonl(decisions)
        self.assertLess(names.index("git_commit"), names.index("append_jsonl"))
        self.assertLess(names.index("write_parquet"), names.index("append_jsonl"))
        second_now = [i for i, e in enumerate(events) if e[0] == "now"][1]
        self.assertLess(names.index("git_commit"), second_now)
        self.assertLess(names.index("write_parquet"), second_now)
        self.assertLess(second_now, names.index("append_jsonl"))

        # _append_jsonl_atomic failure leaves existing decisions.jsonl intact and removes .tmp
        with mock.patch.object(v2.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                v2._append_jsonl_atomic(self.out / "decisions.jsonl", {"signal_date": "20261126"})
        self.assertEqual(len(v2.fs.read_jsonl(self.out / "decisions.jsonl")), 1)
        self.assertEqual(list(self.out.glob(".*.tmp")), [])

    def test_heal_decided_restores_pp_run_gaps_and_stale_state_idempotently(self):
        self.meta = synth_meta(0.5)
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_EARLY, self.T_EARLY):
            r1 = v2.run("20261125", False)
        self.assertFalse(r1["official_books"][v2.PRIMARY_BOOK])
        self.assertFalse(r1["official_books"][v2.PRODUCT_BOOK])
        self.assertTrue(r1["fin_healthy"] is False)
        pp = self.out / "product/core7p_top25_20261125.json"
        run_p = self.out / "runs/run_20261125.json"
        gaps_p = self.out / "gaps.jsonl"
        state_p = self.out / "state.json"

        # Corrupt/remove product, run, gaps, and roll state.json back to stale prior date
        pp.write_text("{corrupt", encoding="utf-8")
        run_p.unlink()
        gaps_p.unlink()
        state_p.write_text(json.dumps({"last_signal_date": "20261124", "signal_date": "20261124", "status": "decided"}))

        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_EARLY):
            r2 = v2.run("20261125", False)
        self.assertEqual(r2["status"], "already_decided")
        self.assertEqual(json.loads(pp.read_text())["signal_date"], "20261125")
        self.assertEqual(json.loads(run_p.read_text())["signal_date"], "20261125")
        self.assertEqual(json.loads(state_p.read_text())["last_signal_date"], "20261125")
        self.assertEqual([g["reason"] for g in v2.fs.read_jsonl(gaps_p)], ["fin_coverage_low"])

        # Idempotent second heal does not duplicate gap
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_EARLY):
            v2.run("20261125", False)
        self.assertEqual(len(v2.fs.read_jsonl(gaps_p)), 1)

        # If a newer date 20261126 is already decided, rerunning 20261125 does NOT overwrite state.json backward
        self.df[["ts_code"]].assign(comp_rank_ew6=0.5).to_parquet(self.fs_v1 / "scores/scores_20261126.parquet", index=False)
        pd.DataFrame({
            "ts_code": self.df.ts_code.tolist(),
            "source_provider": ["tdx_api"] * len(self.df),
            "pre_close_source": ["tdx_raw"] * len(self.df),
        }).to_parquet(self.root / "backtest_cache/daily_20261126.parquet", index=False)
        (self.root / "stock_data/03-working/health/price_crosscheck/crosscheck_20261126.json").write_text(
            json.dumps({"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 0}),
            encoding="utf-8",
        )
        self.cal = ["20261124", "20261125", "20261126", "20261127"]
        self.meta = synth_meta(1.0)
        t26 = datetime(2026, 11, 26, 23, 50, tzinfo=TZ)
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(t26, t26):
            v2.run("20261126", False)
        run_p.unlink()
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(t26):
            v2.run("20261125", False)
        self.assertTrue(run_p.exists())
        self.assertEqual(json.loads(state_p.read_text())["last_signal_date"], "20261126")

    def test_parity_fails_on_different_finite_sets_and_parity_prev_handles_exception(self):
        import io
        from contextlib import redirect_stdout
        with self.clock(self.T_NIGHT, self.T_NIGHT):
            v2.run("20261125", True)

        # One row becomes NaN in recomputed score -> n_old == n_new == 240, Spearman == 1.0, yet parity MUST fail!
        one_nan = self.df.copy()
        one_nan.loc[150, "core7p_native"] = np.nan
        rec_nan = v2.parity("20261125", True, recompute=lambda d: one_nan)
        self.assertEqual((rec_nan["n_old"], rec_nan["n_new"], rec_nan["n_matched"]), (240, 240, 240))
        self.assertFalse(rec_nan["finite_sets_equal"])
        self.assertFalse(rec_nan["ok"])

        # Same finite count (239==239) on DIFFERENT missing codes -> MUST also fail parity!
        df_old_nan = self.df.copy()
        df_old_nan.loc[150, "core7p_native"] = np.nan
        v2._write_parquet_atomic(self.out / "test/scores/v2_scores_20261125.parquet", df_old_nan)
        df_new_nan = self.df.copy()
        df_new_nan.loc[151, "core7p_native"] = np.nan
        rec_diff_nan = v2.parity("20261125", True, recompute=lambda d: df_new_nan)
        self.assertEqual(rec_diff_nan["finite_counts"]["core7p_native"]["old"], 239)
        self.assertEqual(rec_diff_nan["finite_counts"]["core7p_native"]["new"], 239)
        self.assertFalse(rec_diff_nan["finite_sets_equal"])
        self.assertFalse(rec_diff_nan["ok"])
        v2._write_parquet_atomic(self.out / "test/scores/v2_scores_20261125.parquet", self.df)

        # Recompute raises -> parity logs structured error record with "spearman" and "ok": False
        rec_err = v2.parity("20261125", True, recompute=mock.MagicMock(side_effect=RuntimeError("boom")))
        self.assertFalse(rec_err["ok"])
        self.assertIn("spearman", rec_err)
        self.assertIn("RuntimeError: boom", rec_err["error"])

        # --parity-prev exception in main() after run() returns 0 and emits structured error JSON
        self.df[["ts_code"]].assign(comp_rank_ew6=0.5).to_parquet(self.fs_v1 / "scores/scores_20261126.parquet", index=False)
        buf = io.StringIO()
        with (
            mock.patch.object(v2, "parity", side_effect=RuntimeError("prev_boom")),
            self.clock(self.T_NIGHT, self.T_NIGHT, self.T_NIGHT),
            redirect_stdout(buf),
        ):
            rc = v2.main(["--today-session", "--as-of", "20261126", "--test", "--parity-prev"])
        self.assertEqual(rc, 0)
        self.assertIn('"spearman": {}', buf.getvalue())
        self.assertIn('"ok": false', buf.getvalue())
        self.assertEqual(len(v2.fs.read_jsonl(self.out / "test/decisions.jsonl")), 2)

    def test_pipelines_running_at_cutoff_logs_gap_without_reading_inputs(self):
        with (
            mock.patch.object(v2.fs, "committed_and_clean", return_value=True),
            mock.patch.object(v2, "pipelines_running", return_value=True),
            mock.patch.object(v2.fsm, "day_prices", side_effect=AssertionError("must not read prices")),
            mock.patch.object(v2, "score_day", side_effect=AssertionError("must not score")),
            self.clock(self.T_LATE),
        ):
            r = v2.run("20261125", False)
        self.assertEqual(r["status"], "gap_logged")
        gaps = v2.fs.read_jsonl(self.out / "gaps.jsonl")
        self.assertEqual([g["reason"] for g in gaps], ["pipelines_running_at_cutoff"])

    def test_write_spec_refuses_only_when_official_decisions_nonempty(self):
        import io
        from contextlib import redirect_stdout
        # Test decisions alone do NOT block --write-spec
        with self.clock(self.T_NIGHT, self.T_NIGHT):
            v2.run("20261125", True)
        self.spec_path.unlink()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(v2.main(["--write-spec"]), 0)
        self.assertTrue(self.spec_path.exists())

        # Non-empty official decisions.jsonl DOES block --write-spec
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT, self.T_NIGHT):
            v2.run("20261125", False)
        before = self.spec_path.read_text(encoding="utf-8")
        with self.assertRaises(SystemExit):
            v2.main(["--write-spec"])
        self.assertEqual(self.spec_path.read_text(encoding="utf-8"), before)

    def test_as_of_pins_session_date(self):
        import io
        from contextlib import redirect_stdout
        with (
            mock.patch.object(v2, "_now", return_value=datetime(2026, 11, 26, 0, 5, tzinfo=TZ)),
            mock.patch.object(v2, "run", return_value={"status": "ok"}) as m_run,
            redirect_stdout(io.StringIO()),
        ):
            rc = v2.main(["--today-session", "--as-of", "20261125", "--test"])
        self.assertEqual(rc, 0)
        m_run.assert_called_once_with("20261125", True)

    def test_before_replace_deadline_crossed_and_post_replace_gap(self):
        target = self.out / "test_atomic.jsonl"
        with self.assertRaises(v2._DeadlineCrossed):
            v2._append_jsonl_atomic(target, {"a": 1}, before_replace=lambda: (_ for _ in ()).throw(v2._DeadlineCrossed()))
        self.assertFalse(target.exists())
        self.assertEqual(list(self.out.glob(".*.tmp")), [])

        # 1) Deadline crossed inside before_replace -> rewritten as late=True, official=False, gap reason="late"
        with (
            mock.patch.object(v2.fs, "committed_and_clean", return_value=True),
            self.clock(self.T_EARLY, self.T_EARLY, self.T_LATE, self.T_LATE),
        ):
            r1 = v2.run("20261125", False)
        self.assertFalse(r1["official"])
        self.assertFalse(any(r1["official_books"].values()))
        dec1 = v2.fs.read_jsonl(self.out / "decisions.jsonl")[0]
        self.assertTrue(dec1["late"])
        self.assertFalse(dec1["official"])
        self.assertEqual([g["reason"] for g in v2.fs.read_jsonl(self.out / "gaps.jsonl")], ["late"])
        pf1 = json.loads((self.out / "product/core7p_top25_20261125.json").read_text(encoding="utf-8"))
        self.assertFalse(pf1["official"])

        # 2) Deadline crossed right AFTER os.replace -> official decision stays, post_replace_deadline_crossed gap appended
        self.cal = ["20261124", "20261125", "20261126", "20261127"]
        self.df[["ts_code"]].assign(comp_rank_ew6=0.5).to_parquet(self.fs_v1 / "scores/scores_20261126.parquet", index=False)
        pd.DataFrame({
            "ts_code": self.df.ts_code.tolist(),
            "source_provider": ["tdx_api"] * len(self.df),
            "pre_close_source": ["tdx_raw"] * len(self.df),
        }).to_parquet(self.root / "backtest_cache/daily_20261126.parquet", index=False)
        (self.root / "stock_data/03-working/health/price_crosscheck/crosscheck_20261126.json").write_text(
            json.dumps({"pre_close_exdiv_ok": True, "pre_close_exdiv_status": "ok", "pre_close_exdiv_unavailable_rows": 0}),
            encoding="utf-8",
        )
        t26_early = datetime(2026, 11, 27, 8, 50, tzinfo=TZ)
        t26_late = datetime(2026, 11, 27, 9, 31, tzinfo=TZ)
        with (
            mock.patch.object(v2.fs, "committed_and_clean", return_value=True),
            self.clock(t26_early, t26_early, t26_early, t26_late),
        ):
            r2 = v2.run("20261126", False)
        self.assertTrue(r2["official"])
        decs = v2.fs.read_jsonl(self.out / "decisions.jsonl")
        self.assertFalse(decs[1]["late"])
        self.assertTrue(decs[1]["official"])
        gaps26 = [g for g in v2.fs.read_jsonl(self.out / "gaps.jsonl") if g["signal_date"] == "20261126"]
        self.assertEqual([g["reason"] for g in gaps26], ["post_replace_deadline_crossed"])

    def test_v2_lock_serializes_parity_and_write_spec(self):
        with self.clock(self.T_NIGHT, self.T_NIGHT):
            v2.run("20261125", True)

        def probe_flock_busy(lock_path: Path) -> bool:
            code = (
                "import fcntl,sys\n"
                "fh=open(sys.argv[1],'a+')\n"
                "try:\n"
                " fcntl.flock(fh.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB); sys.exit(0)\n"
                "except BlockingIOError:\n"
                " sys.exit(11)\n"
            )
            res = subprocess.run([sys.executable, "-c", code, str(lock_path)], capture_output=True)
            return res.returncode == 11

        # parity() must hold _v2_lock while appending parity.jsonl
        orig_append = v2._append_jsonl_atomic
        saw_locked = []

        def checked_append(path, rec, **kwargs):
            if Path(path).name == "parity.jsonl":
                saw_locked.append(probe_flock_busy(self.out / "test/.v2.lock"))
            return orig_append(path, rec, **kwargs)

        with mock.patch.object(v2, "_append_jsonl_atomic", side_effect=checked_append):
            rec = v2.parity("20261125", True, recompute=lambda d: self.df.copy())
        self.assertTrue(rec["ok"])
        self.assertEqual(saw_locked, [True])
        self.assertFalse(probe_flock_busy(self.out / "test/.v2.lock"))

        # Concurrent parity serialization: child process holds lock briefly while writing; parent parity waits cleanly
        lock_f = self.out / "test/.v2.lock"
        ready_f = self.root / "lock_ready"
        holder = subprocess.Popen([
            sys.executable, "-c",
            "import fcntl,pathlib,sys,time; fh=open(sys.argv[1],'a+'); "
            "fcntl.flock(fh.fileno(), fcntl.LOCK_EX); "
            "pathlib.Path(sys.argv[2]).write_text('1'); "
            "time.sleep(0.2); "
            "import json; p=pathlib.Path(sys.argv[3]); p.write_text(p.read_text()+json.dumps({'signal_date':'holder'})+'\\n'); "
            "fcntl.flock(fh.fileno(), fcntl.LOCK_UN)",
            str(lock_f), str(ready_f), str(self.out / "test/parity.jsonl"),
        ])
        while not ready_f.exists():
            pass
        rec2 = v2.parity("20261125", True, recompute=lambda d: self.df.copy())
        self.assertEqual(holder.wait(timeout=5), 0)
        self.assertTrue(rec2["ok"])
        lines = v2.fs.read_jsonl(self.out / "test/parity.jsonl")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1]["signal_date"], "holder")

        # --write-spec must hold _v2_lock(OUT) across decisions check and _write_json_atomic
        import io
        from contextlib import redirect_stdout
        self.spec_path.unlink()
        orig_write_json = v2._write_json_atomic
        spec_locked = []

        def checked_write_json(path, obj):
            if Path(path) == self.spec_path:
                spec_locked.append(probe_flock_busy(self.out / ".v2.lock"))
            return orig_write_json(path, obj)

        with mock.patch.object(v2, "_write_json_atomic", side_effect=checked_write_json), redirect_stdout(io.StringIO()):
            self.assertEqual(v2.main(["--write-spec"]), 0)
        self.assertEqual(spec_locked, [True])
        self.assertFalse(probe_flock_busy(self.out / ".v2.lock"))

    def test_heal_decided_validates_coverage_spec_hash_holdings_and_isolates_daily(self):
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT, self.T_NIGHT):
            v2.run("20261125", False)
        pp = self.out / "product/core7p_top25_20261125.json"
        run_p = self.out / "runs/run_20261125.json"
        state_p = self.out / "state.json"

        # 1) Missing "coverage" in run_20261125.json and state.json triggers heal
        run_obj = json.loads(run_p.read_text(encoding="utf-8"))
        del run_obj["coverage"]
        run_p.write_text(json.dumps(run_obj), encoding="utf-8")
        st_obj = json.loads(state_p.read_text(encoding="utf-8"))
        del st_obj["coverage"]
        state_p.write_text(json.dumps(st_obj), encoding="utf-8")
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT):
            v2.run("20261125", False)
        self.assertIn("coverage", json.loads(run_p.read_text(encoding="utf-8")))
        self.assertIn("coverage", json.loads(state_p.read_text(encoding="utf-8")))

        # 2) Mismatched spec_hash in product file triggers heal
        pf = json.loads(pp.read_text(encoding="utf-8"))
        pf["spec_hash"] = "0000000000000000"
        pp.write_text(json.dumps(pf), encoding="utf-8")
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT):
            v2.run("20261125", False)
        self.assertEqual(json.loads(pp.read_text(encoding="utf-8"))["spec_hash"], v2.spec_hash())

        # 3) Truncated holdings length in product file triggers heal
        pf = json.loads(pp.read_text(encoding="utf-8"))
        pf["holdings"] = pf["holdings"][:5]
        pp.write_text(json.dumps(pf), encoding="utf-8")
        with mock.patch.object(v2.fs, "committed_and_clean", return_value=True), self.clock(self.T_NIGHT):
            v2.run("20261125", False)
        self.assertEqual(len(json.loads(pp.read_text(encoding="utf-8"))["holdings"]), 25)

        # 4) Historical heal must filter daily[date <= d] so future settlements never trigger rollback
        dates = ["20261125"] + [f"2027{m:02d}{dd:02d}" for m in range(1, 5) for dd in range(1, 29)][:65]
        rows = []
        for idx, dt in enumerate(dates):
            rows.append({"book": v2.PRODUCT_BOOK, "date": dt, "ret_9.6bp": (0.0 if idx == 0 else -0.002)})
            rows.append({"book": "bench_ew", "date": dt, "ret_9.6bp": 0.0})
        pd.DataFrame(rows).to_parquet(self.out / "daily.parquet", index=False)
        pp.unlink()
        run_p.unlink()
        state_p.unlink()
        with (
            mock.patch.object(v2.fs, "committed_and_clean", return_value=True),
            mock.patch.object(v2, "settle", return_value={"settled_days": len(dates), "decisions": 1, "official_decisions": 1}),
            self.clock(self.T_NIGHT),
        ):
            v2.run("20261125", False)
        healed_pf = json.loads(pp.read_text(encoding="utf-8"))
        healed_run = json.loads(run_p.read_text(encoding="utf-8"))
        self.assertEqual(healed_pf["rollback"]["sessions"], 1)
        self.assertFalse(healed_pf["rollback"]["triggered"])
        self.assertEqual(healed_run["rollback"]["sessions"], 1)
        self.assertFalse(healed_run["rollback"]["triggered"])


class ScoreDayTests(unittest.TestCase):
    def test_score_day_recomputes_coverage_after_left_merge_and_checks_unique_keys(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fs_v1 = root / "fs_v1"
            (fs_v1 / "scores").mkdir(parents=True)
            df_all = synth_df()
            v1_codes = df_all["ts_code"].tolist()[:100]
            pd.DataFrame({"ts_code": v1_codes, "comp_rank_ew6": np.linspace(0, 1, 100)}).to_parquet(
                fs_v1 / "scores/scores_20261125.parquet", index=False
            )
            nat20 = df_all.iloc[:20].copy()
            nat20["_cov_qarp_real"] = True
            nat20["_cov_acc_real"] = True
            nat20["_cov_rev_ttm"] = True
            nat20["ocf_ttm"] = 1e8
            nat20["ocf_applicable"] = True
            nat20["dedt_ttm"] = 1e8
            meta20 = synth_meta(1.0)
            with (
                mock.patch.object(v2, "FS_V1", fs_v1),
                mock.patch.object(v2, "WS", root),
                mock.patch("core7p_native_scorer.score_date", return_value=(nat20, meta20)),
            ):
                df, meta = v2.score_day("20261125")
            self.assertEqual(len(df), 100)
            self.assertEqual(meta["coverage"]["n_mask"], 100)
            self.assertAlmostEqual(meta["coverage"]["qarp_real_dedt_cov"], 0.20)
            self.assertAlmostEqual(meta["coverage"]["acc_real_cov"], 0.20)
            self.assertAlmostEqual(meta["coverage"]["ocf_nonfin_cov"], 0.20)
            self.assertFalse(v2.fin_healthy(meta["coverage"])[0])
            self.assertEqual(meta["universe"]["size"], 100)
            self.assertEqual(meta["universe"]["rows_matched"], 20)
            self.assertNotIn("_cov_qarp_real", df.columns)

            # Duplicate ts_code in v1 fails loudly
            pd.DataFrame({"ts_code": ["600000.SH", "600000.SH"], "comp_rank_ew6": [0.5, 0.4]}).to_parquet(
                fs_v1 / "scores/scores_20261125.parquet", index=False
            )
            with (
                mock.patch.object(v2, "FS_V1", fs_v1),
                mock.patch.object(v2, "WS", root),
                self.assertRaises(ValueError),
            ):
                v2.score_day("20261125")


class ShellWrappers(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[3]
        self.sh = self.repo_root / "ops/hermes/stock/forward_shadow_v2.sh"
        self.cutoff_sh = self.repo_root / "ops/hermes/stock/forward_shadow_v2_cutoff.sh"

    def test_cutoff_wrapper_exists_and_executable(self):
        self.assertTrue(self.cutoff_sh.exists())
        mode = self.cutoff_sh.stat().st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR))
        txt = self.cutoff_sh.read_text(encoding="utf-8")
        self.assertIn("forward_shadow_v2.sh", txt)
        self.assertIn("cutoff", txt)

    def test_mode_detection_and_git_commit_failure_exits_3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            (ws / "factor_factory/scripts").mkdir(parents=True)
            (ws / "factor_factory/data/forward_shadow/v2").mkdir(parents=True)
            (ws / "ops/hermes/stock").mkdir(parents=True)
            sh_copy = ws / "ops/hermes/stock/forward_shadow_v2.sh"
            sh_copy.write_text(self.sh.read_text(encoding="utf-8"), encoding="utf-8")
            cutoff_copy = ws / "ops/hermes/stock/forward_shadow_v2_cutoff.sh"
            cutoff_copy.write_text(self.cutoff_sh.read_text(encoding="utf-8"), encoding="utf-8")

            # Fake python printing arg list and simple decided JSON
            fake_py = root / "fake_py.sh"
            fake_py.write_text(
                '#!/bin/bash\n'
                'if [ "$1" = "-c" ]; then exec ' + sys.executable + ' "$@"; fi\n'
                'echo "ARGS:$*" >> "$FAKE_LOG"\n'
                'echo \'{"signal_date":"20261125","status":"decided","official_books":{"a":true},"product_status":"ok","fin_healthy":true}\'\n',
                encoding="utf-8",
            )
            fake_py.chmod(0o755)

            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "pgrep").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
            (bindir / "pgrep").chmod(0o755)
            (bindir / "sleep").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            (bindir / "sleep").chmod(0o755)
            (bindir / "date").write_text(
                '#!/bin/bash\nif [ "$1" = "+%H%M" ]; then echo "$FAKE_HM"; else /bin/date "$@"; fi\n',
                encoding="utf-8",
            )
            (bindir / "date").chmod(0o755)
            (bindir / "git").write_text(
                '#!/bin/bash\n'
                'echo "GIT:$*" >> "$FAKE_LOG"\n'
                'if [ "$1" = "status" ]; then echo " M factor_factory/data/forward_shadow/v2/state.json"; exit 0; fi\n'
                'if [ "$1" = "add" ]; then exit 0; fi\n'
                'if [ "$1" = "commit" ]; then exit "${FAKE_GIT_COMMIT_RC:-1}"; fi\n'
                'exit 0\n',
                encoding="utf-8",
            )
            (bindir / "git").chmod(0o755)

            for hm, expect_mode, expect_flag in (
                ("0840", "premarket", "--prev-session"),
                ("0935", "cutoff", "--prev-session"),
                ("2340", "evening", "--today-session --parity-prev"),
            ):
                logf = root / f"args_{hm}.log"
                env = {
                    **os.environ,
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "STOCK_SYSTEM_WORKSPACE_FORCE": str(ws),
                    "STOCK_SYSTEM_PYTHON_FORCE": str(fake_py),
                    "FAKE_HM": hm,
                    "FAKE_LOG": str(logf),
                    "FAKE_GIT_COMMIT_RC": "1",
                }
                res = subprocess.run(["bash", str(sh_copy)], env=env, capture_output=True, text=True)
                self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
                self.assertIn("git 提交失败", res.stdout)
                log_txt = logf.read_text(encoding="utf-8")
                self.assertIn(expect_flag, log_txt)
                self.assertIn(f"20261125 {expect_mode}", log_txt)

                # When commit succeeds -> exits 0 cleanly
                env_ok = {**env, "FAKE_GIT_COMMIT_RC": "0"}
                res_ok = subprocess.run(["bash", str(sh_copy)], env=env_ok, capture_output=True, text=True)
                self.assertEqual(res_ok.returncode, 0, res_ok.stdout + res_ok.stderr)

            # Verify cutoff wrapper delegates cutoff (--prev-session) even at 23:40
            log_co = root / "args_co.log"
            env_co = {
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "STOCK_SYSTEM_WORKSPACE_FORCE": str(ws),
                "STOCK_SYSTEM_PYTHON_FORCE": str(fake_py),
                "FAKE_HM": "2340",
                "FAKE_LOG": str(log_co),
                "FAKE_GIT_COMMIT_RC": "0",
            }
            res_co = subprocess.run(["bash", str(cutoff_copy)], env=env_co, capture_output=True, text=True)
            self.assertEqual(res_co.returncode, 0, res_co.stdout + res_co.stderr)
            co_txt = log_co.read_text(encoding="utf-8")
            self.assertIn("--prev-session", co_txt)
            self.assertIn("20261125 cutoff", co_txt)
            self.assertNotIn("--today-session", co_txt)

            # Pin TODAY before wait loop (midnight rollover) + exit 2 on wait timeout
            rollover_date = root / "date_roll.sh"
            rollover_flag = root / "date_called"
            rollover_date.write_text(
                '#!/bin/bash\n'
                'if [ "$1" = "+%Y%m%d" ]; then\n'
                '  if [ ! -f "' + str(rollover_flag) + '" ]; then touch "' + str(rollover_flag) + '"; echo "20261125"; else echo "20261126"; fi\n'
                'elif [ "$1" = "+%H%M" ]; then echo "2359"\n'
                'else /bin/date "$@"; fi\n',
                encoding="utf-8",
            )
            rollover_date.chmod(0o755)
            (bindir / "date").write_text(rollover_date.read_text(encoding="utf-8"), encoding="utf-8")
            pgrep_once = root / "pgrep_once"
            (bindir / "pgrep").write_text(
                '#!/bin/bash\nif [ ! -f "' + str(pgrep_once) + '" ]; then touch "' + str(pgrep_once) + '"; exit 0; fi\nexit 1\n',
                encoding="utf-8",
            )
            log_roll = root / "args_roll.log"
            env_roll = {**env_co, "FAKE_LOG": str(log_roll)}
            res_roll = subprocess.run(["bash", str(sh_copy), "evening"], env=env_roll, capture_output=True, text=True)
            self.assertEqual(res_roll.returncode, 0, res_roll.stdout + res_roll.stderr)
            self.assertIn("--as-of 20261125", log_roll.read_text(encoding="utf-8"))

            # Timeout when upstream pipeline never exits -> exit 2, python never called
            (bindir / "pgrep").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            log_to = root / "args_to.log"
            env_to = {**env_co, "FAKE_LOG": str(log_to)}
            res_to = subprocess.run(["bash", str(sh_copy), "evening"], env=env_to, capture_output=True, text=True)
            self.assertEqual(res_to.returncode, 2, res_to.stdout + res_to.stderr)
            self.assertIn("超时", res_to.stdout)
            self.assertFalse(log_to.exists())

            # Timeout for premarket -> exit 2, python never called
            log_pm_to = root / "args_pm_to.log"
            env_pm_to = {**env_co, "FAKE_LOG": str(log_pm_to)}
            res_pm_to = subprocess.run(["bash", str(sh_copy), "premarket"], env=env_pm_to, capture_output=True, text=True)
            self.assertEqual(res_pm_to.returncode, 2, res_pm_to.stdout + res_pm_to.stderr)
            self.assertIn("超时", res_pm_to.stdout)
            self.assertIn("超时", res_pm_to.stderr)
            self.assertFalse(log_pm_to.exists())

            # Timeout for cutoff -> falls through to python so cutoff logs gap cleanly and exits 0
            fake_gap_py = root / "fake_gap_py.sh"
            fake_gap_py.write_text(
                '#!/bin/bash\n'
                'if [ "$1" = "-c" ]; then exec ' + sys.executable + ' "$@"; fi\n'
                'echo "ARGS:$*" >> "$FAKE_LOG"\n'
                'echo \'{"signal_date":"20261125","status":"gap_logged"}\'\n',
                encoding="utf-8",
            )
            fake_gap_py.chmod(0o755)
            log_co_to = root / "args_co_to.log"
            env_co_to = {**env_co, "STOCK_SYSTEM_PYTHON_FORCE": str(fake_gap_py), "FAKE_LOG": str(log_co_to)}
            res_co_to = subprocess.run(["bash", str(sh_copy), "cutoff"], env=env_co_to, capture_output=True, text=True)
            self.assertEqual(res_co_to.returncode, 0, res_co_to.stdout + res_co_to.stderr)
            self.assertIn("--prev-session", log_co_to.read_text(encoding="utf-8"))
            self.assertIn("gap_logged", res_co_to.stdout)

            # pgrep returns rc=2 -> immediately alert stdout & stderr and exit 2 without calling python
            (bindir / "pgrep").write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")
            log_pg2 = root / "args_pg2.log"
            env_pg2 = {**env_co, "FAKE_LOG": str(log_pg2)}
            res_pg2 = subprocess.run(["bash", str(sh_copy), "cutoff"], env=env_pg2, capture_output=True, text=True)
            self.assertEqual(res_pg2.returncode, 2, res_pg2.stdout + res_pg2.stderr)
            self.assertIn("pgrep 异常", res_pg2.stdout)
            self.assertIn("pgrep 异常", res_pg2.stderr)
            self.assertFalse(log_pg2.exists())

            # pgrep returns rc=2 on SECOND pattern only -> still immediately aborts with exit 2
            (bindir / "pgrep").write_text(
                '#!/bin/bash\nif [[ "$2" == *"forward_shadow.py"* ]]; then exit 2; fi\nexit 1\n',
                encoding="utf-8",
            )
            log_pg2b = root / "args_pg2b.log"
            env_pg2b = {**env_co, "FAKE_LOG": str(log_pg2b)}
            res_pg2b = subprocess.run(["bash", str(sh_copy), "evening"], env=env_pg2b, capture_output=True, text=True)
            self.assertEqual(res_pg2b.returncode, 2, res_pg2b.stdout + res_pg2b.stderr)
            self.assertIn("pgrep 异常", res_pg2b.stdout)
            self.assertIn("pgrep 异常", res_pg2b.stderr)
            self.assertFalse(log_pg2b.exists())


if __name__ == "__main__":
    unittest.main()
