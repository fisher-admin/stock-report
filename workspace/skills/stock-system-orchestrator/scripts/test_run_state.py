import tempfile
import unittest
from pathlib import Path

from deployment_receipt import DeploymentReceipt
from run_state import InvalidTransition, RunState


class RunStateAndReceiptTests(unittest.TestCase):
    def test_state_cannot_skip_validation(self):
        state = RunState.new("stock-1", "20260820")
        with self.assertRaises(InvalidTransition):
            state.advance("pushed")

    def test_valid_state_transitions(self):
        state = RunState.new("stock-1", "20260820")
        state.advance("data_ready")
        state.advance("selection_ready")
        state.advance("ai_ready")
        state.advance("validated")
        state.advance("publish_prepared")
        state.advance("committed")
        state.advance("pushed")
        state.advance("remote_confirmed")
        state.advance("delivered")
        self.assertEqual(state.state, "delivered")

    def test_receipt_must_match_run_date_and_sha(self):
        receipt = DeploymentReceipt(
            run_id="stock-1",
            trade_date="20260820",
            local_sha="abc1234",
            remote_sha="abc1234",
            remote_confirmed=True,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        self.assertTrue(receipt.matches("stock-1", "20260820"))
        self.assertFalse(receipt.matches("stock-2", "20260820"))
        self.assertFalse(receipt.matches("stock-1", "20260821"))

    def test_receipt_rejects_unconfirmed_or_mismatched_sha(self):
        receipt = DeploymentReceipt(
            run_id="stock-1",
            trade_date="20260820",
            local_sha="abc1234",
            remote_sha="def5678",
            remote_confirmed=False,
            confirmed_at="2026-08-20T20:00:00+08:00",
        )
        self.assertFalse(receipt.matches("stock-1", "20260820"))

    def test_state_atomic_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "health" / "run_state.json"
            state = RunState.new("stock-99", "20260820")
            state.advance("data_ready")
            state.save(file_path)

            loaded = RunState.load(file_path)
            self.assertEqual(loaded.run_id, "stock-99")
            self.assertEqual(loaded.trade_date, "20260820")
            self.assertEqual(loaded.state, "data_ready")


if __name__ == "__main__":
    unittest.main()
