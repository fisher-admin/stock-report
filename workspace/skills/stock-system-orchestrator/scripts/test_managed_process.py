import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


class ManagedProcessTests(unittest.TestCase):
    def test_unsupported_python_fails_before_starting_any_child(self):
        self.runner()
        import managed_process
        original = managed_process.os.waitid
        try:
            del managed_process.os.waitid
            with mock.patch.object(managed_process.subprocess, 'Popen') as launch:
                with self.assertRaises(ValueError):
                    self.runner()([sys.executable, '-c', 'pass'], timeout=1)
                launch.assert_not_called()
        finally:
            managed_process.os.waitid = original

    def test_guardian_death_does_not_leave_writer_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'ticks'
            # The command terminates its own guardian, not an arbitrary PID.
            child = 'import os,signal,time,pathlib; p=pathlib.Path(' + repr(str(output)) + '); p.write_text("started"); os.kill(os.getppid(),signal.SIGKILL);\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.02)'
            with self.assertRaises((RuntimeError, OSError)):
                self.runner()([sys.executable, '-c', child], timeout=3)
            self.assertTrue(output.exists())
            before = output.read_text()
            time.sleep(.15)
            self.assertEqual(output.read_text(), before)

    def test_malformed_ready_receipt_is_not_success(self):
        self.runner()
        import managed_process
        def corrupt(_pid, _timeout, ready_path):
            ready_path.write_text('{"returncode":true}')
            return 'ready'
        with mock.patch.object(managed_process, '_observe_exit', side_effect=corrupt):
            with self.assertRaises(RuntimeError):
                self.runner()([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=3)

    def test_success_also_stops_background_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'ticks'
            child = 'import time,pathlib; p=pathlib.Path(' + repr(str(output)) + ');\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.02)'
            parent = 'import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); p=pathlib.Path(' + repr(str(output)) + ');\nwhile not p.exists(): time.sleep(.01)'
            result = self.runner()([sys.executable, '-c', parent], timeout=3)
            self.assertEqual(result.returncode, 0)
            before = output.read_text()
            time.sleep(.15)
            self.assertEqual(output.read_text(), before)

    def test_timeout_terminates_signal_ignoring_child(self):
        child = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.runner()([sys.executable, '-c', child], timeout=.3)
        self.assertLess(time.monotonic() - started, 3)

    def test_missing_executable_is_failure_and_does_not_hang(self):
        started = time.monotonic()
        with self.assertRaises((RuntimeError, OSError)):
            self.runner()(['/nonexistent-hermes-test-executable'], timeout=2)
        self.assertLess(time.monotonic() - started, 3)

    def test_repeated_calls_do_not_reap_unrelated_child(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            for _ in range(8):
                result = self.runner()([sys.executable, '-c', 'pass'], timeout=3)
                self.assertEqual(result.returncode, 0)
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait(timeout=2)

    def test_non_reaping_observation_pins_guardian_identity(self):
        self.runner()
        import managed_process
        self.assertTrue(hasattr(managed_process, '_observe_exit'), 'must observe without reaping')
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        try:
            self.assertTrue(managed_process._observe_exit(child.pid, 3))
            status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            self.assertEqual(status.si_pid, child.pid)
        finally:
            child.wait(timeout=3)

    def test_research_uses_guardian_boundary(self):
        import run_post_publish_research as research
        completed = subprocess.CompletedProcess([], 0, 'done', '')
        with mock.patch.object(research, 'run_managed', create=True, return_value=completed) as managed, mock.patch.object(research.subprocess, 'run', return_value=completed):
            research.run_stage('probe', Path('/fake'), {}, timeout_s=1)
        managed.assert_called_once()

    def test_publisher_uses_guardian_boundary(self):
        import stage6_deploy_and_notify as stage
        failed = subprocess.CompletedProcess([], 1, '', 'test failure')
        with mock.patch.object(stage, 'run_managed', create=True, return_value=failed) as managed, mock.patch.object(stage.subprocess, 'run', return_value=failed), mock.patch.object(stage, 'V2_PUBLISH_SCRIPT', Path(__file__)), mock.patch.object(stage, 'PUBLISHED_REPO', Path(__file__).parent):
            stage.deploy_published_repo({'final_action': {'decision': 'observe_only'}}, run_id='test-run', trade_date='20260911')
        managed.assert_called_once()

    def runner(self):
        self.assertIsNotNone(importlib.util.find_spec('managed_process'), 'managed process runner missing')
        from managed_process import run_managed
        return run_managed

    def test_exit_code_and_output(self):
        result = self.runner()([sys.executable, '-c', 'print("done"); raise SystemExit(7)'], timeout=3)
        self.assertEqual(result.returncode, 7)
        self.assertIn('done', result.stdout)

    def test_timeout_stops_descendant_writes(self):
        run = self.runner()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'ticks'
            child = 'import time, pathlib; p=pathlib.Path(' + repr(str(output)) + ');\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.02)'
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(30)'
            with self.assertRaises(subprocess.TimeoutExpired):
                run([sys.executable, '-c', parent], timeout=.5)
            self.assertTrue(output.exists())
            before = output.read_text()
            time.sleep(.15)
            self.assertEqual(output.read_text(), before)

    def test_abrupt_parent_death_stops_descendant_writes(self):
        self.runner()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'ticks'
            child = 'import time,pathlib; p=pathlib.Path(' + repr(str(output)) + ');\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.02)'
            parent = 'from managed_process import run_managed; import sys; run_managed([sys.executable,"-c",' + repr(child) + '],timeout=30)'
            proc = subprocess.Popen([sys.executable, '-c', parent], cwd=Path(__file__).parent)
            try:
                deadline = time.monotonic() + 3
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(output.exists())
                proc.kill()
                proc.wait(timeout=2)
                time.sleep(.3)
                before = output.read_text()
                time.sleep(.15)
                self.assertEqual(output.read_text(), before)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
