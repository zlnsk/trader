"""The adversary.

A proposal enters here with status='pending'. Each gate tries to reject
it. Any gate failing -> rejected. A proposal passes only when EVERY
gate returns PASS (or MARGINAL where the gate allows it). The
complexity penalty is baked in at the expectancy gate.

Gates (in order — cheapest first):

  1. SAMPLE_SIZE        — do we have >= MIN_N_SAMPLES trades to reason about?
  2. PARAM_BOUNDS       — per-change cap & coercion
  3. COOLDOWN           — post-rollback cooldown
  4. REPLAY_IMPROVES    — expected profit > baseline + complexity penalty
  5. SUB_PERIOD         — improvement consistent across halves
  6. BOOTSTRAP          — paired-delta CI excludes zero
  7. REGIME             — no regime sacrifices another

A rejected proposal MUST record the first gate that failed. We stop
evaluating further gates — cheaper failure explains the behaviour.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .. import safety
from .bootstrap import paired_delta_ci, two_sample_delta_ci
from .replay import replay, summarise, ReplayedTrade

log = logging.getLogger("optimizer.validator")

PASS = "pass"
MARGINAL = "marginal"
REJECT = "reject"


@dataclass
class Gate:
    name: str
    verdict: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    overall: str                 # 'pass' | 'reject' | 'marginal'
    gates: list[Gate]
    reason: str | None = None
    n_baseline: int = 0
    n_candidate: int = 0

    def to_json(self) -> dict:
        return {
            "overall": self.overall,
            "reason": self.reason,
            "n_baseline": self.n_baseline,
            "n_candidate": self.n_candidate,
            "gates": [asdict(g) for g in self.gates],
        }


async def _fetch_snapshots(
    pool: asyncpg.Pool, *,
    lookback_days: int, slot_ids: list[int] | None = None,
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    q = [
        "SELECT id, symbol, slot_id, snapshot_ts, score, rsi,",
        "       sigma_below_sma20, stock_regime, crypto_regime,",
        "       hypothetical_outcome_pct",
        "FROM signal_snapshots",
        "WHERE snapshot_ts >= $1",
        "  AND hypothetical_outcome_pct IS NOT NULL",
    ]
    params: list = [cutoff]
    if slot_ids:
        q.append("AND slot_id = ANY($2::int[])")
        params.append(slot_ids)
    async with pool.acquire() as c:
        rows = await c.fetch(" ".join(q), *params)
    return [dict(r) for r in rows]


def _gate_sample_size(snapshots: list[dict]) -> Gate:
    n = len(snapshots)
    if n < safety.MIN_N_SAMPLES:
        return Gate(
            name="sample_size", verdict=REJECT,
            detail={"n": n, "required": safety.MIN_N_SAMPLES},
        )
    return Gate(name="sample_size", verdict=PASS, detail={"n": n})


def _gate_param_bounds(baseline: dict, candidate: dict) -> Gate:
    # Cap change magnitude on each key.
    for k, new in candidate.items():
        old = baseline.get(k)
        if old is None or not isinstance(old, (int, float)) \
                or not isinstance(new, (int, float)):
            continue
        if old == 0:
            if abs(new) > safety.MAX_SINGLE_CHANGE_PCT / 100.0:
                return Gate(
                    name="param_bounds", verdict=REJECT,
                    detail={"key": k, "from": old, "to": new,
                            "reason": "new from zero baseline"},
                )
            continue
        pct_change = abs((new - old) / old) * 100.0
        if pct_change > safety.MAX_SINGLE_CHANGE_PCT:
            return Gate(
                name="param_bounds", verdict=REJECT,
                detail={"key": k, "from": old, "to": new,
                        "pct_change": round(pct_change, 2),
                        "max": safety.MAX_SINGLE_CHANGE_PCT},
            )
    return Gate(name="param_bounds", verdict=PASS)


async def _gate_cooldown(
    pool: asyncpg.Pool, candidate_keys: list[str],
) -> Gate:
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=safety.POST_ROLLBACK_COOLDOWN_HOURS,
    )
    async with pool.acquire() as c:
        rb = await c.fetch(
            """SELECT rollback_events.ts, bad_version_id
               FROM rollback_events
               WHERE ts >= $1""",
            cutoff,
        )
        if not rb:
            return Gate(name="cooldown", verdict=PASS, detail={"recent": 0})
        bad_ids = [r["bad_version_id"] for r in rb]
        clashing = await c.fetch(
            """SELECT DISTINCT key FROM config_values
               WHERE version_id = ANY($1::int[])""",
            bad_ids,
        )
    clashing_keys = {r["key"] for r in clashing}
    conflict = set(candidate_keys) & clashing_keys
    if conflict:
        return Gate(
            name="cooldown", verdict=REJECT,
            detail={"blocked_keys": sorted(conflict),
                    "cooldown_hours": safety.POST_ROLLBACK_COOLDOWN_HOURS},
        )
    return Gate(name="cooldown", verdict=PASS, detail={"recent": len(rb)})


def _gate_replay_improves(
    trades: list[ReplayedTrade], *, n_changed_params: int,
) -> Gate:
    base = summarise(trades, "baseline_accept")
    cand = summarise(trades, "candidate_accept")
    if cand["n"] < safety.MIN_N_SAMPLES:
        return Gate(
            name="replay_improves", verdict=REJECT,
            detail={"reason": "candidate accepts < MIN_N_SAMPLES",
                    "n_candidate": cand["n"]},
        )
    penalty_bps = safety.COMPLEXITY_PENALTY_BPS_PER_PARAM * n_changed_params
    required_delta_pct = penalty_bps / 100.0   # bps → pct
    delta = cand["mean_pct"] - base["mean_pct"]
    if delta <= required_delta_pct:
        return Gate(
            name="replay_improves",
            verdict=REJECT,
            detail={"base_mean_pct": base["mean_pct"],
                    "cand_mean_pct": cand["mean_pct"],
                    "delta": delta,
                    "required_delta_pct": required_delta_pct,
                    "n_changed_params": n_changed_params},
        )
    return Gate(
        name="replay_improves", verdict=PASS,
        detail={"base_mean_pct": base["mean_pct"],
                "cand_mean_pct": cand["mean_pct"],
                "delta": delta},
    )


def _gate_sub_period(trades: list[ReplayedTrade]) -> Gate:
    # Sort by time and split in half. Both halves must show candidate >= baseline
    # (no regression flip).
    tl = sorted(trades, key=lambda t: t.snapshot_ts)
    n = len(tl)
    if n < 2 * safety.MIN_N_SAMPLES:
        return Gate(
            name="sub_period", verdict=MARGINAL,
            detail={"reason": "not enough samples to split", "n": n},
        )
    half = n // 2
    first = summarise(tl[:half], "candidate_accept")["mean_pct"] \
          - summarise(tl[:half], "baseline_accept")["mean_pct"]
    second = summarise(tl[half:], "candidate_accept")["mean_pct"] \
           - summarise(tl[half:], "baseline_accept")["mean_pct"]
    # Reject if sign disagrees or one half contributes >80% of the improvement.
    if first <= 0 or second <= 0:
        return Gate(
            name="sub_period", verdict=REJECT,
            detail={"first_delta": first, "second_delta": second,
                    "reason": "sign mismatch between halves"},
        )
    total = first + second
    if total == 0:
        concentration = 1.0
    else:
        concentration = max(abs(first), abs(second)) / abs(total)
    if concentration > 0.8:
        return Gate(
            name="sub_period", verdict=REJECT,
            detail={"first_delta": first, "second_delta": second,
                    "concentration": round(concentration, 2),
                    "reason": ">80% of improvement in one half"},
        )
    return Gate(
        name="sub_period", verdict=PASS,
        detail={"first_delta": first, "second_delta": second},
    )


def _gate_bootstrap(trades: list[ReplayedTrade]) -> Gate:
    """Two-sample bootstrap: CI of mean(candidate_accepted) -
    mean(baseline_accepted). Reject if the 95% CI contains zero."""
    base = [t.outcome_pct for t in trades if t.baseline_accept]
    cand = [t.outcome_pct for t in trades if t.candidate_accept]
    if len(base) < 10 or len(cand) < 10:
        return Gate(name="bootstrap", verdict=REJECT,
                     detail={"reason": "insufficient accepted in one arm",
                             "n_base": len(base), "n_cand": len(cand)})
    observed, lo, hi = two_sample_delta_ci(
        base, cand, n_samples=safety.BOOTSTRAP_SAMPLES, rng_seed=42,
    )
    if lo <= 0:
        return Gate(
            name="bootstrap", verdict=REJECT,
            detail={"observed": observed, "ci_lo": lo, "ci_hi": hi,
                    "reason": "95% CI includes zero"},
        )
    return Gate(name="bootstrap", verdict=PASS,
                 detail={"observed": observed, "ci_lo": lo, "ci_hi": hi})


def _gate_regime(trades: list[ReplayedTrade]) -> Gate:
    # Compare delta per regime. Reject if any regime with >=15 samples
    # regressed.
    by_regime: dict[str, list[ReplayedTrade]] = {}
    for t in trades:
        if not t.entry_regime:
            continue
        by_regime.setdefault(t.entry_regime, []).append(t)
    regressions = []
    for regime, subset in by_regime.items():
        if len(subset) < 15:
            continue
        b = summarise(subset, "baseline_accept")["mean_pct"]
        c = summarise(subset, "candidate_accept")["mean_pct"]
        if c < b - 0.05:  # >5bps regression in a regime
            regressions.append((regime, b, c))
    if regressions:
        return Gate(
            name="regime", verdict=REJECT,
            detail={"regressions": regressions,
                    "reason": "regime(s) regressed >5bps"},
        )
    return Gate(name="regime", verdict=PASS,
                 detail={"regimes_tested": len(by_regime)})


async def validate_proposal(
    pool: asyncpg.Pool,
    *,
    proposal_id: int,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    slot_ids: list[int] | None = None,
    lookback_days: int = 30,
) -> Verdict:
    """Run every gate. Stops at the first REJECT, returning that verdict."""
    changed_keys = [k for k, v in candidate.items() if baseline.get(k) != v]
    gates: list[Gate] = []
    reason: str | None = None

    snapshots = await _fetch_snapshots(
        pool, lookback_days=lookback_days, slot_ids=slot_ids,
    )

    async def _finalise(verdict: Verdict) -> Verdict:
        await _persist(pool, proposal_id, verdict)
        return verdict

    g = _gate_sample_size(snapshots)
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="sample_size"))

    g = _gate_param_bounds(baseline, candidate)
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="param_bounds"))

    g = await _gate_cooldown(pool, changed_keys)
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="cooldown"))

    trades = replay(snapshots, baseline=baseline, candidate=candidate)
    n_b = sum(1 for t in trades if t.baseline_accept)
    n_c = sum(1 for t in trades if t.candidate_accept)

    g = _gate_replay_improves(trades, n_changed_params=len(changed_keys))
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="replay_improves",
                                          n_baseline=n_b, n_candidate=n_c))

    g = _gate_sub_period(trades)
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="sub_period",
                                          n_baseline=n_b, n_candidate=n_c))

    g = _gate_bootstrap(trades)
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="bootstrap",
                                          n_baseline=n_b, n_candidate=n_c))

    g = _gate_regime(trades)
    gates.append(g)
    if g.verdict == REJECT:
        return await _finalise(Verdict(overall=REJECT, gates=gates,
                                          reason="regime",
                                          n_baseline=n_b, n_candidate=n_c))

    overall = PASS
    if any(x.verdict == MARGINAL for x in gates):
        overall = MARGINAL
    return await _finalise(Verdict(overall=overall, gates=gates,
                                      reason=None,
                                      n_baseline=n_b, n_candidate=n_c))


async def _persist(pool: asyncpg.Pool, proposal_id: int, v: Verdict) -> None:
    if proposal_id is None:
        return
    new_status = {PASS: "validated", MARGINAL: "awaiting_human",
                  REJECT: "rejected"}[v.overall]
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE tuning_proposals
               SET adversary_result=$1::jsonb, adversary_ts=NOW(),
                   status=$2
               WHERE id=$3""",
            json.dumps(v.to_json()), new_status, proposal_id,
        )
