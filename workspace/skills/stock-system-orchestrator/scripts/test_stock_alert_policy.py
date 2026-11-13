import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stock_alert_policy as ap  # noqa: E402

RESEARCH_FAIL_A = """⚠️ 【A股后置研究分析异常】 (退出码: 1)
[post-publish-research] FAILED shortTrackGeneration: RunnerInputError: frozen materialized input disagrees with common PIT copy: /Users/x/universe_20260930.parquet
"""
RESEARCH_FAIL_B = RESEARCH_FAIL_A.replace("20260930", "20261008")
TIMEOUT_FAIL = """⚠️ 【A股后置研究分析异常】 (退出码: 1)
[post-publish-research] FAILED shortTrackGeneration: timeout after 600s
"""


class AlertPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_then_repeat_then_recovery_then_silence(self):
        first = ap.on_failure("post_publish_research", RESEARCH_FAIL_A, rc=1, health_dir=self.h)
        self.assertIn("FAILED shortTrackGeneration", first)
        second = ap.on_failure("post_publish_research", RESEARCH_FAIL_B, rc=1, health_dir=self.h)
        self.assertIn("仍未恢复", second)
        self.assertIn("第 2 次", second)
        self.assertNotIn("shortTrackGeneration", second)
        recovered = ap.on_success("post_publish_research", health_dir=self.h)
        self.assertIn("已恢复", recovered)
        self.assertIn("2 次", recovered)
        self.assertEqual(ap.on_success("post_publish_research", health_dir=self.h), "")
        events = [json.loads(l)["event"] for l in (self.h / "alert_incidents/history.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["new_incident", "repeat", "recovered"])

    def test_changed_failure_is_a_new_full_alert(self):
        ap.on_failure("post_publish_research", RESEARCH_FAIL_A, rc=1, health_dir=self.h)
        changed = ap.on_failure("post_publish_research", TIMEOUT_FAIL, rc=1, health_dir=self.h)
        self.assertIn("timeout after 600s", changed)

    def test_failure_after_recovery_alerts_again_in_full(self):
        ap.on_failure("stock_pipeline", RESEARCH_FAIL_A, rc=1, health_dir=self.h)
        ap.on_success("stock_pipeline", health_dir=self.h)
        again = ap.on_failure("stock_pipeline", RESEARCH_FAIL_A, rc=1, health_dir=self.h)
        self.assertIn("FAILED shortTrackGeneration", again)

    def test_success_without_incident_is_silent(self):
        self.assertEqual(ap.on_success("morning_brief", health_dir=self.h), "")

    def test_jobs_are_independent(self):
        ap.on_failure("morning_brief", RESEARCH_FAIL_A, rc=1, health_dir=self.h)
        self.assertIn("FAILED", ap.on_failure("midday_analysis", RESEARCH_FAIL_A, rc=1, health_dir=self.h))


if __name__ == "__main__":
    unittest.main()
