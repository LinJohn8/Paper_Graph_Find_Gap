import { NextRequest, NextResponse } from 'next/server';
import { decodePaperId } from '@/lib/paper-id';

const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';

export async function GET(request: NextRequest, context: { params: Promise<{ paperId: string }> }) {
  const paperId = decodePaperId((await context.params).paperId);
  const upstream = new URL(`/api/graph/${encodeURIComponent(paperId)}/minimap`, API);
  request.nextUrl.searchParams.forEach((value, key) =>
    upstream.searchParams.set(key === 'kind' ? 'venue' : key, value),
  );
  try {
    const response = await fetch(upstream, { cache: 'no-store' });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ error: 'Local Paper API is unavailable' }, { status: 503 });
  }
}
