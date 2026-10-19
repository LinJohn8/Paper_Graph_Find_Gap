import { NextRequest, NextResponse } from 'next/server';
const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export async function GET(request: NextRequest) {
  const url = new URL('/api/papers', API);
  request.nextUrl.searchParams.forEach((v, k) => url.searchParams.set(k, v));
  try {
    const r = await fetch(url, { cache: 'no-store' });
    return NextResponse.json(await r.json(), { status: r.status });
  } catch {
    return NextResponse.json({ items: [], total: 0, error: 'Local Paper API unavailable' }, { status: 503 });
  }
}
