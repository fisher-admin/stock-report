import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_years as cy  # noqa: E402


def szse_rows(month: str, open_days: set[str]) -> list[dict]:
    days = pd.date_range(f"{month}-01", periods=pd.Timestamp(f"{month}-01").days_in_month).strftime("%Y-%m-%d")
    return [{"jyrq": d, "jybz": "1" if d.replace("-", "") in open_days else "0"} for d in days]


class CalendarYearsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_2027_rule(self):
        opened, closed = cy.provisional_open_dates(2027)
        self.assertEqual(len(opened), 243)
        self.assertEqual(len(closed), 18)
        for d in ("20270101", "20270205", "20270212", "20270405", "20270503", "20270609", "20270915",
                  "20271001", "20271007"):
            self.assertIn(d, closed)
        for d in ("20270104", "20270204", "20270215", "20271008"):
            self.assertIn(d, opened)

    def test_build_is_write_once(self):
        self.assertEqual(cy.build(2027, self.ws)["status"], "built")
        self.assertEqual(cy.build(2027, self.ws)["status"], "unchanged")
        p = cy.year_path(2027, self.ws)
        cal = json.loads(p.read_text())
        cal["open_dates"] = cal["open_dates"][1:]
        p.write_text(json.dumps(cal))
        with self.assertRaises(RuntimeError):
            cy.build(2027, self.ws)

    def test_confirm_stops_at_unpublished_month(self):
        cy.build(2027, self.ws)
        opened = set(cy.provisional_open_dates(2027)[0])
        calls = []

        def fetch(month, ws):
            calls.append(month)
            return szse_rows(month, opened) if month == "2027-01" else []

        rc, rep = cy.confirm(2027, self.ws, fetch=fetch, sleep=lambda s: None)
        self.assertEqual(rc, cy.EXIT_OK)
        self.assertEqual(calls, ["2027-01", "2027-02"])
        self.assertEqual(rep["confirmed"], ["2027-01"])
        cal = json.loads(cy.year_path(2027, self.ws).read_text())
        self.assertEqual(cal["months"]["2027-01"]["status"], "confirmed_szse")
        self.assertEqual(cal["status"], "provisional")
        calls.clear()
        cy.confirm(2027, self.ws, fetch=fetch, sleep=lambda s: None)
        self.assertEqual(calls, ["2027-02"])          # confirmed months are never fetched again

    def test_confirm_conflict_keeps_open_dates(self):
        cy.build(2027, self.ws)
        before = json.loads(cy.year_path(2027, self.ws).read_text())["open_dates"]
        opened = set(before) - {"20270104"}
        rc, rep = cy.confirm(2027, self.ws, fetch=lambda m, ws: szse_rows(m, opened), sleep=lambda s: None)
        self.assertEqual(rc, cy.EXIT_CONFLICT)
        self.assertEqual(rep["conflicts"]["2027-01"]["only_provisional"], ["20270104"])
        cal = json.loads(cy.year_path(2027, self.ws).read_text())
        self.assertEqual(cal["open_dates"], before)
        self.assertEqual(cal["status"], "conflict")

    def test_reconcile(self):
        cy.build(2027, self.ws)
        cache = self.ws / "stock_data/03-working/backtest_cache"
        cache.mkdir(parents=True)
        pd.DataFrame({"vol": [100.0] * 1200}).to_parquet(cache / "daily_20270104.parquet")
        pd.DataFrame({"vol": [100.0] * 1200}).to_parquet(cache / "daily_20270101.parquet")
        self.assertEqual(cy.reconcile("20270104", self.ws)[1]["verdict"], "ok")
        self.assertEqual(cy.reconcile("20270101", self.ws)[1]["verdict"], "traded_on_closed_day")
        self.assertEqual(cy.reconcile("20270102", self.ws)[1]["verdict"], "ok_closed")
        self.assertEqual(cy.reconcile("20270105", self.ws)[1]["verdict"], "tdx_cache_missing")


class SelfHealCalendarExtensionTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "stock-analyzer"))
        import data_provider as dp  # noqa: PLC0415
        from selfheal_client import SelfHealClient, SelfHealError  # noqa: PLC0415

        self.dp, self.Client, self.Err = dp, SelfHealClient, SelfHealError
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        h = self.ws / "stock_data/03-working/health"
        (h / "calendar_years").mkdir(parents=True)
        (h / "trading_calendar.json").write_text(json.dumps(
            {"source": "rds_trade_cal", "generated_at": "2026-11-17 19:30:05", "from": "20261116", "to": "20261231",
             "open_dates": ["20261116", "20261231"]}))
        cy.build(2027, self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def primary(self):
        dp = self.dp

        class Dead:
            def query(self, api, fields="", **p):
                raise dp.DataProviderError("rds trade_cal HTTP 401")

            def __getattr__(self, name):
                if name.startswith("_"):
                    raise AttributeError(name)
                return lambda **p: self.query(name, **p)
        return Dead()

    def test_extends_beyond_paid_and_label_is_stable(self):
        f = self.Client(self.primary(), workspace=self.ws).trade_cal(start_date="20261230", end_date="20270106")
        self.assertEqual(list(f.loc[f.is_open == 1, "cal_date"]), ["20261231", "20270104", "20270105", "20270106"])
        self.assertEqual(f.loc[f.cal_date == "20270104", "pretrade_date"].item(), "20261231")
        src = f["source_provider"].iloc[0]
        self.assertIn("rds_trade_cal@2026-11-17", src)
        self.assertIn("calendar_years(2027:provisional)", src)
        # stage 1 persists the healed frame; the next heal must not nest the label
        p = self.ws / "stock_data/03-working/health/trading_calendar.json"
        cal = json.loads(p.read_text())
        cal.update(source=f"{src}_trade_cal", to="20271231",
                   open_dates=sorted(set(cal["open_dates"]) | set(cy.provisional_open_dates(2027)[0])))
        p.write_text(json.dumps(cal))
        g = self.Client(self.primary(), workspace=self.ws).trade_cal(start_date="20270104", end_date="20270104")
        self.assertEqual(g["source_provider"].iloc[0], "selfheal_persisted_trade_cal(rds_trade_cal@2026-11-17+calendar_years(2027:provisional))")

    def test_overlap_disagreement_fails_closed(self):
        p = self.ws / "stock_data/03-working/health/trading_calendar.json"
        cal = json.loads(p.read_text())
        cal.update(to="20270110", open_dates=cal["open_dates"] + ["20270104"])  # paid says 01-05 closed
        p.write_text(json.dumps(cal))
        with self.assertRaises(self.Err):
            self.Client(self.primary(), workspace=self.ws).trade_cal(start_date="20270104", end_date="20270106")


if __name__ == "__main__":
    unittest.main()
