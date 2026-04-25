"""PR9: auto-kill threshold logic (pure arithmetic checks)."""
import unittest


def _would_trip(today_pct, dd_pct, wtd_pct,
                 daily=2.0, dd5d=5.0, weekly=4.0):
    if daily > 0 and today_pct <= -daily:
        return "daily"
    if dd5d > 0 and dd_pct <= -dd5d:
        return "dd5d"
    if weekly > 0 and wtd_pct <= -weekly:
        return "weekly"
    return None


class AutoKillThresholdsTests(unittest.TestCase):
    def test_healthy_day_no_trip(self):
        self.assertIsNone(_would_trip(-1.0, -2.0, -1.5))

    def test_daily_breach_trips(self):
        self.assertEqual(_would_trip(-2.5, 0, 0), "daily")

    def test_daily_limit_boundary_trips(self):
        # exactly at limit should trip (<= semantics)
        self.assertEqual(_would_trip(-2.0, 0, 0), "daily")

    def test_rolling5d_breach_trips(self):
        self.assertEqual(_would_trip(-1.0, -5.5, -1.0), "dd5d")

    def test_weekly_breach_trips(self):
        self.assertEqual(_would_trip(-1.0, -2.0, -4.1), "weekly")

    def test_daily_priority_over_others(self):
        # All three would trip; daily should win (earliest check).
        self.assertEqual(_would_trip(-3.0, -6.0, -5.0), "daily")

    def test_disabled_limits_never_trip(self):
        # Zero/negative disables the individual check.
        self.assertIsNone(_would_trip(-3.0, -6.0, -5.0,
                                        daily=0, dd5d=0, weekly=0))


if __name__ == "__main__":
    unittest.main()
