"""Circuit breakers — daily loss + drawdown + manual kill.

Called each bot tick before any scan/order activity. If any breaker trips,
the bot halts NEW entries (open positions keep running their stops). A trip
is sticky until the user clears `risk_state.tripped_at` via the dashboard or
direct DB update; auto-reset is intentionally absent so a bad day can't
silently resume trading.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TypedDict

import asyncpg

log = logging.getLogger("bot.risk")


class BreakerStatus(TypedDict):
    tripped: bool
    reason: str | None
    equity_hwm: float | None
    day_start_equity: float | None


async def _get_state(c: asyncpg.Connection) -> dict:
    row = await c.fetchrow("SELECT * FROM risk_state WHERE id=1")
    return dict(row) if row else {}


async def _set_tripped(c: asyncpg.Connection, reason: str) -> None:
    await c.execute(
        """UPDATE risk_state SET tripped_at=now(), tripped_reason=$1, updated_at=now()
           WHERE id=1""",
        reason,
    )
    log.warning("circuit_breaker_tripped reason=%s", reason)


async def evaluate(pool: asyncpg.Pool, cfg: dict, equity: float | None) -> BreakerStatus:
    """Update day-start/HWM if needed; trip if thresholds breached. equity is
    current NetLiquidation in EUR (IBKR base-currency account). None means we
    don't have a snapshot yet — treat as healthy (don't block startup)."""
    if cfg.get("CIRCUIT_BREAKER_ENABLED") is False:
        return {"tripped": False, "reason": None, "equity_hwm": None, "day_start_equity": None}

    today = date.today()
    daily_loss_pct = float(cfg.get("DAILY_LOSS_BREAKER_PCT", -2.0) or 0)
    drawdown_pct = float(cfg.get("DRAWDOWN_BREAKER_PCT", -10.0) or 0)

    async with pool.acquire() as c:
        st = await _get_state(c)
        hwm = float(st["equity_hwm"]) if st.get("equity_hwm") is not None else None
        day_start = float(st["day_start_equity"]) if st.get("day_start_equity") is not None else None
        day_date = st.get("day_start_date")
        tripped_at = st.get("tripped_at")
        tripped_reason = st.get("tripped_reason")

        if equity is not None:
            # HWM tracking.
            if hwm is None or equity > hwm:
                await c.execute(
                    "UPDATE risk_state SET equity_hwm=$1, updated_at=now() WHERE id=1",
                    equity,
                )
                hwm = equity
            # Daily rollover: new calendar day → snapshot day-start equity.
            if day_date != today or day_start is None:
                await c.execute(
                    """UPDATE risk_state SET day_start_equity=$1, day_start_date=$2,
                        updated_at=now() WHERE id=1""",
                    equity, today,
                )
                day_start = equity

        if tripped_at is not None:
            return {"tripped": True, "reason": tripped_reason,
                    "equity_hwm": hwm, "day_start_equity": day_start}

        if equity is not None and day_start and daily_loss_pct < 0:
            pnl_pct = (equity - day_start) / day_start * 100.0
            if pnl_pct <= daily_loss_pct:
                reason = f"daily_loss_breaker pnl={pnl_pct:.2f}% <= {daily_loss_pct}%"
                await _set_tripped(c, reason)
                return {"tripped": True, "reason": reason,
                        "equity_hwm": hwm, "day_start_equity": day_start}

        if equity is not None and hwm and drawdown_pct < 0:
            dd_pct = (equity - hwm) / hwm * 100.0
            if dd_pct <= drawdown_pct:
                reason = f"drawdown_breaker dd={dd_pct:.2f}% <= {drawdown_pct}%"
                await _set_tripped(c, reason)
                return {"tripped": True, "reason": reason,
                        "equity_hwm": hwm, "day_start_equity": day_start}

    return {"tripped": False, "reason": None,
            "equity_hwm": hwm, "day_start_equity": day_start}


# ── PR9: Auto-kill on drawdown ────────────────────────────────────────────────

async def check_auto_kill(pool: asyncpg.Pool, cfg: dict,
                            equity_eur: float | None) -> str | None:
    """Second-layer kill switch. Called every tick alongside evaluate().

    Trips BOT_ENABLED=false and sets config.AUTO_KILLED_REASON when any of:
      - today realized+unrealized P&L  ≤ -DAILY_LOSS_LIMIT_PCT   of day-start equity
      - last-5-days peak-to-current DD ≥  ROLLING_5D_DRAWDOWN_LIMIT_PCT
      - week-to-date realized P&L      ≤ -WEEKLY_LOSS_LIMIT_PCT  of week-start equity

    Does NOT close positions — the exit path (monitor_open_positions)
    continues, only new buys halt. AUTO_KILL_ENABLED=false bypasses; default
    TRUE per spec (this is a safety feature).

    No self-recover: once tripped, an operator must clear both
    BOT_ENABLED and AUTO_KILLED_REASON manually (dashboard button).
    """
    if cfg.get("AUTO_KILL_ENABLED") is False:
        return None

    daily_limit_pct = float(cfg.get("DAILY_LOSS_LIMIT_PCT", 2.0) or 0)
    dd5d_limit_pct = float(cfg.get("ROLLING_5D_DRAWDOWN_LIMIT_PCT", 5.0) or 0)
    weekly_limit_pct = float(cfg.get("WEEKLY_LOSS_LIMIT_PCT", 4.0) or 0)

    # Early-out if already killed — don't re-trip / re-log every tick.
    async with pool.acquire() as c:
        killed = await c.fetchrow(
            "SELECT value FROM config WHERE key='AUTO_KILLED_REASON'"
        )
    if killed and killed["value"] not in (None, "", "null"):
        return None

    if equity_eur is None or equity_eur <= 0:
        return None

    async with pool.acquire() as c:
        # Realized P&L today from closed positions (net of order fees).
        today_pnl = await c.fetchval(
            """SELECT COALESCE(SUM((exit_price-entry_price)*qty -
                      COALESCE((SELECT SUM(fees) FROM orders WHERE position_id=p.id), 0)), 0)
                 FROM positions p
                WHERE status='closed'
                  AND closed_at::date = CURRENT_DATE"""
        )
        # Unrealized P&L on open positions.
        unreal_pnl = await c.fetchval(
            """SELECT COALESCE(SUM((COALESCE(current_price, entry_price) - entry_price) * qty), 0)
                 FROM positions
                WHERE status IN ('opening','open','closing')"""
        )
        # Week-to-date realized.
        wtd_pnl = await c.fetchval(
            """SELECT COALESCE(SUM((exit_price-entry_price)*qty -
                      COALESCE((SELECT SUM(fees) FROM orders WHERE position_id=p.id), 0)), 0)
                 FROM positions p
                WHERE status='closed'
                  AND closed_at >= date_trunc('week', NOW())"""
        )
        # 5-day rolling equity-curve proxy: cumulative realized P&L per day for
        # the last 5 days plus today's unrealized. Drawdown = peak − current.
        dd_rows = await c.fetch(
            """SELECT closed_at::date AS d,
                       COALESCE(SUM((exit_price-entry_price)*qty -
                         COALESCE((SELECT SUM(fees) FROM orders WHERE position_id=p.id), 0)), 0) AS daily
                  FROM positions p
                 WHERE status='closed'
                   AND closed_at >= CURRENT_DATE - INTERVAL '5 days'
              GROUP BY closed_at::date
              ORDER BY closed_at::date"""
        )
        day_start_row = await c.fetchrow(
            "SELECT day_start_equity FROM risk_state WHERE id=1"
        )

    day_start = float(day_start_row["day_start_equity"]) if day_start_row and day_start_row["day_start_equity"] else equity_eur
    today_total = float(today_pnl or 0) + float(unreal_pnl or 0)
    today_pct = (today_total / day_start * 100.0) if day_start else 0.0

    cumulative: list[float] = []
    running = 0.0
    for r in dd_rows:
        running += float(r["daily"] or 0)
        cumulative.append(running)
    cumulative.append(running + float(unreal_pnl or 0))
    peak = max(cumulative) if cumulative else 0.0
    current = cumulative[-1] if cumulative else 0.0
    dd_5d_pct = ((current - peak) / max(day_start, 1) * 100.0) if day_start else 0.0

    wtd_pct = (float(wtd_pnl or 0) / day_start * 100.0) if day_start else 0.0

    reason: str | None = None
    if daily_limit_pct > 0 and today_pct <= -daily_limit_pct:
        reason = f"daily_loss_limit today_pct={today_pct:.2f}%"
    elif dd5d_limit_pct > 0 and dd_5d_pct <= -dd5d_limit_pct:
        reason = f"rolling_5d_drawdown_limit dd={dd_5d_pct:.2f}%"
    elif weekly_limit_pct > 0 and wtd_pct <= -weekly_limit_pct:
        reason = f"weekly_loss_limit wtd_pct={wtd_pct:.2f}%"

    if reason is None:
        return None

    ts = date.today().isoformat()
    full_reason = f"{reason} @ {ts}"
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ('BOT_ENABLED', 'false'::jsonb, 'risk:auto_kill')
                   ON CONFLICT (key) DO UPDATE SET value='false'::jsonb,
                                                    updated_by='risk:auto_kill',
                                                    updated_at=NOW()"""
            )
            await c.execute(
                """INSERT INTO config (key, value, updated_by)
                   VALUES ('AUTO_KILLED_REASON', to_jsonb($1::text), 'risk:auto_kill')
                   ON CONFLICT (key) DO UPDATE SET value=to_jsonb($1::text),
                                                    updated_by='risk:auto_kill',
                                                    updated_at=NOW()""",
                full_reason,
            )
    log.error("AUTO_KILL_TRIPPED reason=%s", full_reason)
    return full_reason
