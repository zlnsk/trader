import { NextResponse } from 'next/server';
import { pool, setConfig } from '@/lib/db';

const ALLOWED_KEYS = new Set([
  'QUANT_SCORE_MIN', 'TARGET_PROFIT_PCT', 'STOP_LOSS_PCT',
  'MIN_NET_MARGIN_EUR', 'SIGMA_BELOW_SMA20', 'RSI_BUY_THRESHOLD',
]);

type Proposal = {
  proposals?: Array<{ key: string; to: unknown }>;
};

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
  if (id === null || (action !== 'approve' && action !== 'reject')) {
    return NextResponse.json(
      { error: 'id (number|numeric string) + action=approve|reject required' },
      { status: 400 },
    );
  }

  const { rows } = await pool.query<{ proposal: Proposal; status: string }>(
    'SELECT proposal, status FROM tuning_proposals WHERE id=$1',
    [id],
  );
  const row = rows[0];
  if (!row) return NextResponse.json({ error: 'not found' }, { status: 404 });
  if (row.status !== 'pending') {
    return NextResponse.json({ error: `already ${row.status}` }, { status: 409 });
  }

  if (action === 'reject') {
    await pool.query(
      `UPDATE tuning_proposals SET status='rejected', reviewed_at=now(), reviewed_by='dashboard'
       WHERE id=$1`,
      [id],
    );
    return NextResponse.json({ ok: true, id, status: 'rejected' });
  }

  const applied: string[] = [];
  for (const p of row.proposal.proposals ?? []) {
    if (!ALLOWED_KEYS.has(p.key) || typeof p.to !== 'number') continue;
    await setConfig(p.key, p.to, 'dashboard:tune');
    applied.push(p.key);
  }
  await pool.query(
    `UPDATE tuning_proposals SET status='applied', reviewed_at=now(), reviewed_by='dashboard'
     WHERE id=$1`,
    [id],
  );
  return NextResponse.json({ ok: true, id, status: 'applied', applied });
}
