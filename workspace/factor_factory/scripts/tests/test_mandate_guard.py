from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mandate_guard as mg  # noqa: E402

ALLOWED = ["600519.SH", "000001.SZ", "300750.SZ", "301001.SZ"]
REJECTED = [
    "600000.SZ", "000001.SH", "300001.SH", "688001.SH", "689009.SH", "430047.BJ", "830799.BJ",
    "920001.BJ", "", "600519", "600519.sh", "600519.SH ", " 600519.SH", "6005190.SH", "60051.SH",
    "200001.SZ", "900901.SH", "002001.sz", "None", None, 600519, 1.5, b"600519.SH", ["600519.SH"],
    # Trailing / leading newlines and non-ASCII Unicode digits
    "600519.SH\n", "000001.SZ\n", "300750.SZ\r\n", "\n600519.SH",
    "60０519.SH", "６００５１９.SH", "00000١.SZ", "300750．SZ",
]


class MandateGuardTests(unittest.TestCase):
    def test_allowed_codes(self):
        for code in ALLOWED:
            self.assertTrue(mg.is_allowed_mandate_code(code), code)

    def test_rejected_codes(self):
        for code in REJECTED:
            self.assertFalse(mg.is_allowed_mandate_code(code), repr(code))

    def test_board_mask_is_numpy_bool_in_order(self):
        mask = mg.is_allowed_mandate_board(
            ALLOWED[:2] + ["688001.SH", "600519.SH\n", "60０519.SH"] + ALLOWED[2:] + [None, "430047.BJ"]
        )
        self.assertIsInstance(mask, np.ndarray)
        self.assertEqual(mask.dtype, bool)
        self.assertEqual(mask.tolist(), [True, True, False, False, False, True, True, False, False])
        self.assertEqual(mg.is_allowed_mandate_board([]).shape, (0,))
        self.assertEqual(mg.is_allowed_mandate_board(np.array(ALLOWED, dtype=object)).all(), True)

    def test_assert_passes_when_clean(self):
        mg.assert_strict_mandate(ALLOWED)
        mg.assert_strict_mandate([])

    def test_assert_lists_every_offender(self):
        with self.assertRaises(AssertionError) as ctx:
            mg.assert_strict_mandate(ALLOWED + ["688001.SH", "430047.BJ", None])
        msg = str(ctx.exception)
        for token in ("688001.SH", "430047.BJ", "None", "3/7"):
            self.assertIn(token, msg)
        self.assertNotIn("600519.SH", msg)

    def test_assert_fails_closed_on_malformed(self):
        for bad in ([""], ["600519.sh"], [None], [600519], ["600519.SH\n"], ["60０519.SH"]):
            with self.assertRaises(AssertionError):
                mg.assert_strict_mandate(bad)


if __name__ == "__main__":
    unittest.main()
