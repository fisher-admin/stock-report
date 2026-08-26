from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_pages_artifact import build_site


REPO_ROOT = Path(__file__).resolve().parents[1]


class PagesArtifactTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
