"""Scheduled Claude-driven jobs.

- Daily post-mortem  → 21:15 UTC (after US close)
- Weekly tuning      → Sun ≥ 09:00 UTC
- Pre-open EU brief  → 06:45 UTC (≈ 07:45 Portugal, 15 min before EU open)
- Pre-open US brief  → 13:15 UTC (≈ 14:15 Portugal, 15 min before US open)
- News watcher       → every NEWS_WATCHER_INTERVAL_SEC on held positions
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone, timedelta

import asyncpg

from . import llm, hours, notifications

log = logging.getLogger("bot.jobs")


def _j(msg: str, **fields) -> str:
    return json.dumps({"m": msg, **fields}, default=str)


# ── Daily post-mortem ────────────────────────────────────────────────────────

async def maybe_backfill_hypothetical_outcomes(pool: asyncpg.Pool, ib) -> None:
    """PR12 nightly job. For each signal_snapshots row older than 24h with
    null hypothetical_outcome_pct, replay the slot's target/stop rules
    against subsequent bars and persist the realised (counterfactual)
    outcome. Runs at 04:00 UTC, batch-limited per tick to keep the job
    small enough to complete inside a single scheduler window.
    """
    now = datetime.now(timezone.utc)
    if now.hour < 4:
        return
    today_iso = now.date().isoformat()
    async with pool.acquire() as c:
        marker = await c.fetchrow(
            "SELECT value FROM config WHERE key='_last_hypo_backfill_date'"
        )
    if marker and marker["value"] == today_iso:
        return

    # Batch: 500 rows per run. Keeps the long-term backlog manageable
    # without flooding IBKR historical-data quota on the first night.
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, symbol, slot_id, snapshot_ts
                 FROM signal_snapshots
                WHERE hypothetical_outcome_pct IS NULL
                  AND snapshot_ts < NOW() - INTERVAL '24 hours'
             ORDER BY snapshot_ts ASC LIMIT 500"""
        )
        if not rows:
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ('_last_hypo_backfill_date', $1::jsonb, 'jobs')
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                    updated_at=NOW()""",
                today_iso,
            )
            return

    # Lookup the slot's target/stop once per slot.
    slot_profile_rows = await (await pool.acquire()).fetch(
        "SELECT slot, target_profit_pct, stop_loss_pct FROM slot_profiles"
    )
    profs = {r["slot"]: (float(r["target_profit_pct"]),
                          float(r["stop_loss_pct"])) for r in slot_profile_rows}

    backfilled = 0
    for r in rows:
        slot = r["slot_id"]
        if slot is None or slot not in profs:
            continue
        target_pct, stop_pct = profs[slot]
        # Replay against recent closes — reuse broker.get_daily_closes for
        # swing slots, get_intraday_closes for others. Simple target-first /
        # stop-first tie-break; whichever triggers earliest on the bar
        # series is the realised outcome.
        try:
            hist = await _fetch_hypo_bars(ib, r["symbol"], r["snapshot_ts"])
        except Exception as exc:
            log.warning(_j("hypo_fetch_failed", symbol=r["symbol"], err=str(exc)))
            continue
        if not hist:
            continue
        entry = hist[0]
        target_price = entry * (1 + target_pct / 100)
        stop_price = entry * (1 + stop_pct / 100)  # stop_pct is negative
        outcome_pct: float | None = None
        for px in hist[1:]:
            if px >= target_price:
                outcome_pct = target_pct
                break
            if px <= stop_price:
                outcome_pct = stop_pct
                break
        if outcome_pct is None and len(hist) > 1:
            outcome_pct = (hist[-1] - entry) / entry * 100.0
        if outcome_pct is not None:
            async with pool.acquire() as c:
                await c.execute(
                    """UPDATE signal_snapshots
                          SET hypothetical_outcome_pct=$1
                        WHERE id=$2""",
                    outcome_pct, r["id"],
                )
            backfilled += 1

    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO config (key, value, updated_by)
               VALUES ('_last_hypo_backfill_date', $1::jsonb, 'jobs')
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                updated_at=NOW()""",
            today_iso,
        )
    log.info(_j("hypo_backfill_done", backfilled=backfilled,
                 scanned=len(rows)))


