import { NextResponse } from 'next/server';
import { setConfig } from '@/lib/db';

const TOGGLES = new Set([
  'MANUAL_APPROVAL_MODE',
  'NEWS_WATCHER_ENABLED',
  'LLM_VETO_ENABLED',
  'MARKET_HOURS_ONLY',
]);

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    key?: unknown;
    value?: unknown;
  };
  if (typeof body.key !== 'string' || !TOGGLES.has(body.key) || typeof body.value !== 'boolean') {
    return NextResponse.json(
      { error: `key must be one of ${[...TOGGLES].join(',')} and value must be boolean` },
      { status: 400 },
    );
  }
  await setConfig(body.key, body.value, 'dashboard:mode');
  return NextResponse.json({ ok: true, key: body.key, value: body.value });
}
