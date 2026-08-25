from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import enforce_public_boundary as boundary  # noqa: E402


class PublicBoundaryTests(unittest.TestCase):
    def test_clean_aggregate_result_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "data/latest/strategy_evaluation.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "contract_version": "evaluation_integrity_v2",
                        "integrity": {"fake_or_impossible_return_count": 0},
                        "strategies": {"prebreakout_v43_control": {"metrics": None}},
                    }
                ),
                encoding="utf-8",
            )

            result = boundary.audit_public_tree(
                root,
                allowed_data_paths={"data/latest/strategy_evaluation.json"},
            )

        self.assertTrue(result["ok"], result)

    def test_raw_history_and_recommendation_detail_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history = root / "data/history/20260811.json"
            detail = root / "data/recommendation_analytics/prebreakout_recommendations.json"
            history.parent.mkdir(parents=True)
            detail.parent.mkdir(parents=True)
            history.write_text("{}", encoding="utf-8")
            detail.write_text('{"rows": [{"stock_code": "000001"}]}', encoding="utf-8")

            result = boundary.audit_public_tree(root, allowed_data_paths=set())

        self.assertFalse(result["ok"])
        self.assertTrue(any("data/history" in item for item in result["violations"]))
        self.assertTrue(any("prebreakout_recommendations.json" in item for item in result["violations"]))

    def test_nonempty_stock_rows_and_private_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result_path = root / "data/latest/review_track_latest.json"
            source_path = root / "system/src/runner.py"
            result_path.parent.mkdir(parents=True)
            source_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps({"stock_rows": [{"stock_code": "000001"}]}),
                encoding="utf-8",
            )
            source_path.write_text(
                'WORKSPACE = "/Users/example/.openclaw/workspace"\n',
                encoding="utf-8",
            )

            result = boundary.audit_public_tree(
                root,
                allowed_data_paths={"data/latest/review_track_latest.json"},
            )

        self.assertFalse(result["ok"])
        self.assertTrue(any("stock_rows" in item for item in result["violations"]))
        self.assertTrue(any("private absolute path" in item for item in result["violations"]))

    def test_row_samples_and_local_runtime_metadata_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result_path = root / "data/latest/review_state.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps(
                    {
                        "date_stats": [{"recommend_date": "20260811", "return_pct": 0.5}],
                        "latest_sample": [{"stock_code": "000001"}],
                        "db_path": "~/.openclaw/private/recommendations.db",
                    }
                ),
                encoding="utf-8",
            )

            result = boundary.audit_public_tree(
                root,
                allowed_data_paths={"data/latest/review_state.json"},
            )

        self.assertFalse(result["ok"])
        self.assertTrue(any("latest_sample" in item for item in result["violations"]))
        self.assertTrue(any("db_path" in item for item in result["violations"]))

    def test_prepare_removes_private_artifacts_and_preserves_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/latest/review_state_unified.json"
            csv_path = root / "data/latest/recommendation_history.csv"
            result_path = root / "data/latest/strategy_evaluation.json"
            history = root / "data/history/20260811.json"
            for path in (raw, csv_path, result_path, history):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")

            report = boundary.prepare_public_tree(root)

            self.assertFalse(raw.exists())
            self.assertFalse(csv_path.exists())
            self.assertFalse(history.exists())
            self.assertTrue(result_path.exists())
            self.assertGreaterEqual(report["removed_count"], 3)

    def test_publication_status_is_reconciled_and_then_passes_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            latest = root / "data/latest"
            latest.mkdir(parents=True)
            manifest_path = latest / "run_manifest.json"
            verdict_path = latest / "system_verdict.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "validation_ok": True,
                        "publish_ready": True,
                        "published": True,
                    }
                ),
                encoding="utf-8",
            )
            verdict_path.write_text(
                json.dumps(
                    {
                        "run": {"pipeline_status": {"publish_ok": False}},
                        "pipeline_status": {"publish_ok": False},
                        "source_lineage": {
                            "ai_publish_readiness": {"ok": True, "published": True}
                        },
                    }
                ),
                encoding="utf-8",
            )
            allowlist = {
                "data/latest/run_manifest.json",
                "data/latest/system_verdict.json",
            }

            before = boundary.audit_public_tree(root, allowed_data_paths=allowlist)
            report = boundary.prepare_public_tree(root)
            after = boundary.audit_public_tree(root, allowed_data_paths=allowlist)
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))

            self.assertFalse(before["ok"])
            self.assertTrue(report["status_reconciliation"]["changed"])
            self.assertTrue(verdict["pipeline_status"]["publish_ok"])
            self.assertTrue(verdict["run"]["pipeline_status"]["publish_ok"])
            self.assertTrue(after["ok"], after)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["published"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            unpublished_report = boundary.prepare_public_tree(root)
            unpublished_verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            unpublished_audit = boundary.audit_public_tree(root, allowed_data_paths=allowlist)

            self.assertTrue(unpublished_report["status_reconciliation"]["changed"])
            self.assertFalse(unpublished_verdict["pipeline_status"]["publish_ok"])
            self.assertFalse(unpublished_verdict["run"]["pipeline_status"]["publish_ok"])
            self.assertTrue(unpublished_audit["ok"], unpublished_audit)

    def test_receipt_recovered_publication_passes_while_manifest_published_is_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            latest = root / "data/latest"
            latest.mkdir(parents=True)
            (latest / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "validation_ok": True,
                        "publish_ready": True,
                        "published": False,
                    }
                ),
                encoding="utf-8",
            )
            (latest / "system_verdict.json").write_text(
                json.dumps(
                    {
                        "run": {
                            "pipeline_status": {
                                "publish_ok": True,
                                "publish_recovered": True,
                            }
                        },
                        "pipeline_status": {
                            "publish_ok": True,
                            "publish_recovered": True,
                        },
                        "source_lineage": {
                            "ai_publish_readiness": {"ok": True, "published": False},
                            "deployment_receipt": {"matched": True, "remote_confirmed": True},
                        },
                    }
                ),
                encoding="utf-8",
            )
            allowlist = {
                "data/latest/run_manifest.json",
                "data/latest/system_verdict.json",
            }

            report = boundary.prepare_public_tree(root)
            verdict = json.loads((latest / "system_verdict.json").read_text(encoding="utf-8"))

            self.assertFalse(report["status_reconciliation"]["changed"])
            self.assertTrue(verdict["pipeline_status"]["publish_ok"])
            self.assertTrue(boundary.audit_public_tree(root, allowed_data_paths=allowlist)["ok"])

    def test_legacy_root_data_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "combined.json").write_text('{"stocks": []}', encoding="utf-8")

            result = boundary.audit_public_tree(root, allowed_data_paths=set())

        self.assertFalse(result["ok"])
        self.assertTrue(any("combined.json" in item for item in result["violations"]))

    def test_allowlisted_frontend_sources_exist_in_repo(self):
        import re

        manifest = (ROOT / "assets/scripts/v2/data/manifest.js").read_text(encoding="utf-8")
        sources = re.findall(r"path:\s*'([^']+)'", manifest)
        allowlist = {
            line.strip()
            for line in (ROOT / "config/public-result-allowlist.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        missing = [path for path in sources if path in allowlist and not (ROOT / path).exists()]
        self.assertEqual(missing, [], f"pages will 404 these allowlisted sources: {missing}")

    def test_literal_credentials_are_rejected_but_environment_lookups_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "system/src/config.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                'token = os.environ.get("API_TOKEN")\napi_key = "literal-secret-value"\n',
                encoding="utf-8",
            )

            result = boundary.audit_public_tree(root, allowed_data_paths=set())

        self.assertFalse(result["ok"])
        self.assertTrue(any("literal credential" in item for item in result["violations"]))


if __name__ == "__main__":
    unittest.main()
