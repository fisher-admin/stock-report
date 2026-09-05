from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_pages_artifact import ArtifactBuildError, build_site
from scripts.enforce_public_boundary import _publication_status_contract


REPO_ROOT = Path(__file__).resolve().parents[1]


class PagesArtifactTests(unittest.TestCase):
    def test_incomplete_ai_or_mismatched_readiness_blocks_publication(self):
        cases = [({}, {}), ({"ai_complete": False}, {}), ({}, {"ai_complete": False}),
                 ({}, {"run_id": "wrong-run"}), ({}, {"publish_mode": "wrong-mode"})]
        for manifest_change, readiness_change in cases:
            with self.subTest(manifest_change=manifest_change, readiness_change=readiness_change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                latest = root / "data/latest"
                latest.mkdir(parents=True)
                manifest = {"validation_ok": True, "publish_ready": True, "published": True, "ai_complete": True, "run_id": "run-1", "trade_date": "20260904", "publish_mode": "full", **manifest_change}
                readiness = {"ok": True, "ai_complete": True, "run_id": "run-1", "trade_date": "20260904", "publish_mode": "full", **readiness_change}
                if not manifest_change and not readiness_change:
                    readiness = {"ok": True}
                verdict = {"pipeline_status": {"publish_ok": True}, "source_lineage": {"ai_publish_readiness": readiness}}
                (latest / "run_manifest.json").write_text(json.dumps(manifest))
                (latest / "system_verdict.json").write_text(json.dumps(verdict))
                self.assertFalse(_publication_status_contract(root)["expected_publish_ok"])

    def test_deployment_refuses_unready_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "scripts.build_pages_artifact.reconcile_public_status_contract",
            return_value={"checked": True, "expected_publish_ok": False},
        ):
            with self.assertRaisesRegex(ArtifactBuildError, "not validated and ready"):
                build_site(REPO_ROOT, Path(tmpdir) / "site", published_artifact=True)

    def test_build_contains_only_sanitized_allowlisted_results(self):
        source_manifest = REPO_ROOT / "data/latest/run_manifest.json"
        original_manifest = source_manifest.read_bytes()

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "site"
            report = build_site(REPO_ROOT, output)

            self.assertTrue(report["ok"])
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "assets/scripts/v2/app.js").is_file())
            self.assertTrue((output / "data/latest/run_manifest.json").is_file())
            self.assertTrue((output / "data/latest/system_verdict.json").is_file())
            self.assertFalse((output / "data/latest/review_state_unified.json").exists())
            self.assertFalse((output / "data/latest/factor_attribution_state.json").exists())

            verdict = json.loads(
                (output / "data/latest/system_verdict.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (output / "data/latest/run_manifest.json").read_text(encoding="utf-8")
            )
            expected_publish_ok = bool(manifest.get("published"))
            self.assertEqual(verdict["pipeline_status"]["publish_ok"], expected_publish_ok)
            self.assertEqual(verdict["run"]["pipeline_status"]["publish_ok"], expected_publish_ok)

            review = json.loads(
                (output / "data/latest/review_state.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("db_path", review)
            self.assertNotIn("latest_sample", review)

        self.assertEqual(original_manifest, source_manifest.read_bytes())

    def test_deployable_artifact_is_stamped_as_published(self):
        source_manifest = REPO_ROOT / "data/latest/run_manifest.json"
        original_manifest = source_manifest.read_bytes()

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "site"
            report = build_site(REPO_ROOT, output, published_artifact=True)

            self.assertTrue(report["ok"])
            manifest = json.loads(
                (output / "data/latest/run_manifest.json").read_text(encoding="utf-8")
            )
            verdict = json.loads(
                (output / "data/latest/system_verdict.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["published"])
            self.assertEqual(manifest["publish_state"], "published")
            self.assertTrue(verdict["pipeline_status"]["publish_ok"])
            self.assertTrue(verdict["run"]["pipeline_status"]["publish_ok"])

        self.assertEqual(original_manifest, source_manifest.read_bytes())


if __name__ == "__main__":
    unittest.main()
