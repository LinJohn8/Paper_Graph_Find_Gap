import { NextRequest, NextResponse } from 'next/server';

const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export const dynamic = 'force-dynamic';
/** A full-year gap scan runs 20-40 s; Node's default fetch has no cap, but the
 *  Next rewrite proxy does (it answered `socket hang up` past ~30 s), which is why
 *  this endpoint is a route handler rather than a rewrite. */
export const maxDuration = 300;

export async function GET(request: NextRequest) {
  const upstream = new URL('/api/gaps', API);
  request.nextUrl.searchParams.forEach((value, key) => upstream.searchParams.set(key, value));
  try {
    const response = await fetch(upstream, { cache: 'no-store' });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ gaps: [], topics: [], note: 'Gap scanning is unavailable (local API down).' }, { status: 503 });
  }
}
