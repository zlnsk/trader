"""Rolling-metric refresh jobs.

Per-table incremental updates. Idempotent: re-running at the same
`as_of_date` overwrites the same keys atomically. No locks against
trader writes — metrics tables are disjoint from positions/orders.

Consumers must check `metrics_refresh_state.as_of_ts` before reading.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import asyncpg

from .definitions import DEFN_VERSION, WINDOW_DAYS, compute_slot_metrics

log = logging.getLogger("optimizer.metrics.refresh")


async def _fetch_trades_window(pool: asyncpg.Pool, *, slot_id: int | None,
                                window_days: int,
                                as_of: datetime,
                                regime: str | None = None) -> list[dict]:
    cutoff = as_of - timedelta(days=window_days)
    q = [
        "SELECT position_id, slot_id, hold_seconds, net_pnl_pct, net_pnl_eur,",
        "       gross_pnl_eur, fees_eur, entry_regime, config_version_id",
        "FROM trade_outcomes WHERE closed_at >= $1 AND closed_at <= $2",
    ]
    params: list = [cutoff, as_of]
    if slot_id is not None:
        q.append("AND slot_id = $3")
        params.append(slot_id)
        if regime is not None:
            q.append("AND entry_regime = $4")
            params.append(regime)
    async with pool.acquire() as c:
        rows = await c.fetch(" ".join(q), *params)
    return [dict(r) for r in rows]


async def refresh_slot_rolling(pool: asyncpg.Pool,
                                as_of: datetime | None = None) -> int:
    as_of = as_of or datetime.now(timezone.utc)
    as_of_date = as_of.date()
    started = time.time()
    written = 0
    err: str | None = None

    try:
        async with pool.acquire() as c:
            slots = await c.fetch("SELECT DISTINCT slot_id FROM trade_outcomes")
        slot_ids = [r["slot_id"] for r in slots]

        for slot_id in slot_ids:
            for window in WINDOW_DAYS:
                trades = await _fetch_trades_window(
                    pool, slot_id=slot_id, window_days=window, as_of=as_of,
                )
                # Aggregate row: config_version_id=0.
                m = compute_slot_metrics(trades)
                await _upsert_slot_row(
                    pool, slot_id=slot_id, window_days=window,
                    as_of_date=as_of_date, config_version_id=0, m=m,
                )
                written += 1
                # Per-config rows only when more than one config
                # contributed trades in this window. Keeps cardinality
                # low during steady state.
                by_cfg: dict[int, list[dict]] = {}
                for t in trades:
                    by_cfg.setdefault(int(t.get("config_version_id") or 0), []).append(t)
                if len(by_cfg) > 1:
                    for cfg_id, subset in by_cfg.items():
                        if cfg_id == 0:
                            continue
                        mm = compute_slot_metrics(subset)
                        await _upsert_slot_row(
                            pool, slot_id=slot_id, window_days=window,
                            as_of_date=as_of_date,
                            config_version_id=cfg_id, m=mm,
                        )
                        written += 1
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.exception("refresh_slot_rolling failed")
    finally:
        duration_ms = int((time.time() - started) * 1000)
        await _mark_refresh_state(
            pool, "metrics_slot_rolling", as_of=as_of,
            duration_ms=duration_ms, rows=written, err=err,
        )
    return written


async def _upsert_slot_row(pool, *, slot_id, window_days, as_of_date,
                            config_version_id, m) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO metrics_slot_rolling
               (slot_id, window_days, as_of_date, config_version_id,
                defn_version, n_samples, win_rate, profit_factor,
                expectancy_bps, avg_hold_sec, sharpe_like, max_dd_pct,
                fees_eur, gross_pnl_eur, net_pnl_eur)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
               ON CONFLICT (slot_id, window_days, as_of_date,
                            config_version_id, defn_version)
               DO UPDATE SET
                 n_samples=EXCLUDED.n_samples,
                 win_rate=EXCLUDED.win_rate,
                 profit_factor=EXCLUDED.profit_factor,
                 expectancy_bps=EXCLUDED.expectancy_bps,
                 avg_hold_sec=EXCLUDED.avg_hold_sec,
                 sharpe_like=EXCLUDED.sharpe_like,
                 max_dd_pct=EXCLUDED.max_dd_pct,
                 fees_eur=EXCLUDED.fees_eur,
                 gross_pnl_eur=EXCLUDED.gross_pnl_eur,
                 net_pnl_eur=EXCLUDED.net_pnl_eur,
                 written_at=NOW()""",
            slot_id, window_days, as_of_date, config_version_id,
            DEFN_VERSION, m.n_samples,
            _num(m.win_rate), _num(m.profit_factor), _num(m.expectancy_bps),
            _num(m.avg_hold_sec), _num(m.sharpe_like), _num(m.max_dd_pct),
            m.fees_eur, m.gross_pnl_eur, m.net_pnl_eur,
        )


def _num(v: float | None) -> float | None:
    """Postgres NUMERIC accepts finite values; convert +/-inf to None."""
    if v is None:
        return None
    if v != v:  # NaN
        return None
    if v == float("inf") or v == float("-inf"):
        return None
    return v


