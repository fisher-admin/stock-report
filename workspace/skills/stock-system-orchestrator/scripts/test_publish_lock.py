import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from publish_lock import PublishAlreadyRunning, PublishLock, UnauthorizedRelease, publish_lock


class PublishLockTests(unittest.TestCase):
    def test_single_publisher_acquires_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / "test.lock"
            with publish_lock(lock_path, run_id="r1", trade_date="20260820"):
                self.assertTrue(lock_path.exists())
                data = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(data["run_id"], "r1")
                self.assertTrue(data.get("token"))
            self.assertFalse(lock_path.exists())

    def test_second_publisher_cannot_enter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / "test.lock"
            with publish_lock(lock_path):
                with self.assertRaises(PublishAlreadyRunning):
                    with publish_lock(lock_path):
                        pass

    def test_unauthorized_release_rejected_when_owner_live(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / "test.lock"
            lock = PublishLock(lock_path, pid=os.getpid(), run_id="live_run")
            tok = lock.acquire()
            self.assertTrue(lock_path.exists())

            # Attempt release with wrong token and unrelated PID -> raises UnauthorizedRelease
            foreign_lock = PublishLock(lock_path, pid=99999999, token="wrong_token")
            with self.assertRaises(UnauthorizedRelease):
                foreign_lock.release(token="wrong_token", pid=99999999)
            self.assertTrue(lock_path.exists())

            # Authorized release with correct token succeeds
            lock.release(token=tok)
            self.assertFalse(lock_path.exists())

    def test_stale_dead_owner_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / "test.lock"
            payload = {"pid": 99999999, "started_at": 12345, "run_id": "dead", "token": "dead_token"}
            lock_path.write_text(json.dumps(payload), encoding="utf-8")

            with publish_lock(lock_path, run_id="live_1"):
                data = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(data["run_id"], "live_1")

    def test_independent_subprocesses_concurrency_and_unauthorized_release(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(tmp_dir) / "proc.lock"
            py = sys.executable
            cli_script = str(Path(__file__).resolve().parent / "publish_lock.py")

            # 1. Start background process holding lock
            bg_script = (
                "import time, sys\n"
                "from pathlib import Path\n"
                "from publish_lock import PublishLock\n"
                f"lock = PublishLock(Path({repr(str(lock_path))}), pid=None, run_id='bg_run')\n"
                "tok = lock.acquire()\n"
                "print(f'TOKEN={tok}', flush=True)\n"
                "time.sleep(2.0)\n"
                "lock.release(token=tok)\n"
            )
            proc1 = subprocess.Popen([py, "-c", bg_script], stdout=subprocess.PIPE, text=True, cwd=str(Path(__file__).resolve().parent))
            line = proc1.stdout.readline().strip()
            self.assertTrue(line.startswith("TOKEN="))

            # 2. Unauthorized release attempt from another process (no token / wrong token) MUST fail (exit code 5)
            res_unauth = subprocess.run(
                [py, cli_script, "--release", "--lock-file", str(lock_path), "--token", "fake_token", "--pid", "99999999"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res_unauth.returncode, 5)
            self.assertIn("Unauthorized release rejected", res_unauth.stderr)
            self.assertTrue(lock_path.exists())

            # 3. Concurrent acquire attempt MUST fail (exit code 4)
            res2 = subprocess.run([py, cli_script, "--acquire", "--lock-file", str(lock_path), "--run-id", "proc2"], capture_output=True, text=True)
            self.assertEqual(res2.returncode, 4)

            # 4. Wait for proc1 to cleanly release
            proc1.wait()
            if proc1.stdout is not None:
                proc1.stdout.close()
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
