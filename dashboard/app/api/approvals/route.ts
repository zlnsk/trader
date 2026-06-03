import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

function coerceId(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && /^\d+$/.test(v)) return Number(v);
  return null;
}

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    id?: unknown;
    action?: unknown;
  };
  const id = coerceId(body.id);
  const action = body.action;
  if (id === null || (action !== 'approve' && action !== 'skip')) {
    return NextResponse.json(
      { error: 'id (number|numeric string) + action=approve|skip required' },
      { status: 400 },
    );
  }

  const { rows } = await pool.query<{ status: string }>(
    'SELECT status FROM pending_approvals WHERE id=$1',
    [id],
  );
  if (!rows[0]) return NextResponse.json({ error: 'not found' }, { status: 404 });
  if (rows[0].status !== 'pending') {
    return NextResponse.json({ error: `already ${rows[0].status}` }, { status: 409 });
  }

  const newStatus = action === 'approve' ? 'approved' : 'skipped';
  await pool.query(
    `UPDATE pending_approvals SET status=$1, reviewed_at=now(), reviewed_by='dashboard' WHERE id=$2`,
    [newStatus, id],
  );
  return NextResponse.json({ ok: true, id, status: newStatus });
}
