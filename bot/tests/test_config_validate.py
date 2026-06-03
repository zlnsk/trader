"""Tests for bot.config startup validation (PR1)."""
import unittest

from bot import config


def _slot(slot, target, stop, *, strategy="intraday", sectors=None):
    return {
        "slot": slot, "strategy": strategy,
        "profile": "balanced",
        "target_profit_pct": target, "stop_loss_pct": stop,
        "sectors_allowed": sectors,
    }


class ValidateSlotRRTests(unittest.TestCase):
    def test_known_good_portfolio_passes(self):
        profiles = [
            _slot(13, 1.0, -1.2),
            _slot(16, 2.0, -1.5),
            _slot(10, 0.8, -0.7),
            _slot(19, 2.2, -1.3, strategy="crypto_scalp",
                  sectors=["Crypto"]),
            _slot(21, 2.8, -1.5, strategy="crypto_scalp",
                  sectors=["Crypto"]),
        ]

        config.validate_slot_rr(profiles)

    def test_deliberately_broken_slot_fails_with_clear_error(self):

        profiles = [
            _slot(13, 1.0, -1.2),
            _slot(77, 0.3, -0.7),
        ]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_slot_rr(profiles)
        msg = str(ctx.exception)
        self.assertIn("slot=77", msg, "error must name the failing slot")
        self.assertIn("target=0.3", msg)
        self.assertIn("stop=-0.7", msg)
        self.assertIn("net_rr", msg)

    def test_multiple_failures_all_reported(self):
        profiles = [
            _slot(88, 0.3, -0.7),
            _slot(99, 0.4, -1.0),
        ]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_slot_rr(profiles)
        msg = str(ctx.exception)
        self.assertIn("slot=88", msg)
        self.assertIn("slot=99", msg)

    def test_crypto_sector_routing(self):


        profiles = [
            _slot(50, 2.2, -1.3, strategy="intraday", sectors=["Crypto"]),
        ]

        config.validate_slot_rr(profiles)

    def test_missing_fields_reported_not_crashed(self):

        profiles = [{"slot": 42, "strategy": "intraday"}]
        with self.assertRaises(config.ConfigError) as ctx:
            config.validate_slot_rr(profiles)
        self.assertIn("slot=42", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
