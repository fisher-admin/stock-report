#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_shadow as cs  # noqa: E402


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name, value in {"WORKSPACE": root, "SHADOW_DIR": root / "shadow"}.items():
            patcher = mock.patch.object(cs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.capture = root / "report.json"

    def tearDown(self):
        self.tmp.cleanup()

    def record(self):
        return json.loads(next(cs.SHADOW_DIR.glob("*/job.json")).read_text())

    def host(self, rc, stdout):
        with mock.patch.object(sys, "stdin", io.StringIO(stdout)):
            cs.main(["host", "--job", "job", "--rc", str(rc), "--capture", str(self.capture)])

    def container(self, rc, stdout, report):
        def fake_run(job, command, capture, timeout):
            self.assertEqual(command, ["python", "x.py"])
            return {"status": "ran", "rc": rc, "stdout": stdout, "capture": report}
        with mock.patch.object(cs, "run_container", fake_run):
            cs.main(["run", "--job", "job", "--capture", str(self.capture), "--", "python", "x.py"])

    def test_match_ignores_volatile_timestamps(self):
        self.container(0, "", {"ok": True, "generated_at": "a", "results": {"disk": []}})
        self.capture.write_text(json.dumps({"ok": True, "generated_at": "b", "results": {"disk": []}}))
        self.host(0, "\n")
        record = self.record()
        self.assertTrue(record["match"])
        self.assertEqual(record["diffs"], [])

    def test_mismatch_lists_rc_stdout_and_capture_keys(self):
        self.container(1, "⚠️ lock", {"ok": False, "results": {"locks": ["x"]}})
        self.capture.write_text(json.dumps({"ok": True, "results": {"locks": []}}))
        self.host(0, "")
        record = self.record()
        self.assertFalse(record["match"])
        self.assertEqual(record["diffs"], ["rc: container=1 host=0", "stdout differs",
                                           "capture.ok differs", "capture.results differs"])

    def test_container_unavailable_is_not_evidence(self):
        with mock.patch.object(cs, "docker_ready", return_value=False):
            cs.main(["run", "--job", "job", "--", "true"])
        self.host(0, "")
        record = self.record()
        self.assertEqual(record["container"]["status"], "docker_unavailable")
        self.assertIsNone(record["match"])


if __name__ == "__main__":
    unittest.main()