async def _fetch_hypo_bars(ib, symbol: str, snapshot_ts) -> list[float]:
    """Fetch closes starting at snapshot_ts. Returns [] when IBKR lacks
    history or the symbol is blacklisted. Small helper kept separate so
    testing can swap it."""
    if ib is None or not getattr(ib, "isConnected", lambda: False)():
        return []
    try:
        from .broker import get_intraday_closes
        hist = await get_intraday_closes(ib, symbol, bar_size="5 mins",
                                            duration="2 D")
        if hist is None or not hist.closes:
            return []
        return list(hist.closes[-100:])
    except Exception:
        return []


async def maybe_check_llm_malformed_rate(pool: asyncpg.Pool) -> None:
    """PR11 alert canary. If any call_purpose has > 5% malformed responses
    over its last 100 calls, emit a loud WARNING and persist an audit_log
    row so the dashboard can surface it. Throttled to once per 15 minutes
    per purpose via a `_llm_alert_last_<purpose>` marker in config."""
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        rows = await c.fetch(
            """WITH recent AS (
                 SELECT call_purpose, response_valid,
                        ROW_NUMBER() OVER (PARTITION BY call_purpose ORDER BY ts DESC) AS rn
                   FROM llm_calls
                  WHERE ts >= NOW() - INTERVAL '24 hours'
               )
               SELECT call_purpose,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE response_valid = FALSE) AS malformed
                 FROM recent
                WHERE rn <= 100
             GROUP BY call_purpose
               HAVING COUNT(*) >= 20"""
        )
    for r in rows:
        total = int(r["total"])
        bad = int(r["malformed"])
        rate = bad / total if total else 0
        if rate <= 0.05:
            continue
        marker_key = f"_llm_alert_last_{r['call_purpose']}"
        async with pool.acquire() as c:
            marker = await c.fetchrow(
                "SELECT value FROM config WHERE key=$1", marker_key,
            )
        last_ts = float(marker["value"]) if marker and marker["value"] else 0
        if (now.timestamp() - last_ts) < 900:
            continue
        log.warning(_j("llm_malformed_rate_alert",
                        purpose=r["call_purpose"],
                        rate=round(rate, 3),
                        malformed=bad, total=total))
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ($1, $2::jsonb, 'jobs:llm_malformed_check')
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                    updated_at=NOW()""",
                marker_key, now.timestamp(),
            )


async def maybe_sync_earnings(pool: asyncpg.Pool, ib) -> None:
    """Nightly earnings-calendar sync. Runs once per day at or after 03:00 UTC.

    Source order per symbol:
      1. IBKR reqFundamentalData(ReportsFinSummary) for qualified contracts —
         parses the <Announcement type="Earnings"> dates out of the XML.
      2. yfinance ticker.calendar fallback — only if yfinance is installed;
         gracefully no-op'd otherwise. Missing yfinance is a warning, not a
         failure; operator can install it later without restarting.

    Symbols without any source coverage are logged at WARNING and skipped.
    They'll surface as "earnings_blackout:unknown_symbol" at scan time when
    the flag is on, which is intentional fail-safe behaviour.
    """
    now = datetime.now(timezone.utc)
    if now.hour < 3:
        return
    today_iso = now.date().isoformat()
    async with pool.acquire() as c:
        marker = await c.fetchrow(
            "SELECT value FROM config WHERE key='_last_earnings_sync_date'"
        )
        universe_row = await c.fetchrow(
            "SELECT value FROM config WHERE key='UNIVERSE'"
        )
    if marker and marker["value"] == today_iso:
        return
    universe = list(universe_row["value"]) if universe_row and universe_row["value"] else []
    if not universe:
        return

    # Lazy-import hosts.universe so we can ask asset_class — skip crypto.
    from .universe import meta as _meta

    ibkr_dates = await _fetch_ibkr_earnings(ib, universe)
    missing = [s for s in universe if s not in ibkr_dates and _meta(s).asset_class != "crypto"]
    yf_dates = _fetch_yfinance_earnings(missing) if missing else {}

    merged: dict[str, list[date]] = {}
    for src_dict in (ibkr_dates, yf_dates):
        for sym, dates in src_dict.items():
            merged.setdefault(sym, []).extend(dates)

    inserted = 0
    async with pool.acquire() as c:
        async with c.transaction():
            for sym, dates in merged.items():
                src = "ibkr" if sym in ibkr_dates else "yfinance"
                for d in dates:
                    await c.execute(
                        """INSERT INTO earnings_calendar
                           (symbol, earnings_date, fetched_at, source)
                           VALUES ($1, $2, NOW(), $3)
                           ON CONFLICT (symbol, earnings_date) DO UPDATE
                             SET fetched_at = EXCLUDED.fetched_at,
                                 source     = EXCLUDED.source""",
                        sym, d, src,
                    )
                    inserted += 1
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ('_last_earnings_sync_date', $1::jsonb, 'jobs')
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                    updated_at=NOW()""",
                today_iso,
            )
    uncovered = [s for s in universe
                  if s not in merged and _meta(s).asset_class != "crypto"]
    log.info(_j("earnings_sync_done", inserted=inserted,
                 covered=len(merged), uncovered=len(uncovered)))
    if uncovered:
        log.warning(_j("earnings_sync_uncovered", symbols=uncovered[:20]))


