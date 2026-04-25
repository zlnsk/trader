"""Tests for trend_ok, trend_ok_v2, uptrend_50_200_ok (PR2)."""
import unittest

from bot import signals


def _series(sma_target: float, last_close: float, length: int = 210,
             linear_head: bool = False) -> list[float]:
    """Build a closes list whose SMA200 ≈ `sma_target` and last element
    equals `last_close`. Uses a constant-plateau shape for determinism.
    """
    base = [sma_target] * (length - 1)
    return base + [last_close]


class TrendOkV1Tests(unittest.TestCase):
    def test_close_above_sma200_passes(self):
        closes = _series(sma_target=100.0, last_close=100.0)
        self.assertTrue(signals.trend_ok(closes))

    def test_close_within_tolerance_passes(self):
        # 5% below SMA200 is on the boundary — default tolerance=-5.
        closes = _series(sma_target=100.0, last_close=95.0)
        self.assertTrue(signals.trend_ok(closes))

    def test_close_below_tolerance_rejects(self):
        closes = _series(sma_target=100.0, last_close=94.0)
        self.assertFalse(signals.trend_ok(closes))

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(signals.trend_ok([100.0] * 50))


class TrendOkV2Tests(unittest.TestCase):
    def test_v2_tighter_than_v1(self):
        # Price at 95% of SMA200 passes v1 but fails v2 (-2% tolerance).
        closes = _series(sma_target=100.0, last_close=95.0)
        self.assertTrue(signals.trend_ok(closes))
        self.assertFalse(signals.trend_ok_v2(closes))

    def test_v2_accepts_small_dip(self):
        closes = _series(sma_target=100.0, last_close=98.01)
        self.assertTrue(signals.trend_ok_v2(closes))

    def test_v2_rejects_boundary_below(self):
        closes = _series(sma_target=100.0, last_close=97.99)
        self.assertFalse(signals.trend_ok_v2(closes))

    def test_v2_custom_tolerance(self):
        closes = _series(sma_target=100.0, last_close=96.0)
        # -5% tolerance → pass
        self.assertTrue(signals.trend_ok_v2(closes, tolerance_pct_v2=-5.0))
        # -3% tolerance → fail
        self.assertFalse(signals.trend_ok_v2(closes, tolerance_pct_v2=-3.0))


class Uptrend50200Tests(unittest.TestCase):
    def test_bullish_golden_cross_passes(self):
        # Steady uptrend: last closes high enough to drag SMA50 > SMA200.
        closes = [100.0] * 150 + [110.0] * 50 + [115.0]
        self.assertTrue(signals.uptrend_50_200_ok(closes))

    def test_bearish_death_cross_rejects(self):
        closes = [100.0] * 150 + [90.0] * 50 + [85.0]
        self.assertFalse(signals.uptrend_50_200_ok(closes))

    def test_close_below_sma200_rejects(self):
        # SMA50 could still be above SMA200 but close below both.
        closes = [100.0] * 150 + [110.0] * 50 + [95.0]
        self.assertFalse(signals.uptrend_50_200_ok(closes))

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(signals.uptrend_50_200_ok([100.0] * 100))


class ApplyTrendFilterDispatchTests(unittest.TestCase):
    """Verify strategy._apply_trend_filter routes v1/v2 correctly."""

    def _import(self):
        return signals.apply_trend_filter

    def test_v1_path_rejects_below_threshold(self):
        apply = self._import()
        closes = _series(sma_target=100.0, last_close=90.0)
        reason = apply(closes, {"trend_filter_enabled": True},
                        {"TREND_FILTER_V2_ENABLED": False}, 200, -5.0)
        self.assertEqual(reason, "trend_filter:below_sma200")

    def test_v1_path_passes(self):
        apply = self._import()
        closes = _series(sma_target=100.0, last_close=100.0)
        self.assertIsNone(apply(closes, {"trend_filter_enabled": True},
                                 {"TREND_FILTER_V2_ENABLED": False}, 200, -5.0))

    def test_v2_path_rejects_with_v2_reason(self):
        apply = self._import()
        closes = _series(sma_target=100.0, last_close=95.0)
        reason = apply(closes, {"trend_filter_enabled": True},
                        {"TREND_FILTER_V2_ENABLED": True,
                         "TREND_TOLERANCE_PCT_V2": -2.0}, 200, -5.0)
        self.assertEqual(reason, "trend_filter_v2:below_sma200")

    def test_v2_golden_cross_path_rejects(self):
        apply = self._import()
        closes = [100.0] * 150 + [90.0] * 50 + [85.0]
        reason = apply(closes, {"trend_filter_enabled": True,
                                  "require_uptrend_50_200": True},
                        {"TREND_FILTER_V2_ENABLED": True,
                         "TREND_TOLERANCE_PCT_V2": -2.0}, 200, -5.0)
        self.assertEqual(reason, "trend_filter_v2:bearish_50_200")

    def test_v2_golden_cross_path_passes(self):
        apply = self._import()
        closes = [100.0] * 150 + [110.0] * 50 + [115.0]
        self.assertIsNone(apply(closes,
                                 {"trend_filter_enabled": True,
                                  "require_uptrend_50_200": True},
                                 {"TREND_FILTER_V2_ENABLED": True,
                                  "TREND_TOLERANCE_PCT_V2": -2.0}, 200, -5.0))


if __name__ == "__main__":
    unittest.main()
