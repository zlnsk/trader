import { OvernightView } from './OvernightView';
import { pool } from '@/lib/db';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

async function initialState() {
  const [enabledRow, openRows, closedAggRow, recentClosedRows, signalRows, pendingOrderRows] =
    await Promise.all([
      pool.query(`SELECT value FROM config WHERE key='OVERNIGHT_ENABLED'`),
      pool.query(
        `SELECT id, symbol, slot, status, entry_price, exit_price, qty,
                current_price, opened_at, closed_at, sector, company_name
           FROM positions
          WHERE strategy = 'overnight' AND status IN ('opening','open','closing')
          ORDER BY opened_at DESC`,
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
        WHERE strategy = 'overnight'
          AND entry_price IS NOT NULL AND exit_price IS NOT NULL`,
      ),
      pool.query(
        `SELECT id, symbol, entry_price, exit_price, qty, opened_at, closed_at,
                ((exit_price - entry_price) / NULLIF(entry_price,0)) * 100 AS return_pct
           FROM positions
          WHERE strategy = 'overnight' AND status = 'closed'
          ORDER BY closed_at DESC NULLS LAST
          LIMIT 20`,
      ),
      pool.query(
        `SELECT id, ts, symbol, quant_score, decision, reason, payload
           FROM signals
          WHERE strategy = 'overnight'
          ORDER BY ts DESC
          LIMIT 30`,
      ),
      pool.query(
        `SELECT o.id, o.position_id, o.side, o.status, o.client_order_id, o.ts,
                p.symbol, p.slot
           FROM orders o
           JOIN positions p ON p.id = o.position_id
          WHERE p.strategy = 'overnight'
            AND o.status IN ('submitted','partial')
          ORDER BY o.ts DESC`,
      ),
    ]);

  const closed = Number(closedAggRow.rows[0]?.closed_count ?? 0);
  const wins = Number(closedAggRow.rows[0]?.wins ?? 0);

  return {
    strategy: 'overnight' as const,
    enabled: enabledRow.rows[0]?.value === true,
    open_positions: openRows.rows,
    pending_orders: pendingOrderRows.rows,
    metrics: {
      closed_count: closed,
      wins,
      losses: Number(closedAggRow.rows[0]?.losses ?? 0),
      win_rate: closed > 0 ? wins / closed : null,
      cumulative_pnl_eur: Number(closedAggRow.rows[0]?.cumulative_pnl_eur ?? 0),
      avg_return_pct: Number(closedAggRow.rows[0]?.avg_return_pct ?? 0),
    },
    recent_closed: recentClosedRows.rows,
    recent_signals: signalRows.rows,
  };
}

export default async function OvernightPage() {
  const initial = await initialState();
  return <OvernightView initial={initial} />;
}
