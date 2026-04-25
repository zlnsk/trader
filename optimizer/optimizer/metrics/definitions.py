"""Metric formulas. Bumping DEFN_VERSION invalidates cross-era
comparison in the validator — intentional friction."""
from __future__ import annotations

from dataclasses import dataclass

DEFN_VERSION = 1

WINDOW_DAYS = (7, 30, 90)


@dataclass
class SlotMetrics:
    n_samples: int
    win_rate: float | None
    profit_factor: float | None
    expectancy_bps: float | None
    avg_hold_sec: float | None
    sharpe_like: float | None
    max_dd_pct: float | None
    fees_eur: float
    gross_pnl_eur: float
    net_pnl_eur: float


def compute_slot_metrics(trades: list[dict]) -> SlotMetrics:
    """Compute rolling metrics from a list of trade_outcomes rows.

    All monetary fields already fee-adjusted at source. `expectancy_bps`
    is per-trade average of net_pnl_pct * 100 (1% = 100 bps). Sharpe-like
    is mean/std of net_pnl_pct (no annualisation — comparability within
    table is what matters, not cross-instrument scale).
    """
    n = len(trades)
    if n == 0:
        return SlotMetrics(
            n_samples=0, win_rate=None, profit_factor=None,
            expectancy_bps=None, avg_hold_sec=None, sharpe_like=None,
            max_dd_pct=None, fees_eur=0.0, gross_pnl_eur=0.0, net_pnl_eur=0.0,
        )

    pct = [float(t["net_pnl_pct"]) for t in trades]
    net = [float(t["net_pnl_eur"]) for t in trades]
    fees = sum(float(t["fees_eur"]) for t in trades)
    gross = sum(float(t["gross_pnl_eur"]) for t in trades)
    holds = [int(t["hold_seconds"]) for t in trades]

    wins = [x for x in pct if x > 0]
    losses = [x for x in pct if x <= 0]
    win_rate = len(wins) / n

    sum_wins = sum(wins)
    sum_losses = sum(losses)
    if sum_losses == 0:
        profit_factor = float("inf") if sum_wins > 0 else 0.0
    else:
        profit_factor = sum_wins / abs(sum_losses)
    expectancy_bps = (sum(pct) / n) * 100.0

    mean = sum(pct) / n
    var = sum((x - mean) ** 2 for x in pct) / max(n - 1, 1)
    std = var ** 0.5
    sharpe_like = (mean / std) if std > 0 else None

    # Max drawdown on the cumulative net curve.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in net:
        cum += v
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100.0) if peak > 0 else 0.0

    return SlotMetrics(
        n_samples=n,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_bps=expectancy_bps,
        avg_hold_sec=sum(holds) / n,
        sharpe_like=sharpe_like,
        max_dd_pct=max_dd_pct,
        fees_eur=fees,
        gross_pnl_eur=gross,
        net_pnl_eur=sum(net),
    )
