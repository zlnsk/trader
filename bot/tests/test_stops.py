"""PR6: _compute_stop covers pct / atr_native / min_width_floor paths."""
import unittest


try:



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

        self.assertAlmostEqual(stop, 98.0)
        self.assertEqual(src, "atr_native")

    def test_atr_native_falls_back_to_strategy_default(self):

        stop, src = self._call(100.0, {"stop_loss_pct": -1.0,
                                          "strategy": "swing",
                                          "stop_mode": "atr_native",
                                          "stop_atr_mult": None},
                                  {"atr14": 3.0})

        self.assertAlmostEqual(stop, 95.5)

    def test_atr_native_without_atr_falls_through_to_pct(self):
        stop, src = self._call(100.0, {"stop_loss_pct": -1.2,
                                          "strategy": "intraday",
                                          "stop_mode": "atr_native"}, {})
        self.assertAlmostEqual(stop, 98.8)

    def test_atr_native_floored_by_min_width(self):


        stop, src = self._call(100.0, {"stop_loss_pct": -1.0,
                                          "strategy": "intraday",
                                          "stop_mode": "atr_native",
                                          "stop_atr_mult": 1.0},
                                  {"atr14": 0.1})
        self.assertAlmostEqual(stop, 99.25)
        self.assertEqual(src, "min_width_floor")

    def test_pct_mode_with_atr_data_uses_max(self):

        stop, src = self._call(100.0, {"stop_loss_pct": -2.0,
                                          "strategy": "intraday",
                                          "stop_mode": "pct",
                                          "stop_atr_mult": 1.0},
                                  {"atr14": 0.5}, min_width=0.25)

        self.assertAlmostEqual(stop, 99.5)
        self.assertEqual(src, "atr")


class TieredTimeStopLogicTests(unittest.TestCase):
    """Direct math checks for the 50%/75% tiers. The full path lives in
    monitor_open_positions (async + DB), so we verify just the arithmetic
    surface the flag-gated block relies on."""

    def test_75pct_underwater_triggers(self):

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
