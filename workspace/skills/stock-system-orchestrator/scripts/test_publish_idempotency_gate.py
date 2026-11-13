#!/usr/bin/env python3
"""publish_idempotency_gate 单元测试。

覆盖：
1. 指纹稳定性：波动字段变化 → 指纹不变
2. 指纹敏感性：语义内容变化 → 指纹变化
3. 判定逻辑：新交易日放行 / 同日同指纹已推送跳过 / 上次未推送放行 / 内容变化放行
4. strip_volatile：嵌套 dict/list、commit sha 剔除
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import publish_idempotency_gate as gate


def make_latest(tmp: Path, verdict_action: str = "observe_only") -> Path:
    latest = tmp / "latest"
    latest.mkdir(parents=True)
    (latest / "system_verdict.json").write_text(json.dumps({
        "generated_at": "2026-08-21T20:22:33+08:00",
        "final_action": {"action": verdict_action, "label": "谨慎执行"},
    }))
    (latest / "run_manifest.json").write_text(json.dumps({
        "trade_date": "20260821", "generated_at": "2026-08-21 20:22:33", "run_id": "r1",
    }))
    (latest / "candidate_state.json").write_text(json.dumps({"candidates": [{"code": "600000"}]}))
    return latest


class FingerprintTests(unittest.TestCase):
    def test_volatile_fields_do_not_change_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = make_latest(Path(tmp))
            fp1 = gate.compute_fingerprint(latest)
            # 只改波动字段
            doc = json.loads((latest / "run_manifest.json").read_text())
            doc["generated_at"] = "2026-08-22 09:00:00"
            doc["run_id"] = "r999"
            (latest / "run_manifest.json").write_text(json.dumps(doc))
            doc2 = json.loads((latest / "system_verdict.json").read_text())
            doc2["generated_at"] = "2026-08-22T09:00:00+08:00"
            (latest / "system_verdict.json").write_text(json.dumps(doc2))
            fp2 = gate.compute_fingerprint(latest)
            self.assertEqual(fp1, fp2, "波动字段变化不应改变指纹")

    def test_semantic_change_alters_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = make_latest(Path(tmp))
            fp1 = gate.compute_fingerprint(latest)
            doc = json.loads((latest / "system_verdict.json").read_text())
            doc["final_action"]["action"] = "execute"
            (latest / "system_verdict.json").write_text(json.dumps(doc))
            fp2 = gate.compute_fingerprint(latest)
            self.assertNotEqual(fp1, fp2, "语义内容变化必须改变指纹")

    def test_strip_volatile_removes_nested_and_sha(self):
        doc = {
            "generated_at": "x",
            "nested": {"updated_at": "y", "keep": 1},
            "items": [{"recorded_at": "z", "v": 2}],
            "sha_like": "a" * 40,
        }
        out = gate.strip_volatile(doc)
        self.assertNotIn("generated_at", out)
        self.assertNotIn("updated_at", out["nested"])
        self.assertNotIn("recorded_at", out["items"][0])
        self.assertEqual(out["sha_like"], "<sha-stripped>")
        self.assertEqual(out["nested"]["keep"], 1)


class GateDecisionTests(unittest.TestCase):
    def _state(self, tmp, trade_date, fingerprint, pushed):
        p = Path(tmp) / "state.json"
        p.write_text(json.dumps({"trade_date": trade_date, "fingerprint": fingerprint, "pushed": pushed}))
        return p

    def test_same_day_same_fp_pushed_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp, "20260821", "abc123", True)
            proceed, reason = gate.check("20260821", "abc123", st)
            self.assertFalse(proceed)
            self.assertIn("duplicate publish skipped", reason)

    def test_new_trade_date_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp, "20260820", "abc123", True)
            proceed, _ = gate.check("20260821", "abc123", st)
            self.assertTrue(proceed)

    def test_prev_not_pushed_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp, "20260821", "abc123", False)
            proceed, reason = gate.check("20260821", "abc123", st)
            self.assertTrue(proceed)
            self.assertIn("never completed", reason)

    def test_content_changed_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._state(tmp, "20260821", "old_fp", True)
            proceed, reason = gate.check("20260821", "new_fp", st)
            self.assertTrue(proceed)
            self.assertIn("content changed", reason)

    def test_no_state_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            proceed, _ = gate.check("20260821", "abc123", Path(tmp) / "missing.json")
            self.assertTrue(proceed)

    def test_mark_pushed_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = make_latest(Path(tmp))
            st = Path(tmp) / "state.json"
            fp = gate.compute_fingerprint(latest)
            assert fp is not None
            gate.mark_pushed("20260821", fp, st)
            proceed, reason = gate.check("20260821", fp, st)
            self.assertFalse(proceed)
            # 换内容后放行
            doc = json.loads((latest / "candidate_state.json").read_text())
            doc["candidates"].append({"code": "000001"})
            (latest / "candidate_state.json").write_text(json.dumps(doc))
            fp2 = gate.compute_fingerprint(latest)
            proceed2, _ = gate.check("20260821", fp2, st)
            self.assertTrue(proceed2)


class PublisherShellContractTests(unittest.TestCase):
    def test_trade_date_is_resolved_before_idempotency_check(self):
        publisher = Path(__file__).with_name("publish_stock_report_v2.sh")
        text = publisher.read_text(encoding="utf-8")
        manifest_pos = text.index('MANIFEST_FILE="$PUB_REPO/data/latest/run_manifest.json"')
        gate_pos = text.index('publish_idempotency_gate.py" --check')
        self.assertLess(manifest_pos, gate_pos)
        self.assertEqual(text.count('TRADE_DATE="$("$PY" -c'), 1)
        self.assertEqual(text.count('RUN_ID="$("$PY" -c'), 1)

    def test_duplicate_skip_writes_receipt_for_current_run(self):
        publisher = Path(__file__).with_name("publish_stock_report_v2.sh")
        text = publisher.read_text(encoding="utf-8")
        start = text.index('if [ "$RC" -eq 7 ]')
        end = text.index('  log "WARN: 幂等闸内部错误', start)
        skip_block = text[start:end]
        self.assertIn("write_deployment_receipt", skip_block)
        self.assertIn("verify_remote_alignment", skip_block)


if __name__ == "__main__":
    unittest.main()
