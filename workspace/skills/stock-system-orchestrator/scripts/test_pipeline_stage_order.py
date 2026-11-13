import subprocess
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import run_pipeline
from run_pipeline import build_production_stages
import stage3_strategy_suite as stage3


class PipelineStageOrderTests(unittest.TestCase):
    def test_short_track_shadow_is_excluded_from_production(self):
        self.assertNotIn("short_track_shadow", stage3.PRODUCTION_STEP_NAMES)
        self.assertNotIn("short_track_shadow", stage3.CRITICAL_STEPS)

    def test_stage3_runs_only_production_steps(self):
        def completed(name, *_args):
            return {
                "name": name,
                "returncode": 0,
                "ok": True,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            stage3, "OUT_JSON", Path(tmpdir) / "missing.json"
        ), mock.patch.object(stage3, "run_step", side_effect=completed) as run_step, mock.patch.object(
            stage3, "write_health_snapshot"
        ):
            code = stage3.main()

        self.assertEqual(code, 0)
        self.assertEqual(
            [call.args[0] for call in run_step.call_args_list],
            ["prebreakout_suite", "strategy_publication_layer"],
        )

    def test_prebreakout_timeout_has_operational_headroom(self):
        self.assertEqual(stage3.STEP_TIMEOUT_SECONDS["prebreakout_suite"], 1800)

    def test_strategy_child_environment_is_unbuffered(self):
        original = {"EXISTING": "1"}
        child = stage3.build_step_env(original)
        self.assertEqual(child["PYTHONUNBUFFERED"], "1")
        self.assertNotIn("PYTHONUNBUFFERED", original)

    def test_main_pipeline_excludes_heavy_research_before_deploy(self):
        stages = build_production_stages(False)
        names = [name for name, _ in stages]
        self.assertNotIn("prebreakoutDailyPan", names)
        self.assertNotIn("prebreakoutShadowResearch", names)
        self.assertNotIn("factorAttribution", names)
        self.assertNotIn("shortTrackEvaluation", names)
        self.assertEqual(names[-1], "latestStateSync")

    def test_production_stages_order(self):
        stages = build_production_stages(False)
        names = [name for name, _ in stages]
        expected = [
            "dataPreparation",
            "marketSnapshot",
            "strategySuite",
            "aiEnrichmentAndPublish",
            "aiBackfill",
            "recommendationWarehouse",
            "validation",
            "latestStateSync",
        ]
        self.assertEqual(names, expected)

    def test_strategy_steps_record_elapsed_time_for_timeout_diagnosis(self):
        popen = mock.Mock(pid=123, returncode=0)
        popen.communicate.return_value = ("ok", "")
        with mock.patch.object(
            stage3.subprocess, "Popen", return_value=popen
        ), mock.patch.object(
            stage3.time, "monotonic", side_effect=[10.0, 12.5]
        ):
            result = stage3.run_step("probe", "python", Path("/tmp/fake.py"), {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["elapsed_seconds"], 2.5)

    def test_attached_step_timeout_is_structured_instead_of_hanging_stage3(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["fake"], timeout=7, output=b"partial\xff", stderr=b"error\xfe"
        )
        popen = mock.Mock(pid=123, returncode=-signal.SIGTERM)
        popen.communicate.side_effect = [timeout, ("partial", "error")]
        with mock.patch.object(
            stage3.subprocess, "Popen", return_value=popen
        ), mock.patch.object(stage3.os, "killpg") as killpg:
            result = stage3.run_step("short_track_shadow", "python", Path("/tmp/fake.py"), {})
        self.assertFalse(result["ok"])
        self.assertTrue(result["timeout"])
        self.assertIn("partial", result["stdout_tail"])
        killpg.assert_called_once_with(123, signal.SIGTERM)

    def test_inner_step_timeouts_fit_inside_outer_strategy_timeout(self):
        self.assertLess(
            sum(stage3.STEP_TIMEOUT_SECONDS.values()),
            run_pipeline.STAGE_TIMEOUT_SECONDS["strategySuite"],
        )


if __name__ == "__main__":
    unittest.main()
