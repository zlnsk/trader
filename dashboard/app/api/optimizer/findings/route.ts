import { NextResponse } from 'next/server';
import { pool } from '@/lib/db';

export async function GET(req: Request) {
  const url = new URL(req.url);
  const unresolved = url.searchParams.get('unresolved') === 'true';
  const { rows } = await pool.query(
    `SELECT id, ts, detector, severity, subject, body, evidence,
            resolved_at, resolution, proposal_id
       FROM optimizer_findings
       WHERE ts >= NOW() - INTERVAL '30 days'
         ${unresolved ? 'AND resolved_at IS NULL' : ''}
       ORDER BY ts DESC LIMIT 200`,
  );
  return NextResponse.json({ findings: rows });
}

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    id?: number; resolution?: string;
  };
  if (typeof body.id !== 'number') {
    return NextResponse.json({ error: 'id required' }, { status: 400 });
  }
  await pool.query(
    `UPDATE optimizer_findings
        SET resolved_at=NOW(), resolution=$2
      WHERE id=$1`,
    [body.id, body.resolution ?? 'manual'],
  );
  return NextResponse.json({ ok: true, id: body.id });
}
