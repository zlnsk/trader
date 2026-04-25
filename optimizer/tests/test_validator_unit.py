"""Unit tests for the adversary that do NOT require a DB.

Covers pure-function gates: param_bounds, replay_improves, sub_period,
bootstrap, regime. sample_size, cooldown are DB-dependent and live in
the integration tests.
"""
import unittest
from datetime import datetime, timedelta, timezone

from optimizer.validator.adversary import (
    _gate_param_bounds, _gate_replay_improves, _gate_sub_period,
    _gate_bootstrap, _gate_regime,
    PASS, REJECT, MARGINAL,
)
from optimizer.validator.replay import ReplayedTrade


def _rt(i: int, *, b_ok: bool, c_ok: bool, outcome: float,
         regime: str | None = "momentum",
         ts: datetime | None = None) -> ReplayedTrade:
    return ReplayedTrade(
        snapshot_id=i, snapshot_ts=ts or datetime(2026, 4, 1, tzinfo=timezone.utc),
        symbol="X", slot_id=10, baseline_accept=b_ok, candidate_accept=c_ok,
        outcome_pct=outcome, entry_regime=regime,
    )


class ParamBoundsTests(unittest.TestCase):
    def test_within_bounds(self):
        g = _gate_param_bounds({"TARGET_PROFIT_PCT": 1.0}, {"TARGET_PROFIT_PCT": 1.05})
        self.assertEqual(g.verdict, PASS)

    def test_exceeds_cap(self):
        g = _gate_param_bounds({"TARGET_PROFIT_PCT": 1.0}, {"TARGET_PROFIT_PCT": 2.0})
        self.assertEqual(g.verdict, REJECT)
        self.assertGreater(g.detail["pct_change"], 15)


class ReplayImprovesTests(unittest.TestCase):
    def test_rejects_no_improvement(self):
        # 60 trades, both accept all, same outcomes -> no delta
        trades = [_rt(i, b_ok=True, c_ok=True, outcome=1.0) for i in range(60)]
        g = _gate_replay_improves(trades, n_changed_params=1)
        self.assertEqual(g.verdict, REJECT)

    def test_accepts_improvement_above_penalty(self):
        # Candidate rejects all -5% outcomes, baseline accepts them.
        trades = []
        for i in range(50):
            trades.append(_rt(i, b_ok=True, c_ok=True, outcome=1.0))
        for i in range(50):
            trades.append(_rt(50 + i, b_ok=True, c_ok=False, outcome=-2.0))
        g = _gate_replay_improves(trades, n_changed_params=1)
        self.assertEqual(g.verdict, PASS)

    def test_rejects_when_candidate_accepts_too_few(self):
        trades = [_rt(i, b_ok=True, c_ok=False, outcome=1.0) for i in range(60)]
        g = _gate_replay_improves(trades, n_changed_params=1)
        self.assertEqual(g.verdict, REJECT)


class SubPeriodTests(unittest.TestCase):
    def test_concentrated_improvement_rejected(self):
        # First half trivial, second half wildly different
        trades = []
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(50):
            trades.append(_rt(i, b_ok=True, c_ok=True, outcome=0.0,
                                 ts=t0 + timedelta(days=i)))
        for i in range(50):
            trades.append(_rt(i + 50, b_ok=True, c_ok=False, outcome=-10.0,
                                 ts=t0 + timedelta(days=50 + i)))
        g = _gate_sub_period(trades)
        # first half delta 0, second half large positive -> concentrated in one half
        self.assertEqual(g.verdict, REJECT)

    def test_consistent_improvement_passes(self):
        # Both halves show candidate rejects losers evenly.
        trades = []
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(100):
            outcome = -2.0 if i % 2 == 0 else 1.0
            c_ok = outcome > 0  # candidate rejects losers
            trades.append(_rt(
                i, b_ok=True, c_ok=c_ok, outcome=outcome,
                ts=t0 + timedelta(days=i),
            ))
        g = _gate_sub_period(trades)
        self.assertEqual(g.verdict, PASS)


class BootstrapTests(unittest.TestCase):
    def test_rejects_when_ci_includes_zero(self):
        # Both arms take the same trades with the same outcomes —
        # mean-of-accepted is identical, CI straddles zero.
        trades = [_rt(i, b_ok=True, c_ok=True, outcome=1.0) for i in range(60)]
        g = _gate_bootstrap(trades)
        self.assertEqual(g.verdict, REJECT)

    def test_passes_when_candidate_filters_losers(self):
        # Baseline takes wins +1 and losses -1; candidate takes only wins.
        # mean(cand) = +1, mean(base) = 0. Delta +1, CI excludes zero.
        trades = []
        for i in range(60):
            trades.append(_rt(i, b_ok=True, c_ok=True, outcome=1.0))
        for i in range(60):
            trades.append(_rt(100 + i, b_ok=True, c_ok=False, outcome=-1.0))
        g = _gate_bootstrap(trades)
        self.assertEqual(g.verdict, PASS)


class RegimeTests(unittest.TestCase):
    def test_rejects_when_one_regime_regresses(self):
        trades = []
        # momentum: baseline +1%, candidate +1.5% (improvement)
        for i in range(20):
            trades.append(_rt(i, b_ok=True, c_ok=True, outcome=1.5,
                                 regime="momentum"))
        # risk_off: baseline +0%, candidate -1% (regression)
        for i in range(20):
            trades.append(_rt(100 + i, b_ok=False, c_ok=True, outcome=-1.0,
                                 regime="risk_off"))
        g = _gate_regime(trades)
        self.assertEqual(g.verdict, REJECT)

    def test_passes_when_all_regimes_stable(self):
        trades = [_rt(i, b_ok=True, c_ok=True, outcome=1.0, regime="momentum")
                   for i in range(20)]
        g = _gate_regime(trades)
        self.assertEqual(g.verdict, PASS)


if __name__ == "__main__":
    unittest.main()
