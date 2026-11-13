#!/usr/bin/env python3
"""Strict contract after maintenance_mode.apply (pre-push hook, 20261009)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import validate_publication_contract as contract  # noqa: E402
import test_validate_publication_contract as base  # noqa: E402

MARKER = {"enabled": True, "title": "系统校准升级中"}


def run(latest: Path) -> tuple[int, str]:
    output = io.StringIO()
    with mock.patch.object(contract, "LATEST", latest), mock.patch.object(
        sys, "argv", ["validate_publication_contract.py", "--strict"]
    ), contextlib.redirect_stdout(output):
        result = contract.main()
    return result, output.getvalue()


def withhold(latest: Path, *, manifest=True, candidates=True, keep_one=False) -> None:
    shadow = json.loads((latest / "prebreakout_shadow_watch.json").read_text(encoding="utf-8"))
    for row in shadow["short_track_strategies"]:
        kept = row["candidates"][:1] if keep_one else []
        row["candidates"], row["candidate_count"] = kept, len(kept)
        row.setdefault("status", "present")  # as in real published rows
    base._write(latest, "prebreakout_shadow_watch.json", shadow)
    base._write(latest, "run_manifest.json", {"maintenance": MARKER} if manifest else {})
    base._write(latest, "candidate_state.json", {"candidates": [], "maintenance": MARKER} if candidates else {"candidates": []})


class MaintenanceContractTests(unittest.TestCase):
    def test_fully_withheld_lists_pass_under_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = Path(tmp)
            base._base_latest(latest)
            withhold(latest)
            result, output = run(latest)
        self.assertEqual(result, 0, output)
        self.assertIn("维护模式", output)

    def test_partially_withheld_lists_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = Path(tmp)
            base._base_latest(latest)
            withhold(latest, keep_one=True)
            result, output = run(latest)
        self.assertNotEqual(result, 0, output)
        self.assertIn("维护模式下名单必须完全撤下", output)

    def test_empty_lists_without_both_markers_still_fail(self):
        for flags in ({"manifest": False}, {"candidates": False}):
            with tempfile.TemporaryDirectory() as tmp:
                latest = Path(tmp)
                base._base_latest(latest)
                withhold(latest, **flags)
                result, output = run(latest)
            self.assertNotEqual(result, 0, (flags, output))
            self.assertIn("候选数必须为 20", output)


if __name__ == "__main__":
    unittest.main()
