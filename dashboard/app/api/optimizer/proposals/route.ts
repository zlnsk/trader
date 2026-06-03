import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export async function GET(req: Request) {
  const url = new URL(req.url);
  const status = url.searchParams.get('status');
  const params: (string | number)[] = [];
  let where = "ts >= NOW() - INTERVAL '30 days'";
  if (status) {
    params.push(status);
    where += ` AND status=$${params.length}`;
  }
  const { rows } = await pool.query(
    `SELECT id, ts, status, source, rationale, proposal,
            adversary_result, adversary_ts, canary_id, applied_version_id,
            reviewed_at, reviewed_by, rolled_back_at
     FROM tuning_proposals WHERE ${where}
     ORDER BY ts DESC LIMIT 200`,
    params,
  );
  return NextResponse.json({ proposals: rows });
}

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    id?: number; action?: string; slot_ids?: number[];
  };
  const id = typeof body.id === 'number' ? body.id : null;
  if (id === null || typeof body.action !== 'string') {
    return NextResponse.json({ error: 'id+action required' }, { status: 400 });
  }

  const action = body.action;
  if (action === 'reject') {
    await pool.query(
      `UPDATE tuning_proposals
          SET status='rejected', reviewed_at=NOW(), reviewed_by='dashboard'
        WHERE id=$1`,
      [id],
    );
    return NextResponse.json({ ok: true, id, status: 'rejected' });
  }
  if (action === 'approve') {
    
    await pool.query(
      `UPDATE tuning_proposals
          SET status='approved', reviewed_at=NOW(), reviewed_by='dashboard'
        WHERE id=$1 AND status IN ('validated','awaiting_human','pending')`,
      [id],
    );
    return NextResponse.json({ ok: true, id, status: 'approved' });
  }
  return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
}
