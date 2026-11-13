import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "qlib_adapter"
    / "record_factor_factory_round.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("record_factor_factory_round", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordFactorFactoryRoundTests(unittest.TestCase):
    def test_build_round_payload_extracts_metrics_and_artifacts(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outputs/evolution").mkdir(parents=True)

            (root / "outputs/evolution/qlib_factor_scorecard.json").write_text(
                json.dumps(
                    {
                        "qlib_version": "0.9.7",
                        "label_maturity": {"status": "intraday_pending"},
                        "promotion_gate": {"eligible_for_promotion": False},
                        "model_scorecards": [
                            {
                                "model": "ridge",
                                "rank_ic": {"mean": -0.25, "count": 7},
                                "top_bottom_spread": {"mean": -0.005},
                            },
                            {
                                "model": "lightgbm",
                                "rank_ic": {"mean": 0.12, "count": 7},
                                "top_bottom_spread": {"mean": 0.003},
                            },
                        ],
                    }
                )
            )
            (root / "outputs/evolution/validation_readiness_and_label_gap_report.json").write_text(
                json.dumps(
                    {
                        "validation_readiness": {
                            "audit_weight_ready": False,
                            "audit_weight_blocked": True,
                        },
                        "label_gap": {
                            "latest_rows_with_any_missing_label": 1,
                            "latest_missing_label_counts": {"close_to_close": 1},
                        },
                    }
                )
            )
            (root / "outputs/evolution/qlib_production_decision_log.md").write_text("log")
            overlay_manifest = root / "outputs/evolution/formal_overlay_contract_inputs.json"
            overlay_manifest.write_text(
                json.dumps(
                    {
                        "overlay_constraints": {
                            "top_n": 20,
                            "max_names_per_industry": 2,
                            "industry_blacklist": ["软件服务"],
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )

            payload = module.build_round_payload(root, "round-x", overlay_manifest=overlay_manifest)

            self.assertEqual(payload["params"]["round_name"], "round-x")
            self.assertEqual(payload["params"]["qlib_version"], "0.9.7")
            self.assertEqual(payload["params"]["label_maturity_status"], "intraday_pending")
            self.assertEqual(payload["params"]["overlay_manifest_path"], str(overlay_manifest))
            self.assertEqual(payload["params"]["overlay_top_n"], 20)
            self.assertEqual(payload["params"]["overlay_max_names_per_industry"], 2)
            self.assertEqual(payload["params"]["overlay_industry_blacklist_count"], 1)
            self.assertEqual(payload["metrics"]["qlib_model_count"], 2)
            self.assertEqual(payload["metrics"]["best_rank_ic_mean"], 0.12)
            self.assertEqual(payload["metrics"]["audit_weight_ready"], 0.0)
            self.assertEqual(payload["metrics"]["latest_rows_with_any_missing_label"], 1.0)
            self.assertIn(
                str(root / "outputs/evolution/qlib_production_decision_log.md"),
                payload["artifact_paths"],
            )

    def test_build_round_payload_ignores_missing_artifacts(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outputs/evolution").mkdir(parents=True)
            (root / "outputs/evolution/qlib_factor_scorecard.json").write_text(
                json.dumps({"model_scorecards": []})
            )
            payload = module.build_round_payload(root, "minimal")
            self.assertEqual(payload["metrics"]["qlib_model_count"], 0)
            self.assertTrue(all(Path(p).exists() for p in payload["artifact_paths"]))


if __name__ == "__main__":
    unittest.main()