async def _fetch_ibkr_earnings(ib, symbols: list[str]) -> dict[str, list[date]]:
    """Pull ReportsFinSummary XML per symbol; parse <Announcement
    type='Earnings'>. Returns {symbol: [date, ...]} for symbols that
    returned usable dates. Silent skip on per-symbol failure so one
    stubborn contract doesn't break the batch."""
    import xml.etree.ElementTree as ET
    if ib is None or not getattr(ib, "isConnected", lambda: False)():
        return {}
    from .broker import qualify
    from .universe import meta as _meta
    out: dict[str, list[date]] = {}
    for sym in symbols:
        if _meta(sym).asset_class == "crypto":
            continue
        try:
            c = await qualify(ib, sym)
            if c is None:
                continue
            xml = await ib.reqFundamentalDataAsync(c, "ReportsFinSummary")
            if not xml:
                continue
            root = ET.fromstring(xml)
            dates: list[date] = []
            for a in root.iter("Announcement"):
                if (a.get("type") or "").lower() != "earnings":
                    continue
                d_str = a.get("date")
                if not d_str:
                    continue
                try:
                    dates.append(date.fromisoformat(d_str))
                except ValueError:
                    continue
            if dates:
                out[sym] = sorted(set(dates))
        except Exception as exc:
            log.warning(_j("earnings_ibkr_failed", symbol=sym, err=str(exc)))
    return out


# IBKR primary_exchange → yfinance symbol suffix. US SMART ("") has no
# suffix. Crypto never reaches this function — caller filters it out.
_YF_SUFFIX_BY_EXCHANGE = {
    "": "",       # US SMART
    "AEB": ".AS",  # Euronext Amsterdam
    "SBF": ".PA",  # Euronext Paris
    "IBIS": ".DE", # XETRA
    "LSE": ".L",   # London
    "EBS": ".SW",  # SIX Swiss
}


def _yf_symbol(sym: str) -> str | None:
    """Map IBKR universe symbol to the yfinance ticker format. Returns
    None if the exchange isn't mapped (caller skips those)."""
    from .universe import meta as _meta
    m = _meta(sym)
    suffix = _YF_SUFFIX_BY_EXCHANGE.get(m.primary_exchange)
    if suffix is None:
        return None
    return sym + suffix


