"""Earnings-calendar gate for the scan pipeline.

Public surface:
    next_earnings_date(rows_or_pool, symbol, today) → date | None
    check_blackout(next_date, today, blackout_days) → reason | None
    apply_earnings_blackout(slot_profile, symbol, next_date, cfg, today)

The module is deliberately split into pure helpers (pass `today` + a
pre-fetched row list) and a database-aware convenience layer. Tests cover
the pure helpers; the scan code path uses the DB layer.

Spec behaviour: if `earnings_blackout_days > 0` and no calendar row exists
for the symbol, reject the trade (FAIL-SAFE) and emit a loud log so the
gap in earnings data gets noticed.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

log = logging.getLogger("bot.earnings")


def next_earnings_date_from_rows(
    rows: Iterable[dict], symbol: str, today: date,
) -> date | None:
    """Find the next earnings date for `symbol` at or after `today`.
    `rows` is a pre-fetched iterable of mappings with 'symbol' and
    'earnings_date' keys. Returns None when no future row exists for the
    symbol (caller decides: blackout-unknown vs not-covered)."""
    best: date | None = None
    for r in rows:
        if r.get("symbol") != symbol:
            continue
        d = r.get("earnings_date")
        if d is None:
            continue
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                continue
        if d < today:
            continue
        if best is None or d < best:
            best = d
    return best


def symbol_tracked(rows: Iterable[dict], symbol: str) -> bool:
    """True when at least one row (past or future) exists for the symbol.
    Distinguishes 'no upcoming earnings' (symbol known) from 'unknown
    symbol' (sync job hasn't seen it) — the FAIL-SAFE policy in
    apply_earnings_blackout only rejects the unknown-symbol case."""
    for r in rows:
        if r.get("symbol") == symbol:
            return True
    return False


def check_blackout(
    next_date: date | None,
    today: date,
    blackout_days: int,
) -> str | None:
    """Pure gate. Returns a reason string when blacked out, None otherwise.
    Does NOT enforce the unknown-symbol rule — that needs knowledge of
    'is the symbol tracked at all?' which belongs to the caller."""
    if blackout_days <= 0:
        return None
    if next_date is None:
        return None  # covered by apply_earnings_blackout's unknown-symbol guard
    delta = (next_date - today).days
    if delta <= blackout_days:
        return f"earnings_blackout:{delta}d_to_earnings"
    return None


def apply_earnings_blackout(
    slot_profile: dict,
    symbol: str,
    today: date,
    rows: Iterable[dict],
    cfg: dict,
) -> str | None:
    """Scan-time gate. Returns None on pass, reason string on reject.

    - EARNINGS_BLACKOUT_ENABLED=false → always pass.
    - slot blackout_days == 0 → slot opts out, always pass.
    - symbol has no row in earnings_calendar → FAIL-SAFE reject with loud
      log ("earnings_blackout:unknown_symbol"). Operator should investigate
      the sync job.
    - symbol tracked, no upcoming earnings date → pass.
    - next earnings within blackout_days → reject.
    """
    if not cfg.get("EARNINGS_BLACKOUT_ENABLED"):
        return None
    blackout_days = int(slot_profile.get("earnings_blackout_days") or 0)
    if blackout_days <= 0:
        return None
    # Materialise rows once; callers may pass a generator.
    rows_list = list(rows)
    if not symbol_tracked(rows_list, symbol):
        log.warning(
            "earnings_blackout_unknown_symbol symbol=%s slot=%s — treating "
            "as blackout (fail-safe). Check jobs.maybe_sync_earnings.",
            symbol, slot_profile.get("slot"),
        )
        return "earnings_blackout:unknown_symbol"
    next_date = next_earnings_date_from_rows(rows_list, symbol, today)
    return check_blackout(next_date, today, blackout_days)
