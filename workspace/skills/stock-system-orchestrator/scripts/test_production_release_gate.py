import unittest
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import production_release_gate as gate
from production_release_gate import inspect_release, wait_for_release


def paths() -> dict[str, Path]:
    result = {name: Path(f"/tmp/{name}.json") for name in ("manifest", "readiness", "validation", "orchestrator", "receipt")}
    result["lock_dir"] = Path("/tmp/stock_pipeline_gate.lockdir")
    return result


def state(run_id: str = "run-1", trade_date: str = "20260828") -> dict[Path, dict]:
    p = paths()
    return {
        p["manifest"]: {"run_id": run_id, "trade_date": trade_date},
        p["readiness"]: {
            "run": {"run_id": run_id, "trade_date": trade_date},
            "base_ok": True,
            "checks": {"strategy_latest_trade_date": trade_date},
        },
        p["validation"]: {"run": {"run_id": run_id, "trade_date": trade_date}, "ok": True},
        p["orchestrator"]: {
            "run_id": run_id,
            "trade_date": trade_date,
            "selection_ok": True,
            "publish_ok": True,
            "completed_at": datetime.now().astimezone().isoformat(),
        },
        p["receipt"]: {
            "run_id": run_id,
            "trade_date": trade_date,
            "remote_confirmed": True,
            "local_sha": "abc",
            "remote_sha": "abc",
        },
    }


class ProductionReleaseGateTests(unittest.TestCase):
    def test_matching_release_is_ready(self):
        p = paths()
        docs = state()
        bundle, errors = inspect_release(
            p,
            load_fn=lambda path: docs.get(path, {}),
            lock_fn=lambda _path: False,
        )
        self.assertEqual(errors, [])
        self.assertEqual(bundle["run_id"], "run-1")

    def test_active_pipeline_blocks_release(self):
        p = paths()
        docs = state()
        bundle, errors = inspect_release(
            p,
            load_fn=lambda path: docs.get(path, {}),
            lock_fn=lambda _path: True,
        )
        self.assertIsNone(bundle)
        self.assertIn("仍在运行", errors[0])

    def test_reused_pid_with_different_start_time_is_not_an_active_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_dir = Path(tmpdir) / "stock_pipeline_gate.lockdir"
            lock_dir.mkdir()
            (lock_dir / "owner.txt").write_text(
                "123|Thu Aug 27 19:30:00 2026|token\n", encoding="utf-8"
            )
            proc = subprocess.CompletedProcess(
                ["ps"], 0, stdout="Fri Aug 28 09:00:00 2026\n", stderr=""
            )
            with mock.patch.object(gate.os, "kill", return_value=None), mock.patch.object(
                gate.subprocess, "run", return_value=proc
            ):
                active = gate.pipeline_lock_active(lock_dir)
        self.assertFalse(active)

    def test_malformed_run_id_is_rejected(self):
        p = paths()
        docs = state("../escape", "20260828")
        bundle, errors = inspect_release(
            p,
            load_fn=lambda path: docs.get(path, {}),
            lock_fn=lambda _path: False,
            expected_trade_date="20260828",
        )
        self.assertIsNone(bundle)
        self.assertTrue(any("run_id" in item for item in errors))

    def test_expected_trade_date_comes_from_explicit_env_then_calendar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calendar = Path(tmpdir) / "trading_calendar.json"
            calendar.write_text(
                json.dumps(
                    {
                        "from": "20260801",
                        "to": "20260831",
                        "open_dates": ["20260827", "20260828"],
                    }
                ),
                encoding="utf-8",
            )
            from_calendar = gate.resolve_expected_trade_date(
                calendar,
                env={},
                now_local=datetime.fromisoformat("2026-08-28T20:00:00+08:00"),
            )
            from_env = gate.resolve_expected_trade_date(
                calendar,
                env={"OPENCLAW_TARGET_TRADE_DATE": "20260827"},
                now_local=datetime.fromisoformat("2026-08-28T20:00:00+08:00"),
            )
        self.assertEqual(from_calendar, "20260828")
        self.assertEqual(from_env, "20260827")

    def test_atomic_json_write_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "target.json"
            link = root / "delivery.json"
            target.write_text('{"safe": true}', encoding="utf-8")
            link.symlink_to(target)
            gate.write_json_atomic(link, {"run_id": "stock-1"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"safe": True})
            self.assertFalse(link.is_symlink())
            self.assertEqual(json.loads(link.read_text(encoding="utf-8")), {"run_id": "stock-1"})

    def test_duration_parser_rejects_nan_and_caps_extreme_values(self):
        self.assertEqual(gate.parse_duration("nan", default=30, minimum=1, maximum=60), 30)
        self.assertEqual(gate.parse_duration("9999", default=30, minimum=1, maximum=60), 60)

    def test_wait_reloads_until_matching_release(self):
        p = paths()
        old = state("old", "20260827")
        old[p["orchestrator"]]["publish_ok"] = False
        new = state("new", "20260828")
        calls = {"n": 0}

        def load(path):
            phase = old if calls["n"] < 5 else new
            calls["n"] += 1
            return phase.get(path, {})

        clock = iter([0.0, 0.0, 0.0, 1.0])
        with mock.patch("production_release_gate.time.monotonic", side_effect=lambda: next(clock)), mock.patch(
            "production_release_gate.time.sleep"
        ):
            bundle, errors = wait_for_release(
                p,
                load_fn=load,
                lock_fn=lambda _path: False,
                expected_trade_date="20260828",
                timeout_s=2,
                interval_s=0.01,
            )
        self.assertEqual(errors, [])
        self.assertEqual(bundle["run_id"], "new")
        self.assertGreater(calls["n"], 5)

    def test_terminal_current_run_failure_stops_waiting_immediately(self):
        p = paths()
        docs = state("old-run", "20260828")
        docs[p["orchestrator"]] = {
            "run_id": "failed-run",
            "trade_date": "20260828",
            "status": "failed",
            "ok": False,
            "finished_at": "2026-09-03T09:00:00+08:00",
            "failed_stage": "strategySuite",
            "stages": [{"stage": "strategySuite", "ok": False}],
        }
        with mock.patch("production_release_gate.time.sleep") as sleep:
            bundle, errors = wait_for_release(
                p,
                load_fn=lambda path: docs.get(path, {}),
                lock_fn=lambda _path: False,
                expected_trade_date="20260828",
                timeout_s=3600,
            )
        self.assertIsNone(bundle)
        self.assertTrue(any("主管线已终止失败" in error for error in errors))
        sleep.assert_not_called()

    def test_running_current_attempt_is_not_terminal(self):
        p = paths()
        docs = state("run-1", "20260828")
        docs[p["orchestrator"]].update(
            {
                "status": "running",
                "ok": False,
                "selection_ok": False,
                "publish_ok": False,
                "stages": [{"stage": "strategySuite", "ok": False}],
            }
        )
        bundle, errors = wait_for_release(
            p,
            load_fn=lambda path: docs.get(path, {}),
            lock_fn=lambda _path: False,
            expected_trade_date="20260828",
            timeout_s=0,
        )
        self.assertIsNone(bundle)
        self.assertFalse(any("主管线已终止失败" in error for error in errors))


