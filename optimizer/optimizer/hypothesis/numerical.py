"""Numerical hypothesis generator using Optuna TPE.

Searches over bounded ranges of a small set of numerical keys. The
objective is REPLAY expectancy over the lookback window, with complexity
penalty. This is a hypothesis generator, not a final decision — every
proposal it emits must still pass the adversary before application.

Storage: we do NOT persist Optuna's SQLite study across process runs.
Each run starts fresh. The study's job is to find a single good
candidate; that candidate gets written to tuning_proposals, then the
adversary decides. Running TPE from scratch every cadence is cheap
enough (100 trials, pure Python gate evaluation).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:  # pragma: no cover
    optuna = None

from .. import safety
from ..config_store import active_global_version
from ..config_store.versions import _values_of, get_managed_keys
from ..validator.replay import replay, summarise

log = logging.getLogger("optimizer.hypothesis.numerical")


# Which keys this source is allowed to touch. Restricted to float thresholds
# that replay actually acts on. Adding more requires updating replay._accept.
_TUNABLE_KEYS = ("QUANT_SCORE_MIN", "RSI_BUY_THRESHOLD", "SIGMA_BELOW_SMA20")


async def _load_baseline(pool: asyncpg.Pool) -> dict[str, Any]:
    v = await active_global_version(pool)
    if v is None:
        return {}
    return await _values_of(pool, v["id"])


async def _load_snapshots(pool: asyncpg.Pool, *, lookback_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, symbol, slot_id, snapshot_ts, score, rsi,
                       sigma_below_sma20, stock_regime, crypto_regime,
                       hypothetical_outcome_pct
                 FROM signal_snapshots
                WHERE snapshot_ts >= $1
                  AND hypothetical_outcome_pct IS NOT NULL""",
            cutoff,
        )
    return [dict(r) for r in rows]


def _clamp_suggest(trial, key: str, baseline_value: float,
                    mk_min: float | None, mk_max: float | None) -> float:
    """Suggest around baseline within MAX_SINGLE_CHANGE_PCT, intersected
    with the managed-keys range."""
    span = baseline_value * (safety.MAX_SINGLE_CHANGE_PCT / 100.0) if baseline_value else 1.0
    lo = baseline_value - span
    hi = baseline_value + span
    if mk_min is not None:
        lo = max(lo, mk_min)
    if mk_max is not None:
        hi = min(hi, mk_max)
    if lo >= hi:
        return baseline_value
    return trial.suggest_float(key, lo, hi)


async def search(
    pool: asyncpg.Pool,
    *,
    lookback_days: int = 30,
    n_trials: int = 100,
    seed: int = 42,
) -> dict | None:
    """Run a TPE search. Returns a dict describing the best candidate
    {key: value}, OR None when not enough data to even start. Does NOT
    write a proposal — see propose_best() for that."""
    if optuna is None:
        raise RuntimeError("optuna not installed; pip install optuna>=3.6")
    baseline = await _load_baseline(pool)
    if not baseline:
        log.warning("no active baseline config — cannot run numerical search")
        return None
    snapshots = await _load_snapshots(pool, lookback_days=lookback_days)
    if len(snapshots) < safety.MIN_N_SAMPLES:
        log.info("numerical.search: insufficient snapshots (%d < %d)",
                  len(snapshots), safety.MIN_N_SAMPLES)
        return None

    mk = await get_managed_keys(pool)

    # Baseline replay once; reused for delta computation.
    base_replay = replay(snapshots, baseline=baseline, candidate=baseline)
    base_summary = summarise(base_replay, "baseline_accept")
    base_mean = base_summary["mean_pct"]

    def objective(trial):
        candidate = dict(baseline)
        for k in _TUNABLE_KEYS:
            if k not in baseline:
                continue
            managed = mk.get(k)
            candidate[k] = _clamp_suggest(
                trial, k, float(baseline[k]),
                managed.min_value if managed else None,
                managed.max_value if managed else None,
            )
        rep = replay(snapshots, baseline=baseline, candidate=candidate)
        cand_summary = summarise(rep, "candidate_accept")
        if cand_summary["n"] < safety.MIN_N_SAMPLES:
            return -1e6  # invalid: too few accepted
        # Complexity penalty: count keys that actually moved.
        n_moved = sum(
            1 for k in _TUNABLE_KEYS
            if k in baseline and abs(candidate[k] - float(baseline[k])) > 1e-6
        )
        penalty = (safety.COMPLEXITY_PENALTY_BPS_PER_PARAM * n_moved) / 100.0
        return cand_summary["mean_pct"] - base_mean - penalty

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    if study.best_value <= 0:
        return None   # no TPE improvement -> don't even emit a proposal
    candidate = dict(baseline)
    for k, v in study.best_params.items():
        candidate[k] = float(v)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "best_value": float(study.best_value),
        "n_trials": n_trials,
    }


async def propose(pool: asyncpg.Pool, **search_kwargs) -> int | None:
    """Run search + write a tuning_proposals row if a candidate beats
    baseline. Returns the new proposal id, or None."""
    result = await search(pool, **search_kwargs)
    if result is None:
        return None
    # Reduce to a "changes-only" dict for clarity in the proposal body.
    changes = []
    for k, new_v in result["candidate"].items():
        old_v = result["baseline"].get(k)
        if old_v is None or abs(float(old_v) - float(new_v)) < 1e-6:
            continue
        changes.append({"key": k, "from": float(old_v), "to": float(new_v)})
    if not changes:
        return None
    proposal = {
        "generator": "numerical.tpe",
        "proposals": changes,
        "best_value_pct": result["best_value"],
        "n_trials": result["n_trials"],
    }
    rationale = (
        f"numerical.tpe found +{result['best_value']:.3f}% expectancy "
        f"over {len(changes)} param(s)"
    )
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO tuning_proposals
               (proposal, rationale, source, status)
               VALUES ($1::jsonb, $2, 'numerical', 'pending')
               RETURNING id""",
            proposal, rationale,
        )
    return int(row["id"])
