#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stock_dag as dag  # noqa: E402

TZ = dag.TZ
DATES = ["20261029", "20261030", "20261102", "20261103"]  # Fri 10-30 -> Mon 11-02


def t(date, hhmm):
    return dag.at(date, hhmm)


class WindowAndCycleTests(unittest.TestCase):
    def test_next_open_deadline_crosses_weekend(self):
        step = {"earliest": "23:30", "deadline": "next_open 09:25"}
        earliest, deadline = dag.window(step, "20261030", DATES)
        self.assertEqual(earliest, t("20261030", "23:30"))
        self.assertEqual(deadline, t("20261102", "09:25"))

    def test_active_cycles(self):
        self.assertEqual(dag.active_cycles(t("20261030", "08:00"), DATES), ["20261029", "20261030"])
        saturday = datetime(2026, 10, 31, 1, 0, tzinfo=TZ)
        self.assertEqual(dag.active_cycles(saturday, DATES), ["20261030"])


class DecideTests(unittest.TestCase):
    STEP = {"id": "research", "depends_on": ["selection"]}

    def run_decide(self, upstream, now, record=None):
        states = {"selection": {"status": upstream}, "research": record or {"status": "waiting"}}
        return dag.decide(self.STEP, states["research"], states, now, t("20261030", "22:15"),
                          t("20261102", "06:00"))

    def test_waits_until_upstream_succeeds_and_earliest_passes(self):
        self.assertEqual(self.run_decide("running", t("20261030", "22:30")), "none")
        self.assertEqual(self.run_decide("success", t("20261030", "22:00")), "none")
        self.assertEqual(self.run_decide("success", t("20261030", "22:15")), "start")

    def test_failed_upstream_skips_instead_of_failing(self):
        self.assertEqual(self.run_decide("failed", t("20261030", "21:00")), "skip")

    def test_deadline_marks_missed(self):
        self.assertEqual(self.run_decide("running", t("20261102", "06:00")), "miss")

    def test_after_only_needs_upstream_finished(self):
        step = {"id": "closure", "after": ["selection"]}
        states = {"selection": {"status": "failed"}, "closure": {"status": "waiting"}}
        self.assertEqual(dag.decide(step, states["closure"], states, t("20261030", "21:00"),
                                    t("20261030", "20:45"), t("20261102", "08:00")), "start")

    def test_retry_waits_for_its_time(self):
        record = {"status": "retry_wait", "retry_at": dag.iso(t("20261030", "21:20"))}
        step = {"id": "selection"}
        states = {"selection": record}
        args = (t("20261030", "19:30"), t("20261030", "23:30"))
        self.assertEqual(dag.decide(step, record, states, t("20261030", "21:00"), *args), "none")
        self.assertEqual(dag.decide(step, record, states, t("20261030", "21:20"), *args), "retry")


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.scripts = root / "scripts"
        (self.scripts / "stock").mkdir(parents=True)
        self.output = root / "output"
        calendar = root / "calendar.json"
        calendar.write_text(json.dumps({"open_dates": DATES}))
        for name, value in {"STATE_DIR": root / "dag", "SCRIPTS": self.scripts, "HERMES_OUTPUT": self.output,
                            "CALENDAR": calendar}.items():
            patcher = mock.patch.object(dag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dag, "open_dates", lambda calendar_path=calendar: DATES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, mode, steps):
        path = Path(self.tmp.name) / "dag.json"
        path.write_text(json.dumps({"mode": mode, "steps": steps}))
        return path

    def script(self, name, body):
        (self.scripts / "stock" / name).write_text(body)

    def wait_rc(self, cycle, step_id, attempt=1):
        rc = dag.STATE_DIR / "runs" / cycle / f"{step_id}.{attempt}.rc"
        for _ in range(100):
            if rc.exists() and rc.read_text().strip():
                return
            time.sleep(0.05)
        self.fail("step did not finish")

    def test_live_success_relays_output_and_unblocks_downstream(self):
        self.script("sel.sh", "echo selection done\n")
        self.script("pub.sh", "echo published\n")
        cfg = self.config("live", [
            {"id": "selection", "script": "stock/sel.sh", "earliest": "19:30", "deadline": "23:30"},
            {"id": "broadcast", "script": "stock/pub.sh", "earliest": "20:00", "deadline": "23:30",
             "depends_on": ["selection"]},
        ])
        self.assertEqual(dag.tick(t("20261030", "19:30"), cfg), [])
        self.wait_rc("20261030", "selection")
        self.assertEqual(dag.tick(t("20261030", "20:00"), cfg), ["selection done"])
        self.wait_rc("20261030", "broadcast")
        self.assertEqual(dag.tick(t("20261030", "20:05"), cfg), ["published"])
        state = json.loads((dag.STATE_DIR / "20261030.json").read_text())
        self.assertEqual(state["steps"]["broadcast"]["status"], "success")

    def test_live_failure_retries_then_skips_downstream_with_one_message(self):
        self.script("sel.sh", "echo boom; exit 3\n")
        self.script("pub.sh", "echo should not run\n")
        cfg = self.config("live", [
            {"id": "selection", "script": "stock/sel.sh", "earliest": "19:30", "deadline": "23:30",
             "retry": {"max_attempts": 2, "delay_minutes": 90}},
            {"id": "broadcast", "script": "stock/pub.sh", "earliest": "20:00", "deadline": "23:30",
             "depends_on": ["selection"]},
        ])
        dag.tick(t("20261030", "19:30"), cfg)
        self.wait_rc("20261030", "selection")
        messages = dag.tick(t("20261030", "19:35"), cfg)
        self.assertIn("21:05 自动重试", messages[0])
        dag.tick(t("20261030", "21:05"), cfg)
        self.wait_rc("20261030", "selection", attempt=2)
        messages = dag.tick(t("20261030", "21:10"), cfg)
        self.assertEqual(messages[0], "boom")
        self.assertIn("跳过：broadcast", messages[1])
        state = json.loads((dag.STATE_DIR / "20261030.json").read_text())
        self.assertEqual(state["steps"]["broadcast"]["status"], "skipped")
        self.assertFalse((dag.STATE_DIR / "runs" / "20261030" / "broadcast.1.log").exists())

    def test_forward_only_step_is_reported_as_gap_when_missed(self):
        cfg = self.config("live", [
            {"id": "selection", "script": "stock/none.sh", "earliest": "19:30", "deadline": "23:30"},
            {"id": "forward_shadow", "script": "stock/none.sh", "earliest": "23:30",
             "deadline": "next_open 09:25", "depends_on": ["selection"], "forward_only": True},
        ])
        state = dag.load_state("20261030", json.loads(cfg.read_text())["steps"])
        state["steps"]["selection"]["status"] = "running"
        state["steps"]["selection"].update(pid=999999, rc_file="/nonexistent", log="/nonexistent", attempts=1)
        dag.save_state(state)
        messages = dag.tick(t("20261102", "09:30"), cfg)
        self.assertTrue(any("forward_shadow（只前瞻，记为缺口）" in m for m in messages))

    def test_first_run_does_not_judge_an_untracked_past_day(self):
        cfg = self.config("live", [{"id": "selection", "script": "stock/x.sh", "earliest": "19:30", "deadline": "23:30"}])
        self.assertEqual(dag.tick(t("20261030", "08:00"), cfg), [])
        self.assertFalse((dag.STATE_DIR / "20261029.json").exists())

    def test_shadow_mirrors_hermes_runs_and_records_would_start(self):
        job = self.output / "job1"
        job.mkdir(parents=True)
        (job / "2026-10-30_19-45-41.md").write_text("**Run Time:** 2026-10-30 19:45:41\n\nok\n")
        cfg = self.config("shadow", [
            {"id": "selection", "script": "stock/x.sh", "hermes_job": "job1", "earliest": "19:30", "deadline": "23:30"},
            {"id": "broadcast", "script": "stock/y.sh", "hermes_job": "job2", "earliest": "20:00",
             "deadline": "23:30", "depends_on": ["selection"]},
        ])
        self.assertEqual(dag.tick(t("20261030", "19:30"), cfg), [])
        dag.tick(t("20261030", "19:50"), cfg)
        state = json.loads((dag.STATE_DIR / "20261030.json").read_text())
        self.assertEqual(state["steps"]["selection"]["status"], "success")
        self.assertEqual(state["steps"]["selection"]["would_start_at"][11:16], "19:30")
        self.assertNotIn("would_start_at", state["steps"]["broadcast"])
        dag.tick(t("20261030", "20:00"), cfg)
        state = json.loads((dag.STATE_DIR / "20261030.json").read_text())
        self.assertEqual(state["steps"]["broadcast"]["would_start_at"][11:16], "20:00")
        self.assertFalse((dag.STATE_DIR / "runs").exists(), "shadow mode must not launch anything")


class ConfigTests(unittest.TestCase):
    def test_production_config_is_topological_and_complete(self):
        config = json.loads(dag.CONFIG.read_text())
        seen = set()
        for step in config["steps"]:
            for dep in (step.get("depends_on") or []) + (step.get("after") or []):
                self.assertIn(dep, seen, f"{step['id']} depends on later/unknown {dep}")
            self.assertTrue((dag.SCRIPTS / step["script"]).is_file(), step["script"])
            self.assertTrue((dag.WORKSPACE / "ops/hermes" / step["script"]).is_file(), f"untracked {step['script']}")
            seen.add(step["id"])
        self.assertIn(config["mode"], {"shadow", "live"})


if __name__ == "__main__":
    unittest.main()
