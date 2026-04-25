"""Tests for signals.ibs + apply_ibs_filter (PR3)."""
import unittest

from bot import signals


class IBSCalculationTests(unittest.TestCase):
    def test_close_at_low(self):
        self.assertEqual(signals.ibs(10, 5, 5), 0.0)

    def test_close_at_high(self):
        self.assertEqual(signals.ibs(10, 5, 10), 1.0)

    def test_midpoint(self):
        self.assertEqual(signals.ibs(10, 5, 7.5), 0.5)

    def test_three_quarters(self):
        self.assertAlmostEqual(signals.ibs(100, 90, 97.5), 0.75)

    def test_one_quarter(self):
        self.assertAlmostEqual(signals.ibs(100, 90, 92.5), 0.25)

    def test_zero_range_returns_none(self):
        self.assertIsNone(signals.ibs(10, 10, 10))

    def test_inverted_range_returns_none(self):
        # Degenerate: high < low shouldn't happen in real bars but the
        # helper should refuse rather than return something misleading.
        self.assertIsNone(signals.ibs(5, 10, 7))

    def test_clamps_above_one(self):
        # Close above high is broken data; clamp at 1.0 rather than return >1.
        self.assertEqual(signals.ibs(10, 5, 15), 1.0)

    def test_clamps_below_zero(self):
        self.assertEqual(signals.ibs(10, 5, 0), 0.0)


class IBSLastTests(unittest.TestCase):
    def test_last_bar(self):
        highs = [100, 101, 102]
        lows = [90, 91, 92]
        closes = [95, 96, 92]  # close at low of last bar
        self.assertEqual(signals.ibs_last(highs, lows, closes), 0.0)

    def test_empty_returns_none(self):
        self.assertIsNone(signals.ibs_last([], [], []))


class ApplyIBSFilterTests(unittest.TestCase):
    def test_flag_off_always_passes(self):
        cfg = {"IBS_FILTER_ENABLED": False}
        slot = {"ibs_max": 0.4}
        payload = {"ibs": 0.9}  # way over threshold
        self.assertIsNone(signals.apply_ibs_filter(slot, payload, cfg))

    def test_flag_on_rejects_above_threshold(self):
        cfg = {"IBS_FILTER_ENABLED": True}
        slot = {"ibs_max": 0.4}
        payload = {"ibs": 0.45}
        reason = signals.apply_ibs_filter(slot, payload, cfg)
        self.assertEqual(reason, "ibs_filter:ibs>0.4")

    def test_flag_on_passes_at_threshold(self):
        cfg = {"IBS_FILTER_ENABLED": True}
        slot = {"ibs_max": 0.4}
        payload = {"ibs": 0.4}
        self.assertIsNone(signals.apply_ibs_filter(slot, payload, cfg))

    def test_flag_on_passes_below_threshold(self):
        cfg = {"IBS_FILTER_ENABLED": True}
        slot = {"ibs_max": 0.4}
        payload = {"ibs": 0.1}
        self.assertIsNone(signals.apply_ibs_filter(slot, payload, cfg))

    def test_missing_ibs_max_passes(self):
        cfg = {"IBS_FILTER_ENABLED": True}
        slot = {"ibs_max": None}
        payload = {"ibs": 0.9}
        self.assertIsNone(signals.apply_ibs_filter(slot, payload, cfg))

    def test_missing_ibs_value_passes(self):
        # Payload has no IBS (zero-range bar, thin market) → let through
        # so insufficient-data doesn't silently block trading.
        cfg = {"IBS_FILTER_ENABLED": True}
        slot = {"ibs_max": 0.4}
        payload = {}
        self.assertIsNone(signals.apply_ibs_filter(slot, payload, cfg))


class ScorePayloadIncludesIBSTests(unittest.TestCase):
    def test_score_populates_ibs_in_payload(self):
        # Provide enough OHLC to satisfy ATR + IBS. 30 bars with clear IBS
        # on the last bar (close near low).
        closes = [100.0] * 30
        highs = [100.5] * 30
        lows = [99.5] * 30
        closes[-1] = 99.6
        highs[-1] = 100.8
        lows[-1] = 99.4
        _, payload = signals.score(closes, highs=highs, lows=lows,
                                     volumes=[1000] * 30)
        self.assertIn("ibs", payload)
        # close 99.6, low 99.4, high 100.8 → (99.6-99.4)/(100.8-99.4)
        #   = 0.2/1.4 ≈ 0.143
        self.assertAlmostEqual(payload["ibs"], 0.1429, places=3)


if __name__ == "__main__":
    unittest.main()
