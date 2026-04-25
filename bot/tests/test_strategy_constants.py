"""Unit tests for bot/strategies/constants.py.

Guards the slot → strategy mapping used as a fallback when slot_profiles
isn't available (e.g. _log_signal resolving strategy from slot alone).
If the DB's slot_profiles ranges ever drift from this module, either this
test or a migration should be updated — not both forgotten.
"""
from __future__ import annotations

import unittest

from bot.strategies import constants as c


class ForSlotTest(unittest.TestCase):
    def test_swing_slots(self):
        for slot in range(1, 10):
            self.assertEqual(c.for_slot(slot), c.MEAN_REV, slot)

    def test_intraday_slots(self):
        for slot in range(10, 19):
            self.assertEqual(c.for_slot(slot), c.INTRADAY, slot)

    def test_crypto_scalp_slots(self):
        for slot in range(19, 25):
            self.assertEqual(c.for_slot(slot), c.CRYPTO_SCALP, slot)

    def test_overnight_slots(self):
        for slot in range(25, 30):
            self.assertEqual(c.for_slot(slot), c.OVERNIGHT, slot)

    def test_none_slot_returns_unknown(self):
        self.assertEqual(c.for_slot(None), c.UNKNOWN)

    def test_out_of_range_returns_unknown(self):
        self.assertEqual(c.for_slot(0), c.UNKNOWN)
        self.assertEqual(c.for_slot(100), c.UNKNOWN)

    def test_all_tuple_covers_every_mapped_range(self):
        produced = {c.for_slot(s) for s in range(1, 30)}
        self.assertTrue(produced.issubset(set(c.ALL)))
        # Every canonical tag should be reachable from some slot.
        self.assertEqual(produced, set(c.ALL))


if __name__ == "__main__":
    unittest.main()
