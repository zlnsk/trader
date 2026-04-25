"""PR7: per-strategy re-entry cooldown default resolution."""
import unittest


try:
    from bot import strategy
    _OK = True
except ImportError:
    strategy = None
    _OK = False


@unittest.skipUnless(_OK, "asyncpg not available in this env")
class CooldownDefaultsTests(unittest.TestCase):
    def test_swing_default(self):
        self.assertEqual(strategy._COOLDOWN_SECONDS_BY_STRATEGY["swing"], 86400)

    def test_intraday_default(self):
        self.assertEqual(strategy._COOLDOWN_SECONDS_BY_STRATEGY["intraday"], 7200)

    def test_crypto_default(self):
        self.assertEqual(strategy._COOLDOWN_SECONDS_BY_STRATEGY["crypto_scalp"], 1800)

    def test_unknown_strategy_zero(self):
        # .get with default 0 → no cooldown row inserted (closed path).
        self.assertEqual(
            strategy._COOLDOWN_SECONDS_BY_STRATEGY.get("ether", 0), 0
        )


if __name__ == "__main__":
    unittest.main()
