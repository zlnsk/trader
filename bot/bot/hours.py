"""Market-hours gate, per currency/region.

All times in UTC. DST offsets assume current 2026 rules (US on DST from March,
EU on DST from late March); approximate but correct for Apr-Oct window.

Crypto (asset_class="crypto") is 24/7 — market_open_for_symbol always returns
True for crypto symbols regardless of weekday/time.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .universe import meta


def _hhmm_utc(now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    return now.hour, now.minute


def _is_weekday(now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)).weekday() < 5


def _in_window(h: int, m: int, start: tuple[int, int], end: tuple[int, int]) -> bool:
    cur = h * 60 + m
    return start[0] * 60 + start[1] <= cur < end[0] * 60 + end[1]


def us_market_open(now: datetime | None = None) -> bool:
    """US regular hours 09:30-16:00 ET ≈ 13:30-20:00 UTC during DST (Apr-Oct)."""
    if not _is_weekday(now):
        return False
    h, m = _hhmm_utc(now)
    return _in_window(h, m, (13, 30), (20, 0))


def eu_market_open(now: datetime | None = None) -> bool:
    """EU + UK 09:00-17:30 local ≈ 07:00-15:30 UTC during DST. Earliest close
    applied conservatively so we never sell into a closed venue."""
    if not _is_weekday(now):
        return False
    h, m = _hhmm_utc(now)
    return _in_window(h, m, (7, 0), (15, 30))


def market_open_for(currency: str, now: datetime | None = None) -> bool:
    if currency == "USD":
        return us_market_open(now)
    return eu_market_open(now)


def market_open_for_symbol(symbol: str, now: datetime | None = None) -> bool:
    """Asset-class-aware gate. Crypto 24/7; stocks dispatch via currency."""
    m = meta(symbol)
    if m.asset_class == "crypto":
        return True
    return market_open_for(m.currency, now)


def any_market_open(now: datetime | None = None) -> bool:
    return us_market_open(now) or eu_market_open(now)

def minutes_to_close_for(currency: str, now: datetime | None = None) -> int | None:
    """Minutes until the venue closes for this currency. None if the venue
    is not currently open (i.e. not in session). Uses the same conservative
    close times as `market_open_for`:
      USD -> 20:00 UTC (US regular hours close)
      other -> 15:30 UTC (EU conservative close)
    """
    if not _is_weekday(now):
        return None
    if not market_open_for(currency, now):
        return None
    h, m = _hhmm_utc(now)
    cur = h * 60 + m
    close_min = (20 * 60) if currency == "USD" else (15 * 60 + 30)
    return max(0, close_min - cur)


def minutes_to_close_for_symbol(symbol: str, now: datetime | None = None) -> int | None:
    """None when not in session OR for crypto (24/7)."""
    m = meta(symbol)
    if m.asset_class == "crypto":
        return None
    return minutes_to_close_for(m.currency, now)


# Conservative defaults when config keys are absent. US closing auction
# typically routes MOC ~10 min before close; Euronext MOC deadlines are tighter
# (~5-10 min), so the EU window ends earlier. Per-currency because
# a single (10, 20) window under-serves EU names and can over-serve US names.
_MOC_WINDOW_DEFAULTS: dict[str, tuple[int, int]] = {
    "USD": (10, 20),
    "EUR": (5, 15),
    "GBP": (5, 15),
    "CHF": (5, 15),
    "DKK": (5, 15),
}


def moc_window_for_currency(currency: str, cfg: dict | None = None) -> tuple[int, int]:
    """(min_minutes, max_minutes) window for routing a time-stop via MOC.
    Reads MOC_WINDOW_{MIN,MAX}_MINUTES_{USD,EU} from config when present;
    EU key covers all non-USD currencies. Defaults match historical behaviour
    for USD and a conservative narrower window for EU venues.
    """
    lo_default, hi_default = _MOC_WINDOW_DEFAULTS.get(currency, (5, 15))
    if not cfg:
        return lo_default, hi_default
    region = "USD" if currency == "USD" else "EU"
    try:
        lo = int(cfg.get(f"MOC_WINDOW_MIN_MINUTES_{region}", lo_default))
        hi = int(cfg.get(f"MOC_WINDOW_MAX_MINUTES_{region}", hi_default))
    except (TypeError, ValueError):
        return lo_default, hi_default
    if lo < 0 or hi < lo:
        return lo_default, hi_default
    return lo, hi

