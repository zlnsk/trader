"""Tests for fees.net_expected_rr + fees.slippage_bps_for (PR1).

Run: `python -m unittest bot.tests.test_fees_rr` from the bot/ root, or
`python -m unittest discover` to sweep everything.
"""
import unittest

from bot import fees


class SlippageDispatchTests(unittest.TestCase):
    def test_us_stock_uses_us_largecap(self):
        self.assertEqual(fees.slippage_bps_for("stock", "USD"),
                         fees.SLIPPAGE_BPS_US_LARGECAP)

    def test_eu_stock_uses_eu(self):
        self.assertEqual(fees.slippage_bps_for("stock", "EUR"),
                         fees.SLIPPAGE_BPS_EU)
        self.assertEqual(fees.slippage_bps_for("stock", "GBP"),
                         fees.SLIPPAGE_BPS_EU)

    def test_crypto_sim_vs_live(self):
        self.assertEqual(fees.slippage_bps_for("crypto", "USD", crypto_paper_sim=True),
                         fees.SLIPPAGE_BPS_CRYPTO_SIM)
        self.assertEqual(fees.slippage_bps_for("crypto", "USD", crypto_paper_sim=False),
                         fees.SLIPPAGE_BPS_CRYPTO_LIVE)


class NetExpectedRRTests(unittest.TestCase):
    def test_us_stock_healthy_slot(self):
        # slot 13 baseline: target 1.0, stop -1.2, US equity
        profile = {"target_profit_pct": 1.0, "stop_loss_pct": -1.2}
        rr = fees.net_expected_rr(
            profile, price=100.0, asset_class="stock", currency="USD",
            slot_size_eur=1000.0,
        )
        # Net target after ~7bps fee + 6bps slip ≈ 0.87%; net stop ≈ 1.33%
        # → R:R ≈ 0.65. Must exceed the 0.6 floor.
        self.assertGreaterEqual(rr, 0.6, f"slot with R:R {rr:.3f} should pass")

    def test_old_intraday_safe_fails_floor(self):
        # Pre-PR1 intraday_safe: target 0.5, stop -0.7 — documented failure.
        profile = {"target_profit_pct": 0.5, "stop_loss_pct": -0.7}
        rr = fees.net_expected_rr(
            profile, price=100.0, asset_class="stock", currency="USD",
            slot_size_eur=1000.0,
        )
        self.assertLess(rr, 0.6,
                         f"legacy slot R:R {rr:.3f} was supposed to fail")

    def test_new_intraday_safe_passes_floor(self):
        # PR1 retarget for slots 10-12: target 0.8, stop -0.7
        profile = {"target_profit_pct": 0.8, "stop_loss_pct": -0.7}
        rr = fees.net_expected_rr(
            profile, price=100.0, asset_class="stock", currency="USD",
            slot_size_eur=1000.0,
        )
        self.assertGreaterEqual(rr, 0.6,
                                 f"new intraday_safe R:R {rr:.3f} must pass")

    def test_old_crypto_balanced_marginal(self):
        # Pre-PR1 crypto_balanced: target 1.5, stop -1.0
        profile = {"target_profit_pct": 1.5, "stop_loss_pct": -1.0}
        rr = fees.net_expected_rr(
            profile, price=10000.0, asset_class="crypto", currency="USD",
            slot_size_eur=1000.0, crypto_paper_sim=True,
        )
        # Old crypto_balanced clears 0.6 even pre-retune under sim slippage;
        # the real motivation was LIVE slippage (15 bps) collapsing it below.
        rr_live = fees.net_expected_rr(
            profile, price=10000.0, asset_class="crypto", currency="USD",
            slot_size_eur=1000.0, crypto_paper_sim=False,
        )
        self.assertGreater(rr, rr_live,
                             "sim slippage should yield a better R:R than live")

    def test_new_crypto_balanced_passes_floor(self):
        # PR1 retarget for slots 19-20: target 2.2, stop -1.3
        profile = {"target_profit_pct": 2.2, "stop_loss_pct": -1.3}
        rr = fees.net_expected_rr(
            profile, price=10000.0, asset_class="crypto", currency="USD",
            slot_size_eur=1000.0, crypto_paper_sim=True,
        )
        self.assertGreaterEqual(rr, 0.6,
                                 f"new crypto_balanced R:R {rr:.3f} must pass")
        # Also under realistic live slippage:
        rr_live = fees.net_expected_rr(
            profile, price=10000.0, asset_class="crypto", currency="USD",
            slot_size_eur=1000.0, crypto_paper_sim=False,
        )
        self.assertGreaterEqual(rr_live, 0.6,
                                 f"new crypto R:R under live slip {rr_live:.3f} must pass")

    def test_degenerate_inputs_return_zero(self):
        # Zero or negative target/stop → 0, not a negative number or an
        # exception. Validator keys off the 0.6 floor so 0.0 correctly fails.
        self.assertEqual(
            fees.net_expected_rr(
                {"target_profit_pct": 0.0, "stop_loss_pct": -1.0},
                price=100.0, asset_class="stock",
            ), 0.0,
        )
        self.assertEqual(
            fees.net_expected_rr(
                {"target_profit_pct": 1.0, "stop_loss_pct": 0.0},
                price=100.0, asset_class="stock",
            ), 0.0,
        )
        self.assertEqual(
            fees.net_expected_rr(
                {"target_profit_pct": 1.0, "stop_loss_pct": -1.0},
                price=0.0, asset_class="stock",
            ), 0.0,
        )


if __name__ == "__main__":
    unittest.main()