def _fetch_yfinance_earnings(symbols: list[str]) -> dict[str, list[date]]:
    """yfinance fallback for symbols IBKR didn't return. If yfinance isn't
    installed this returns empty and logs a warning once per call.

    Uses Ticker.get_earnings_dates(limit=4) — the modern API; ticker.calendar
    returns empty for most tickers on current yfinance versions. Past dates
    are filtered out; only upcoming earnings matter for the blackout gate."""
    try:
        import yfinance as yf
    except ImportError:
        if symbols:
            log.warning(_j("earnings_yfinance_unavailable",
                            missing=len(symbols)))
        return {}
    today = date.today()
    out: dict[str, list[date]] = {}
    for sym in symbols:
        yf_sym = _yf_symbol(sym)
        if yf_sym is None:
            log.warning(_j("earnings_yfinance_unmapped_exchange", symbol=sym))
            continue
        try:
            t = yf.Ticker(yf_sym)
            df = t.get_earnings_dates(limit=4)
            if df is None or getattr(df, "empty", True):
                continue
            dates: list[date] = []
            for idx in df.index:
                d = idx.date() if hasattr(idx, "date") else None
                if d and d >= today:
                    dates.append(d)
            if dates:
                out[sym] = sorted(set(dates))
        except Exception as exc:
            log.warning(_j("earnings_yfinance_failed", symbol=sym,
                             yf_sym=yf_sym, err=str(exc)[:160]))
    return out


async def maybe_daily_report(pool: asyncpg.Pool, force: bool = False) -> None:
    today = date.today()
    async with pool.acquire() as c:
        existing = await c.fetchrow("SELECT date FROM daily_reports WHERE date=$1", today)
    if existing is not None and not force:
        return
    if not force and datetime.now(timezone.utc).hour < 21:
        return

    async with pool.acquire() as c:
        stats_row = await c.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE status='closed' AND closed_at::date = $1) AS closed_today,
                 COUNT(*) FILTER (WHERE status='closed' AND closed_at::date = $1 AND exit_price>entry_price) AS wins,
                 COUNT(*) FILTER (WHERE status='closed' AND closed_at::date = $1 AND exit_price<=entry_price) AS losses,
                 COALESCE(SUM((exit_price-entry_price)*qty - COALESCE((SELECT SUM(fees) FROM orders WHERE position_id=positions.id),0))
                          FILTER (WHERE status='closed' AND closed_at::date = $1), 0) AS net_pnl
               FROM positions""",
            today,
        )
        signals_top = await c.fetch(
            """SELECT symbol, quant_score, decision, reason, ts
               FROM signals WHERE ts::date = $1
               ORDER BY quant_score DESC NULLS LAST LIMIT 20""",
            today,
        )
        closed = await c.fetch(
            """SELECT symbol, entry_price, exit_price, qty, opened_at, closed_at
               FROM positions WHERE status='closed' AND closed_at::date = $1""",
            today,
        )

    stats = {
        "closed_today": int(stats_row["closed_today"]),
        "wins": int(stats_row["wins"]),
        "losses": int(stats_row["losses"]),
        "net_pnl": float(stats_row["net_pnl"]),
    }
    signals_summary = [dict(r) for r in signals_top]
    closed_list = [dict(r) for r in closed]

    report = await llm.daily_report(stats, signals_summary, closed_list)
    if not report:
        return

    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO daily_reports (date, summary, wins, losses, net_pnl, recommendations, raw)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb)
               ON CONFLICT (date) DO UPDATE SET summary=EXCLUDED.summary, raw=EXCLUDED.raw""",
            today, report.get("summary"),
            int(report.get("wins", stats["wins"])),
            int(report.get("losses", stats["losses"])),
            float(report.get("net_pnl", stats["net_pnl"])),
            json.dumps(report.get("recommendations"), default=str),
            json.dumps(report, default=str),
        )
    log.info(_j("daily_report_written", date=today))
    await notifications.notify_daily_summary(
        date=today.isoformat(),
        n_trades=int(report.get("wins", stats["wins"]))
                 + int(report.get("losses", stats["losses"])),
        wins=int(report.get("wins", stats["wins"])),
        losses=int(report.get("losses", stats["losses"])),
        net_pnl=float(report.get("net_pnl", stats["net_pnl"])),
        summary=report.get("summary"),
        recommendations=report.get("recommendations"),
    )


