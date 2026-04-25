import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export async function GET() {
  const { rows } = await pool.query(
    `SELECT ca.id, ca.proposal_id, ca.canary_version_id, ca.baseline_version_id,
            ca.slot_ids, ca.started_at, ca.ended_at, ca.status,
            ca.min_trades_required, ca.required_ci_bps, ca.result,
            tp.source AS proposal_source, tp.rationale AS proposal_rationale
       FROM canary_assignments ca
       LEFT JOIN tuning_proposals tp ON tp.id = ca.proposal_id
      ORDER BY ca.started_at DESC LIMIT 100`,
  );
  return NextResponse.json({ canaries: rows });
}

// POST: abort a running canary manually.
export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    id?: number; action?: string;
  };
  if (typeof body.id !== 'number' || body.action !== 'abort') {
    return NextResponse.json({ error: 'id+action=abort required' }, { status: 400 });
  }
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(
      `UPDATE canary_assignments SET status='aborted', ended_at=NOW()
        WHERE id=$1 AND status='running'`,
      [body.id],
    );
    await client.query(
      `UPDATE config_versions
          SET deactivated_at=NOW(), deactivated_by='dashboard',
              deactivated_reason='manual_abort'
        WHERE id = (SELECT canary_version_id FROM canary_assignments WHERE id=$1)`,
      [body.id],
    );
    await client.query(
      `UPDATE tuning_proposals SET status='canary_failed'
        WHERE id = (SELECT proposal_id FROM canary_assignments WHERE id=$1)`,
      [body.id],
    );
    await client.query('COMMIT');
  } catch (e) {
    await client.query('ROLLBACK').catch(() => {});
    throw e;
  } finally {
    client.release();
  }
  return NextResponse.json({ ok: true, id: body.id, status: 'aborted' });
}
