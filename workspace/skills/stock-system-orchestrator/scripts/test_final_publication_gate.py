"""Tests for final_publication_gate.py (isolated temp repos; nothing is pushed)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import final_publication_gate as fg  # noqa: E402

GATE = SCRIPT_DIR / "final_publication_gate.py"
NOW = datetime(2026, 10, 8, 19, 40)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return proc.stdout.strip()


def batch_docs(run_id="stock-20261008T113000Z", trade_date="20261008", action="observe_only",
               codes=("600512", "601985"), cand_date=None, strat_date=None, passed=True):
    return {
        "run_manifest.json": {"run_id": run_id, "trade_date": trade_date, "validation_ok": True,
                              "publish_ready": True, "publish_mode": "full", "ai_complete": True},
        "system_verdict.json": {
            "schema_version": "system_verdict.v2",
            "run": {"run_id": run_id},
            "dates": {"decision_trade_date": trade_date},
            "date_contract": {"status": "pass" if passed else "block", "passed": passed},
            "gates": {"freshness_gate": {"hard_blocking": True, "passed": passed}},
            "final_action": {"action": action, "label": "x"},
        },
        "candidate_state.json": {"latest_trade_date": cand_date or trade_date,
                                 "candidates": [{"code": c} for c in codes]},
        "strategy_state.json": {"latest_trade_date": strat_date or trade_date},
    }


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.health = root / "health"
        self.health.mkdir()
        (self.repo / "data/latest").mkdir(parents=True)
        (self.repo / "assets").mkdir()
        (self.repo / "index.html").write_text("<html></html>", encoding="utf-8")
        git(self.repo, "init", "-q")
        self.calendar = self.health / "trading_calendar.json"
        self.calendar.write_text(json.dumps({
            "source": "rds_trade_cal", "generated_at": "2026-10-08 19:30:16",
            "from": "20250826", "to": "20261129",
            "open_dates": ["20260928", "20260929", "20260930", "20261008", "20261009"],
        }), encoding="utf-8")

    def write(self, docs: dict):
        for name, payload in docs.items():
            (self.repo / "data/latest" / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def commit(self, msg="c"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def receipt(self, run_id, trade_date, sha="abc"):
        (self.health / "deployment_receipt.json").write_text(json.dumps({
            "run_id": run_id, "trade_date": trade_date, "local_sha": sha, "remote_sha": sha,
            "remote_confirmed": True}), encoding="utf-8")

    def evaluate(self, **kw):
        return fg.evaluate(self.repo, self.health, calendar_path=self.calendar, now=NOW, **kw)

    def close(self):
        self.tmp.cleanup()


class FinalGateUnitTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.write(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930"))
        self.f.commit("previous published batch")
        self.f.receipt("stock-20260930T113015Z", "20260930")

    def tearDown(self):
        self.f.close()

    def test_valid_observe_only_new_batch_passes(self):
        self.f.write(batch_docs())
        result = self.f.evaluate(expected_run_id="stock-20261008T113000Z", expected_trade_date="20261008")
        self.assertEqual(result["mode"], "new_batch")
        self.assertTrue(result["passed"], result["blockers"])

    def test_halt_blocks_new_batch(self):
        self.f.write(batch_docs(action="halt"))
        result = self.f.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("VERDICT_HALT", result["blockers"])

    def test_final_action_must_be_structured(self):
        for bad in ("halt", {"decision": "observe_only"}, {"action": "go"}, None):
            with self.subTest(bad=bad):
                docs = batch_docs()
                docs["system_verdict.json"]["final_action"] = bad
                self.f.write(docs)
                result = self.f.evaluate()
                self.assertFalse(result["passed"])
                self.assertTrue(any(b.startswith("VERDICT_FINAL_ACTION_MALFORMED") for b in result["blockers"]))

    def test_unreadable_verdict_blocks(self):
        self.f.write(batch_docs())
        (self.f.repo / "data/latest/system_verdict.json").write_text("{truncated", encoding="utf-8")
        result = self.f.evaluate()
        self.assertFalse(result["passed"])
        self.assertTrue(result["blockers"][0].startswith("UNREADABLE:system_verdict.json"))

    def test_relabelled_dates_block(self):
        # Oct 2 pattern: 20260930 data, candidate/strategy labelled 20261002.
        self.f.write(batch_docs(run_id="stock-20261002T113007Z", trade_date="20260930",
                                cand_date="20261002", strat_date="20261002", passed=False, action="halt"))
        result = self.f.evaluate()
        self.assertFalse(result["passed"])
        for code in ("VERDICT_HALT", "DATE_CONTRACT_NOT_PASSED", "HARD_GATE_FAILED:freshness_gate",
                     "CANDIDATE_DATE_MISMATCH:20261002", "STRATEGY_DATE_MISMATCH:20261002"):
            self.assertIn(code, result["blockers"])

    def test_strategy_gate_execution_block_is_publishable_observe_only(self):
        # 20261014: strategy_gate hard-blocked (zero sector overlap) with a valid observe_only verdict.
        docs = batch_docs()
        docs["system_verdict.json"]["gates"]["strategy_gate"] = {"hard_blocking": True, "passed": False}
        self.f.write(docs)
        result = self.f.evaluate()
        self.assertTrue(result["passed"], result["blockers"])
        self.assertEqual(result["checks"]["execution_blocked_gates"], ["strategy_gate"])

    def test_market_gate_block_with_halt_still_refuses_new_batch(self):
        docs = batch_docs(action="halt")
        docs["system_verdict.json"]["gates"]["market_gate"] = {"hard_blocking": True, "passed": False}
        self.f.write(docs)
        result = self.f.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("VERDICT_HALT", result["blockers"])
        self.assertNotIn("HARD_GATE_FAILED:market_gate", result["blockers"])

    def test_caller_identity_mismatch_blocks(self):
        self.f.write(batch_docs())
        result = self.f.evaluate(expected_run_id="stock-other", expected_trade_date="20261009")
        self.assertIn("BATCH_RUN_ID_MISMATCH:expected=stock-other:manifest=stock-20261008T113000Z", result["blockers"])
        self.assertTrue(any(b.startswith("BATCH_TRADE_DATE_MISMATCH") for b in result["blockers"]))

    def test_verdict_from_other_batch_blocks(self):
        docs = batch_docs()
        docs["system_verdict.json"]["run"]["run_id"] = "stock-20261007T113000Z"
        self.f.write(docs)
        result = self.f.evaluate()
        self.assertIn("VERDICT_RUN_MISMATCH:stock-20261007T113000Z", result["blockers"])

    def test_closed_or_unknown_trade_date_blocks(self):
        self.f.write(batch_docs(run_id="stock-20261002T113000Z", trade_date="20261002"))
        result = self.f.evaluate()
        self.assertTrue(any(b.startswith("TRADE_DATE_NOT_OPEN_SESSION:closed") for b in result["blockers"]))
        self.f.calendar.unlink()
        self.f.write(batch_docs())
        result = self.f.evaluate()
        self.assertTrue(any(b.startswith("TRADE_DATE_NOT_OPEN_SESSION:error") for b in result["blockers"]))

    def test_full_mode_without_same_day_ai_warns_not_blocks(self):
        # 2026-10-15: 验证结果改为发布页标注，不再阻断发布。
        docs = batch_docs()
        docs["run_manifest.json"]["ai_complete"] = False
        self.f.write(docs)
        result = self.f.evaluate()
        self.assertIn("FULL_MODE_WITHOUT_SAME_DAY_AI", result["warnings"])
        self.assertNotIn("FULL_MODE_WITHOUT_SAME_DAY_AI", result["blockers"])

    def test_refresh_keeps_list_and_allows_labelled_halt_status(self):
        docs = batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930", action="halt", passed=False)
        self.f.write(docs)
        result = self.f.evaluate()
        self.assertEqual(result["mode"], "refresh")
        self.assertTrue(result["passed"], result["blockers"])
        self.assertEqual(result["status_update"], "halt_status_refresh_without_new_selection")

    def test_refresh_cannot_swap_recommendation_list(self):
        self.f.write(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930", codes=("000001", "601985")))
        result = self.f.evaluate()
        self.assertEqual(result["mode"], "refresh")
        self.assertIn("REFRESH_CHANGES_RECOMMENDATION_LIST", result["blockers"])

    def test_refresh_may_correct_mislabelled_date_to_batch_date_only(self):
        # Published HEAD carries the Oct 2 mislabel (candidates dated 20261002 on a 20260930 batch).
        bad = batch_docs(run_id="stock-20261002T113007Z", trade_date="20260930", cand_date="20261002")
        self.f.write(bad)
        self.f.commit("mislabelled publish")
        self.f.receipt("stock-20261002T113007Z", "20260930")
        corrected = batch_docs(run_id="stock-20261002T113007Z", trade_date="20260930", cand_date="20260930")
        self.f.write(corrected)
        result = self.f.evaluate()
        self.assertEqual(result["mode"], "refresh")
        self.assertTrue(result["passed"], result["blockers"])
        self.assertEqual(result["label_correction"], {"from": "20261002", "to": "20260930"})
        moved = batch_docs(run_id="stock-20261002T113007Z", trade_date="20260930", cand_date="20261008")
        self.f.write(moved)
        self.assertIn("REFRESH_RELABELS_CANDIDATE_DATE:20261008", self.f.evaluate()["blockers"])

    def test_unconfirmed_receipt_forces_new_batch_rules(self):
        (self.f.health / "deployment_receipt.json").write_text(json.dumps({
            "run_id": "stock-20260930T113015Z", "trade_date": "20260930", "remote_confirmed": False}))
        self.f.write(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930", action="halt"))
        result = self.f.evaluate()
        self.assertEqual(result["mode"], "new_batch")
        self.assertIn("VERDICT_HALT", result["blockers"])


class FinalGateCliTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.write(batch_docs(run_id="stock-20260930T113015Z", trade_date="20260930"))
        self.f.commit("published")
        self.f.receipt("stock-20260930T113015Z", "20260930")

    def tearDown(self):
        self.f.close()

    def run_gate(self, *args):
        return subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True,
                              env={**os.environ, "STOCK_SYSTEM_WORKSPACE": "/nonexistent"})

    def test_cli_block_writes_record_and_exit_5(self):
        self.f.write(batch_docs(action="halt"))
        proc = self.run_gate("validate", "--repo", str(self.f.repo), "--health-dir", str(self.f.health),
                             "--calendar", str(self.f.calendar))
        self.assertEqual(proc.returncode, fg.EXIT_BLOCKED, proc.stdout)
        record = json.loads((self.f.health / "publication_validation.json").read_text())
        self.assertFalse(record["passed"])
        ledger = (self.f.health / "publication_ledger.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(ledger[-1])["event"], "blocked")

    def test_content_changed_after_rebase_requires_revalidation(self):
        # Use a calendar whose generated_at is fresh relative to the real clock.
        cal = json.loads(self.f.calendar.read_text())
        cal["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cal["to"] = "20991231"
        self.f.calendar.write_text(json.dumps(cal))
        self.f.write(batch_docs())
        proc = self.run_gate("validate", "--repo", str(self.f.repo), "--health-dir", str(self.f.health),
                             "--calendar", str(self.f.calendar))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.f.commit("validated publication")
        self.assertEqual(self.run_gate("record-commit", "--repo", str(self.f.repo), "--health-dir", str(self.f.health)).returncode, 0)
        self.assertEqual(self.run_gate("verify-after-rebase", "--repo", str(self.f.repo), "--health-dir", str(self.f.health)).returncode, 0)
        # A concurrent writer's change to published content lands via rebase.
        (self.f.repo / "data/latest/candidate_state.json").write_text(json.dumps({"latest_trade_date": "20261008", "candidates": [{"code": "999999"}]}))
        self.f.commit("remote change")
        proc = self.run_gate("verify-after-rebase", "--repo", str(self.f.repo), "--health-dir", str(self.f.health))
        self.assertEqual(proc.returncode, fg.EXIT_CHANGED, proc.stdout)
        # A change outside published paths does not require re-validation.
        self.f.write(batch_docs())
        self.f.commit("restore")
        self.run_gate("record-commit", "--repo", str(self.f.repo), "--health-dir", str(self.f.health))
        (self.f.repo / "README.md").write_text("docs")
        self.f.commit("docs only")
        self.assertEqual(self.run_gate("verify-after-rebase", "--repo", str(self.f.repo), "--health-dir", str(self.f.health)).returncode, 0)


@unittest.skipUnless(os.environ.get("PUBLISHED_HISTORY_REPO") and os.environ.get("REAL_TRADING_CALENDAR"),
                     "replay needs a clone of the published repo and the production calendar")
class RealHistoryReplayTests(unittest.TestCase):
    """Replay real published commits (Sep 28 - Oct 2) as if this gate had existed."""

    SEQUENCE = [
        ("2a9a3e1", "2026-09-28 19:51", True),
        ("193af17", "2026-09-29 08:01", True),
        ("8374672", "2026-09-29 12:01", True),
        ("d5c657b", "2026-09-30 19:50", True),
        ("1c0863a", "2026-10-01 08:01", True),
        ("b84f6b6", "2026-10-01 12:01", True),
        ("c75092d", "2026-10-01 19:43", False),
        ("b19ab1d", "2026-10-02 08:01", False),
        ("ce0b094", "2026-10-02 19:43", False),
    ]

    def show(self, commit, name):
        repo = os.environ["PUBLISHED_HISTORY_REPO"]
        return json.loads(subprocess.run(["git", "-C", repo, "show", f"{commit}:data/latest/{name}"],
                                         capture_output=True, text=True, check=True).stdout)

    def test_replay(self):
        f = Fixture()
        f.calendar.write_text(Path(os.environ["REAL_TRADING_CALENDAR"]).read_text(encoding="utf-8"))
        try:
            f.write({n: self.show("44b31a7", n) for n in fg.REQUIRED_LATEST})
            f.commit("20260924 published")
            m = self.show("44b31a7", "run_manifest.json")
            f.receipt(m["run_id"], m["trade_date"])
            outcomes = []
            for commit, when, expect_pass in self.SEQUENCE:
                f.write({n: self.show(commit, n) for n in fg.REQUIRED_LATEST})
                # The only production calendar available was generated on Oct 2;
                # evaluate as of just after that snapshot (validity window).
                result = fg.evaluate(f.repo, f.health, calendar_path=f.calendar,
                                     now=datetime(2026, 10, 2, 23, 59))
                outcomes.append((commit, result["mode"], result["passed"], result["blockers"]))
                self.assertEqual(result["passed"], expect_pass, (commit, result["mode"], result["blockers"]))
                if result["passed"]:
                    f.commit(commit)
                    f.receipt(result["batch"]["run_id"], result["batch"]["trade_date"])
                else:
                    git(f.repo, "checkout", "--", ".")  # blocked content is never committed
            self.__class__.outcomes = outcomes
            print("\nREPLAY", json.dumps(outcomes, ensure_ascii=False))
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
