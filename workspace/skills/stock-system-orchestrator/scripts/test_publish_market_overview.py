#!/usr/bin/env python3
"""Regression tests for the local publication generator order."""

from __future__ import annotations

import unittest

import publish_market_overview as publisher


class PublishMarketOverviewTests(unittest.TestCase):
    def test_heatmap_is_generated_before_morning_brief_that_reads_it(self):
        names = [script.name for script in publisher.SCRIPTS]

        self.assertLess(
            names.index("generate_market_industry_heatmap.py"),
            names.index("generate_market_morning_brief.py"),
            "morning brief must read the freshly generated heatmap",
        )

    def test_latest_states_are_generated_before_unified_payload(self):
        names = [script.name for script in publisher.SCRIPTS]

        self.assertLess(
            names.index("generate_latest_states.py"),
            names.index("generate_unified_decision_payload.py"),
            "unified payload must read the freshly generated market_state",
        )

    def test_generated_dates_reject_stale_unified_morning_brief(self):
        with self.assertRaisesRegex(RuntimeError, "unified morning brief"):
            publisher.validate_generated_dates(
                {"trade_date": "20260825", "market_data_trade_date": "20260825"},
                {"latest_trade_date": "20260825"},
                {"generated_at": "2026-08-26 08:00:18", "morning_brief": {"trade_date": "20260824"}},
                {"trade_date": "20260825"},
            )

    def test_lagging_selection_manifest_degrades_instead_of_failing(self):
        # 2026-10-13: the 10-12 evening chain failed, so the brief for 10-12
        # was refused because only the selection manifest still said 10-09.
        dates = publisher.validate_generated_dates(
            {"trade_date": "20261012", "market_data_trade_date": "20261012"},
            {"latest_trade_date": "20261012"},
            {"generated_at": "2026-10-13 08:00:30", "morning_brief": {"trade_date": "20261012"}},
            {"trade_date": "20261009"},
        )
        self.assertEqual(dates["strategy_lagging_trade_date"], "20261009")

    def test_coherent_dates_have_no_lag(self):
        dates = publisher.validate_generated_dates(
            {"trade_date": "20261022", "market_data_trade_date": "20261022"},
            {"latest_trade_date": "20261022"},
            {"generated_at": "x", "morning_brief": {"trade_date": "20261022"}},
            {"trade_date": "20261022"},
        )
        self.assertIsNone(dates["strategy_lagging_trade_date"])

    def test_manifest_ahead_of_market_is_still_drift(self):
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            publisher.validate_generated_dates(
                {"trade_date": "20261012", "market_data_trade_date": "20261012"},
                {"latest_trade_date": "20261012"},
                {"generated_at": "x", "morning_brief": {"trade_date": "20261012"}},
                {"trade_date": "20261013"},
            )

    def test_lagging_selection_skips_page_publish(self):
        from unittest import mock

        summary = {"strategy_lagging_trade_date": "20261009"}
        with mock.patch.object(publisher, "run_generators"), \
                mock.patch.object(publisher, "verify_outputs", return_value=summary), \
                mock.patch.object(publisher, "publish_via_v2") as publish:
            self.assertEqual(publisher.main(), 0)
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
