import { NextResponse } from 'next/server';
import { computeState } from '@/lib/state';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

export async function GET() {
  try {
    const state = await computeState();
    return NextResponse.json(state, {
      headers: { 'Cache-Control': 'no-store' },
    });
  } catch (e) {
    console.error('[api/state] computeState failed:', e);
    return NextResponse.json(
      { error: 'state_unavailable' },
      { status: 500 },
    );
  }
}
