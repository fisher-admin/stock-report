"""Integration test: run the real publish_stock_report_v2.sh in a sandbox.

Paths in a copy of the script are rewritten to a temporary workspace; content
generators are replaced by no-ops so the fixture content is what gets
validated.  "origin" is a local bare repository.  Nothing touches production.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REAL_MODULES = ["final_publication_gate.py", "exchange_calendar.py", "publish_lock.py",
                "publish_idempotency_gate.py", "deployment_receipt.py", "maintenance_mode.py",
                "committed_public_boundary.py"]
PUBLIC_ALLOWLIST = ["data/latest/run_manifest.json", "data/latest/system_verdict.json",
                    "data/latest/candidate_state.json", "data/latest/strategy_state.json",
                    "data/latest/review_track_latest.json", "data/latest/market_context.json"]
NOOP_SCRIPTS = ["strategy_publication_layer.py", "generate_industry_heatmap.py", "generate_latest_states.py",
                "dual_track_publication.py", "sanitize_public_report.py", "validate_publication_contract.py",
                "publish_guard.py"]
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def docs(run_id, trade_date, action="observe_only", passed=True, codes=("600512", "601985")):
    return {
        "run_manifest.json": {"run_id": run_id, "trade_date": trade_date, "validation_ok": True,
                              "publish_ready": True, "publish_mode": "full", "ai_complete": True},
        "system_verdict.json": {"schema_version": "system_verdict.v2", "run": {"run_id": run_id},
                                "dates": {"decision_trade_date": trade_date},
                                "date_contract": {"status": "pass" if passed else "block", "passed": passed},
                                "gates": {"freshness_gate": {"hard_blocking": True, "passed": passed}},
                                "final_action": {"action": action}},
        "candidate_state.json": {"latest_trade_date": trade_date, "candidates": [{"code": c} for c in codes]},
        "strategy_state.json": {"latest_trade_date": trade_date},
    }


class PublisherSandbox:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.scripts = self.ws / "skills/stock-system-orchestrator/scripts"
        self.health = self.ws / "stock_data/03-working/health"
        self.scripts.mkdir(parents=True)
        self.health.mkdir(parents=True)
        for name in REAL_MODULES:
            shutil.copy(SCRIPT_DIR / name, self.scripts / name)
        for name in NOOP_SCRIPTS:
            (self.scripts / name).write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        text = (SCRIPT_DIR / "publish_stock_report_v2.sh").read_text(encoding="utf-8")
        text = text.replace("/Users/fisher/.openclaw/workspace", str(self.ws))
        text = text.replace("/Users/fisher/.openclaw/logs/publish_v2", str(root / "logs"))
        self.publisher = self.scripts / "publish_stock_report_v2.sh"
        self.publisher.write_text(text, encoding="utf-8")
        # origin + published clone
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.ws / "stock-report"
        git(root, "clone", "-q", str(self.origin), str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "data/latest").mkdir(parents=True)
        (self.repo / "assets").mkdir()
        (self.repo / "index.html").write_text("<html></html>", encoding="utf-8")
        (self.repo / "config").mkdir()
        (self.repo / "config/public-result-allowlist.txt").write_text("\n".join(PUBLIC_ALLOWLIST) + "\n", encoding="utf-8")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/build_pages_artifact.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        (self.repo / "generate_view_summaries.py").write_text(
            "from pathlib import Path\nPath(__file__).parent.joinpath('data/latest/review_track_latest.json').write_text('{}')\n",
            encoding="utf-8")
        self.write(docs("stock-20260930T113015Z", "20260930"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "previous published batch")
        git(self.repo, "push", "-q", "origin", "main")
        sha = git(self.repo, "rev-parse", "HEAD")
        (self.health / "deployment_receipt.json").write_text(json.dumps({
            "run_id": "stock-20260930T113015Z", "trade_date": "20260930", "local_sha": sha, "remote_sha": sha,
            "remote_confirmed": True, "confirmed_at": "2026-09-30T19:50:00+08:00"}), encoding="utf-8")
        now = datetime.now()
        (self.health / "trading_calendar.json").write_text(json.dumps({
            "source": "rds_trade_cal", "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "from": "20250826", "to": "20991231", "open_dates": ["20260930", "20261008"]}), encoding="utf-8")

    def write(self, payload):
        for name, doc in payload.items():
            (self.repo / "data/latest" / name).write_text(json.dumps(doc), encoding="utf-8")

    def run(self, run_id="", trade_date=""):
        env = {**os.environ, **GIT_ENV, "STOCK_SYSTEM_PYTHON": sys.executable,
               "STOCK_SYSTEM_WORKSPACE": str(self.ws), "HOME": str(self.ws.parent),
               "OPENCLAW_PUBLISHED_REPO": str(self.repo), "RUN_ID": run_id, "TRADE_DATE": trade_date}
        return subprocess.run(["bash", str(self.publisher)], cwd=self.ws, env=env, capture_output=True,
                              text=True, timeout=120)

    def origin_head(self):
        return git(self.repo, "ls-remote", "origin", "refs/heads/main").split()[0]

    def close(self):
        self.tmp.cleanup()


class PublisherFinalGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sb = PublisherSandbox()
        self.before = self.sb.origin_head()

    def tearDown(self):
        self.sb.close()

    def test_valid_new_batch_is_pushed_and_recorded(self):
        self.sb.write(docs("stock-20261008T113000Z", "20261008"))
        proc = self.sb.run("stock-20261008T113000Z", "20261008")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotEqual(self.sb.origin_head(), self.before)
        record = json.loads((self.sb.health / "publication_validation.json").read_text())
        self.assertTrue(record["passed"])
        self.assertEqual(record["mode"], "new_batch")
        self.assertEqual(record["push_outcome"], "pushed")
        self.assertEqual(record["pushed_sha"], self.sb.origin_head())
        receipt = json.loads((self.sb.health / "deployment_receipt.json").read_text())
        self.assertEqual(receipt["run_id"], "stock-20261008T113000Z")

    def test_maintenance_mode_pushes_calibration_status_without_picks(self):
        flag = self.sb.ws / "config/stock_maintenance_mode.json"
        flag.parent.mkdir(parents=True)
        flag.write_text(json.dumps({"enabled": True, "since": "2026-10-09T22:00:00+08:00"}), encoding="utf-8")
        self.sb.write(docs("stock-20261008T113000Z", "20261008"))
        proc = self.sb.run("stock-20261008T113000Z", "20261008")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotEqual(self.sb.origin_head(), self.before)
        pushed = json.loads(git(self.sb.repo, "show", "origin/main:data/latest/candidate_state.json"))
        self.assertEqual(pushed["candidates"], [])
        self.assertTrue(pushed["maintenance"]["enabled"])
        manifest = json.loads(git(self.sb.repo, "show", "origin/main:data/latest/run_manifest.json"))
        self.assertEqual(manifest["maintenance"]["since"], "2026-10-09T22:00:00+08:00")
        record = json.loads((self.sb.health / "publication_validation.json").read_text())
        self.assertTrue(record["checks"]["maintenance"])

    def test_non_allowlisted_data_file_is_never_committed_or_pushed(self):
        local_before = git(self.sb.repo, "rev-parse", "HEAD")
        (self.sb.repo / "data/latest/review_state_unified.json").write_text('{"stock_rows": [{"stock_code": "000001"}]}')
        self.sb.write(docs("stock-20261008T113000Z", "20261008"))
        proc = self.sb.run("stock-20261008T113000Z", "20261008")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("review_state_unified.json", proc.stdout)
        self.assertEqual(self.sb.origin_head(), self.before)
        self.assertEqual(git(self.sb.repo, "rev-parse", "HEAD"), local_before)
        self.assertEqual(git(self.sb.repo, "diff", "--cached", "--name-only"), "")  # unstaged again

    def test_malformed_maintenance_flag_blocks_publish(self):
        flag = self.sb.ws / "config/stock_maintenance_mode.json"
        flag.parent.mkdir(parents=True)
        flag.write_text("{broken", encoding="utf-8")
        self.sb.write(docs("stock-20261008T113000Z", "20261008"))
        proc = self.sb.run("stock-20261008T113000Z", "20261008")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.sb.origin_head(), self.before)

    def test_halt_new_batch_is_never_committed_or_pushed(self):
        local_before = git(self.sb.repo, "rev-parse", "HEAD")
        self.sb.write(docs("stock-20261008T113000Z", "20261008", action="halt", passed=False))
        proc = self.sb.run("stock-20261008T113000Z", "20261008")
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertEqual(self.sb.origin_head(), self.before)
        self.assertEqual(git(self.sb.repo, "rev-parse", "HEAD"), local_before)
        receipt = json.loads((self.sb.health / "deployment_receipt.json").read_text())
        self.assertEqual(receipt["run_id"], "stock-20260930T113015Z")

    def test_caller_identity_mismatch_blocks(self):
        self.sb.write(docs("stock-20261008T113000Z", "20261008"))
        proc = self.sb.run("stock-other-run", "20261008")
        self.assertEqual(proc.returncode, 5, proc.stdout)
        self.assertEqual(self.sb.origin_head(), self.before)

    def test_concurrent_remote_change_to_published_content_aborts_push(self):
        other = Path(self.sb.tmp.name) / "other"
        git(Path(self.sb.tmp.name), "clone", "-q", str(self.sb.origin), str(other))
        (other / "data/latest/extra_from_other_writer.json").write_text('{"x": 1}')
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "concurrent writer")
        git(other, "push", "-q", "origin", "main")
        concurrent_head = self.sb.origin_head()
        self.sb.write(docs("stock-20261008T113000Z", "20261008"))
        proc = self.sb.run("stock-20261008T113000Z", "20261008")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.sb.origin_head(), concurrent_head)
        record = json.loads((self.sb.health / "publication_validation.json").read_text())
        logs = "".join(p.read_text() for p in (Path(self.sb.tmp.name) / "logs").glob("*.log"))
        self.assertTrue(record.get("changed_after_validation"), logs[-3000:])
        self.assertNotIn("pushed_sha", record)

    def test_same_batch_refresh_with_unchanged_list_is_published(self):
        self.sb.write(docs("stock-20260930T113015Z", "20260930"))
        (self.sb.repo / "data/latest/market_context.json").write_text('{"morning": "refresh"}')
        proc = self.sb.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = json.loads((self.sb.health / "publication_validation.json").read_text())
        self.assertEqual(record["mode"], "refresh")


if __name__ == "__main__":
    unittest.main()