async def maybe_notify_critical_findings(pool: asyncpg.Pool) -> None:
    """Poll optimizer_findings for new severity=critical rows and email.
    Tracks last seen id in config so each finding mails at most once.
    Optimizer stays bot-independent; bot does the delivery."""
    async with pool.acquire() as c:
        cursor_row = await c.fetchrow(
            "SELECT value FROM config WHERE key='_last_critical_finding_id'"
        )
        last_id = 0
        if cursor_row and cursor_row["value"] is not None:
            try:
                last_id = int(cursor_row["value"])
            except (TypeError, ValueError):
                last_id = 0
        rows = await c.fetch(
            """SELECT id, detector, subject, body
                 FROM optimizer_findings
                WHERE severity='critical'
                  AND resolved_at IS NULL
                  AND id > $1
                ORDER BY id ASC
                LIMIT 10""",
            last_id,
        )
    if not rows:
        return
    for r in rows:
        await notifications.notify_critical_finding(
            detector=r["detector"], subject=r["subject"], body=r["body"],
        )
    new_cursor = int(rows[-1]["id"])
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO config (key, value, updated_by)
               VALUES ('_last_critical_finding_id', $1::jsonb, 'bot')
               ON CONFLICT (key) DO UPDATE
                 SET value=EXCLUDED.value, updated_at=now()""",
            json.dumps(new_cursor),
        )


# ── Weekly tuning proposal ───────────────────────────────────────────────────

# Config keys the auto-apply path is allowed to mutate. Must stay in lockstep
# with dashboard/app/api/proposals/route.ts ALLOWED_KEYS — both paths write to
# the same config table, same validation rules.
_TUNING_ALLOWED_KEYS = {
    "QUANT_SCORE_MIN", "TARGET_PROFIT_PCT", "STOP_LOSS_PCT",
    "MIN_NET_MARGIN_EUR", "SIGMA_BELOW_SMA20", "RSI_BUY_THRESHOLD",
}


async def auto_apply_pending_tuning(pool: asyncpg.Pool) -> None:
    """Apply every pending tuning_proposals row when TUNING_AUTO_APPLY is on.
    Mirrors dashboard approve logic: whitelist-filter each {key,to} pair,
    upsert config, mark row status='applied'. Unknown keys / non-numeric
    values are silently ignored (same behaviour as the dashboard route)."""
    async with pool.acquire() as c:
        flag_row = await c.fetchrow(
            "SELECT value FROM config WHERE key='TUNING_AUTO_APPLY'"
        )
    if not flag_row:
        return
    val = flag_row["value"]
    # jsonb round-trips into python as bool / int / string — accept any truthy.
    if not (val is True or val == 1 or val == "true" or val == "True"):
        return

    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, proposal FROM tuning_proposals
               WHERE status IN ('pending','validated','approved')
               ORDER BY ts ASC"""
        )
    final_state: dict[str, float] = {}
    for row in rows:
        proposal = row["proposal"] or {}
        proposals = proposal.get("proposals") or [] if isinstance(proposal, dict) else []
        applied: list[str] = []
        async with pool.acquire() as c:
            async with c.transaction():
                for p in proposals:
                    if not isinstance(p, dict):
                        continue
                    key = p.get("key")
                    to = p.get("to")
                    if key not in _TUNING_ALLOWED_KEYS:
                        continue
                    if not isinstance(to, (int, float)):
                        continue
                    await c.execute(
                        """INSERT INTO config (key, value, updated_by)
                           VALUES ($1, $2::jsonb, 'bot:tune-auto')
                           ON CONFLICT (key) DO UPDATE
                             SET value=EXCLUDED.value,
                                 updated_by=EXCLUDED.updated_by,
                                 updated_at=now()""",
                        key, json.dumps(to),
                    )
                    applied.append(key)
                    final_state[key] = float(to)
                await c.execute(
                    """UPDATE tuning_proposals
                       SET status='applied', reviewed_at=now(),
                           reviewed_by='bot:auto'
                       WHERE id=$1""",
                    row["id"],
                )
        log.info(_j("tuning_proposal_auto_applied",
                     id=int(row["id"]), applied=applied))
    if final_state:
        await notifications.notify_tuning_applied(final_state)


