"""Unit tests for Stage 6 Deployment & Notification Receipt Strict Validation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import stage6_deploy_and_notify as s6
from deployment_receipt import DeploymentReceipt


class Stage6DeployAndNotifyReceiptTests(unittest.TestCase):
    def test_cleanup_failure_returns_failed_receipt_without_retry(self):
        verdict = {'final_action': {'decision': 'observe_only'}}
        with mock.patch.object(s6, 'run_managed', side_effect=RuntimeError('cleanup unconfirmed')) as run, mock.patch('pathlib.Path.exists', return_value=True):
            result = s6.deploy_published_repo(verdict, run_id='test-run', trade_date='20260911')
        self.assertFalse(result['ok'])
        self.assertIs(result.get('cleanup_unconfirmed'), True)
        self.assertEqual(run.call_count, 1)

    def test_publisher_receives_existing_noninteractive_network_environment(self):
        verdict = {"final_action": {"decision": "observe_only"}}
        network_env = {"GIT_TERMINAL_PROMPT": "0", "HTTPS_PROXY": "http://127.0.0.1:7897"}
        with mock.patch.object(s6, "git_network_env", return_value=network_env), \
             mock.patch.object(s6, "run_managed", return_value=subprocess.CompletedProcess([], 1, "", "network error")) as run, \
             mock.patch("pathlib.Path.exists", return_value=True):
            result = s6.deploy_published_repo(verdict, run_id="run-new", trade_date="20260909")
        self.assertFalse(result["ok"])
        self.assertEqual(run.call_args.kwargs["env"].get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(run.call_args.kwargs["env"]["HTTPS_PROXY"], network_env["HTTPS_PROXY"])
        self.assertEqual(run.call_args.kwargs["env"]["RUN_ID"], "run-new")

    def test_publisher_timeout_keeps_partial_diagnostic_output(self):
        verdict = {"final_action": {"decision": "observe_only"}}
        timeout = subprocess.TimeoutExpired("publisher", 300, output=b"step4 fetch\xff", stderr=b"HTTP2 framing error\xfe")
        with mock.patch.object(s6, "run_managed", side_effect=timeout), \
             mock.patch("pathlib.Path.exists", return_value=True):
            result = s6.deploy_published_repo(verdict, run_id="run-new", trade_date="20260909")
        self.assertFalse(result["ok"])
        self.assertIn("step4 fetch", result.get("publisher_stdout_tail", ""))
        self.assertIn("HTTP2 framing error", result.get("publisher_stderr_tail", ""))

    def test_missing_receipt_fails_stage6(self):
        verdict = {"run_id": "run-100", "final_action": {"decision": "observe_only"}, "trade_date": "20260820"}
        mock_proc = mock.MagicMock(returncode=0, stdout="published ok", stderr="")
        with mock.patch.object(s6, "run_managed", return_value=mock_proc), \
             mock.patch.object(s6, "V2_PUBLISH_SCRIPT", Path("/fake/v2.sh")), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("deployment_receipt.DeploymentReceipt.load", return_value=None):
            res = s6.deploy_published_repo(verdict, run_id="run-100", trade_date="20260820")
            self.assertFalse(res["ok"])
            self.assertIn("deployment_receipt.json not found", res["error"])

    def test_mismatched_run_id_or_date_fails_stage6(self):
        verdict = {"run_id": "run-expected", "final_action": {"decision": "observe_only"}, "trade_date": "20260820"}
        bad_receipt = DeploymentReceipt(
            run_id="run-wrong",
            trade_date="20260820",
            local_sha="sha_abc",
            remote_sha="sha_abc",
            remote_confirmed=True,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        mock_proc = mock.MagicMock(returncode=0, stdout="published ok", stderr="")
        with mock.patch.object(s6, "run_managed", return_value=mock_proc), \
             mock.patch.object(s6, "V2_PUBLISH_SCRIPT", Path("/fake/v2.sh")), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("deployment_receipt.DeploymentReceipt.load", return_value=bad_receipt):
            res = s6.deploy_published_repo(verdict, run_id="run-expected", trade_date="20260820")
            self.assertFalse(res["ok"])
            self.assertIn("receipt mismatch", res["error"])

    def test_old_receipt_replay_with_null_verdict_run_id_fails(self):
        # verdict has run_id: None (like in production system_verdict.json)
        verdict = {"final_action": {"decision": "observe_only", "label": "只观察"}, "dates": {"decision_trade_date": "20260820"}}
        old_replay_receipt = DeploymentReceipt(
            run_id="stock-20260820T113000Z-old",
            trade_date="20260820",
            local_sha="sha_match",
            remote_sha="sha_match",
            remote_confirmed=True,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        mock_proc = mock.MagicMock(returncode=0, stdout="published ok", stderr="")
        with mock.patch.object(s6, "run_managed", return_value=mock_proc), \
             mock.patch.object(s6, "V2_PUBLISH_SCRIPT", Path("/fake/v2.sh")), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("deployment_receipt.DeploymentReceipt.load", return_value=old_replay_receipt):
            # Current pipeline run is new: "stock-20260820T193000Z-new"
            res = s6.deploy_published_repo(verdict, run_id="stock-20260820T193000Z-new", trade_date="20260820")
            self.assertFalse(res["ok"])
            self.assertIn("receipt mismatch", res["error"])

    def test_mismatched_remote_sha_fails_stage6(self):
        verdict = {"run_id": "run-expected", "final_action": {"decision": "observe_only"}, "trade_date": "20260820"}
        bad_receipt = DeploymentReceipt(
            run_id="run-expected",
            trade_date="20260820",
            local_sha="local_sha_111",
            remote_sha="remote_sha_222",
            remote_confirmed=True,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        mock_proc = mock.MagicMock(returncode=0, stdout="published ok", stderr="")
        with mock.patch.object(s6, "run_managed", return_value=mock_proc), \
             mock.patch.object(s6, "V2_PUBLISH_SCRIPT", Path("/fake/v2.sh")), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("deployment_receipt.DeploymentReceipt.load", return_value=bad_receipt):
            res = s6.deploy_published_repo(verdict, run_id="run-expected", trade_date="20260820")
            self.assertFalse(res["ok"])
            self.assertIn("receipt", res["error"])

    def test_matching_confirmed_receipt_succeeds(self):
        verdict = {"run_id": "run-expected", "final_action": {"decision": "observe_only"}, "trade_date": "20260820"}
        good_receipt = DeploymentReceipt(
            run_id="run-expected",
            trade_date="20260820",
            local_sha="sha_match",
            remote_sha="sha_match",
            remote_confirmed=True,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        mock_proc = mock.MagicMock(returncode=0, stdout="published ok", stderr="")
        with mock.patch.object(s6, "run_managed", return_value=mock_proc), \
             mock.patch.object(s6, "V2_PUBLISH_SCRIPT", Path("/fake/v2.sh")), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("deployment_receipt.DeploymentReceipt.load", return_value=good_receipt), \
             mock.patch("stage6_deploy_and_notify.git_head_short", return_value="sha_match"):
            res = s6.deploy_published_repo(verdict, run_id="run-expected", trade_date="20260820")
            self.assertTrue(res["ok"])
            self.assertEqual(res["deployment_receipt"]["run_id"], "run-expected")


if __name__ == "__main__":
    unittest.main()
