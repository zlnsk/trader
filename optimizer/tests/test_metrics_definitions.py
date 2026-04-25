"""Pure-function tests for metric formulas. No DB, no network."""
import math
import unittest

from optimizer.metrics.definitions import compute_slot_metrics


def _trade(pct: float, eur: float = None, fees: float = 0.5, hold: int = 3600):
    if eur is None:
        eur = pct * 10.0  # slot_size 1000 -> 1% = 10 eur
    gross = eur + fees
    return {
        "net_pnl_pct": pct, "net_pnl_eur": eur, "gross_pnl_eur": gross,
        "fees_eur": fees, "hold_seconds": hold,
    }


class SlotMetricsTests(unittest.TestCase):
    def test_empty(self):
        m = compute_slot_metrics([])
        self.assertEqual(m.n_samples, 0)
        self.assertIsNone(m.win_rate)
        self.assertEqual(m.net_pnl_eur, 0.0)

    def test_all_wins(self):
        trades = [_trade(1.0), _trade(2.0), _trade(0.5)]
        m = compute_slot_metrics(trades)
        self.assertEqual(m.n_samples, 3)
        self.assertEqual(m.win_rate, 1.0)
        # No losses -> profit_factor is +inf (normalised to None by _num wrapper
        # elsewhere). Formula itself returns inf; that's correct arithmetic.
        self.assertTrue(math.isinf(m.profit_factor))

    def test_all_losses_profit_factor_zero(self):
        trades = [_trade(-1.0), _trade(-0.5)]
        m = compute_slot_metrics(trades)
        self.assertEqual(m.win_rate, 0.0)
        self.assertEqual(m.profit_factor, 0.0)

    def test_mixed_profit_factor(self):
        # wins: +2 +1 = 3; losses: -1 -0.5 = -1.5; PF = 3/1.5 = 2.0
        trades = [_trade(2.0), _trade(1.0), _trade(-1.0), _trade(-0.5)]
        m = compute_slot_metrics(trades)
        self.assertAlmostEqual(m.profit_factor, 2.0, places=4)
        self.assertEqual(m.win_rate, 0.5)

    def test_expectancy_bps(self):
        # avg pct = 0.4% -> 40 bps
        trades = [_trade(1.0), _trade(-0.2), _trade(0.4)]
        m = compute_slot_metrics(trades)
        self.assertAlmostEqual(m.expectancy_bps, (1.0 - 0.2 + 0.4) / 3 * 100, places=3)

    def test_max_dd_pct_with_losing_sequence(self):
        # Curve: +10, -5, -8, +3. Peak 10, trough 10-5-8 = -3 -> DD 13 from peak.
        trades = [_trade(1.0, 10.0), _trade(-0.5, -5.0),
                    _trade(-0.8, -8.0), _trade(0.3, 3.0)]
        m = compute_slot_metrics(trades)
        # Peak = 10, min-after = 10-5-8 = -3, dd = 13, dd% = 130.
        self.assertAlmostEqual(m.max_dd_pct, 130.0, places=1)


if __name__ == "__main__":
    unittest.main()
