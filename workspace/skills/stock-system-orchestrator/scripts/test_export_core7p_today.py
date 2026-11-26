import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_core7p_today as E  # noqa: E402

CODES = [f"60{i:04d}.SH" for i in range(100, 130)]
INDUSTRIES = ("银行", "电子", "医药生物", "食品饮料", "电力设备", "汽车", "基础化工", "机械设备")


def fixture(n=25, **over):
    hs = [{"rank": i + 1, "ts_code": CODES[i], "code": CODES[i][:6], "name": f"股{i}",
           "industry": INDUSTRIES[i % len(INDUSTRIES)],
           "score_pct": 0.9, "dedt_ttm": 1.0e9 + i, "dedt_yoy": 0.3, "rev_yoy": 0.2, "roe_dedt": 0.1, "ocf_ttm": None,
           "ocf_applicable": False, "pe_ttm": 15.0, "pb": 2.0, "dedt_source": "seed_profit_dedt",
           "latest_report_date": "20260930", "latest_ann_date": "20261030", "action": "HOLD",
           "plan_status": "planned_T+1_open"} for i in range(n)]
    d = {"schema": "core7p_top25_v1", "signal_date": "20261125", "generated_at": "2026-11-25 20:00:00",
         "book": "core7p_profitable_top25", "rule": "r", "status": "ok", "official": False, "spec_hash": "abc",
         "coverage": {"qarp_real_dedt_cov": 0.97, "acc_real_cov": 0.93, "ocf_nonfin_cov": 0.9, "degraded_pillars": []},
         "rollback": {"rule": "x", "sessions": 3, "rel_excess_60d": None, "triggered": False},
         "friction": {"lot": 100, "min_commission_rmb": 5, "stamp_duty_sell_bps": 5,
                      "accounts": {"100000": {"names_buyable": 23, "cash_drag_pct": 1.2}}},
         "holdings": hs}
    d.update(over)
    return d


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.repo = Path(self.tmp.name) / "repo"
        (self.ws / "factor_factory/data/forward_shadow/v2/product").mkdir(parents=True)
        (self.repo / "data/latest").mkdir(parents=True)
        self.out = self.repo / "data/latest/core7p_today_state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, doc, date="20261125"):
        p = self.ws / f"factor_factory/data/forward_shadow/v2/product/core7p_top25_{date}.json"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def run_main(self, *extra):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = E.main(["--repo", str(self.repo), "--workspace", str(self.ws), *extra])
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_unavailable_when_source_missing(self):
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (0, "unavailable"))
        self.assertFalse(self.out.exists())

    def test_missing_source_clears_existing_exported_file(self):
        self.put(fixture())
        self.run_main()
        self.assertTrue(self.out.exists())
        (self.ws / "factor_factory/data/forward_shadow/v2/product/core7p_top25_20261125.json").unlink()
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (0, "unavailable"))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(doc["status"], "unavailable")
        self.assertEqual(doc["reason"], "暂无最新 Core7P 产品文件")
        self.assertNotIn("holdings", doc)
        self.assertEqual(list(self.out.parent.glob("*.tmp")), [])

    def test_valid_export(self):
        self.put(fixture())
        rc, o = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual((o["status"], o["holdings"]), ("ok", 25))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual((doc["schema"], doc["status"], doc["signal_date"]), ("core7p_today_v1", "ok", "20261125"))
        self.assertFalse(doc["stale"])
        self.assertEqual(len(doc["holdings"]), 25)
        self.assertNotIn("constrained_rank", doc["holdings"][0])
        self.assertIn("非投资建议", doc["product"]["label"])
        self.assertEqual(doc["coverage"]["qarp_real_dedt_cov"], 0.97)
        self.assertIn("accounts", doc["friction"])
        self.assertFalse(doc["product"]["official"])

    def test_latest_date_wins(self):
        self.put(fixture(signal_date="20261124"), "20261124")
        self.put(fixture(signal_date="20261125"), "20261125")
        self.run_main()
        self.assertEqual(json.loads(self.out.read_text())["signal_date"], "20261125")

    def test_board_violation_rejected_incl_newline_and_fullwidth(self):
        for bad in ("688001.SH", "830001.BJ", "300001.SH", "002001.SH", "600100.SH\n", "６００１００.SH"):
            d = fixture()
            d["holdings"][3]["ts_code"] = bad
            d["holdings"][3]["code"] = bad[:6]
            self.put(d)
            rc, o = self.run_main("--check")
            self.assertEqual((rc, o["status"]), (1, "invalid"), repr(bad))
            self.assertTrue(any("board" in e for e in o["errors"]))

    def test_unhashable_ts_code_fails_gracefully_no_typeerror(self):
        for bad in (["600100.SH"], {"code": "600100.SH"}, 600100, None):
            d = fixture()
            d["holdings"][0]["ts_code"] = bad
            self.put(d)
            rc, o = self.run_main("--check")
            self.assertEqual((rc, o["status"]), (1, "invalid"), repr(bad))
            self.assertTrue(any("board" in e for e in o["errors"]))

    def test_nonpositive_dedt_rejected(self):
        for bad in (0, -5e8, None, "1e9"):
            d = fixture()
            d["holdings"][0]["dedt_ttm"] = bad
            self.put(d)
            rc, o = self.run_main("--check")
            self.assertEqual((rc, o["status"]), (1, "invalid"), bad)
            self.assertTrue(any("dedt_ttm" in e for e in o["errors"]))

    def test_sw1_industry_cap_and_source_guards(self):
        d = fixture()
        for i in range(5):
            d["holdings"][i]["industry"] = "银行"
        self.put(d)
        rc, o = self.run_main("--check")
        self.assertEqual((rc, o["status"]), (1, "invalid"))
        self.assertTrue(any("industry" in e for e in o["errors"]))

        d = fixture(validation_errors=["bad_row"])
        self.put(d)
        rc, o = self.run_main("--check")
        self.assertEqual((rc, o["status"]), (1, "invalid"))
        self.assertTrue(any("validation_errors" in e for e in o["errors"]))

        for bad_off in (None, "true", 1, "false"):
            d = fixture(official=bad_off)
            self.put(d)
            rc, o = self.run_main("--check")
            self.assertEqual((rc, o["status"]), (1, "invalid"), repr(bad_off))
            self.assertTrue(any("official" in e for e in o["errors"]))

    def test_dedt_source_allowlist_and_srv_private_path(self):
        for ok_src in ("seed_profit_dedt", "em_deduct_parent_netprofit", "em_deduct_eps_derived"):
            d = fixture()
            d["holdings"][0]["dedt_source"] = ok_src
            self.put(d)
            self.assertEqual(self.run_main("--check")[0], 0, ok_src)
        for bad_src in ("fallback_parent_netprofit", None, "other"):
            d = fixture()
            d["holdings"][0]["dedt_source"] = bad_src
            self.put(d)
            rc, o = self.run_main("--check")
            self.assertEqual(rc, 1, bad_src)
            self.assertTrue(any("dedt_source" in e for e in o["errors"]))

        d = fixture(rule="from /srv/hermes-stock/secret")
        self.put(d)
        rc, o = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.assertTrue(any("private absolute path" in e for e in o["errors"]))

    def test_size_duplicates_names(self):
        self.put(fixture(n=19))
        self.assertEqual(self.run_main("--check")[0], 1)
        d = fixture()
        d["holdings"][1] = copy.deepcopy(d["holdings"][0])
        self.put(d)
        self.assertTrue(any("duplicate" in e for e in self.run_main("--check")[1]["errors"]))
        d = fixture()
        for x in d["holdings"][:8]:
            x["name"] = None
        self.put(d)
        self.assertTrue(any("name" in e for e in self.run_main("--check")[1]["errors"]))

    def test_invalid_publishes_generic_unavailable_without_leaking_errors(self):
        self.put(fixture())
        self.run_main()
        d = fixture()
        d["holdings"][0]["ts_code"] = "SECRET_LEAK_TOKEN.BJ"
        d["holdings"][0]["dedt_ttm"] = -1
        self.put(d)
        rc, _ = self.run_main()
        self.assertEqual(rc, 1)
        blob = self.out.read_text(encoding="utf-8")
        doc = json.loads(blob)
        self.assertEqual(doc["status"], "unavailable")
        self.assertEqual(doc["reason"], "Core7P 产品文件未通过发布校验，暂不展示")
        self.assertNotIn("SECRET_LEAK_TOKEN", blob)
        self.assertNotIn("holdings", doc)

    def test_nested_blocks_reconstructed_via_allowlist_and_constrained_rank(self):
        d = fixture()
        d["coverage"]["secret_cov"] = 0.1
        d["coverage"]["degraded_pillars"] = ["acc", 123, {"evil": 1}]
        d["rollback"]["secret_rb"] = "leak"
        d["friction"]["slippage_bps"] = 10
        d["friction"]["extra_fr"] = "leak"
        d["friction"]["accounts"]["100000"]["secret_acct"] = "leak"
        d["friction"]["accounts"]["bad"] = {"names_buyable": "not_int", "cash_drag_pct": 1.0}
        d["holdings"][0]["constrained_rank"] = 1
        d["holdings"][0]["secret_holding"] = "leak"
        self.put(d)
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(set(doc["coverage"].keys()), {"qarp_real_dedt_cov", "acc_real_cov", "ocf_nonfin_cov", "degraded_pillars"})
        self.assertEqual(doc["coverage"]["degraded_pillars"], ["acc"])
        self.assertEqual(set(doc["rollback"].keys()), {"rule", "sessions", "rel_excess_60d", "triggered"})
        self.assertEqual(set(doc["friction"].keys()), {"lot", "min_commission_rmb", "stamp_duty_sell_bps", "slippage_bps", "accounts"})
        self.assertEqual(doc["friction"]["slippage_bps"], 10.0)
        self.assertEqual(doc["friction"]["accounts"], {"100000": {"names_buyable": 23, "cash_drag_pct": 1.2}})
        self.assertEqual(doc["holdings"][0]["constrained_rank"], 1)
        self.assertNotIn("constrained_rank", doc["holdings"][1])
        self.assertNotIn("leak", json.dumps(doc, ensure_ascii=False))

    def test_stale_from_expected_date_and_ew6(self):
        self.put(fixture(signal_date="20261125"), "20261125")
        rc, o = self.run_main("--expected-date", "20261126")
        self.assertEqual(rc, 0)
        self.assertTrue(o["stale"])
        self.assertTrue(json.loads(self.out.read_text())["stale"])

        (self.repo / "data/latest/ew6_today_state.json").write_text(
            json.dumps({"schema": "ew6_today_v1", "status": "ok", "signal_date": "20261126"}), encoding="utf-8"
        )
        rc, o = self.run_main()
        self.assertTrue(json.loads(self.out.read_text())["stale"])

        (self.repo / "data/latest/ew6_today_state.json").write_text(
            json.dumps({"schema": "ew6_today_v1", "status": "ok", "signal_date": "20261125"}), encoding="utf-8"
        )
        rc, o = self.run_main()
        self.assertFalse(json.loads(self.out.read_text())["stale"])

    def test_check_does_not_write(self):
        self.put(fixture())
        rc, o = self.run_main("--check")
        self.assertEqual((rc, o["status"]), (0, "ok"))
        self.assertFalse(self.out.exists())

    def test_unchanged_skip_ignores_generated_at(self):
        self.put(fixture())
        self.run_main()
        first = self.out.read_text(encoding="utf-8")
        doc = json.loads(first)
        doc["generated_at"] = "2000-01-01 00:00:00"
        self.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        rc, o = self.run_main()
        self.assertTrue(o.get("unchanged"))
        self.assertEqual(json.loads(self.out.read_text())["generated_at"], "2000-01-01 00:00:00")
        self.put(fixture(generated_at="2026-11-26 09:00:00"))
        self.assertTrue(self.run_main()[1].get("unchanged"))
        d = fixture()
        d["holdings"][0]["dedt_ttm"] = 2e9
        self.put(d)
        self.assertNotIn("unchanged", self.run_main()[1])
        self.assertEqual(json.loads(self.out.read_text())["holdings"][0]["dedt_ttm"], 2e9)

    def test_public_boundary_clean(self):
        self.put(fixture())
        self.run_main()
        blob = self.out.read_text(encoding="utf-8")
        self.assertFalse(E.PRIVATE_PATH.search(blob))
        for k in ("stock_rows", "latest_sample", "db_path"):
            self.assertNotIn(f'"{k}"', blob)
        self.assertLess(len(blob.encode()), 1_000_000)

    def test_unhashable_and_invalid_holding_field_types_degrade_cleanly(self):
        self.put(fixture())
        self.run_main()
        bad_cases = [
            ("dedt_source", {"evil": 1}),
            ("dedt_source", ["seed_profit_dedt"]),
            ("rev_yoy", {"evil": 1}),
            ("rev_yoy", [0.1]),
            ("rev_yoy", True),
            ("rev_yoy", float("nan")),
            ("dedt_yoy", "0.3"),
            ("rank", True),
            ("rank", 1.5),
            ("ocf_applicable", "false"),
            ("name", {"evil": 1}),
            ("industry", 123),
            ("code", 600100),
            ("constrained_rank", "1"),
        ]
        for field, bad_val in bad_cases:
            d = fixture()
            d["holdings"][0][field] = bad_val
            self.put(d)
            rc, o = self.run_main()
            self.assertEqual((rc, o["status"]), (1, "invalid"), f"{field}={bad_val!r}")
            doc = json.loads(self.out.read_text(encoding="utf-8"))
            self.assertEqual(doc["status"], "unavailable", field)

    def test_non_numeric_account_keys_dropped(self):
        d = fixture()
        d["friction"]["accounts"]["bad_label"] = {"names_buyable": 12, "cash_drag_pct": 2.0}
        d["friction"]["accounts"]["<script>"] = {"names_buyable": 12, "cash_drag_pct": 2.0}
        self.put(d)
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (0, "ok"))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(list(doc["friction"]["accounts"].keys()), ["100000"])

    def test_signal_date_mismatch_and_future_date_rejected(self):
        from datetime import datetime, timedelta
        self.put(fixture(signal_date="20261124"), "20261125")
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (1, "invalid"))
        self.assertTrue(any("filename date" in e for e in o["errors"]), o["errors"])
        (self.ws / "factor_factory/data/forward_shadow/v2/product/core7p_top25_20261125.json").unlink()

        tomorrow = (datetime.now(E.TZ) + timedelta(days=1)).strftime("%Y%m%d")
        self.put(fixture(signal_date=tomorrow), tomorrow)
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (1, "invalid"))
        self.assertTrue(any("future" in e for e in o["errors"]), o["errors"])

    def test_huge_int_overflow_handled_gracefully(self):
        from unittest import mock

        self.assertFalse(E._num(10**400))
        for field in ("dedt_ttm", "pe_ttm"):
            d = fixture()
            d["holdings"][0][field] = 10**400
            self.put(d)
            rc, o = self.run_main()
            self.assertEqual((rc, o["status"]), (1, "invalid"), field)
            doc = json.loads(self.out.read_text(encoding="utf-8"))
            self.assertEqual(doc["status"], "unavailable", field)

        d = fixture()
        d["coverage"]["qarp_real_dedt_cov"] = 10**400
        self.put(d)
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (0, "ok"))
        doc = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertIsNone(doc["coverage"]["qarp_real_dedt_cov"])

        with mock.patch.object(E, "check_source", side_effect=OverflowError("int too large")):
            rc, o = self.run_main()
            self.assertEqual((rc, o["status"]), (1, "invalid"))
            self.assertTrue(any("validation error" in e for e in o["errors"]))
            doc = json.loads(self.out.read_text(encoding="utf-8"))
            self.assertEqual(doc["status"], "unavailable")

    def test_holding_report_and_ann_date_validation(self):
        # Empty/None and <= signal_date are allowed
        d = fixture()
        d["holdings"][0]["latest_report_date"] = ""
        d["holdings"][0]["latest_ann_date"] = None
        d["holdings"][1]["latest_ann_date"] = "20261125"
        self.put(d)
        rc, o = self.run_main()
        self.assertEqual((rc, o["status"]), (0, "ok"))

        bad_values = [
            "2026-09-30",
            "20260230",
            "bad",
            "20260930 ",
            "   ",
            "20261126",
            20260930,
            True,
            ["20260930"],
            0,
        ]
        for field in ("latest_report_date", "latest_ann_date"):
            for bad in bad_values:
                d = fixture()
                d["holdings"][0][field] = bad
                self.put(d)
                rc, o = self.run_main()
                self.assertEqual((rc, o["status"]), (1, "invalid"), f"{field}={bad!r}")
                doc = json.loads(self.out.read_text(encoding="utf-8"))
                self.assertEqual(doc["status"], "unavailable", f"{field}={bad!r}")


if __name__ == "__main__":
    unittest.main()
