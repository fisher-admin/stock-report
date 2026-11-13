#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import run_ai_summary_and_publish as report


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class AiSummaryPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.health = self.root / "health"
        self.analysis = self.root / "ai_analysis"
        self.latest = self.root / "latest"
        self.lock_dir = self.health / "stock_pipeline_gate.lockdir"
        self.receipt_file = self.health / "deployment_receipt.json"
        self.delivery_file = self.health / "delivery_receipt.json"
        os.environ["OPENCLAW_RECEIPT_TIMEOUT_SECONDS"] = "0.1"
        os.environ["OPENCLAW_RECEIPT_INTERVAL_SECONDS"] = "0.01"
        os.environ["OPENCLAW_TARGET_TRADE_DATE"] = "20260818"
        self.patches = [
            mock.patch.object(report, "WORKSPACE", self.root),
            mock.patch.object(report, "HEALTH_DIR", self.health),
            mock.patch.object(report, "AI_ANALYSIS_DIR", self.analysis),
            mock.patch.object(report, "RUN_MANIFEST", self.latest / "run_manifest.json"),
            mock.patch.object(report, "SYSTEM_VERDICT", self.latest / "system_verdict.json"),
            mock.patch.object(report, "VALIDATION_REPORT", self.health / "validation_report.json", create=True),
            mock.patch.object(report, "AI_READINESS", self.health / "ai_publish_readiness.json", create=True),
            mock.patch.object(report, "ORCHESTRATOR_RUN", self.health / "orchestrator_run.json", create=True),
            mock.patch.object(report, "PIPELINE_LOCK_DIR", self.lock_dir, create=True),
            mock.patch.object(report, "RECEIPT_FILE", self.receipt_file, create=True),
            mock.patch.object(report, "DELIVERY_RECEIPT", self.delivery_file, create=True),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def write_state(
        self,
        *,
        manifest_run: str = "run-1",
        readiness_run: str = "run-1",
        validation_run: str = "run-1",
        orchestrator_run: str = "run-1",
        rows: int = 20,
        ready: int = 20,
        total: int = 20,
        write_receipt: bool = True,
        verdict_action: object = "observe_only",
        write_publication: bool = True,
    ) -> None:
        trade_date = "20260818"
        _write(self.latest / "run_manifest.json", {"trade_date": trade_date, "run_id": manifest_run})
        _write(
            self.health / "ai_publish_readiness.json",
            {
                "run": {"run_id": readiness_run, "trade_date": trade_date},
                "base_ok": True,
                "published": True,
                "ai_complete": ready == total,
                "publish_mode": "full" if ready == total else "enhanced_degraded",
                "checks": {
                    "strategy_latest_trade_date": trade_date,
                    "ai_status_counts": {
                        "ready": ready,
                        "partial": 0,
                        "pending": total - ready,
                        "failed": 0,
                        "total": total,
                    },
                },
            },
        )
        _write(
            self.health / "validation_report.json",
            {"run": {"run_id": validation_run, "trade_date": trade_date}, "ok": True},
        )
        _write(
            self.health / "orchestrator_run.json",
            {
                "run_id": orchestrator_run,
                "trade_date": trade_date,
                "completed_at": datetime.now().astimezone().isoformat(),
                "selection_ok": True,
                "publish_ok": True,
            },
        )
        # 2026-10-03: the broadcaster also requires the published verdict and a
        # passing final-publication record for the same batch and commit.
        _write(
            self.latest / "system_verdict.json",
            {
                "schema_version": "system_verdict.v2",
                "run": {"run_id": manifest_run},
                "dates": {"decision_trade_date": trade_date},
                "final_action": {"action": verdict_action, "label": "只观察"},
            },
        )
        if write_publication:
            _write(
                self.health / "publication_validation.json",
                {
                    "passed": True,
                    "mode": "new_batch",
                    "batch": {"run_id": manifest_run, "trade_date": trade_date},
                    "pushed_sha": "abc1234",
                },
            )
        if write_receipt:
            _write(
                self.receipt_file,
                {
                    "run_id": manifest_run,
                    "trade_date": trade_date,
                    "local_sha": "abc1234",
                    "remote_sha": "abc1234",
                    "remote_confirmed": True,
                    "confirmed_at": datetime.now().astimezone().isoformat(),
                },
            )
        _write(
            self.analysis / f"{trade_date}.json",
            [
                {
                    "code": f"{idx:06d}",
                    "name": f"样本{idx}",
                    "ai_status": "ready",
                    "success": True,
                    "ai_score": 70,
                    "ai_advice": "观察",
                    "model_name": "gemini-3.7-flash",
                }
                for idx in range(1, rows + 1)
            ],
        )

    def invoke(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = report.main()
        return code, output.getvalue()

    def test_broadcaster_never_invokes_publish_script(self) -> None:
        self.write_state()
        code, output = self.invoke()
        self.assertEqual(code, 0)
        text = Path(report.__file__).read_text(encoding="utf-8")
        self.assertNotIn("publish_stock_report_v2.sh", text)

    def test_refuses_mismatched_manifest_and_readiness_run(self) -> None:
        self.write_state(readiness_run="different")
        code, output = self.invoke()
        self.assertEqual(code, 10)
        self.assertIn("状态不一致", output)

    def test_reports_missing_seats_as_failed(self) -> None:
        self.write_state(rows=2, ready=2, total=20)
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("生产席位: 2/20", output)
        self.assertIn("失败/未就绪: 18", output)

    def test_reports_models_from_model_name_instead_of_hardcoded_names(self) -> None:
        self.write_state()
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("gemini-3.7-flash×20", output)
        self.assertNotIn("mimo-v2.5 / grok-4.5", output)

    def test_does_not_publish_while_pipeline_lock_is_active(self) -> None:
        self.write_state()
        self.lock_dir.mkdir(parents=True)
        (self.lock_dir / "owner.txt").write_text(f"{os.getpid()} active", encoding="utf-8")
        code, output = self.invoke()
        self.assertEqual(code, 10)
        self.assertIn("仍在运行", output)

    def test_missing_receipt_returns_code_10_and_warns(self) -> None:
        self.write_state(write_receipt=False)
        code, output = self.invoke()
        self.assertEqual(code, 10)
        self.assertIn("未检测到", output)

    def test_broadcaster_rejects_a_completed_release_for_the_wrong_trade_date(self) -> None:
        self.write_state()
        with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20260819"}):
            code, output = self.invoke()
        self.assertEqual(code, 10)
        self.assertIn("交易日", output)

    def test_broadcast_idempotent_deduplication(self) -> None:
        self.write_state(manifest_run="run-dedupe", readiness_run="run-dedupe", validation_run="run-dedupe", orchestrator_run="run-dedupe")
        code1, output1 = self.invoke()
        self.assertEqual(code1, 0)
        self.assertIn("A股选股 AI深度分析报告", output1)

        # Second invocation should be silent
        code2, output2 = self.invoke()
        self.assertEqual(code2, 0)
        self.assertEqual(output2, "")

    # ---- 2026-10-03 reliability revision ----
    def test_halt_verdict_is_not_broadcast(self) -> None:
        self.write_state(verdict_action="halt")
        code, output = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("halt", output)
        self.assertNotIn("A股选股 AI深度分析报告", output)
        self.assertFalse((self.health / "broadcast_ledger.json").exists())

    def test_unstructured_final_action_is_not_broadcast(self) -> None:
        self.write_state()
        _write(self.latest / "system_verdict.json", {"run": {"run_id": "run-1"},
               "dates": {"decision_trade_date": "20260818"}, "final_action": "observe_only"})
        code, output = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("final_action", output)

    def test_missing_final_publication_record_blocks_broadcast(self) -> None:
        self.write_state(write_publication=False)
        code, output = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("最终发布检查", output)

    def test_publication_record_for_other_commit_blocks_broadcast(self) -> None:
        self.write_state()
        record = json.loads((self.health / "publication_validation.json").read_text(encoding="utf-8"))
        record["pushed_sha"] = "other"
        _write(self.health / "publication_validation.json", record)
        code, output = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("回执不一致", output)

    def test_same_trade_date_same_list_new_run_is_not_rebroadcast(self) -> None:
        self.write_state(manifest_run="run-a", readiness_run="run-a", validation_run="run-a", orchestrator_run="run-a")
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("A股选股 AI深度分析报告", output)
        self.write_state(manifest_run="run-b", readiness_run="run-b", validation_run="run-b", orchestrator_run="run-b")
        code, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        receipt = json.loads(self.delivery_file.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "skipped_duplicate_batch")
        self.assertEqual(receipt["run_id"], "run-b")

    def test_delivery_record_is_honest_about_handoff(self) -> None:
        self.write_state()
        code, _ = self.invoke()
        self.assertEqual(code, 0)
        receipt = json.loads(self.delivery_file.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "handed_to_hermes_stdout")
        self.assertEqual(receipt["transport_confirmation"], "pending_hermes_delivery_result")
        self.assertNotIn("delivered_at", receipt)

    def test_delivery_receipt_write_replaces_symlink_without_touching_target(self) -> None:
        target = self.root / "protected.json"
        target.write_text('{"safe": true}', encoding="utf-8")
        self.health.mkdir(parents=True, exist_ok=True)
        self.delivery_file.symlink_to(target)
        report.record_delivery("run-safe", "20260828")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"safe": True})
        self.assertFalse(self.delivery_file.is_symlink())
        self.assertEqual(json.loads(self.delivery_file.read_text(encoding="utf-8"))["run_id"], "run-safe")

    def test_broadcaster_waits_for_new_completed_run_before_validating(self) -> None:
        """A 20:00 invocation must tolerate the 19:30 run still being in flight."""
        old_trade_date = "20260827"
        new_trade_date = "20260828"
        old = {"trade_date": old_trade_date, "run_id": "old-run"}
        new = {"trade_date": new_trade_date, "run_id": "new-run"}
        old_readiness = {
            "run": {"run_id": "old-run", "trade_date": old_trade_date},
            "base_ok": True,
            "checks": {"strategy_latest_trade_date": old_trade_date, "ai_status_counts": {"ready": 20, "total": 20}},
        }
        new_readiness = {
            "run": {"run_id": "new-run", "trade_date": new_trade_date},
            "base_ok": True,
            "checks": {"strategy_latest_trade_date": new_trade_date, "ai_status_counts": {"ready": 20, "total": 20}},
        }
        old_validation = {"run": {"run_id": "old-run", "trade_date": old_trade_date}, "ok": True}
        new_validation = {"run": {"run_id": "new-run", "trade_date": new_trade_date}, "ok": True}
        old_orchestrator = {"run_id": "old-run", "trade_date": old_trade_date, "selection_ok": True, "publish_ok": False}
        new_orchestrator = {
            "run_id": "new-run",
            "trade_date": new_trade_date,
            "selection_ok": True,
            "publish_ok": True,
            "completed_at": datetime.now().astimezone().isoformat(),
        }
        old_receipt = {
            "run_id": "old-run",
            "trade_date": old_trade_date,
            "remote_confirmed": True,
            "local_sha": "old-sha",
            "remote_sha": "old-sha",
        }
        new_receipt = {
            "run_id": "new-run",
            "trade_date": new_trade_date,
            "remote_confirmed": True,
            "local_sha": "abc1234",
            "remote_sha": "abc1234",
        }
        snapshots = {
            "old": {
                report.RUN_MANIFEST: old,
                report.AI_READINESS: old_readiness,
                report.VALIDATION_REPORT: old_validation,
                report.ORCHESTRATOR_RUN: old_orchestrator,
                report.RECEIPT_FILE: old_receipt,
            },
            "new": {
                report.RUN_MANIFEST: new,
                report.AI_READINESS: new_readiness,
                report.VALIDATION_REPORT: new_validation,
                report.ORCHESTRATOR_RUN: new_orchestrator,
                report.RECEIPT_FILE: new_receipt,
                report.SYSTEM_VERDICT: {
                    "run": {"run_id": "new-run"},
                    "dates": {"decision_trade_date": new_trade_date},
                    "final_action": {"action": "observe_only"},
                },
                report.publication_validation_path(): {
                    "passed": True,
                    "batch": {"run_id": "new-run", "trade_date": new_trade_date},
                    "pushed_sha": "abc1234",
                },
            },
        }
        calls = {"count": 0}
        real_load_json = report.load_json

        def load_snapshot(path):
            phase = "old" if calls["count"] < 4 else "new"
            calls["count"] += 1
            return snapshots[phase].get(path, real_load_json(path))

        with mock.patch.object(report, "load_json", side_effect=load_snapshot), mock.patch.object(
            report, "is_already_delivered", return_value=False
        ), mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": new_trade_date}):
            self.analysis.mkdir(parents=True, exist_ok=True)
            (self.analysis / f"{new_trade_date}.json").write_text(
                json.dumps(
                    [
                        {"code": f"{idx:06d}", "name": "样本", "ai_advice": "观察", "ai_score": 70, "model_name": "test"}
                        for idx in range(1, 21)
                    ]
                ),
                encoding="utf-8",
            )
            code, output = self.invoke()

        self.assertEqual(code, 0, output)
        self.assertIn(new_trade_date, output)
        self.assertGreater(calls["count"], 4)


if __name__ == "__main__":
    unittest.main()