async def refresh_regime_rolling(pool: asyncpg.Pool,
                                   as_of: datetime | None = None) -> int:
    as_of = as_of or datetime.now(timezone.utc)
    as_of_date = as_of.date()
    started = time.time()
    written = 0
    err = None
    try:
        async with pool.acquire() as c:
            combos = await c.fetch(
                """SELECT DISTINCT slot_id, entry_regime
                   FROM trade_outcomes WHERE entry_regime IS NOT NULL"""
            )
        for r in combos:
            slot_id = r["slot_id"]
            regime = r["entry_regime"]
            for window in WINDOW_DAYS:
                trades = await _fetch_trades_window(
                    pool, slot_id=slot_id, window_days=window,
                    as_of=as_of, regime=regime,
                )
                m = compute_slot_metrics(trades)
                async with pool.acquire() as c:
                    await c.execute(
                        """INSERT INTO metrics_regime_rolling
                           (slot_id, regime, window_days, as_of_date,
                            config_version_id, defn_version, n_samples,
                            win_rate, profit_factor, expectancy_bps, net_pnl_eur)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                           ON CONFLICT (slot_id, regime, window_days, as_of_date,
                                        config_version_id, defn_version)
                           DO UPDATE SET n_samples=EXCLUDED.n_samples,
                             win_rate=EXCLUDED.win_rate,
                             profit_factor=EXCLUDED.profit_factor,
                             expectancy_bps=EXCLUDED.expectancy_bps,
                             net_pnl_eur=EXCLUDED.net_pnl_eur,
                             written_at=NOW()""",
                        slot_id, regime, window, as_of_date, 0, DEFN_VERSION,
                        m.n_samples, _num(m.win_rate), _num(m.profit_factor),
                        _num(m.expectancy_bps), m.net_pnl_eur,
                    )
                written += 1
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.exception("refresh_regime_rolling failed")
    finally:
        duration_ms = int((time.time() - started) * 1000)
        await _mark_refresh_state(
            pool, "metrics_regime_rolling", as_of=as_of,
            duration_ms=duration_ms, rows=written, err=err,
        )
    return written


async def refresh_llm_rolling(pool: asyncpg.Pool,
                                as_of: datetime | None = None) -> int:
    """Derive per-touchpoint × verdict accuracy from hypothetical_outcome_pct
    on signal_snapshots. `allow` accurate when outcome >=0; `veto` accurate
    when outcome <0. `abstain` not scored (no commitment to a direction)."""
    as_of = as_of or datetime.now(timezone.utc)
    as_of_date = as_of.date()
    started = time.time()
    written = 0
    err = None
    try:
        for window in WINDOW_DAYS:
            cutoff = as_of - timedelta(days=window)
            async with pool.acquire() as c:
                rows = await c.fetch(
                    """SELECT strategy AS touchpoint, llm_verdict AS verdict,
                              COUNT(*) AS n,
                              AVG(CASE
                                    WHEN llm_verdict='allow' AND
                                         hypothetical_outcome_pct >= 0 THEN 1.0
                                    WHEN llm_verdict='veto'  AND
                                         hypothetical_outcome_pct < 0 THEN 1.0
                                    WHEN llm_verdict IN ('allow','veto') THEN 0.0
                                  END) AS accuracy
                       FROM signal_snapshots
                      WHERE snapshot_ts >= $1 AND snapshot_ts <= $2
                        AND llm_verdict IS NOT NULL
                        AND hypothetical_outcome_pct IS NOT NULL
                   GROUP BY strategy, llm_verdict""",
                    cutoff, as_of,
                )
            for r in rows:
                async with pool.acquire() as c:
                    await c.execute(
                        """INSERT INTO metrics_llm_rolling
                           (touchpoint, verdict, window_days, as_of_date,
                            defn_version, n_samples, accuracy, brier_score,
                            call_count, cost_eur)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,NULL,$8,NULL)
                           ON CONFLICT (touchpoint, verdict, window_days,
                                        as_of_date, defn_version)
                           DO UPDATE SET n_samples=EXCLUDED.n_samples,
                             accuracy=EXCLUDED.accuracy,
                             call_count=EXCLUDED.call_count,
                             written_at=NOW()""",
                        r["touchpoint"], r["verdict"], window, as_of_date,
                        DEFN_VERSION, int(r["n"] or 0),
                        float(r["accuracy"]) if r["accuracy"] is not None else None,
                        int(r["n"] or 0),
                    )
                written += 1
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.exception("refresh_llm_rolling failed")
    finally:
        duration_ms = int((time.time() - started) * 1000)
        await _mark_refresh_state(
            pool, "metrics_llm_rolling", as_of=as_of,
            duration_ms=duration_ms, rows=written, err=err,
        )
    return written


async def _mark_refresh_state(pool, table_name: str, *,
                                as_of: datetime, duration_ms: int,
                                rows: int, err: str | None) -> None:
    async with pool.acquire() as c:
        if err:
            await c.execute(
                """INSERT INTO metrics_refresh_state
                   (table_name, as_of_ts, duration_ms, rows_written,
                    last_error, last_error_ts)
                   VALUES ($1,$2,$3,$4,$5,NOW())
                   ON CONFLICT (table_name) DO UPDATE SET
                     last_error=EXCLUDED.last_error,
                     last_error_ts=NOW()""",
                table_name, as_of, duration_ms, rows, err,
            )
        else:
            await c.execute(
                """INSERT INTO metrics_refresh_state
                   (table_name, as_of_ts, duration_ms, rows_written,
                    last_error, last_error_ts)
                   VALUES ($1,$2,$3,$4,NULL,NULL)
                   ON CONFLICT (table_name) DO UPDATE SET
                     as_of_ts=EXCLUDED.as_of_ts,
                     duration_ms=EXCLUDED.duration_ms,
                     rows_written=EXCLUDED.rows_written""",
                table_name, as_of, duration_ms, rows,
            )


async def refresh_all(pool: asyncpg.Pool) -> dict:
    """Convenience: run all refreshers. Returns rows written per table."""
    out: dict[str, int] = {}
    out["slot"] = await refresh_slot_rolling(pool)
    out["regime"] = await refresh_regime_rolling(pool)
    out["llm"] = await refresh_llm_rolling(pool)
    return out
