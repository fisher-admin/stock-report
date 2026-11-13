import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_factor_audit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_factor_audit", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunFactorAuditOverlayTests(unittest.TestCase):
    def test_load_selection_overlay_parses_manifest_constraints(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "formal_overlay_contract_inputs.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "contract_version": "2026-04-24.formal_overlay_contract.v1",
                        "candidate_id": "shadow_candidate_02_cap2_blacklist_3",
                        "overlay_constraints": {
                            "top_n": 20,
                            "max_names_per_industry": 2,
                            "industry_blacklist": ["专用机械", "软件服务", "软件服务"],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            overlay = module.load_selection_overlay(manifest_path)

            self.assertEqual(overlay.manifest_path, manifest_path.resolve())
            self.assertEqual(overlay.contract_version, "2026-04-24.formal_overlay_contract.v1")
            self.assertEqual(overlay.candidate_id, "shadow_candidate_02_cap2_blacklist_3")
            self.assertEqual(overlay.top_n, 20)
            self.assertEqual(overlay.max_names_per_industry, 2)
            self.assertEqual(overlay.industry_blacklist, ("专用机械", "软件服务"))

    def test_select_ranked_holdings_applies_blacklist_and_industry_cap(self):
        module = load_module()
        overlay = module.SelectionOverlay(
            manifest_path=Path("/tmp/formal_overlay_contract_inputs.json"),
            top_n=4,
            max_names_per_industry=2,
            industry_blacklist=("软件服务",),
        )
        day = pd.DataFrame(
            {
                "ts_code": [
                    "000001.SZ",
                    "000002.SZ",
                    "000003.SZ",
                    "000004.SZ",
                    "000005.SZ",
                    "000006.SZ",
                    "000007.SZ",
                ],
                "score": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0],
                "next_return_close_to_close_1d": [0.02, 0.03, 0.01, 0.04, -0.01, 0.05, 0.00],
                "industry": ["专用机械", "软件服务", "电气设备", "专用机械", "专用机械", "电气设备", "环保"],
            }
        )

        chosen = module.select_ranked_holdings(
            day,
            score_col="score",
            return_col="next_return_close_to_close_1d",
            top_n=module.effective_top_n(20, overlay),
            selection_overlay=overlay,
        )

        self.assertEqual(list(chosen["ts_code"]), ["000001.SZ", "000003.SZ", "000004.SZ", "000006.SZ"])
        self.assertNotIn("软件服务", set(chosen["industry"]))
        self.assertLessEqual(chosen["industry"].value_counts().max(), 2)
        self.assertEqual(len(chosen), 4)

    def test_evaluate_strategy_uses_overlay_top_n_override(self):
        module = load_module()
        overlay = module.SelectionOverlay(
            manifest_path=Path("/tmp/formal_overlay_contract_inputs.json"),
            top_n=1,
        )
        panel = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(
                    [
                        "2026-04-01",
                        "2026-04-01",
                        "2026-04-01",
                        "2026-04-02",
                        "2026-04-02",
                        "2026-04-02",
                    ]
                ),
                "ts_code": ["A", "B", "C", "A", "B", "C"],
                "score": [10.0, 8.0, 5.0, 9.0, 7.0, 6.0],
                "next_return_close_to_close_1d": [0.05, 0.02, -0.01, 0.03, 0.01, -0.02],
            }
        )

        metrics = module.evaluate_strategy(
            panel,
            score_col="score",
            return_col="next_return_close_to_close_1d",
            top_n=3,
            round_trip_cost=0.0,
            selection_overlay=overlay,
        )

        self.assertEqual(list(metrics["daily_returns"].round(6)), [0.05, 0.03])
        self.assertEqual(list(metrics["daily_turnover"].round(6)), [1.0, 0.0])
        self.assertEqual(metrics["metrics"]["days"], 2)
        self.assertAlmostEqual(metrics["metrics"]["turnover"], 0.5)


if __name__ == "__main__":
    unittest.main()
