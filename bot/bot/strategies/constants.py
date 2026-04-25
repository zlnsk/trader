"""Canonical strategy tag enum.

The bot writes four distinct strategy tags to the DB, intentionally:

    MEAN_REV     — swing mean-reversion (slots 1-9, RSI-14 on daily bars)
    INTRADAY     — intraday mean-reversion (slots 10-18, RSI-2 on 5m bars)
    CRYPTO_SCALP — crypto scalping (slots 19-24)
    OVERNIGHT    — close-to-open (slots 25-29, MOC entry / MOO exit)

`mean_rev` and `intraday` share the bot/strategy.py scan loop but are tagged
separately in `positions.strategy`, `signals.strategy`, `signal_snapshots.strategy`,
and `trade_outcomes.strategy` for attribution. Dashboards and the optimizer
group by these tags, so renaming any of them is a schema-level change.

Use `for_slot(slot)` when you need the canonical tag for a slot range rather
than reading `slot_profiles.strategy` — the latter is authoritative but
requires a DB roundtrip. `for_slot` is a fast in-process fallback.
"""
from __future__ import annotations

MEAN_REV = "mean_rev"
INTRADAY = "intraday"
CRYPTO_SCALP = "crypto_scalp"
OVERNIGHT = "overnight"
UNKNOWN = "unknown"

ALL = (MEAN_REV, INTRADAY, CRYPTO_SCALP, OVERNIGHT)

# Slot → strategy fallback. Must stay in sync with the `strategy` column on
# slot_profiles rows. If slot_profiles drift, that is the source of truth.
_SLOT_RANGES: tuple[tuple[range, str], ...] = (
    (range(1, 10), MEAN_REV),
    (range(10, 19), INTRADAY),
    (range(19, 25), CRYPTO_SCALP),
    (range(25, 30), OVERNIGHT),
)


def for_slot(slot: int | None) -> str:
    if slot is None:
        return UNKNOWN
    for rng, strategy in _SLOT_RANGES:
        if slot in rng:
            return strategy
    return UNKNOWN
