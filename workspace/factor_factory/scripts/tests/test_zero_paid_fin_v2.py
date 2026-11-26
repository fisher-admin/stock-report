from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zero_paid_fin_v2 as zp  # noqa: E402

CAL = np.array([d.strftime("%Y%m%d") for d in pd.date_range("2022-01-01", "2025-02-28")], dtype="U8")
ENDS = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


def _cal_to(d: str) -> np.ndarray:
    return CAL[CAL <= d]


def _ann(end: str, lag: int = 30) -> str:
    return (pd.Timestamp(end) + pd.Timedelta(days=lag)).strftime("%Y%m%d")


def quarters(upto=(2024, 3)):
    """2022Q1.. with single-quarter value 10*(idx+1); returns [(end, cum, idx)]."""
    out, idx = [], 0
    for y in range(2022, upto[0] + 1):
        cum = 0.0
        for q in range(1, 5):
            if (y, q) > upto:
                break
            cum += 10.0 * (idx + 1)
            out.append((f"{y}{ENDS[q]}", cum, idx))
            idx += 1
    return out


class Env:
    def __init__(self, tmp: Path):
        self.seed = tmp / "seed"
        self.em = tmp / "em"
        for sub in ("income_vip", "fina_indicator_vip", "forecast_vip", "express", "cf_v2", "early"):
            (self.seed / sub).mkdir(parents=True, exist_ok=True)
        self.inc, self.fi, self.fc, self.ex, self.cf = [], [], [], [], []
        self.sw = []

    def add_company(self, code, upto=(2024, 3), flag="0", dedt_factor=0.5, with_cf=True, lag=30):
        for end, cum, _ in quarters(upto):
            a = _ann(end, lag)
            self.inc.append(dict(ts_code=code, ann_date=a, f_ann_date=a, end_date=end, report_type="1",
                                 total_revenue=cum * 2, n_income_attr_p=cum, update_flag=flag))
            if dedt_factor is not None:
                self.fi.append(dict(ts_code=code, ann_date=a, end_date=end, profit_dedt=cum * dedt_factor, q_dtprofit=0.0, update_flag=flag))
            if with_cf:
                self.cf.append(dict(ts_code=code, ann_date=a, f_ann_date=a, end_date=end, report_type="1", n_cashflow_act=cum * 3, update_flag=flag))

    def write_seed(self):
        for sub, rows in (("income_vip", self.inc), ("fina_indicator_vip", self.fi), ("forecast_vip", self.fc),
                          ("express", self.ex), ("cf_v2", self.cf)):
            if rows:
                pd.DataFrame(rows).to_parquet(self.seed / sub / "all.parquet")
        if self.sw:
            pd.DataFrame(self.sw).to_parquet(self.seed / "early/sw_member.parquet")

    def write_em(self, tbl, name, rows):
        d = self.em / tbl
        d.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).astype(str).to_parquet(d / name)

    def store(self):
        self.write_seed()
        return zp.ZeroPaidFinStoreV2(workspace=self.seed.parent, seed_dir=self.seed, em_dir=self.em,
                                     daily_basic_dir=self.seed.parent / "no_db")


def run(store, d, codes, mv=1.0, pb=1.0, **kw):
    n = len(codes)
    return store.pit_fundamentals(d, codes, _cal_to(d), np.full(n, mv), np.full(n, pb), **kw)


def em_rep(code6, secu, rep, notice, seen_day, seen_at, np_, eps=None, deps=None, rev=None, **extra):
    row = dict(SECUCODE=secu, SECURITY_CODE=code6, REPORTDATE=rep + " 00:00:00", UPDATE_DATE=notice + " 00:00:00",
               NOTICE_DATE=notice + " 00:00:00", BASIC_EPS=eps, DEDUCT_BASIC_EPS=deps, TOTAL_OPERATE_INCOME=rev,
               PARENT_NETPROFIT=np_, _key=f"{code6}|{rep}", _hash="h", _seen_date=seen_day, _seen_at=seen_at)
    row.update(extra)
    return row


