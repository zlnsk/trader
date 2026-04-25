"""Weekly meta-learner report.

Looks at the optimizer's own track record:
  - per-source: n_proposed, n_validated, n_rejected, n_applied, n_rolled_back
  - net-improvement-actually-realised (apply events that weren't rolled back)
  - validator strictness: rejected/total
  - LLM cost per applied change (cost_per_win)
  - adversary gate failure histogram (which gate rejects most?)

Writes a row in optimizer_meta_reports. Never auto-acts. The weekly
report surfaces via dashboard — human decides if the optimizer itself
needs tuning.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

log = logging.getLogger("optimizer.meta")


async def generate_weekly(pool: asyncpg.Pool) -> int:
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    tag = f"{iso_year}-W{iso_week:02d}"

    async with pool.acquire() as c:
        existing = await c.fetchrow(
            "SELECT id FROM optimizer_meta_reports WHERE iso_week=$1", tag,
        )
    if existing:
        return int(existing["id"])

    since = now - timedelta(days=7)

    async with pool.acquire() as c:
        # Per-source funnel
        per_source = await c.fetch(
            """SELECT COALESCE(source, 'unknown') AS source,
                      COUNT(*) AS n_proposed,
                      COUNT(*) FILTER (WHERE status='validated') AS n_validated,
                      COUNT(*) FILTER (WHERE status='rejected') AS n_rejected,
                      COUNT(*) FILTER (WHERE status='applied') AS n_applied,
                      COUNT(*) FILTER (WHERE rolled_back_at IS NOT NULL) AS n_rolled_back
                 FROM tuning_proposals
                WHERE ts >= $1
             GROUP BY source""",
            since,
        )
        # Gate histogram from rejected proposals
        gate_hist = await c.fetch(
            """SELECT adversary_result->>'reason' AS gate,
                      COUNT(*) AS n
                 FROM tuning_proposals
                WHERE status='rejected' AND adversary_ts >= $1
             GROUP BY adversary_result->>'reason'""",
            since,
        )
        # LLM cost attributed to the optimizer this week
        llm_cost = await c.fetchrow(
            """SELECT COALESCE(SUM(cost_usd),0) AS total,
                      COUNT(*) AS n_calls
                 FROM llm_spend
                WHERE ts >= $1 AND touchpoint LIKE 'optimizer:%'""",
            since,
        )
        # Rollbacks this week
        rbs = await c.fetch(
            """SELECT trigger, COUNT(*) AS n FROM rollback_events
                WHERE ts >= $1 GROUP BY trigger""",
            since,
        )
        # Net P&L realised under the currently-active version
        realized = await c.fetchrow(
            """SELECT COALESCE(SUM(net_pnl_eur), 0) AS net_eur,
                      COUNT(*) AS n_trades
                 FROM trade_outcomes
                WHERE closed_at >= $1""",
            since,
        )

    per_source_out = [dict(r) for r in per_source]
    gate_hist_out = [dict(r) for r in gate_hist]
    report = {
        "iso_week": tag,
        "generated_at": now.isoformat(),
        "per_source": per_source_out,
        "gate_histogram": gate_hist_out,
        "llm_cost_usd": float(llm_cost["total"] or 0),
        "llm_call_count": int(llm_cost["n_calls"] or 0),
        "rollbacks_by_trigger": [dict(r) for r in rbs],
        "trading": {
            "n_trades": int(realized["n_trades"] or 0),
            "net_pnl_eur": float(realized["net_eur"] or 0),
        },
    }

    # Short human-readable summary
    best_source = None
    if per_source_out:
        valid_sources = [s for s in per_source_out
                          if (s.get("n_applied") or 0) > 0]
        if valid_sources:
            best_source = max(valid_sources,
                                key=lambda s: (s["n_applied"] - s["n_rolled_back"]))
    if best_source:
        summary = (
            f"{tag}: {realized['n_trades']} trades, "
            f"€{report['trading']['net_pnl_eur']:.2f} net. "
            f"Best source: {best_source['source']} "
            f"({best_source['n_applied']} applied, "
            f"{best_source['n_rolled_back']} rolled back). "
            f"LLM spend: ${report['llm_cost_usd']:.2f}."
        )
    else:
        summary = (
            f"{tag}: {realized['n_trades']} trades, "
            f"€{report['trading']['net_pnl_eur']:.2f} net. "
            f"No applied proposals this week. "
            f"LLM spend: ${report['llm_cost_usd']:.2f}."
        )

    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO optimizer_meta_reports
               (iso_week, report, summary)
               VALUES ($1, $2::jsonb, $3)
               RETURNING id""",
            tag, json.dumps(report, default=str), summary,
        )
    log.info("meta_report_written", extra={"tag": tag})
    return int(row["id"])
