import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { pool, setConfigTrusted } from '@/lib/db';

export async function POST(req: NextRequest) {
  let body: { confirm?: string; version_id?: number } = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json(
      { ok: false, error: 'body_required', expected: { confirm: 'KILL' } },
      { status: 400 },
    );
  }
  if (body.confirm !== 'KILL') {
    return NextResponse.json(
      { ok: false, error: 'confirm_required', expected: { confirm: 'KILL' } },
      { status: 400 },
    );
  }
  if (typeof body.version_id === 'number') {
    const { rows } = await pool.query<{ id: number }>(
      "SELECT id FROM config_versions WHERE status = 'active' ORDER BY id DESC LIMIT 1",
    );
    const activeId = rows[0]?.id;
    if (activeId !== undefined && activeId !== body.version_id) {
      return NextResponse.json(
        {
          ok: false,
          error: 'version_id_stale',
          active_version_id: activeId,
          supplied_version_id: body.version_id,
        },
        { status: 409 },
      );
    }
  }
  await setConfigTrusted('BOT_ENABLED', false, 'dashboard:kill');
  return NextResponse.json({ ok: true, enabled: false });
}