class DeployRecoveryTests(unittest.TestCase):
    """20261009: a deploy-only failure healed by a confirmed receipt releases research."""

    @staticmethod
    def failed_deploy(docs, p, **overrides):
        orchestrator = dict(docs[p["orchestrator"]])
        orchestrator.update({"status": "failed", "publish_ok": False, "failed_stage": "deployAndNotify"})
        orchestrator.update(overrides)
        docs[p["orchestrator"]] = orchestrator
        return docs

    def inspect(self, docs, p):
        return inspect_release(p, load_fn=lambda path: docs.get(path, {}), lock_fn=lambda _path: False)

    def test_receipt_confirmed_deploy_recovery_releases(self):
        p = paths()
        bundle, errors = self.inspect(self.failed_deploy(state(), p), p)
        self.assertEqual(errors, [])
        self.assertTrue(bundle["publish_recovered"])

    def test_wait_returns_recovered_release_instead_of_terminal_failure(self):
        p = paths()
        docs = self.failed_deploy(state(trade_date="20260828"), p)
        bundle, errors = wait_for_release(p, load_fn=lambda path: docs.get(path, {}), lock_fn=lambda _path: False,
                                          expected_trade_date="20260828", timeout_s=0)
        self.assertEqual(errors, [])
        self.assertTrue(bundle["publish_recovered"])

    def test_no_recovery_without_matching_receipt(self):
        p = paths()
        docs = self.failed_deploy(state(), p)
        docs[p["receipt"]] = dict(docs[p["receipt"]], run_id="other-run")
        bundle, _ = self.inspect(docs, p)
        self.assertIsNone(bundle)

    def test_no_recovery_when_selection_or_other_stage_failed(self):
        p = paths()
        for overrides in ({"failed_stage": "strategySuite"}, {"selection_ok": False}):
            bundle, errors = self.inspect(self.failed_deploy(state(), p, **overrides), p)
            self.assertIsNone(bundle, overrides)
            self.assertEqual(errors, ["本轮主选股或远程发布尚未完成"])

    def test_clean_release_is_not_flagged_as_recovered(self):
        p = paths()
        bundle, _ = self.inspect(state(), p)
        self.assertFalse(bundle["publish_recovered"])


if __name__ == "__main__":
    unittest.main()
