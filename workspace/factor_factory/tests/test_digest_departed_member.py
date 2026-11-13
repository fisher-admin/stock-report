#!/usr/bin/env python3
"""forward_shadow_digest must survive a held buffered_top100 member that is absent from today's scores.

Regression for 2026-10-16: 300231.SZ was held from the 20261014 rebalance, slid under the 60d-amount median on
20261016 and dropped out of the universe; `comp.loc[list(top100)]` raised KeyError, no digest and no tier_ledger row.
Reads the real 20261016 scores/books but never writes: tier_monitor.record_day is mocked.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import forward_shadow_digest as dg  # noqa: E402

D = "20261016"
GHOST = "ZZZ999.SZ"


class DepartedMemberTests(unittest.TestCase):
    def test_build_tolerates_unscored_held_member(self):
        real = dg.read_jsonl

        def with_ghost(p):
            rows = real(p)
            if Path(p).name == "books.jsonl":
                for r in rows:
                    if r.get("is_rebalance"):
                        r["buffered_top100"] = list(r["buffered_top100"]) + [GHOST]
            return rows

        with mock.patch.object(dg, "read_jsonl", with_ghost), \
                mock.patch.object(dg.tm, "record_day", lambda *a, **k: "mocked"):
            md, _ = dg.build(D)
        self.assertIn(GHOST, md)
        self.assertIn("not scored today", md)
        self.assertIn("buffered_top100 leaders", md)


if __name__ == "__main__":
    unittest.main()
