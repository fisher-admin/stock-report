#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import orchestrator_common as common  # noqa: E402
import stage4_ai_backfill_loop as backfill  # noqa: E402
import strategy_publication_layer as publication  # noqa: E402


class StrategyRetirementTests(unittest.TestCase):
    def test_failed_strategies_are_archived_not_active(self):
        self.assertEqual(common.RETAINED_STRATEGIES, [common.TARGET_STRATEGY])
        self.assertEqual(publication.ACTIVE_STRATEGY_IDS, [publication.PREBREAKOUT_ID])
        for strategy_id in (
            "greenfield_o2c_v1",
            "t1_factor_v1",
            "S3_intraday_shape_daily",
            "wts_auction_v1",
            "auction_chase",
            "simple_pead",
            "intraday_reversal",
        ):
            self.assertIn(strategy_id, common.ARCHIVED_STRATEGIES)
            self.assertEqual(common.ARCHIVED_STRATEGIES[strategy_id]["status"], "archived_historical_only")

    def test_daily_schedulers_do_not_expand_retired_strategies(self):
        stage3 = (SCRIPT_DIR / "stage3_strategy_suite.py").read_text(encoding="utf-8")
        self.assertNotIn("run_step('build_greenfield_pool'", stage3)
        self.assertNotIn("run_step('daily_o2c_selection'", stage3)

        closure = (SCRIPT_DIR.parents[2] / "ops/run_daily_closure.sh").read_text(encoding="utf-8")
        self.assertNotIn('"$WS/skills/stock-system-orchestrator/scripts/generate_wts_tracking.py"', closure)
        self.assertNotIn('bash "$WS/ops/run_prebreakout_shadow.sh"', closure)
        s3 = (SCRIPT_DIR.parents[2] / "ops/run_s3_tracking.sh").read_text(encoding="utf-8")
        self.assertNotIn('"$WS/skills/stock-system-orchestrator/scripts/generate_s3_tracking.py"', s3)

        trade_date, codes, stocks, metadata = backfill.pending_o2c_codes("20260811")
        self.assertEqual(codes, [])
        self.assertEqual(stocks, [])
        self.assertEqual(metadata["skipped_reason"], "strategy_archived")

        alert = (SCRIPT_DIR / "stock_pipeline_alert.py").read_text(encoding="utf-8")
        self.assertIn('SIDS = ["prebreakout_v41"]', alert)
        self.assertNotIn("三策略 AI 覆盖均达标", alert)

    def test_canonical_strategy_output_contains_active_strategy_only_and_no_empty_curve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            working = root / "working"
            published = root / "published"
            with mock.patch.object(publication, "WORKING_REPO", working), mock.patch.object(
                publication, "PUBLISHED_REPO", published
            ):
                payload = publication.write_canonical_strategy_backtests(
                    "20260811",
                    [{"code": "000001.SZ", "score": 80.0}],
                    [{"code": "000002.SZ", "score": 70.0}],
                    [{"code": "000003.SZ", "score": 60.0}],
                    {
                        publication.PREBREAKOUT_ID: 1.0,
                        publication.O2C_ID: 0.0,
                        publication.T1_ID: 0.0,
                    },
                    {
                        publication.PREBREAKOUT_ID: "20260811",
                        publication.O2C_ID: "20260702",
                        publication.T1_ID: "20260702",
                    },
                )
        self.assertEqual([row["id"] for row in payload["strategies"]], [publication.PREBREAKOUT_ID])
        self.assertEqual(payload["strategies"][0]["artifact_kind"], "candidate_snapshot")
        self.assertNotIn("curve", payload["strategies"][0])


if __name__ == "__main__":
    unittest.main()
