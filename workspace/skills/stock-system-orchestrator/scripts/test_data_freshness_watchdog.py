#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import data_freshness_watchdog as watchdog


class DataFreshnessWatchdogTests(unittest.TestCase):
    def test_official_calendar_handles_multi_day_exchange_holiday(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            calendar = Path(tmpdir) / "trading_calendar.json"
            calendar.write_text(
                json.dumps(
                    {
                        "from": "20260901",
                        "to": "20261130",
                        "open_dates": ["20260929", "20260930", "20261009", "20261012"],
                    }
                ),
                encoding="utf-8",
            )

            expected, open_dates = watchdog._expected_trading_day(
                dt.date(2026, 10, 6),
                calendar_path=calendar,
            )

        self.assertEqual(expected, dt.date(2026, 9, 30))
        self.assertEqual(
            watchdog._trading_days_between(dt.date(2026, 9, 29), expected, open_dates=open_dates),
            1,
        )

    def test_missing_calendar_falls_back_to_weekdays(self) -> None:
        expected, open_dates = watchdog._expected_trading_day(
            dt.date(2026, 8, 23),
            calendar_path=Path("/definitely/missing/calendar.json"),
        )

        self.assertEqual(expected, dt.date(2026, 8, 21))
        self.assertEqual(open_dates, [])

    def test_post_close_open_day_requires_same_day_data(self) -> None:
        open_dates = ["20260818", "20260819", "20260820"]

        before_deadline = watchdog._allowed_stale_trading_days(
            dt.datetime(2026, 8, 19, 15, 0),
            open_dates=open_dates,
        )
        after_deadline = watchdog._allowed_stale_trading_days(
            dt.datetime(2026, 8, 19, 21, 5),
            open_dates=open_dates,
        )

        self.assertEqual(before_deadline, watchdog.STALE_TRADING_DAYS)
        self.assertEqual(after_deadline, 0)

    def test_hermes_runtime_bypasses_retired_openclaw_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            flag = Path(tmpdir) / "stock_alerts.disabled"
            flag.write_text("disabled", encoding="utf-8")

            self.assertTrue(watchdog.alerts_disabled(disable_flag=flag, runtime_owner="openclaw"))
            self.assertFalse(watchdog.alerts_disabled(disable_flag=flag, runtime_owner="hermes"))

    def test_hermes_runtime_delegates_delivery_to_scheduler(self) -> None:
        report = {
            "checked_at": "2026-08-19T21:05:00",
            "expected_trading_day": "2026-08-19",
            "stale_findings": ["stale"],
            "last_run": {"ok": False, "run_file": "run.json"},
        }
        with mock.patch.dict(os.environ, {"STOCK_HEALTH_RUNTIME": "hermes"}), mock.patch.object(
            watchdog, "_send_discord"
        ) as send, mock.patch.object(watchdog, "_macos_notify") as notify:
            sent = watchdog.dispatch_alert(report)

        self.assertEqual(sent["delivery_owner"], "hermes")
        send.assert_not_called()
        notify.assert_not_called()

    def test_recovered_publish_clears_last_run_watchdog(self) -> None:
        last = {
            "ok": False,
            "selection_ok": True,
            "publish_ok": False,
            "run_file": "stock-20260820T113023Z.json",
        }
        healed = watchdog.recover_last_run_if_publish_healed(
            last,
            {"trade_date": "20260820", "published": True},
            dt.date(2026, 8, 20),
            {},
        )
        self.assertFalse(healed["ok"])
        self.assertNotIn("publish_recovered", healed)

        still_bad = watchdog.recover_last_run_if_publish_healed(
            last,
            {"trade_date": "20260818", "published": True},
            dt.date(2026, 8, 20),
        )
        self.assertFalse(still_bad["ok"])

    def test_recovered_yesterday_run_stays_healthy_before_today_closes(self) -> None:
        last = {
            "ok": False,
            "run_id": "stock-20260824T113017Z",
            "trade_date": "20260824",
            "selection_ok": True,
            "publish_ok": False,
        }
        healed = watchdog.recover_last_run_if_publish_healed(
            last,
            {"trade_date": "20260824", "published": False},
            dt.date(2026, 8, 25),
            {
                "run_id": "stock-20260824T113017Z",
                "trade_date": "20260824",
                "remote_confirmed": True,
                "local_sha": "abc",
                "remote_sha": "abc",
            },
        )
        self.assertTrue(healed["ok"])
        self.assertTrue(healed["publish_recovered"])

    def test_last_run_preserves_run_id_for_receipt_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            run_id = "stock-20260824T113017Z"
            (run_dir / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "selection_ok": True,
                        "publish_ok": False,
                        "research_ok": True,
                        "stages": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(watchdog, "HEALTH_RUNS", run_dir):
                last = watchdog.check_last_run()

        self.assertEqual(last["run_id"], run_id)

    def test_watchdog_message_is_chinese_and_silent_when_healthy(self) -> None:
        silent = watchdog.format_watchdog_message({"healthy": True, "stale_findings": ["x"]})
        self.assertEqual(silent, "")

        text = watchdog.format_watchdog_message(
            {
                "healthy": False,
                "expected_trading_day": "2026-08-20",
                "stale_findings": ["公开报告交易日还停在 2026-08-18，落后约 2 个交易日（应到 2026-08-20）"],
                "last_run": {"ok": False, "selection_ok": True, "publish_ok": False},
            }
        )
        self.assertIn("选股数据可能停更", text)
        self.assertIn("今天应有交易日：2026-08-20", text)
        self.assertIn("当天报告没有发出去", text)
        self.assertNotIn("{", text)
        self.assertNotIn("run_file", text)
        self.assertNotIn("stdout", text)

    def test_auxiliary_failure_uses_degraded_message_not_data_stale_message(self) -> None:
        text = watchdog.format_watchdog_message(
            {
                "healthy": True,
                "auxiliary_healthy": False,
                "auxiliary_findings": ["当天后置研究未完成"],
                "expected_trading_day": "2026-08-28",
            }
        )
        self.assertIn("附属任务异常", text)
        self.assertIn("不影响已发布选股结果", text)
        self.assertNotIn("选股数据可能停更", text)

    def test_torn_manifest_and_run_json_do_not_crash_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "run_manifest.json"
            runs = root / "runs"
            runs.mkdir()
            manifest.write_text('{"trade_date":', encoding="utf-8")
            (runs / "stock-20260828T113000Z.json").write_text('{"run_id":', encoding="utf-8")
            with mock.patch.object(watchdog, "RUN_MANIFEST", manifest), mock.patch.object(
                watchdog, "HEALTH_RUNS", runs
            ):
                manifest_state = watchdog.check_manifest()
                run_state = watchdog.check_last_run()
        self.assertFalse(manifest_state["ok"])
        self.assertFalse(run_state["ok"])


if __name__ == "__main__":
    unittest.main()
