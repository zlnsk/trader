import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

const STRATEGY = 'overnight';

export async function GET() {
  try {
    const [enabledRow, openRows, closedAggRow, recentClosedRows, signalRows, pendingOrderRows] =
      await Promise.all([
        pool.query(`SELECT value FROM config WHERE key='OVERNIGHT_ENABLED'`),
        pool.query(
          `SELECT id, symbol, slot, status, entry_price, exit_price, qty,
                  current_price, opened_at, closed_at, sector, company_name
             FROM positions
            WHERE strategy = $1 AND status IN ('opening','open','closing')
            ORDER BY opened_at DESC`,
          [STRATEGY],
        ),
        pool.query(
          `SELECT
             COUNT(*) FILTER (WHERE status='closed')                        AS closed_count,
             COUNT(*) FILTER (WHERE status='closed'
                              AND exit_price > entry_price)                 AS wins,
             COUNT(*) FILTER (WHERE status='closed'
                              AND exit_price <= entry_price)                AS losses,
             COALESCE(
               SUM((exit_price - entry_price) * qty)
                 FILTER (WHERE status='closed'), 0)                          AS cumulative_pnl_eur,
             COALESCE(
               AVG(((exit_price - entry_price) / NULLIF(entry_price,0)) * 100)
                 FILTER (WHERE status='closed'), 0)                          AS avg_return_pct
           FROM positions
          WHERE strategy = $1
            AND entry_price IS NOT NULL AND exit_price IS NOT NULL`,
          [STRATEGY],
        ),
        pool.query(
          `SELECT id, symbol, entry_price, exit_price, qty, opened_at, closed_at,
                  ((exit_price - entry_price) / NULLIF(entry_price,0)) * 100 AS return_pct
             FROM positions
            WHERE strategy = $1 AND status = 'closed'
            ORDER BY closed_at DESC NULLS LAST
            LIMIT 20`,
          [STRATEGY],
        ),
        pool.query(
          `SELECT id, ts, symbol, quant_score, decision, reason, payload
             FROM signals
            WHERE strategy = $1
            ORDER BY ts DESC
            LIMIT 30`,
          [STRATEGY],
        ),
        pool.query(
          `SELECT o.id, o.position_id, o.side, o.status, o.client_order_id, o.ts,
                  p.symbol, p.slot
             FROM orders o
             JOIN positions p ON p.id = o.position_id
            WHERE p.strategy = $1
              AND o.status IN ('submitted','partial')
            ORDER BY o.ts DESC`,
          [STRATEGY],
        ),
      ]);

    const closed = Number(closedAggRow.rows[0]?.closed_count ?? 0);
    const wins = Number(closedAggRow.rows[0]?.wins ?? 0);
    const win_rate = closed > 0 ? wins / closed : null;

    return NextResponse.json(
      {
        strategy: STRATEGY,
        enabled: enabledRow.rows[0]?.value === true,
        open_positions: openRows.rows,
        pending_orders: pendingOrderRows.rows,
        metrics: {
          closed_count: closed,
          wins,
          losses: Number(closedAggRow.rows[0]?.losses ?? 0),
          win_rate,
          cumulative_pnl_eur: Number(closedAggRow.rows[0]?.cumulative_pnl_eur ?? 0),
          avg_return_pct: Number(closedAggRow.rows[0]?.avg_return_pct ?? 0),
        },
        recent_closed: recentClosedRows.rows,
        recent_signals: signalRows.rows,
      },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (e) {
    console.error('[api/strategies/overnight] query failed:', e);
    return NextResponse.json(
      { error: 'overnight_state_unavailable' },
      { status: 500 },
    );
  }
}
