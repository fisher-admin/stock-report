import unittest
from unittest import mock
from pathlib import Path
import tempfile
import run_post_publish_research as research


class ResearchDeadlineTests(unittest.TestCase):
    def test_notice_uses_finalization_budget_and_leaves_cleanup_time(self):
        now = [0]
        budget = research.ResearchDeadline(7200, clock=lambda:now[0])
        self.assertTrue(hasattr(budget, 'notification_timeout'))
        now[0] = 6890
        self.assertEqual(budget.notification_timeout(), 8)
        now[0] = 6899
        self.assertEqual(budget.notification_timeout(), 0)

    def test_wrapper_start_is_not_reset_at_python_entry(self):
        with mock.patch.dict(research.os.environ, {'STOCK_RESEARCH_STARTED_MONOTONIC': '20'}):
            budget = research.ResearchDeadline(7200, clock=lambda: 100)
        self.assertEqual(budget.remaining(), 6520)

    def test_invalid_wrapper_start_fails_closed(self):
        for value in ('nan', 'inf', '-1', '101', 'broken'):
            with self.subTest(value=value), mock.patch.dict(research.os.environ, {'STOCK_RESEARCH_STARTED_MONOTONIC': value}):
                with self.assertRaises(ValueError):
                    research.ResearchDeadline(7200, clock=lambda: 100)

    def test_cleanup_failure_is_fatal_not_a_retryable_stage_failure(self):
        with mock.patch.object(research, 'run_managed', side_effect=RuntimeError('cleanup unconfirmed')):
            result = research.run_stage('probe', Path('/unused'), {}, timeout_s=1)
        self.assertFalse(result['ok'])
        self.assertIs(result.get('cleanup_unconfirmed'), True)

    def test_budget_reserves_cleanup_and_outer_margin(self):
        self.assertTrue(hasattr(research, 'ResearchDeadline'), 'global deadline is missing')
        budget = research.ResearchDeadline(7200, clock=lambda: 100)
        self.assertEqual(budget.remaining(), 6600)
        self.assertEqual(budget.stage_timeout(3600), 3600)
        self.assertEqual(budget.finish_deadline, 7000)

    def test_wait_and_completed_stages_consume_same_budget(self):
        self.assertTrue(hasattr(research, 'ResearchDeadline'), 'global deadline is missing')
        now = [0]
        budget = research.ResearchDeadline(7200, clock=lambda: now[0])
        now[0] = 6400
        self.assertEqual(budget.stage_timeout(3600), 200)
        now[0] = 6601
        self.assertEqual(budget.stage_timeout(600), 0)

    def test_config_and_environment_follow_scheduler_precedence(self):
        self.assertTrue(hasattr(research, 'effective_script_timeout'), 'effective timeout resolver is missing')
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.yaml'
            config.write_text('cron:\n  script_timeout_seconds: 7200\n')
            self.assertEqual(research.effective_script_timeout({}, config), 7200)
            self.assertEqual(research.effective_script_timeout({'HERMES_CRON_SCRIPT_TIMEOUT': '1800'}, config), 1800)
            self.assertEqual(research.effective_script_timeout({'HERMES_CRON_SCRIPT_TIMEOUT': 'nan'}, config), 7200)

    def test_too_small_budget_fails_closed(self):
        self.assertTrue(hasattr(research, 'ResearchDeadline'), 'global deadline is missing')
        with self.assertRaises(ValueError):
            research.ResearchDeadline(60)

    def test_malformed_config_does_not_override_valid_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.yaml'
            config.write_text('cron: [broken')
            self.assertEqual(research.effective_script_timeout({'HERMES_CRON_SCRIPT_TIMEOUT': '1800'}, config), 1800)

    def test_zero_stage_budget_does_not_launch_process(self):
        with mock.patch.object(research.subprocess, 'run') as run:
            result = research.run_stage('probe', Path('/not-used'), {}, timeout_s=0)
        self.assertFalse(result['ok'])
        self.assertEqual(result.get('error'), 'total_budget_exhausted')
        run.assert_not_called()
