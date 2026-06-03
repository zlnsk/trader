"""Per-currency MOC window resolution.

Previously the bot hardcoded `10 <= mtc <= 20` in strategy.py:681 for all
venues. Euronext MOC deadlines are tighter; the helper reads per-currency
config keys and falls back to sane defaults. These tests pin the defaults and
the config override so a future key rename is caught at test time, not in a
silent no-MOC-route regression.
"""
from __future__ import annotations

import unittest

from bot import hours


class MocWindowTest(unittest.TestCase):
    def test_usd_default(self):
        self.assertEqual(hours.moc_window_for_currency("USD", None), (10, 20))

    def test_eur_default_is_tighter(self):
        lo, hi = hours.moc_window_for_currency("EUR", None)
        self.assertLess(hi, 20)
        self.assertLessEqual(lo, hi)

    def test_config_overrides_usd(self):
        cfg = {
            "MOC_WINDOW_MIN_MINUTES_USD": 8,
            "MOC_WINDOW_MAX_MINUTES_USD": 18,
        }
        self.assertEqual(hours.moc_window_for_currency("USD", cfg), (8, 18))

    def test_config_overrides_eu(self):
        cfg = {
            "MOC_WINDOW_MIN_MINUTES_EU": 3,
            "MOC_WINDOW_MAX_MINUTES_EU": 12,
        }
        self.assertEqual(hours.moc_window_for_currency("GBP", cfg), (3, 12))

    def test_invalid_config_falls_back_to_defaults(self):
        cfg = {"MOC_WINDOW_MIN_MINUTES_USD": "abc"}
        self.assertEqual(hours.moc_window_for_currency("USD", cfg), (10, 20))

    def test_negative_window_falls_back(self):
        cfg = {"MOC_WINDOW_MIN_MINUTES_USD": -1, "MOC_WINDOW_MAX_MINUTES_USD": 20}
        self.assertEqual(hours.moc_window_for_currency("USD", cfg), (10, 20))

    def test_inverted_window_falls_back(self):
        cfg = {"MOC_WINDOW_MIN_MINUTES_USD": 20, "MOC_WINDOW_MAX_MINUTES_USD": 10}
        self.assertEqual(hours.moc_window_for_currency("USD", cfg), (10, 20))


if __name__ == "__main__":
    unittest.main()