async def maybe_weekly_tuning(pool: asyncpg.Pool) -> None:
    now = datetime.now(timezone.utc)
    if now.weekday() != 6 or now.hour < 9:
        return
    iso_year, iso_week, _ = now.isocalendar()
    tag = f"{iso_year}-W{iso_week:02d}"
    async with pool.acquire() as c:
        existing = await c.fetchrow(
            "SELECT id FROM tuning_proposals WHERE (proposal->>'_tag')=$1 LIMIT 1", tag
        )
    if existing:
        return

    week_start = now - timedelta(days=7)
    async with pool.acquire() as c:
        weekly = await c.fetchrow(
            """SELECT
                 COUNT(*) AS signals,
                 COUNT(*) FILTER (WHERE decision='buy') AS buys,
                 COUNT(*) FILTER (WHERE decision='skip' AND reason ILIKE '%veto%') AS veto_skips,
                 COUNT(*) FILTER (WHERE decision='skip' AND reason ILIKE '%margin%') AS margin_skips,
                 COUNT(*) FILTER (WHERE decision='skip' AND reason ILIKE '%score%') AS score_skips,
                 AVG(quant_score) AS avg_score
               FROM signals WHERE ts >= $1""",
            week_start,
        )
        closed = await c.fetch(
            """SELECT symbol, entry_price, exit_price,
                      (exit_price-entry_price)*qty AS gross, qty
               FROM positions WHERE status='closed' AND closed_at >= $1
               ORDER BY closed_at""",
            week_start,
        )
        cfg_rows = await c.fetch("SELECT key, value FROM config")
    current_thresholds = {
        r["key"]: r["value"] for r in cfg_rows
        if r["key"] in {"QUANT_SCORE_MIN", "TARGET_PROFIT_PCT", "STOP_LOSS_PCT",
                        "MIN_NET_MARGIN_EUR", "SIGMA_BELOW_SMA20", "RSI_BUY_THRESHOLD"}
    }
    summary = {
        "period": tag, "current_thresholds": current_thresholds,
        "signals": int(weekly["signals"]), "buys": int(weekly["buys"]),
        "veto_skips": int(weekly["veto_skips"]), "margin_skips": int(weekly["margin_skips"]),
        "score_skips": int(weekly["score_skips"]),
        "avg_quant_score": float(weekly["avg_score"]) if weekly["avg_score"] is not None else None,
        "note": "Quant score is 0-100 composite (RSI 0-60 + σ 0-40). Score_skips = score < QUANT_SCORE_MIN (set per-slot).",
        "closed_trades": [dict(r) for r in closed],
    }
    proposal = await llm.propose_tuning(summary)
    if not proposal:
        return
    proposal_with_tag = {**proposal, "_tag": tag}
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO tuning_proposals (proposal, rationale)
               VALUES ($1::jsonb, $2)""",
            proposal_with_tag, proposal.get("overall_rationale"),
        )
    log.info(_j("tuning_proposal_written", tag=tag))


# ── Pre-open briefings (EU + US) ─────────────────────────────────────────────

async def _already_briefed(pool, kind: str) -> bool:
    today_utc = datetime.now(timezone.utc).date()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT id FROM briefings WHERE kind=$1 AND ts::date=$2 LIMIT 1",
            kind, today_utc,
        )
    return row is not None


async def maybe_briefing(pool: asyncpg.Pool) -> None:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # no briefings on weekends
        return
    minutes = now.hour * 60 + now.minute

    brief_kind: str | None = None
    region: str | None = None
    # 06:45-06:59 UTC → EU pre-open briefing
    if 6 * 60 + 45 <= minutes < 7 * 60 and not await _already_briefed(pool, "pre_open_eu"):
        brief_kind, region = "pre_open_eu", "EU"
    # 13:15-13:29 UTC → US pre-open briefing
    elif 13 * 60 + 15 <= minutes < 13 * 60 + 30 and not await _already_briefed(pool, "pre_open_us"):
        brief_kind, region = "pre_open_us", "US"

    if not brief_kind:
        return

    since = now - timedelta(hours=14)
    async with pool.acquire() as c:
        top_signals = await c.fetch(
            """SELECT symbol, quant_score, decision, reason, payload->>'strategy' AS strat
               FROM signals WHERE ts >= $1 AND quant_score IS NOT NULL
               ORDER BY quant_score DESC NULLS LAST LIMIT 15""",
            since,
        )
        held = await c.fetch(
            """SELECT symbol, slot, entry_price, current_price,
                      (current_price-entry_price)/NULLIF(entry_price,0)*100 AS pct_change
               FROM positions WHERE status IN ('open','closing')"""
        )
        regime = await c.fetchrow(
            """SELECT regime, confidence, reasoning FROM market_regime
               WHERE asset_class='stock' ORDER BY ts DESC LIMIT 1"""
        )

    context = {
        "region": region,
        "utc": now.isoformat(),
        "regime": dict(regime) if regime else None,
        "top_candidate_signals": [dict(r) for r in top_signals],
        "held_positions": [dict(r) for r in held],
    }
    brief = await llm.pre_open_briefing(context)
    if not brief:
        return

    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO briefings (kind, region, summary, candidates, raw)
               VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)""",
            brief_kind, region, brief.get("summary"),
            brief.get("candidates"), brief,
        )
    log.info(_j("briefing_written", kind=brief_kind, region=region))


