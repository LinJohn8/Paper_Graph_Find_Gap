import { NextRequest, NextResponse } from 'next/server';
const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export async function GET(request: NextRequest) {
  const upstream = new URL('/api/graph/related', API);
  request.nextUrl.searchParams.forEach((value, key) =>
    upstream.searchParams.set(key === 'kind' ? 'venue' : key, value),
  );
  try {
    const r = await fetch(upstream, { cache: 'no-store' });
    return NextResponse.json(await r.json(), { status: r.status });
  } catch {
    return NextResponse.json({ error: 'Local Paper API is unavailable' }, { status: 503 });
  }
}
