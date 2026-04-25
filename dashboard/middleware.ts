import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export function middleware(req: NextRequest) {
  const secret = process.env.PROXY_SECRET;
  if (!secret) return NextResponse.next();
  const got = req.headers.get('x-proxy-secret') ?? '';
  if (!timingSafeEqual(got, secret)) {
    return new NextResponse('forbidden', { status: 403 });
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/', '/((?!_next/static|_next/image|favicon.ico).*)'],
};