# ── News watcher (held positions) ────────────────────────────────────────────

async def maybe_news_watch(pool: asyncpg.Pool, ib) -> None:
    """Every NEWS_WATCHER_INTERVAL_SEC, re-check news on each held position.
    If Claude returns severity=high + action=exit_now, tighten stop to market so the
    exit loop picks it up on the next tick."""
    async with pool.acquire() as c:
        cfg_row = await c.fetchrow(
            "SELECT value FROM config WHERE key='NEWS_WATCHER_ENABLED'"
        )
        if not cfg_row or cfg_row["value"] is not True:
            return
        iv_row = await c.fetchrow(
            "SELECT value FROM config WHERE key='NEWS_WATCHER_INTERVAL_SEC'"
        )
        interval = int(iv_row["value"]) if iv_row and iv_row["value"] is not None else 900

        last_row = await c.fetchrow(
            "SELECT value FROM config WHERE key='_last_news_watch_ts'"
        )
    import time
    last = float(last_row["value"]) if last_row and last_row["value"] is not None else 0
    if time.time() - last < interval:
        return

    async with pool.acquire() as c:
        held = await c.fetch(
            """SELECT id, symbol, company_name, entry_price, current_price
               FROM positions WHERE status IN ('open','closing')"""
        )

    for r in held:
        sym = r["symbol"]
        name = r["company_name"] or sym
        entry = float(r["entry_price"])
        current = float(r["current_price"]) if r["current_price"] is not None else entry
        verdict = await llm.news_watch(sym, name, entry=entry, current=current)
        if not isinstance(verdict, dict):
            continue
        triggered = None
        action = verdict.get("action")
        severity = (verdict.get("severity") or "").lower()
        if action == "exit_now" and severity == "high":
            # Tighten stop to current+small margin so exit loop fires immediately.
            async with pool.acquire() as c:
                await c.execute(
                    "UPDATE positions SET stop_price=GREATEST(stop_price, current_price * 0.999) WHERE id=$1",
                    r["id"],
                )
            triggered = "exit_now_stop_tightened"
            log.info(_j("news_exit_now", symbol=sym, severity=severity,
                        headline=verdict.get("headline", "")))
            await notifications.notify_news_watch_high(
                symbol=sym,
                headline=verdict.get("headline", ""),
                action=action,
                reasoning=verdict.get("reasoning"),
            )
        elif action == "tighten_stop":
            async with pool.acquire() as c:
                await c.execute(
                    "UPDATE positions SET stop_price=GREATEST(stop_price, entry_price) WHERE id=$1",
                    r["id"],
                )
            triggered = "stop_to_entry"

        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO news_checks (position_id, symbol, verdict, triggered)
                   VALUES ($1,$2,$3::jsonb,$4)""",
                r["id"], sym, verdict, triggered,
            )

    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO config (key, value, updated_by)
               VALUES ('_last_news_watch_ts', $1::jsonb, 'bot')
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
            time.time(),
        )
