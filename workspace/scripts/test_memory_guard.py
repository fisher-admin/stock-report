#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import memory_guard as guard  # noqa: E402

GiB = 1 << 30


def proc(pid: int, pgid: int, gib: float, name: str, args: str) -> dict:
    return {"pid": pid, "pgid": pgid, "rss": int(gib * GiB), "name": name, "args": args}


class SampleTests(unittest.TestCase):
    def test_parse_swap_free(self) -> None:
        text = "total = 3072.00M  used = 1817.00M  free = 1255.00M  (encrypted)"
        self.assertEqual(guard.parse_swap_free(text), int(1255.0 * (1 << 20)))
        self.assertIsNone(guard.parse_swap_free(""))

    def test_critical_levels(self) -> None:
        self.assertTrue(guard.is_critical({"pressure_level": 4, "swap_free": None}))
        self.assertTrue(guard.is_critical({"pressure_level": 2, "swap_free": 100 << 20}))
        self.assertFalse(guard.is_critical({"pressure_level": 2, "swap_free": 2 * GiB}))
        self.assertFalse(guard.is_critical({"pressure_level": 1, "swap_free": 0}))


class VictimTests(unittest.TestCase):
    # Shape of the 2026-10-26 panic: a render pool plus ffmpeg pipes, next to
    # the stock system and Hermes.
    PROCS = [
        proc(100, 100, 3.0, "Python", "/opt/homebrew/bin/python3 render_par.py real"),
        proc(101, 100, 1.5, "Python", "/opt/homebrew/bin/python3 render_par.py real"),
        proc(102, 100, 0.5, "ffmpeg", "ffmpeg -f rawvideo -i -"),
        proc(200, 200, 6.0, "python3.14", "/Users/fisher/.openclaw/venv/bin/python run_pipeline.py"),
        proc(300, 300, 4.0, "python3", "/Users/fisher/.hermes/hermes-agent/venv/bin/python gateway run"),
        proc(400, 400, 8.0, "Google Chrome Helper", "/Applications/Google Chrome.app/..."),
        proc(500, 500, 0.2, "Python", "python3 small.py"),
    ]

    def test_largest_unprotected_group_is_chosen(self) -> None:
        victim = guard.choose_victim(self.PROCS, self_pid=999)
        self.assertEqual(victim["pgid"], 100)
        self.assertEqual(sorted(p["pid"] for p in victim["members"]), [100, 101, 102])

    def test_stock_system_hermes_and_self_are_protected(self) -> None:
        protected = [p for p in self.PROCS if p["pgid"] in (200, 300)]
        self.assertIsNone(guard.choose_victim(protected, self_pid=999))
        me = [proc(42, 42, 5.0, "Python", "/usr/bin/python3 -I /x/scripts/memory_guard.py")]
        self.assertIsNone(guard.choose_victim(me, self_pid=1))

    def test_small_groups_are_left_alone(self) -> None:
        self.assertIsNone(guard.choose_victim([self.PROCS[-1]], self_pid=999))


if __name__ == "__main__":
    unittest.main()
