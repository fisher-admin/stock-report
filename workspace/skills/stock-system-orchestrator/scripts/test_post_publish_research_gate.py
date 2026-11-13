import unittest
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import run_post_publish_research as research


SCRIPT = Path(__file__).with_name("run_post_publish_research.py")


class PostPublishResearchGateTests(unittest.TestCase):
    def test_manual_recovery_preserves_original_failed_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp)
            stage = health / 'probe.py'
            stage.write_text('# fixture')
            old = {'run_id': 'run-28', 'state': 'failed', 'all_ok': False, 'trade_date': '20260828'}
            (health / 'post_publish_research_run.json').write_text(json.dumps(old))
            release = {'run_id': 'run-28', 'trade_date': '20260828', 'orchestrator': {}}
            with mock.patch.object(research, 'HEALTH_DIR', health), mock.patch.object(research, 'POST_PUBLISH_RESEARCH_STAGES', [('probe', stage)]), mock.patch.object(research, 'wait_for_release', return_value=(release, [])), mock.patch.object(research, 'resolve_expected_trade_date', return_value='20260828'), mock.patch.object(research, 'run_stage', return_value={'stage': 'probe', 'ok': True}), mock.patch.dict(os.environ, {'STOCK_RESEARCH_RECOVERY': '1'}), mock.patch('sys.argv', ['research']):
                self.assertEqual(research.main(), 0)
            saved = json.loads((health / 'post_publish_research_run.json').read_text())
            self.assertEqual(saved.get('execution_mode'), 'manual_recovery')
            self.assertEqual(saved.get('source_run_id'), 'run-28')
            self.assertNotEqual(saved.get('research_run_id'), 'run-28')
            archived = list((health / 'research_run_archive').glob('*.json'))
            self.assertEqual(len(archived), 1)
            self.assertEqual(json.loads(archived[0].read_text()), old)

    def test_terminal_failure_is_saved_before_bounded_notification_retries(self):
        self.assertTrue(hasattr(research, 'retry_failures'), 'research does not invoke bounded notification retries')
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp)
            stage = health / 'stage.py'
            stage.write_text('# fixture')
            release = {'run_id':'run-28', 'trade_date':'20260828', 'orchestrator':{}}
            def retry(events, **kwargs):
                saved = json.loads((health / 'post_publish_research_run.json').read_text())
                self.assertEqual(saved['state'], 'failed')
                self.assertIs(saved['all_ok'], False)
                self.assertIn('deadline', kwargs)
                return events
            with mock.patch.object(research, 'HEALTH_DIR', health), mock.patch.object(research, 'POST_PUBLISH_RESEARCH_STAGES', [('probe',stage)]), mock.patch.object(research, 'wait_for_release', return_value=(release, [])), mock.patch.object(research, 'resolve_expected_trade_date', return_value='20260828'), mock.patch.object(research, 'run_stage', return_value={'stage':'probe','ok':False}), mock.patch.object(research, 'retry_failures', side_effect=retry) as retries, mock.patch('sys.argv',['research']):
                self.assertEqual(research.main(), 1)
            retries.assert_called_once()

    def test_cleanup_uncertainty_stops_remaining_stages_and_saves_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp)
            stage = health / 'stage.py'
            stage.write_text('# test')
            release = {'run_id': 'run-28', 'trade_date': '20260828', 'orchestrator': {}}
            failure = {'stage': 'first', 'ok': False, 'cleanup_unconfirmed': True}
            with mock.patch.object(research, 'HEALTH_DIR', health), mock.patch.object(research, 'POST_PUBLISH_RESEARCH_STAGES', [('first', stage), ('second', stage)]), mock.patch.object(research, 'wait_for_release', return_value=(release, [])), mock.patch.object(research, 'resolve_expected_trade_date', return_value='20260828'), mock.patch.object(research, 'run_stage', return_value=failure) as run, mock.patch('sys.argv', ['research']):
                code = research.main()
            saved = json.loads((health / 'post_publish_research_run.json').read_text())
            self.assertEqual(code, 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(saved['state'], 'failed')
            self.assertIs(saved['all_ok'], False)
            self.assertEqual(saved['stages'][-1].get('error'), 'prior_cleanup_unconfirmed')

    def test_factor_stage_is_an_executable_report_not_the_helper_library(self):
        script = dict(research.POST_PUBLISH_RESEARCH_STAGES)["factorAttribution"]
        result = subprocess.run([str(research.PREFERRED_PYTHON), str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--as-of", result.stdout)

    def test_factor_stage_uses_matching_date_and_incremental_refresh(self):
        self.assertTrue(hasattr(research, "stage_arguments"))
        self.assertEqual(research.stage_arguments("factorAttribution", "20260907"),
                         ["--as-of", "20260907", "--refresh-days", "1"])
        self.assertEqual(research.stage_arguments("shortTrackGeneration", "20260907"),
                         ["--trade-date", "20260907"])

    def test_short_track_runner_is_the_single_combined_generation_and_evaluation_stage(self):
        names = [name for name, _script in research.POST_PUBLISH_RESEARCH_STAGES]
        self.assertEqual(names[0], "shortTrackGeneration")
        self.assertNotIn("shortTrackEvaluation", names)

    def test_research_job_has_release_receipt_and_lock_gate(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("deployment_receipt", text)
        self.assertIn("stock_pipeline_gate.lockdir", text)
        self.assertIn("wait_for_release", text)
        self.assertIn("OPENCLAW_RUN_ID", text)

    def test_timeout_output_bytes_are_recorded_without_secondary_crash(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["fake"], timeout=1, output=b"partial\xff", stderr=b"error\xfe"
        )
        with mock.patch.object(research, "run_managed", side_effect=timeout):
            result = research.run_stage("probe", Path("/tmp/fake.py"), {}, [], timeout_s=7)
        self.assertTrue(result["timeout"])
        self.assertEqual(result["timeout_seconds"], 7)
        self.assertIn("partial", result["stdout_tail"])
        self.assertIn("error", result["stderr_tail"])

    def test_factory_timeout_turns_running_marker_into_terminal_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            health = Path(tmpdir) / "health"
            health.mkdir()
            status = health / "prebreakout_factory_status.json"
            status.write_text(
                json.dumps({"state": "running", "run_id": "run-1", "trade_date": "20260828"}),
                encoding="utf-8",
            )
            with mock.patch.object(research, "HEALTH_DIR", health):
                research.mark_factory_timeout(
                    "20260828",
                    "run-1",
                    {
                        "script": "/tmp/prebreakout_factory.py",
                        "timeout": True,
                        "timeout_seconds": 3600,
                    },
                )
            payload = json.loads(status.read_text(encoding="utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "timeout")
        self.assertIn("3600", payload["error"])

    def test_blocked_release_archives_previous_summary_before_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp)
            old = {'run_id': 'run-22', 'state': 'failed', 'all_ok': False, 'trade_date': '20261022'}
            (health / 'post_publish_research_run.json').write_text(json.dumps(old))
            with mock.patch.object(research, 'HEALTH_DIR', health), mock.patch.object(research, 'POST_PUBLISH_RESEARCH_STAGES', []), mock.patch.object(research, 'wait_for_release', return_value=(None, ['本轮主管线尚未在今天完成'])), mock.patch.object(research, 'resolve_expected_trade_date', return_value='20261022'), mock.patch.dict(os.environ, {'OPENCLAW_PRODUCTION_RELEASE_TIMEOUT_SECONDS': '0'}), mock.patch('sys.argv', ['research']):
                self.assertEqual(research.main(), 1)
            archived = list((health / 'research_run_archive').glob('*.json'))
            self.assertEqual([json.loads(path.read_text()) for path in archived], [old])
            self.assertEqual(json.loads((health / 'post_publish_research_run.json').read_text())['state'], 'blocked')

    def test_research_main_writes_failed_summary_when_release_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            health = Path(tmpdir) / "health"
            published = Path(tmpdir) / "published"
            with mock.patch.object(research, "HEALTH_DIR", health), mock.patch.object(
                research, "PUBLISHED_REPO", published
            ), mock.patch.object(research, "POST_PUBLISH_RESEARCH_STAGES", []), mock.patch.object(
                research, "wait_for_release", return_value=(None, ["未检测到匹配的远程发布回执"])
            ), mock.patch.object(
                research, "resolve_expected_trade_date", return_value="20260828"
            ), mock.patch.dict(
                os.environ, {"OPENCLAW_PRODUCTION_RELEASE_TIMEOUT_SECONDS": "0"}
            ):
                with mock.patch("sys.argv", ["run_post_publish_research.py"]):
                    code = research.main()
            payload = json.loads((health / "post_publish_research_run.json").read_text(encoding="utf-8"))
            self.assertTrue(list((health / 'research_alerts').glob('*.json')), 'blocked release must have a local failure event')
        self.assertEqual(code, 1)
        self.assertFalse(payload["all_ok"])
        self.assertEqual(payload["release_gate_errors"], ["未检测到匹配的远程发布回执"])

    def test_research_main_passes_release_identity_to_each_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            health = Path(tmpdir) / "health"
            published = Path(tmpdir) / "published"
            stage = Path(tmpdir) / "stage.py"
            stage.write_text("# fake\n", encoding="utf-8")
            release = {
                "run_id": "run-28",
                "trade_date": "20260828",
                "orchestrator": {"started_at": "2026-08-28T11:30:00Z"},
            }
            with mock.patch.object(research, "HEALTH_DIR", health), mock.patch.object(
                research, "PUBLISHED_REPO", published
            ), mock.patch.object(
                research, "POST_PUBLISH_RESEARCH_STAGES", [("probe", stage)]
            ), mock.patch.object(
                research, "wait_for_release", return_value=(release, [])
            ), mock.patch.object(
                research, "resolve_expected_trade_date", return_value="20260828"
            ), mock.patch.object(
                research, "run_stage", return_value={"stage": "probe", "ok": True}
            ) as run_stage, mock.patch.object(research, "write_json", wraps=research.write_json) as write_json, mock.patch(
                "sys.argv", ["run_post_publish_research.py"]
            ):
                code = research.main()
        self.assertEqual(code, 0)
        args = run_stage.call_args.args
        self.assertEqual(args[0:3], ("probe", stage, mock.ANY))
        self.assertEqual(args[3], ["--trade-date", "20260828"])
        self.assertEqual(args[4], 1800)
        stage_env = args[2]
        self.assertEqual(stage_env["OPENCLAW_RUN_ID"], "run-28")
        self.assertEqual(stage_env["OPENCLAW_TARGET_TRADE_DATE"], "20260828")
        self.assertEqual(write_json.call_args_list[0].args[1]["state"], "running")
        self.assertEqual(write_json.call_args_list[-1].args[1]["state"], "completed")

    def test_later_research_stage_runs_after_short_track_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            health = Path(tmpdir) / "health"
            published = Path(tmpdir) / "published"
            stage = Path(tmpdir) / "stage.py"
            stage.write_text("# fake\n", encoding="utf-8")
            release = {
                "run_id": "run-28",
                "trade_date": "20260828",
                "orchestrator": {"started_at": "2026-08-28T11:30:00Z"},
            }
            def execute_stage(name, *_args):
                progress = json.loads((health / "post_publish_research_run.json").read_text())
                self.assertEqual(progress.get("current_stage"), name)
                self.assertEqual(progress["state"], "running")
                if name == "factorAttribution":
                    self.assertTrue(list((health / 'research_alerts').glob('*.json')), 'failure must be recorded before next stage')
                    self.assertEqual(progress["stages"], [{"stage": "shortTrackGeneration", "ok": False}])
                    self.assertIs(progress["all_ok"], False)
                return {"stage": name, "ok": name == "factorAttribution"}
            with mock.patch.object(research, "HEALTH_DIR", health), mock.patch.object(
                research, "PUBLISHED_REPO", published
            ), mock.patch.object(
                research,
                "POST_PUBLISH_RESEARCH_STAGES",
                [("shortTrackGeneration", stage), ("factorAttribution", stage)],
            ), mock.patch.object(
                research, "wait_for_release", return_value=(release, [])
            ), mock.patch.object(
                research, "resolve_expected_trade_date", return_value="20260828"
            ), mock.patch.object(
                research,
                "run_stage",
                side_effect=execute_stage,
            ) as run_stage, mock.patch(
                "sys.argv", ["run_post_publish_research.py"]
            ):
                code = research.main()
            payload = json.loads((health / "post_publish_research_run.json").read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertEqual(run_stage.call_count, 2)
        self.assertEqual(
            [stage["stage"] for stage in payload["stages"]],
            ["shortTrackGeneration", "factorAttribution"],
        )


if __name__ == "__main__":
    unittest.main()
