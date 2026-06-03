import { NextResponse } from 'next/server';
import { setConfigTrusted } from '@/lib/db';

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    enabled?: unknown;
    confirm?: unknown;
  };
  if (typeof body.enabled !== 'boolean') {
    return NextResponse.json(
      { error: 'enabled must be boolean' },
      { status: 400 },
    );
  }
  const expected = body.enabled ? 'RESUME' : 'PAUSE';
  if (body.confirm !== expected) {
    return NextResponse.json(
      { error: 'confirm_required', expected: { confirm: expected, enabled: body.enabled } },
      { status: 400 },
    );
  }
  await setConfigTrusted('BOT_ENABLED', body.enabled, 'dashboard:pause');
  return NextResponse.json({ ok: true, enabled: body.enabled });
}
