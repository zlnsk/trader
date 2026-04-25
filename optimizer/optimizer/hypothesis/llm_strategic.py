"""Weekly strategic review.

Gives the LLM a broad picture of recent performance and asks for the
kind of observation a human notices but a Bayesian optimizer never
would: "this slot hasn't traded on Fridays in 2 months", "every win
came from 3 symbols", "risk_off regime accounts for 80% of losses".

These become either:
  - tuning_proposals (if the observation motivates a bounded-param change)
  - optimizer_findings with severity='info' (if it's an observation, not
    an action)

Always routes through the adversary for proposals. Findings require
human review before they become anything else.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .. import safety
from ..config_store import active_global_version
from ..config_store.versions import _values_of, get_managed_keys
from ..llm import chat, LLMError

log = logging.getLogger("optimizer.hypothesis.llm_strategic")


_SYSTEM = """You are the strategic reviewer for a trading optimizer.

You are given recent_findings — unresolved warnings and post-mortems from
prior operations. Use them as primary context: flag repeated patterns,
propose concrete mitigations for unresolved items, avoid re-surfacing
issues already marked resolved unless fresh evidence contradicts the fix.

You receive aggregated weekly stats + per-slot rolling metrics and
produce two outputs:

  findings: list of interesting observations. Each has {subject, body}
            and severity ('info'|'warning'|'critical').
  proposals: list of parameter-tuning proposals tied to a specific finding.

Ground every observation in the numbers you were given. If you have
none, return empty lists. DO NOT speculate.

Respond with JSON exactly shaped:
{
  "findings": [{"subject": "...", "body": "...", "severity": "info"}],
  "proposals": [{"key": "...", "from": 1.0, "to": 1.1, "reason": "..."}]
}
"""


async def _weekly_snapshot(pool: asyncpg.Pool) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with pool.acquire() as c:
        agg = await c.fetchrow(
            """SELECT COUNT(*) AS n,
                      AVG(net_pnl_pct) AS avg_pct,
                      SUM(net_pnl_eur) AS net_eur,
                      SUM(fees_eur) AS fees_eur
                 FROM trade_outcomes WHERE closed_at >= $1""",
            since,
        )
        per_slot = await c.fetch(
            """SELECT slot_id, COUNT(*) AS n,
                      AVG(net_pnl_pct) AS avg_pct,
                      SUM(net_pnl_eur) AS net_eur
                 FROM trade_outcomes WHERE closed_at >= $1
                 GROUP BY slot_id ORDER BY slot_id"""
            , since,
        )
        per_regime = await c.fetch(
            """SELECT entry_regime, COUNT(*) AS n,
                      AVG(net_pnl_pct) AS avg_pct
                 FROM trade_outcomes
                WHERE closed_at >= $1 AND entry_regime IS NOT NULL
                GROUP BY entry_regime""",
            since,
        )
        per_dow = await c.fetch(
            """SELECT entry_day_of_week AS dow, COUNT(*) AS n,
                      AVG(net_pnl_pct) AS avg_pct
                 FROM trade_outcomes
                WHERE closed_at >= $1 AND entry_day_of_week IS NOT NULL
                GROUP BY entry_day_of_week ORDER BY entry_day_of_week""",
            since,
        )
    # Pull recent post-mortem / unresolved findings so the LLM learns from past
    # incidents. Without this the reviewer is amnesiac — rediscovers the same
    # systemic issues each run.
    async with pool.acquire() as c:
        findings = await c.fetch(
            """SELECT ts, detector, severity, subject, body, resolution
                 FROM optimizer_findings
                WHERE ts >= $1
                  AND (resolution IS NULL OR severity IN ('critical','warning'))
                ORDER BY ts DESC LIMIT 20""",
            since,
        )
    return {
        "window_days": 7,
        "summary": dict(agg) if agg else {},
        "per_slot": [dict(r) for r in per_slot],
        "per_regime": [dict(r) for r in per_regime],
        "per_dow": [dict(r) for r in per_dow],
        "recent_findings": [dict(r) for r in findings],
    }


async def propose(pool: asyncpg.Pool) -> dict:
    snapshot = await _weekly_snapshot(pool)
    if (snapshot["summary"].get("n") or 0) < 20:
        log.info("llm_strategic: not enough trades (%s)",
                  snapshot["summary"].get("n"))
        return {"findings": [], "proposals": []}
    active = await active_global_version(pool)
    if active is None:
        return {"findings": [], "proposals": []}
    current = await _values_of(pool, active["id"])
    mk = await get_managed_keys(pool)
    managed = {k: {"min": v.min_value, "max": v.max_value}
                for k, v in mk.items()}
    try:
        parsed = await chat(
            pool, purpose="strategic_review",
            system=_SYSTEM,
            user=json.dumps({
                "snapshot": snapshot,
                "current_config": current,
                "managed_keys": managed,
            }, default=str),
            max_tokens=1600,
        )
    except LLMError as exc:
        log.warning("llm_strategic_llm_error: %s", exc)
        return {"findings": [], "proposals": []}

    findings_out = []
    for f in (parsed.get("findings") or [])[:10]:
        if not isinstance(f, dict):
            continue
        subject = f.get("subject")
        body = f.get("body")
        sev = (f.get("severity") or "info").lower()
        if sev not in ("info", "warning", "critical"):
            sev = "info"
        if not subject:
            continue
        async with pool.acquire() as c:
            row = await c.fetchrow(
                """INSERT INTO optimizer_findings
                   (detector, severity, subject, body, evidence)
                   VALUES ('llm_strategic',$1,$2,$3,$4::jsonb)
                   RETURNING id""",
                sev, subject, body or "",
                json.dumps(snapshot, default=str),
            )
        findings_out.append(int(row["id"]))

    proposals_out = []
    for p in (parsed.get("proposals") or [])[:3]:
        if not isinstance(p, dict):
            continue
        key = p.get("key")
        to = p.get("to")
        if key not in mk or not isinstance(to, (int, float)):
            continue
        async with pool.acquire() as c:
            row = await c.fetchrow(
                """INSERT INTO tuning_proposals
                   (proposal, rationale, source, status)
                   VALUES ($1::jsonb,$2,'llm_strategic','pending')
                   RETURNING id""",
                {
                    "generator": "llm_strategic",
                    "proposals": [{"key": key, "from": p.get("from"),
                                     "to": float(to)}],
                },
                f"llm_strategic: {p.get('reason', '')}",
            )
        proposals_out.append(int(row["id"]))

    log.info("llm_strategic_done", extra={
        "findings": findings_out, "proposals": proposals_out,
    })
    return {"findings": findings_out, "proposals": proposals_out}
