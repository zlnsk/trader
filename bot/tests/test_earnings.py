"""Tests for bot.earnings (PR4)."""
import unittest
from datetime import date, timedelta

from bot import earnings


def _rows(pairs):
    return [{"symbol": s, "earnings_date": d} for s, d in pairs]


class NextEarningsDateTests(unittest.TestCase):
    def test_returns_nearest_future_date(self):
        today = date(2026, 4, 20)
        rows = _rows([
            ("AAPL", date(2026, 4, 25)),
            ("AAPL", date(2026, 7, 28)),
            ("MSFT", date(2026, 4, 24)),
        ])
        self.assertEqual(
            earnings.next_earnings_date_from_rows(rows, "AAPL", today),
            date(2026, 4, 25),
        )

    def test_ignores_past_dates(self):
        today = date(2026, 4, 20)
        rows = _rows([
            ("AAPL", date(2026, 2, 1)),
            ("AAPL", date(2026, 4, 25)),
        ])
        self.assertEqual(
            earnings.next_earnings_date_from_rows(rows, "AAPL", today),
            date(2026, 4, 25),
        )

    def test_unknown_symbol_returns_none(self):
        today = date(2026, 4, 20)
        rows = _rows([("MSFT", date(2026, 4, 24))])
        self.assertIsNone(
            earnings.next_earnings_date_from_rows(rows, "AAPL", today)
        )

    def test_iso_string_accepted(self):
        today = date(2026, 4, 20)
        rows = [{"symbol": "AAPL", "earnings_date": "2026-04-25"}]
        self.assertEqual(
            earnings.next_earnings_date_from_rows(rows, "AAPL", today),
            date(2026, 4, 25),
        )


class CheckBlackoutTests(unittest.TestCase):
    def test_disabled_when_days_zero(self):
        self.assertIsNone(
            earnings.check_blackout(date(2026, 4, 21), date(2026, 4, 20), 0)
        )

    def test_inside_window_rejects(self):
        # Swing default 3 days.
        reason = earnings.check_blackout(
            date(2026, 4, 22), date(2026, 4, 20), 3,
        )
        self.assertIsNotNone(reason)
        self.assertIn("2d_to_earnings", reason)

    def test_on_window_edge_rejects(self):
        # date is blackout_days away → still blocked.
        reason = earnings.check_blackout(
            date(2026, 4, 23), date(2026, 4, 20), 3,
        )
        self.assertIsNotNone(reason)
        self.assertIn("3d_to_earnings", reason)

    def test_outside_window_passes(self):
        self.assertIsNone(
            earnings.check_blackout(date(2026, 4, 24), date(2026, 4, 20), 3)
        )

    def test_none_date_passes_through_pure_helper(self):
        # The pure helper doesn't enforce unknown-symbol fail-safe.
        self.assertIsNone(earnings.check_blackout(None, date(2026, 4, 20), 3))


class ApplyEarningsBlackoutTests(unittest.TestCase):
    def test_flag_off_always_passes(self):
        rows = []
        slot = {"earnings_blackout_days": 3, "slot": 1}
        self.assertIsNone(earnings.apply_earnings_blackout(
            slot, "AAPL", date(2026, 4, 20), rows,
            {"EARNINGS_BLACKOUT_ENABLED": False},
        ))

    def test_unknown_symbol_fails_safe(self):
        rows = _rows([("MSFT", date(2026, 4, 24))])
        slot = {"earnings_blackout_days": 3, "slot": 1}
        reason = earnings.apply_earnings_blackout(
            slot, "AAPL", date(2026, 4, 20), rows,
            {"EARNINGS_BLACKOUT_ENABLED": True},
        )
        self.assertEqual(reason, "earnings_blackout:unknown_symbol")

    def test_tracked_symbol_outside_window_passes(self):
        rows = _rows([("AAPL", date(2026, 4, 30))])
        slot = {"earnings_blackout_days": 3, "slot": 1}
        self.assertIsNone(earnings.apply_earnings_blackout(
            slot, "AAPL", date(2026, 4, 20), rows,
            {"EARNINGS_BLACKOUT_ENABLED": True},
        ))

    def test_tracked_symbol_inside_window_rejects(self):
        rows = _rows([("AAPL", date(2026, 4, 22))])
        slot = {"earnings_blackout_days": 3, "slot": 1}
        reason = earnings.apply_earnings_blackout(
            slot, "AAPL", date(2026, 4, 20), rows,
            {"EARNINGS_BLACKOUT_ENABLED": True},
        )
        self.assertIsNotNone(reason)
        self.assertIn("earnings_blackout", reason)

    def test_zero_blackout_days_opts_out(self):
        # crypto slots set earnings_blackout_days=0 and don't get tracked,
        # so the flag being on must not affect them.
        rows = []
        slot = {"earnings_blackout_days": 0, "slot": 19}
        self.assertIsNone(earnings.apply_earnings_blackout(
            slot, "BTC", date(2026, 4, 20), rows,
            {"EARNINGS_BLACKOUT_ENABLED": True},
        ))

    def test_tracked_symbol_no_upcoming_earnings_passes(self):
        # Symbol has past rows only → tracked, but no upcoming event.
        rows = _rows([("AAPL", date(2026, 2, 1))])
        slot = {"earnings_blackout_days": 3, "slot": 1}
        self.assertIsNone(earnings.apply_earnings_blackout(
            slot, "AAPL", date(2026, 4, 20), rows,
            {"EARNINGS_BLACKOUT_ENABLED": True},
        ))


if __name__ == "__main__":
    unittest.main()
