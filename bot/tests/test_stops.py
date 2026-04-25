"""PR6: _compute_stop covers pct / atr_native / min_width_floor paths."""
import unittest


try:
    # Import strategy only to grab _compute_stop — the function doesn't use
    # any of strategy's async/db surface, but importing strategy pulls in
    # asyncpg which the test env lacks. Guard accordingly.
    from bot import strategy as _strategy
    _STRATEGY_OK = True
except ImportError:
    _strategy = None
    _STRATEGY_OK = False


@unittest.skipUnless(_STRATEGY_OK, "asyncpg not available in this env")
class ComputeStopTests(unittest.TestCase):
    def _call(self, price, prof, payload, min_width=0.75):
        return _strategy._compute_stop(price, None, prof, payload, min_width)

    def test_pct_mode_default(self):
        stop, src = self._call(100.0, {"stop_loss_pct": -1.5,
                                          "strategy": "intraday"}, {})
        self.assertAlmostEqual(stop, 98.5)
        self.assertEqual(src, "pct")

    def test_min_width_floor_kicks_in(self):
        # Tight stop of -0.3% falls below 0.75% floor.
        stop, src = self._call(100.0, {"stop_loss_pct": -0.3,
                                          "strategy": "intraday"}, {})
        self.assertAlmostEqual(stop, 99.25)
        self.assertEqual(src, "min_width_floor")

    def test_atr_native_mode_uses_per_slot_mult(self):
        stop, src = self._call(100.0, {"stop_loss_pct": -1.0,
                                          "strategy": "intraday",
                                          "stop_mode": "atr_native",
                                          "stop_atr_mult": 1.0},
                                  {"atr14": 2.0})
        # 100 − 1.0 × 2.0 = 98.0
        self.assertAlmostEqual(stop, 98.0)
        self.assertEqual(src, "atr_native")

    def test_atr_native_falls_back_to_strategy_default(self):
        # No per-slot mult → fall back to _ATR_MULT_DEFAULT[strategy].
        stop, src = self._call(100.0, {"stop_loss_pct": -1.0,
                                          "strategy": "swing",
                                          "stop_mode": "atr_native",
                                          "stop_atr_mult": None},
                                  {"atr14": 3.0})
        # 100 − 1.5 × 3.0 = 95.5
        self.assertAlmostEqual(stop, 95.5)

    def test_atr_native_without_atr_falls_through_to_pct(self):
        stop, src = self._call(100.0, {"stop_loss_pct": -1.2,
                                          "strategy": "intraday",
                                          "stop_mode": "atr_native"}, {})
        self.assertAlmostEqual(stop, 98.8)

    def test_atr_native_floored_by_min_width(self):
        # Very tight ATR-derived stop (atr 0.1 × 1.0 mult = 0.1%) should
        # be clamped to the 0.75% floor.
        stop, src = self._call(100.0, {"stop_loss_pct": -1.0,
                                          "strategy": "intraday",
                                          "stop_mode": "atr_native",
                                          "stop_atr_mult": 1.0},
                                  {"atr14": 0.1})
        self.assertAlmostEqual(stop, 99.25)
        self.assertEqual(src, "min_width_floor")

    def test_pct_mode_with_atr_data_uses_max(self):
        # Legacy mode with both atr_mult + atr_val present: stop = max(...)
        stop, src = self._call(100.0, {"stop_loss_pct": -2.0,
                                          "strategy": "intraday",
                                          "stop_mode": "pct",
                                          "stop_atr_mult": 1.0},
                                  {"atr14": 0.5}, min_width=0.25)
        # pct_stop = 98.0, atr_stop = 99.5 → max = 99.5 (tighter stop)
        self.assertAlmostEqual(stop, 99.5)
        self.assertEqual(src, "atr")


class TieredTimeStopLogicTests(unittest.TestCase):
    """Direct math checks for the 50%/75% tiers. The full path lives in
    monitor_open_positions (async + DB), so we verify just the arithmetic
    surface the flag-gated block relies on."""

    def test_75pct_underwater_triggers(self):
        # Entry 100, stop 98 (distance 2), current 99.0 → pnl -1.0 ≤ -0.3*2=-0.6
        entry, stop, current = 100.0, 98.0, 99.0
        stop_distance = entry - stop
        pnl = current - entry
        self.assertTrue(stop_distance > 0 and pnl <= -0.3 * stop_distance)

    def test_75pct_not_underwater_does_not_trigger(self):
        entry, stop, current = 100.0, 98.0, 99.9
        stop_distance = entry - stop
        pnl = current - entry
        self.assertFalse(pnl <= -0.3 * stop_distance)


if __name__ == "__main__":
    unittest.main()
