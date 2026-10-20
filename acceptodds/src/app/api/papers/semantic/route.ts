import { NextRequest, NextResponse } from 'next/server';

const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export const dynamic = 'force-dynamic';
/** Semantic search ranks a whole year's (or the whole corpus's) vectors first time
 *  round, which can take 20 s; the Next rewrite proxy dropped that with `socket
 *  hang up`, so this is a route handler with a generous budget. */
export const maxDuration = 300;

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ items: [], note: 'Invalid JSON body', method: 'error' }, { status: 400 });
  }
  try {
    const response = await fetch(new URL('/api/papers/semantic', API), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json(
      { items: [], note: 'Semantic search is unavailable (local API down).', method: 'error' },
      { status: 503 },
    );
  }
}
