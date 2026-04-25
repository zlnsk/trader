"""PR5 analysis — retrospective P&L impact of historical widen decisions.

For every stop_adjust_decisions row where legacy_widen_action=TRUE, compute:
  - actual_pnl_pct: realized P&L of the corresponding position exit
  - counterfactual_pnl_pct: what P&L would have been if the stop had
    stayed at stop_before (i.e. the "hold / refuse to widen" alternative)
  - delta: counterfactual − actual

Prints a summary to stdout.

Usage:
    python scripts/analyze_widen_history.py

Requires DATABASE_URL in the environment.
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


async def run() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """SELECT s.id AS decision_id, s.position_id, s.ts, s.symbol,
                      s.entry_price, s.current_price, s.stop_before,
                      s.stop_after, s.reasoning,
                      p.exit_price, p.qty, p.opened_at, p.closed_at,
                      p.status
                 FROM stop_adjust_decisions s
            LEFT JOIN positions p ON p.id = s.position_id
                WHERE s.legacy_widen_action = TRUE
             ORDER BY s.ts ASC"""
        )
    finally:
        await conn.close()

    if not rows:
        print("No legacy widen decisions found. Nothing to analyze.")
        print("(Expected — PR5 bans widen going forward. Historical "
              "widen decisions would only exist once persistence lands "
              "via PR5's stop_adjust_decisions table + a future backfill "
              "from log archives.)")
        return 0

    n = len(rows)
    actual_total = 0.0
    counterfactual_total = 0.0
    details = []
    for r in rows:
        if r["exit_price"] is None or r["entry_price"] in (None, 0):
            continue
        entry = float(r["entry_price"])
        actual_exit = float(r["exit_price"])
        actual_pct = (actual_exit - entry) / entry * 100.0
        # Counterfactual: position would have been stopped at stop_before
        # when price first touched it. Proxy: use stop_before if actual exit
        # was lower, else the actual exit (widen didn't change the outcome).
        stop_before = float(r["stop_before"] or entry)
        cf_exit = stop_before if actual_exit < stop_before else actual_exit
        cf_pct = (cf_exit - entry) / entry * 100.0
        delta = cf_pct - actual_pct
        actual_total += actual_pct
        counterfactual_total += cf_pct
        details.append((r["symbol"], actual_pct, cf_pct, delta))

    print(f"Legacy widen decisions: {n}")
    print(f"Actual avg P&L %:          {actual_total / n:+.3f}")
    print(f"Counterfactual avg P&L %:  {counterfactual_total / n:+.3f}")
    print(f"Counterfactual − actual:   {(counterfactual_total - actual_total) / n:+.3f} pp/trade")
    print()
    print("Per-decision breakdown (top 20 by absolute delta):")
    details.sort(key=lambda t: abs(t[3]), reverse=True)
    for sym, a, c, d in details[:20]:
        print(f"  {sym:<6}  actual={a:+.2f}%  counterfactual={c:+.2f}%  delta={d:+.2f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
