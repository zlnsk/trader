"""Unit tests for fill_quality math.

Covers the three pure functions that feed the dashboard's Fill-quality card
and the paper→live readiness decision: slippage_bps sign convention, paper
shadow penalty direction, and edge cases (missing inputs, zero spread).
Before 2026-04-24 this module had zero tests; a silent sign flip would have
been invisible until a human read the card.
"""
from __future__ import annotations

import math
import unittest

from bot import fill_quality as fq


class ComputeSlippageBpsTest(unittest.TestCase):
    def test_buy_positive_slip_means_paid_more(self):
        # Paid 100.50 vs mid 100.00 → +50 bps
        self.assertAlmostEqual(
            fq.compute_slippage_bps("BUY", 100.50, 100.00), 50.0, places=3,
        )

    def test_sell_positive_slip_means_got_less(self):
        # Sold at 99.50 vs mid 100.00 → +50 bps
        self.assertAlmostEqual(
            fq.compute_slippage_bps("SELL", 99.50, 100.00), 50.0, places=3,
        )

    def test_buy_negative_slip_means_price_improvement(self):
        self.assertAlmostEqual(
            fq.compute_slippage_bps("BUY", 99.50, 100.00), -50.0, places=3,
        )

    def test_missing_mid_returns_none(self):
        self.assertIsNone(fq.compute_slippage_bps("BUY", 100.00, None))

    def test_zero_fill_price_returns_none(self):
        self.assertIsNone(fq.compute_slippage_bps("BUY", 0.0, 100.00))

    def test_zero_mid_returns_none(self):
        self.assertIsNone(fq.compute_slippage_bps("BUY", 100.00, 0.0))


class ShadowFillPriceTest(unittest.TestCase):
    def test_paper_buy_penalty_raises_fill(self):
        # 20 bps spread → half-spread = 10 bps → paper buy at 100 fills at 100.10
        got = fq.shadow_fill_price("BUY", 100.00, 20.0, paper=True)
        self.assertAlmostEqual(got, 100.10, places=5)

    def test_paper_sell_penalty_lowers_fill(self):
        got = fq.shadow_fill_price("SELL", 100.00, 20.0, paper=True)
        self.assertAlmostEqual(got, 99.90, places=5)

    def test_live_mode_returns_fill_unchanged(self):
        self.assertAlmostEqual(
            fq.shadow_fill_price("BUY", 100.00, 20.0, paper=False),
            100.00, places=5,
        )

    def test_missing_spread_returns_fill_unchanged(self):
        self.assertAlmostEqual(
            fq.shadow_fill_price("BUY", 100.00, None, paper=True),
            100.00, places=5,
        )

    def test_zero_spread_returns_fill_unchanged(self):
        self.assertAlmostEqual(
            fq.shadow_fill_price("BUY", 100.00, 0.0, paper=True),
            100.00, places=5,
        )

    def test_missing_fill_returns_none(self):
        self.assertIsNone(fq.shadow_fill_price("BUY", 0.0, 20.0, paper=True))


class QuoteDefaultsTest(unittest.TestCase):
    def test_empty_quote_has_all_none(self):
        q = fq.Quote()
        self.assertIsNone(q.bid)
        self.assertIsNone(q.ask)
        self.assertIsNone(q.mid)
        self.assertIsNone(q.spread_bps)


class PaperOptimismAggregateTest(unittest.TestCase):
    """The dashboard's Fill-quality card computes
    paper_optimism_eur = Σ (fill - shadow) * qty for BUY (penalty raises shadow,
    optimism is positive) and Σ (shadow - fill) * qty for SELL. Mirror that
    math against the primitives so a refactor of shadow_fill_price surfaces
    here rather than only in the UI."""

    def test_buy_optimism_is_half_spread_eur(self):
        fill = 100.00
        qty = 10.0
        shadow = fq.shadow_fill_price("BUY", fill, 20.0, paper=True)
        self.assertAlmostEqual((shadow - fill) * qty, 1.0, places=4)

    def test_sell_optimism_is_half_spread_eur(self):
        fill = 100.00
        qty = 10.0
        shadow = fq.shadow_fill_price("SELL", fill, 20.0, paper=True)
        self.assertAlmostEqual((fill - shadow) * qty, 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
