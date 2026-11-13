#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
import re
from unittest import mock
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_system_verdict as verdict  # noqa: E402


class GenerateSystemVerdictTests(unittest.TestCase):
    def test_public_readiness_lineage_preserves_run_identity(self):
        readiness = {"ok": True, "published": False, "ai_complete": True, "publish_mode": "full",
                     "run": {"run_id": "run-1", "trade_date": "20260904"}}
        with mock.patch.object(verdict, "load_json", side_effect=lambda path, *args: readiness if path == verdict.AI_PUBLISH_JSON else {}):
            result = verdict.build_system_verdict()
        lineage = result["source_lineage"]["ai_publish_readiness"]
        self.assertEqual(lineage.get("run_id"), "run-1")
        self.assertEqual(lineage.get("trade_date"), "20260904")
        self.assertEqual(lineage.get("publish_mode"), "full")
        self.assertIs(lineage.get("ai_complete"), True)

    def test_load_json_reads_existing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / 'run.json'
            sample.write_text('{"run_id":"stock-20260911T113014Z"}', encoding='utf-8')
            run_id = verdict.load_json(sample).get("run_id")
        self.assertIsInstance(run_id, str)
        self.assertRegex(run_id, re.compile(r"^stock-\d{8}T\d{6}Z$"))

    def test_matching_deployment_receipt_recovers_publish_status(self) -> None:
        publish_ok, recovered = verdict.resolve_publish_status(
            {
                "run_id": "stock-20260824T113017Z",
                "selection_ok": True,
                "publish_ok": False,
            },
            "20260824",
            {
                "run_id": "stock-20260824T113017Z",
                "trade_date": "20260824",
                "remote_confirmed": True,
                "local_sha": "abc",
                "remote_sha": "abc",
            },
        )
        self.assertTrue(publish_ok)
        self.assertTrue(recovered)

    def test_mismatched_receipt_cannot_recover_publish_status(self) -> None:
        publish_ok, recovered = verdict.resolve_publish_status(
            {
                "run_id": "stock-20260824T113017Z",
                "selection_ok": True,
                "publish_ok": False,
            },
            "20260824",
            {
                "run_id": "stock-other",
                "trade_date": "20260824",
                "remote_confirmed": True,
                "local_sha": "abc",
                "remote_sha": "abc",
            },
        )
        self.assertFalse(publish_ok)
        self.assertFalse(recovered)

    def test_selection_failure_cannot_be_healed_by_receipt(self) -> None:
        publish_ok, recovered = verdict.resolve_publish_status(
            {
                "run_id": "stock-20260824T113017Z",
                "selection_ok": False,
                "publish_ok": False,
            },
            "20260824",
            {
                "run_id": "stock-20260824T113017Z",
                "trade_date": "20260824",
                "remote_confirmed": True,
                "local_sha": "abc",
                "remote_sha": "abc",
            },
        )
        self.assertFalse(publish_ok)
        self.assertFalse(recovered)

    def test_run_metadata_prefers_current_validation_over_stale_orchestrator(self) -> None:
        metadata = verdict.resolve_run_metadata(
            {
                "run": {
                    "run_id": "stock-20260903T113016Z",
                    "started_at": "2026-09-03T11:30:16Z",
                }
            },
            {
                "run_id": "stock-20260903T014455Z",
                "started_at": "2026-09-03T01:44:55Z",
            },
        )
        self.assertEqual(metadata["run_id"], "stock-20260903T113016Z")
        self.assertEqual(metadata["started_at"], "2026-09-03T11:30:16Z")


if __name__ == "__main__":
    unittest.main()
