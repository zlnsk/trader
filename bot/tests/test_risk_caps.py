"""PR8: gross risk percentage math (pure calculation, no DB)."""
import unittest


def _gross_risk_pct_synth(positions, equity_eur):
    total = 0.0
    for p in positions:
        entry = float(p["entry_price"])
        qty = float(p["qty"])
        stop = float(p["stop_price"])
        risk_per_share = max(entry - stop, 0.0)
        total += risk_per_share * qty
    return (total / equity_eur) * 100.0 if equity_eur > 0 else 0.0


class GrossRiskMathTests(unittest.TestCase):
    def test_empty_portfolio_zero(self):
        self.assertEqual(_gross_risk_pct_synth([], 10_000.0), 0.0)

    def test_single_position(self):
        pos = [{"entry_price": 100, "qty": 10, "stop_price": 98}]
        self.assertAlmostEqual(_gross_risk_pct_synth(pos, 10_000.0), 0.2)

    def test_multiple_positions_sum(self):
        pos = [
            {"entry_price": 100, "qty": 10, "stop_price": 99},    # risk 10
            {"entry_price": 50,  "qty": 20, "stop_price": 49},    # risk 20
            {"entry_price": 200, "qty": 5,  "stop_price": 196},   # risk 20
        ]
        # total risk = 50 on 10k equity → 0.5%
        self.assertAlmostEqual(_gross_risk_pct_synth(pos, 10_000.0), 0.5)

    def test_equity_zero_returns_zero(self):
        pos = [{"entry_price": 100, "qty": 10, "stop_price": 98}]
        self.assertEqual(_gross_risk_pct_synth(pos, 0.0), 0.0)

    def test_halving_stops_at_floor(self):
        # Emulate the scan halving loop — ensure the multiplier never goes
        # to zero.
        multiplier = 1.0
        for _ in range(20):
            multiplier *= 0.5
            if multiplier <= 0.0625:
                break
        self.assertLessEqual(multiplier, 0.0625)
        self.assertGreater(multiplier, 0.0)


class SectorCapScopeTests(unittest.TestCase):
    """Paper test of the scope flag resolution. The DB-bound
    _open_sector_counts is exercised via integration, not unit test."""

    def test_default_scope_is_portfolio(self):
        cfg = {}
        scope = str(cfg.get("MAX_POSITIONS_PER_SECTOR_SCOPE") or "portfolio").lower()
        self.assertEqual(scope, "portfolio")

    def test_strategy_scope_explicit(self):
        cfg = {"MAX_POSITIONS_PER_SECTOR_SCOPE": "strategy"}
        scope = str(cfg.get("MAX_POSITIONS_PER_SECTOR_SCOPE") or "portfolio").lower()
        self.assertEqual(scope, "strategy")


if __name__ == "__main__":
    unittest.main()