def em_inc(code6, secu, rep, notice, seen_day, seen_at, np_, dedt, rev=None, **extra):
    row = dict(SECUCODE=secu, SECURITY_CODE=code6, NOTICE_DATE=notice + " 00:00:00", REPORT_DATE=rep + " 00:00:00",
               PARENT_NETPROFIT=np_, TOTAL_OPERATE_INCOME=rev, DEDUCT_PARENT_NETPROFIT=dedt,
               _key=f"{code6}|{rep}", _hash="h", _seen_date=seen_day, _seen_at=seen_at)
    row.update(extra)
    return row


class ZeroPaidFinV2Tests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.env = Env(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_ttm_math_yoy_and_source(self):
        self.env.add_company("000001.SZ")
        r = run(self.env.store(), "20241130", ["000001.SZ"])
        self.assertAlmostEqual(r["np_ttm"][0], 380.0)
        self.assertAlmostEqual(r["dedt_ttm"][0], 190.0)
        self.assertAlmostEqual(r["rev_ttm"][0], 760.0)
        self.assertAlmostEqual(r["dedt_yoy"][0], (190 - 110) / 110, places=6)
        self.assertAlmostEqual(r["rev_yoy"][0], 760 / 440 - 1, places=6)
        self.assertEqual(r["dedt_source"][0], "seed_profit_dedt")
        self.assertEqual(r["latest_report_date"][0], "20240930")
        self.assertEqual(r["latest_ann_date"][0], _ann("20240930"))
        self.assertAlmostEqual(r["ocf_ttm"][0], 380.0 * 3)
        self.assertTrue(r["ocf_applicable"][0])
        self.assertTrue(np.isfinite(r["ep_dedt"][0]) and np.isfinite(r["jump"][0]))

    def test_side_right_announcement_day_not_visible(self):
        self.env.add_company("000001.SZ")
        store = self.env.store()
        ann = _ann("20240930")  # 20241030
        r0 = run(store, ann, ["000001.SZ"])
        self.assertEqual(r0["latest_report_date"][0], "20240630")
        r1 = run(store, "20241031", ["000001.SZ"])
        self.assertEqual(r1["latest_report_date"][0], "20240930")
        self.assertEqual(r1["ev_age"][0], 0.0)

    def test_first_version_beats_revision(self):
        self.env.add_company("000001.SZ")
        self.env.inc.append(dict(ts_code="000001.SZ", ann_date="20241120", f_ann_date="20241120", end_date="20240930",
                                 report_type="1", total_revenue=1e6, n_income_attr_p=1e6, update_flag="1"))
        self.env.fi.append(dict(ts_code="000001.SZ", ann_date="20241120", end_date="20240930", profit_dedt=1e6, q_dtprofit=0.0, update_flag="1"))
        r = run(self.env.store(), "20241130", ["000001.SZ"])
        self.assertAlmostEqual(r["np_ttm"][0], 380.0)
        self.assertAlmostEqual(r["dedt_ttm"][0], 190.0)

    def test_f_ann_date_is_max_with_ann_date(self):
        self.env.add_company("000001.SZ")
        for row in self.env.inc:
            if row["end_date"] == "20240930":
                row["f_ann_date"] = "20241105"
        for row in self.env.fi:
            if row["end_date"] == "20240930":
                row["ann_date"] = "20241030"
        store = self.env.store()
        self.assertEqual(run(store, "20241105", ["000001.SZ"])["latest_report_date"][0], "20240630")
        self.assertEqual(run(store, "20241106", ["000001.SZ"])["latest_report_date"][0], "20240930")

    def test_em_first_seen_rule_and_availability(self):
        self.env.add_company("000001.SZ")
        self.env.write_em("report", "seen_20250125.parquet",
                          [em_rep("000001", "000001.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="2", deps="1.5", rev="900")])
        self.env.write_em("report", "seen_20250201.parquet",
                          [em_rep("000001", "000001.SZ", "2024-12-31", "2025-01-20", "20250201", "2025-02-01 10:00:00", "450", eps="2", deps="1.5", rev="950")])
        store = self.env.store()
        r_before = run(store, "20250125", ["000001.SZ"])  # seen day itself not visible
        self.assertEqual(r_before["latest_report_date"][0], "20240930")
        r = run(store, "20250126", ["000001.SZ"])
        self.assertEqual(r["latest_report_date"][0], "20241231")
        self.assertAlmostEqual(r["np_ttm"][0], 400.0)  # first-seen version, not 450
        self.assertEqual(r["latest_ann_date"][0], "20250125")  # max(notice, seen_date)
        self.assertAlmostEqual(r["rev_ttm"][0], 900.0)
        self.assertAlmostEqual(float(store.em_R("latest")["n_income_attr_p"].iloc[0]), 450.0)

    def test_em_eff_fail_closed_and_actual_seen_date(self):
        store = self.env.store()
        eff = store._em_eff(
            pd.Series(["20250120", "20250120", "20250120", "20250120", "20250129"], dtype=object),
            pd.Series(["", "bad", "2025012", "20250125", "20250125"], dtype=object),
            pd.Series(["20250126", "", "20250126", "20250127", "20250126"], dtype=object),
        )
        # Missing/invalid _seen_date ALWAYS fails closed to '99999999', even when _actual_seen_date is valid
        self.assertEqual(eff.tolist(), ["99999999", "99999999", "99999999", "20250127", "20250129"])

        # End-to-end: row missing _seen_date is never visible; row with _actual_seen_date waits until next day after max
        self.env.write_em("report", "seen_20250125.parquet", [
            em_rep("000001", "000001.SZ", "2024-12-31", "2025-01-20", "", "2025-01-25 10:00:00", "400", eps="2", deps="1.5", rev="900"),
            em_rep("000002", "000002.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="2", deps="1.5", rev="900", _actual_seen_date="20250127"),
        ])
        store2 = self.env.store()
        r_26 = run(store2, "20250126", ["000001.SZ", "000002.SZ"])
        self.assertEqual(list(r_26["latest_report_date"]), ["", ""])
        r_28 = run(store2, "20250128", ["000001.SZ", "000002.SZ"])
        self.assertEqual(list(r_28["latest_report_date"]), ["", "20241231"])
        self.assertEqual(r_28["latest_ann_date"][1], "20250127")

    def test_em_report_income_merge_is_strictly_causal(self):
        """Adding a future EM income file (_seen_date > d_target) must leave d_target bit-for-bit identical."""
        self.env.add_company("000001.SZ")
        self.env.write_em("report", "seen_20250125.parquet", [
            em_rep("000001", "000001.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00",
                   "400", eps="2", deps="1.5", rev="900"),
        ])
        r_before = run(self.env.store(), "20250127", ["000001.SZ"])
        self.assertEqual(r_before["latest_report_date"][0], "20241231")
        self.assertEqual(r_before["dedt_source"][0], "em_deduct_eps_derived")
        self.assertAlmostEqual(r_before["dedt_ttm"][0], 300.0)

        # Now capture a future income file on 20250205 (notice 20250120!)
        env2 = Env(Path(self._td.name) / "sub2")
        env2.add_company("000001.SZ")
        env2.write_em("report", "seen_20250125.parquet", [
            em_rep("000001", "000001.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00",
                   "400", eps="2", deps="1.5", rev="900"),
        ])
        env2.write_em("income", "seen_20250205.parquet", [
            em_inc("000001", "000001.SZ", "2024-12-31", "2025-01-20", "20250205", "2025-02-05 10:00:00",
                   "410", "350", rev="910"),
        ])
        st2 = env2.store()
        r_after = run(st2, "20250127", ["000001.SZ"])
        for k in r_before:
            np.testing.assert_array_equal(np.asarray(r_before[k]), np.asarray(r_after[k]))

        # After 20250205 becomes visible (20250206), income takes over cleanly
        r_post = run(st2, "20250206", ["000001.SZ"])
        self.assertEqual(r_post["dedt_source"][0], "em_deduct_parent_netprofit")
        self.assertAlmostEqual(r_post["dedt_ttm"][0], 350.0)
        self.assertAlmostEqual(r_post["np_ttm"][0], 410.0)
        self.assertEqual(r_post["latest_ann_date"][0], "20250205")

    def test_future_seed_rows_do_not_suppress_visible_em(self):
        """Seed rows announced AFTER d_target must never suppress EM rows visible on d_target."""
        self.env.add_company("000001.SZ")
        # Future seed 2024Q4 announced 20250220
        self.env.inc.append(dict(ts_code="000001.SZ", ann_date="20250220", f_ann_date="20250220", end_date="20241231",
                                 report_type="1", total_revenue=1000.0, n_income_attr_p=500.0, update_flag="0"))
        self.env.fi.append(dict(ts_code="000001.SZ", ann_date="20250220", end_date="20241231", profit_dedt=450.0, q_dtprofit=0.0, update_flag="0"))
        self.env.cf.append(dict(ts_code="000001.SZ", ann_date="20250220", f_ann_date="20250220", end_date="20241231",
                                report_type="1", n_cashflow_act=1200.0, update_flag="0"))
        self.env.fc.append(dict(ts_code="000001.SZ", ann_date="20250220", end_date="20250331", net_profit_min=100.0, net_profit_max=100.0, update_flag="0"))
        self.env.ex.append(dict(ts_code="000001.SZ", ann_date="20250220", end_date="20241231", revenue=1000.0, n_income=500.0, update_flag="0"))

        # EM already captured 2024Q4 + 2025Q1 forecast on 20250125
        self.env.write_em("income", "seen_20250125.parquet", [
            em_inc("000001", "000001.SZ", "2024-12-31", "2025-01-24", "20250125", "2025-01-25 10:00:00", "400", "350", rev="900"),
        ])
        self.env.write_em("cashflow", "seen_20250125.parquet", [
            dict(SECUCODE="000001.SZ", SECURITY_CODE="000001", NOTICE_DATE="2025-01-24 00:00:00", REPORT_DATE="2024-12-31 00:00:00",
                 NETCASH_OPERATE="888", _key="000001|2024-12-31", _hash="h", _seen_date="20250125", _seen_at="2025-01-25 10:00:00"),
        ])
        self.env.write_em("forecast", "seen_20250215.parquet", [
            dict(SECUCODE="000001.SZ", SECURITY_CODE="000001", NOTICE_DATE="2025-02-15 00:00:00", REPORT_DATE="2025-03-31 00:00:00",
                 PREDICT_FINANCE_CODE="004", PREDICT_FINANCE="归属于上市公司股东的净利润", PREDICT_AMT_LOWER="5000000", PREDICT_AMT_UPPER="5000000",
                 _key="000001|2025-03-31", _hash="h", _seen_date="20250215", _seen_at="2025-02-15 10:00:00"),
        ])
        store = self.env.store()
        r = run(store, "20250217", ["000001.SZ"])
        self.assertEqual(r["latest_report_date"][0], "20241231")
        self.assertAlmostEqual(r["dedt_ttm"][0], 350.0)
        self.assertAlmostEqual(r["ocf_ttm"][0], 888.0)
        self.assertEqual(r["ev_age"][0], 1.0)  # 20250215 -> avail 20250216 -> age 1 on 20250217

        # After 20250220 becomes visible (20250221), seed takes over
        r_after = run(store, "20250221", ["000001.SZ"])
        self.assertAlmostEqual(r_after["dedt_ttm"][0], 450.0)
        self.assertEqual(r_after["dedt_source"][0], "seed_profit_dedt")
        self.assertAlmostEqual(r_after["ocf_ttm"][0], 1200.0)

    def test_late_fina_indicator_arrival_recovers_dedt(self):
        self.env.add_company("000001.SZ")
        for row in self.env.fi:
            if row["end_date"] == "20240930":
                row["ann_date"] = "20241105"  # inc arrived 20241030 -> avail 20241031; fi arrives 20241105 -> avail 20241106
        store = self.env.store()
        r_before = run(store, "20241105", ["000001.SZ"])
        self.assertTrue(np.isnan(r_before["dedt_ttm"][0]))
        self.assertEqual(r_before["dedt_source"][0], "fallback_parent_netprofit")
        self.assertTrue(np.isfinite(r_before["ep_dedt"][0]))

        r_after = run(store, "20241106", ["000001.SZ"])
        self.assertAlmostEqual(r_after["dedt_ttm"][0], 190.0)
        self.assertEqual(r_after["dedt_source"][0], "seed_profit_dedt")

    def test_em_does_not_override_seed(self):
        self.env.add_company("000001.SZ")
        self.env.write_em("report", "seen_20241101.parquet",
                          [em_rep("000001", "000001.SZ", "2024-09-30", "2024-10-30", "20241101", "2024-11-01 10:00:00", "99999", rev="99999")])
        r = run(self.env.store(), "20241130", ["000001.SZ"])
        self.assertAlmostEqual(r["np_ttm"][0], 380.0)
        self.assertEqual(r["dedt_source"][0], "seed_profit_dedt")

    def test_em_dedt_derivation_and_strict_non_mixed_fallback_labels(self):
        env = self.env
        env.write_em("report", "seen_20250125.parquet", [
            em_rep("000001", "000001.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="2", deps="1.5", rev="900"),
            em_rep("000002", "000002.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="2", deps=None, rev="900"),
            em_rep("000004", "000004.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="0", deps="1", rev="900"),
            em_rep("000005", "000005.SZ", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="2", deps="1.5", rev="900"),
        ])
        env.write_em("income", "seen_20250125.parquet", [dict(
            SECUCODE="000005.SZ", SECURITY_CODE="000005", NOTICE_DATE="2025-01-20 00:00:00", REPORT_DATE="2024-12-31 00:00:00",
            PARENT_NETPROFIT="400", TOTAL_OPERATE_INCOME="900", DEDUCT_PARENT_NETPROFIT="350", _key="000005|2024-12-31",
            _hash="h", _seen_date="20250125", _seen_at="2025-01-25 11:00:00")])
        store = env.store()
        codes = ["000001.SZ", "000002.SZ", "000004.SZ", "000005.SZ", "000003.SZ"]
        r = run(store, "20250126", codes)
        self.assertEqual(list(r["dedt_source"]), ["em_deduct_eps_derived", "fallback_parent_netprofit", "fallback_parent_netprofit",
                                                  "em_deduct_parent_netprofit", "missing"])
        self.assertAlmostEqual(r["dedt_ttm"][0], 400 * 1.5 / 2)
        self.assertTrue(np.isnan(r["dedt_ttm"][1]))  # strict dedt_ttm NEVER passes on parent NP fallback
        self.assertTrue(np.isnan(r["dedt_ttm"][2]))
        self.assertAlmostEqual(r["ep_dedt"][1], 400.0 / 1e4)  # ep_dedt still populated via fallback for QARP
        self.assertAlmostEqual(r["dedt_ttm"][3], 350.0)
        self.assertTrue(np.isnan(r["dedt_ttm"][4]))
        cov = store.coverage(r, np.ones(5, dtype=bool))
        self.assertAlmostEqual(cov["qarp_real_dedt_cov"], 2 / 5)
        self.assertAlmostEqual(cov["dedt_ttm_cov"], 2 / 5)

    def test_strict_ttm_zero_mixing_and_weakest_source_across_quarters(self):
        # 000001.SZ: Q1..Q4 seed, 2024Q3 EM income, 2023Q4 EM eps_derived -> weakest contributing source is em_deduct_eps_derived
        self.env.add_company("000001.SZ", upto=(2024, 2))
        self.env.fi = [r for r in self.env.fi if r["end_date"] != "20231231"]
        self.env.inc = [r for r in self.env.inc if r["end_date"] != "20231231"]
        self.env.write_em("report", "seen_20240425.parquet", [
            em_rep("000001", "000001.SZ", "2023-12-31", "2024-04-20", "20240425", "2024-04-25 10:00:00",
                   "220", eps="2", deps="1.0", rev="440"),
        ])
        self.env.write_em("income", "seen_20241030.parquet", [
            em_inc("000001", "000001.SZ", "2024-09-30", "2024-10-29", "20241030", "2024-10-30 10:00:00",
                   "300", "150", rev="600"),
        ])
        # 000002.SZ: missing profit_dedt ONLY in contributing prior year Q3 (20230930) -> strict dedt_ttm MUST be NaN!
        self.env.add_company("000002.SZ", upto=(2024, 3))
        self.env.fi = [r for r in self.env.fi if not (r["ts_code"] == "000002.SZ" and r["end_date"] == "20230930")]
        store = self.env.store()
        r = run(store, "20241130", ["000001.SZ", "000002.SZ"])
        self.assertEqual(r["dedt_source"][0], "em_deduct_eps_derived")
        self.assertTrue(np.isfinite(r["dedt_ttm"][0]))
        self.assertTrue(np.isnan(r["dedt_ttm"][1]))
        self.assertTrue(np.isnan(r["dedt_yoy"][1]))
        self.assertEqual(r["dedt_source"][1], "fallback_parent_netprofit")
        self.assertTrue(np.isfinite(r["ep_dedt"][1]))
        self.assertTrue(np.isfinite(r["roe_dedt"][1]))
        cov = store.coverage(r, np.ones(2, dtype=bool))
        self.assertAlmostEqual(cov["qarp_real_dedt_cov"], 0.5)
        self.assertAlmostEqual(cov["dedt_ttm_cov"], 0.5)

    def test_pit_industry_map_is_causal(self):
        self.env.sw = [
            dict(ts_code="000001.SZ", l1_name="电子", in_date="20200101", out_date="20230601"),
            dict(ts_code="000001.SZ", l1_name="银行", in_date="20230601", out_date=None),
            dict(ts_code="000002.SZ", l1_name="医药生物", in_date="20240101", out_date=None),
        ]
        self.env.add_company("000001.SZ")
        store = self.env.store()
        m_may = store.industry_map("20230531")
        self.assertEqual(m_may.get("000001.SZ"), "电子")
        self.assertEqual(m_may.get("000002.SZ", ""), "")
        m_jun = store.industry_map("20230601")
        self.assertEqual(m_jun.get("000001.SZ"), "银行")
        self.assertEqual(m_jun.get("000002.SZ", ""), "")
        # Standalone _ocf respects PIT industry at d_target
        r_may = run(store, "20230531", ["000001.SZ"])
        r_nov = run(store, "20241130", ["000001.SZ"])
        self.assertTrue(r_may["ocf_applicable"][0])
        self.assertFalse(r_nov["ocf_applicable"][0])

    def test_pit_industry_map_rejects_blank_or_invalid_in_date(self):
        self.env.sw = [
            dict(ts_code="000001.SZ", l1_name="电子", in_date="", out_date=None),
            dict(ts_code="000002.SZ", l1_name="医药生物", in_date=None, out_date=None),
            dict(ts_code="000003.SZ", l1_name="银行", in_date="bad_date", out_date=None),
            dict(ts_code="000004.SZ", l1_name="通信", in_date="2024", out_date=None),
            dict(ts_code="000005.SZ", l1_name="计算机", in_date="20240101", out_date=None),
        ]
        store = self.env.store()
        m = store.industry_map("20241130")
        self.assertEqual(m, {"000005.SZ": "计算机"})

    def test_ocf_not_applicable_for_financials(self):
        self.env.add_company("000001.SZ")
        self.env.add_company("600000.SH")
        self.env.sw = [dict(ts_code="000001.SZ", l1_name="银行", in_date="20200101", out_date=None),
                       dict(ts_code="600000.SH", l1_name="电子", in_date="20200101", out_date=None)]
        store = self.env.store()
        r = run(store, "20241130", ["000001.SZ", "600000.SH"])
        self.assertEqual(list(r["ocf_applicable"]), [False, True])
        self.assertTrue(np.isnan(r["ocf_ttm"][0]))
        self.assertAlmostEqual(r["ocf_ttm"][1], 380.0 * 3)
        self.assertAlmostEqual(store.coverage(r, np.ones(2, dtype=bool))["ocf_nonfin_cov"], 1.0)
        r2 = run(store, "20241130", ["000001.SZ", "600000.SH"], ind_names=["电子", "非银金融"])
        self.assertEqual(list(r2["ocf_applicable"]), [True, False])

    def test_mandate_filtering(self):
        self.env.add_company("000001.SZ")
        self.env.add_company("688001.SH")
        self.env.add_company("430047.BJ")
        self.env.add_company("600000.SZ")
        store = self.env.store()
        ts = set(store.inc_raw["ts_code"]) | set(store.fi_raw["ts_code"]) | set(store.cf_raw["ts_code"])
        self.assertEqual(ts, {"000001.SZ"})
        r = run(store, "20241130", ["000001.SZ", "688001.SH", "430047.BJ", "600000.SZ"])
        self.assertTrue(np.isfinite(r["dedt_ttm"][0]))
        self.assertTrue(np.isnan(r["dedt_ttm"][1:]).all())
        self.assertEqual(list(r["dedt_source"][1:]), ["missing"] * 3)

    def test_em_rows_mandate_filtered(self):
        self.env.write_em("report", "seen_20250125.parquet", [
            em_rep("688001", "688001.SH", "2024-12-31", "2025-01-20", "20250125", "2025-01-25 10:00:00", "400", eps="2", deps="1", rev="900")])
        store = self.env.store()
        self.assertEqual(len(store.em_R("first")), 0)

    def test_relaxed_forecast_filter(self):
        env = self.env
        env.fc = [
            dict(ts_code="000010.SZ", ann_date="20241120", end_date="20241231", net_profit_min=1000.0, net_profit_max=2000.0, update_flag="1"),
            dict(ts_code="000011.SZ", ann_date="20241101", end_date="20241231", net_profit_min=1000.0, net_profit_max=2000.0, update_flag="1"),
            dict(ts_code="000012.SZ", ann_date="20241120", end_date="20240630", net_profit_min=1000.0, net_profit_max=2000.0, update_flag="1"),
            dict(ts_code="000013.SZ", ann_date="20241120", end_date="20240331", net_profit_min=1000.0, net_profit_max=2000.0, update_flag="1"),
        ]
        r = run(env.store(), "20241125", ["000010.SZ", "000011.SZ", "000012.SZ", "000013.SZ"])
        self.assertEqual(r["ev_age"][0], 4.0)
        self.assertTrue(np.isnan(r["ev_age"][1]))
        self.assertEqual(r["ev_age"][2], 4.0)
        self.assertTrue(np.isnan(r["ev_age"][3]))

    def test_income_event_keeps_strict_lag_filter(self):
        env = self.env
        env.add_company("000001.SZ")
        env.inc.append(dict(ts_code="000001.SZ", ann_date="20241120", f_ann_date="20241120", end_date="20241231", report_type="1",
                            total_revenue=1.0, n_income_attr_p=1.0, update_flag="0"))
        r = run(env.store(), "20241130", ["000001.SZ"])
        self.assertEqual(r["latest_report_date"][0], "20240930")

    def test_em_forecast_units_and_seed_precedence(self):
        env = self.env
        env.fc = [dict(ts_code="000010.SZ", ann_date="20241120", end_date="20241231", net_profit_min=1000.0, net_profit_max=2000.0, update_flag="1")]
        env.write_em("forecast", "seen_20241122.parquet", [
            dict(SECUCODE="000010.SZ", SECURITY_CODE="000010", NOTICE_DATE="2024-11-20 00:00:00", REPORT_DATE="2024-12-31 00:00:00",
                 PREDICT_FINANCE_CODE="004", PREDICT_FINANCE="归属于上市公司股东的净利润", PREDICT_AMT_LOWER="1000000", PREDICT_AMT_UPPER="2000000",
                 _key="a", _hash="h", _seen_date="20241122", _seen_at="2024-11-22 10:00:00"),
            dict(SECUCODE="000020.SZ", SECURITY_CODE="000020", NOTICE_DATE="2024-11-20 00:00:00", REPORT_DATE="2024-12-31 00:00:00",
                 PREDICT_FINANCE_CODE="004", PREDICT_FINANCE="归属于上市公司股东的净利润", PREDICT_AMT_LOWER="1000000", PREDICT_AMT_UPPER="2000000",
                 _key="b", _hash="h", _seen_date="20241122", _seen_at="2024-11-22 10:00:00"),
            dict(SECUCODE="000021.SZ", SECURITY_CODE="000021", NOTICE_DATE="2024-11-20 00:00:00", REPORT_DATE="2024-12-31 00:00:00",
                 PREDICT_FINANCE_CODE="005", PREDICT_FINANCE="扣除非经常性损益后的净利润", PREDICT_AMT_LOWER="1000000", PREDICT_AMT_UPPER="2000000",
                 _key="c", _hash="h", _seen_date="20241122", _seen_at="2024-11-22 10:00:00"),
        ])
        store = env.store()
        f = store.em_forecast()
        self.assertEqual(set(f["ts_code"]), {"000010.SZ", "000020.SZ"})
        self.assertAlmostEqual(float(f["np_cum"].iloc[0]), 1.5e6)
        r = run(store, "20241125", ["000010.SZ", "000020.SZ", "000021.SZ"])
        self.assertEqual(r["ev_age"][0], 4.0)
        self.assertEqual(r["ev_age"][1], 2.0)
        self.assertTrue(np.isnan(r["ev_age"][2]))

    def test_em_only_forecast_visible_after_seen_day(self):
        env = self.env
        env.write_em("forecast", "seen_20241122.parquet", [
            dict(SECUCODE="000020.SZ", SECURITY_CODE="000020", NOTICE_DATE="2024-11-20 00:00:00", REPORT_DATE="2024-12-31 00:00:00",
                 PREDICT_FINANCE_CODE="004", PREDICT_FINANCE="归属于上市公司股东的净利润", PREDICT_AMT_LOWER="1000000", PREDICT_AMT_UPPER="2000000",
                 _key="b", _hash="h", _seen_date="20241122", _seen_at="2024-11-22 10:00:00")])
        store = env.store()
        self.assertTrue(np.isnan(run(store, "20241122", ["000020.SZ"])["ev_age"][0]))
        self.assertEqual(run(store, "20241125", ["000020.SZ"])["ev_age"][0], 2.0)

    def test_reconcile(self):
        env = self.env
        env.add_company("000001.SZ")
        env.write_em("income", "seen_20241101.parquet", [dict(
            SECUCODE="000001.SZ", SECURITY_CODE="000001", NOTICE_DATE="2024-10-30 00:00:00", REPORT_DATE="2024-09-30 00:00:00",
            PARENT_NETPROFIT="300.5", TOTAL_OPERATE_INCOME="600", DEDUCT_PARENT_NETPROFIT="150", _key="k", _hash="h",
            _seen_date="20241101", _seen_at="2024-11-01 10:00:00")])
        rec = zp.reconcile_2026q3(store=env.store(), end_date="20240930")
        self.assertEqual(rec["n_overlap_codes"], 1)
        self.assertEqual(rec["revenue_agree"], 1.0)
        self.assertEqual(rec["parent_np_agree"], 1.0)
        self.assertEqual(rec["dedt_agree"], 1.0)

    def test_deterministic_and_aligned(self):
        self.env.add_company("000001.SZ")
        self.env.add_company("300001.SZ", dedt_factor=None)
        store = self.env.store()
        codes = ["300001.SZ", "999999.SZ", "000001.SZ"]
        a, b = run(store, "20241130", codes), run(store, "20241130", codes)
        for k in a:
            np.testing.assert_array_equal(np.asarray(a[k]), np.asarray(b[k]))
            self.assertEqual(len(a[k]), 3)
        self.assertEqual(a["dedt_source"][0], "fallback_parent_netprofit")
        self.assertEqual(a["dedt_source"][1], "missing")


if __name__ == "__main__":
    unittest.main()
