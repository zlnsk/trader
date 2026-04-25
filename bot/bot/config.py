"""Config validation — loud, explicit, fail-fast at startup.

Exists to catch classes of misconfiguration that pre-trade gates can't, like
a slot whose fee-adjusted R:R is so low that no realistic win rate produces
positive expectancy. Validations run in main.Bot.run() before the scan loop
starts; a failure raises ConfigError with a named-slot message and the bot
exits non-zero.

Add new validations here, not in main.py — keeps the surface small and
testable in isolation.
"""
from __future__ import annotations

import logging
from typing import Iterable

from . import fees

log = logging.getLogger("bot.config")


class ConfigError(RuntimeError):
    """Raised when a validation invariant is violated. The bot should
    propagate this and exit — these are not recoverable at runtime."""


# Per PR1: floor on fee+slippage-adjusted R:R for any active slot. Below this,
# required WR is implausibly high for mean-reversion (break-even WR at R:R=0.6
# is ~63%; below 0.5 it's >66%). Bump requires an explicit code change, not a
# runtime config tweak.
MIN_SLOT_NET_RR = 0.6

# Reference prices used to compute notional-sensitive fees. Close enough to
# observed mid-prices across the 2026 universe that the validator's verdict
# matches what real trades will see. Not load-bearing on actual order math.
_REFERENCE_PRICE_STOCK = {
    "USD": 100.0,
    "EUR": 100.0,
    "GBP": 1000.0,  # LSE pence-denominated names often trade around 100-5000p
    "CHF": 100.0,
    "DKK": 500.0,
}
_REFERENCE_PRICE_CRYPTO = 10000.0


def _infer_asset_class(profile: dict) -> str:
    """Use strategy label or sectors_allowed to decide asset class. Avoids
    plumbing a new column just for validation — matches what the scan loop
    itself does."""
    if profile.get("strategy") == "crypto_scalp":
        return "crypto"
    sectors = profile.get("sectors_allowed") or []
    if isinstance(sectors, (list, tuple)) and "Crypto" in sectors:
        return "crypto"
    return "stock"


def _reference_price(asset_class: str, currency: str) -> float:
    if asset_class == "crypto":
        return _REFERENCE_PRICE_CRYPTO
    return _REFERENCE_PRICE_STOCK.get(currency, 100.0)


def validate_slot_rr(
    slot_profiles: Iterable[dict],
    slot_size_eur: float = 1000.0,
    crypto_paper_sim: bool = True,
    min_rr: float = MIN_SLOT_NET_RR,
) -> None:
    """Refuse to start when any active slot has net_expected_rr < min_rr.

    Raises ConfigError naming the offending slot, its computed R:R, and the
    target/stop values that produced it. A single message lists every failing
    slot — don't want the first failure to hide later ones during a migration
    that breaks multiple tiers at once.
    """
    failures: list[str] = []
    for profile in slot_profiles:
        # Overnight strategy exits via MOO (deterministic) not target/stop, so
        # the R:R check is meaningless — target/stop in its slot profile are
        # sentinel placeholders populated only to satisfy NOT NULL constraints.
        if profile.get("strategy") == "overnight":
            continue
        asset_class = _infer_asset_class(profile)
        currency = "USD" if asset_class == "crypto" else str(profile.get("currency", "USD"))
        price = _reference_price(asset_class, currency)
        try:
            rr = fees.net_expected_rr(
                profile,
                price=price,
                asset_class=asset_class,
                currency=currency,
                slot_size_eur=slot_size_eur,
                crypto_paper_sim=crypto_paper_sim,
            )
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(
                f"slot={profile.get('slot','?')} could not compute R:R: {exc}"
            )
            continue
        if rr < min_rr:
            failures.append(
                f"slot={profile.get('slot','?')} "
                f"strategy={profile.get('strategy','?')} "
                f"target={profile.get('target_profit_pct')} "
                f"stop={profile.get('stop_loss_pct')} "
                f"net_rr={rr:.3f} < min={min_rr:.2f}"
            )
    if failures:
        msg = (
            f"{len(failures)} slot(s) fail net_expected_rr ≥ {min_rr:.2f} check:\n  "
            + "\n  ".join(failures)
        )
        log.error(msg)
        raise ConfigError(msg)
    log.info("validate_slot_rr passed (min_rr=%.2f)", min_rr)


def validate(slot_profiles: Iterable[dict], **kwargs) -> None:
    """Top-level validation entrypoint. Today it wraps only validate_slot_rr;
    add future checks here. Callers pass kwargs like slot_size_eur and
    crypto_paper_sim so this stays pure / testable."""
    validate_slot_rr(slot_profiles, **kwargs)
