import { NextResponse } from 'next/server';
import { decodePaperId } from '@/lib/paper-id';
const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export async function GET(_request: Request, context: { params: Promise<{ paperId: string }> }) {
  const paperId = decodePaperId((await context.params).paperId);
  try {
    const response = await fetch(`${API}/api/papers/${encodeURIComponent(paperId)}/related`, { cache: 'no-store' });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ related: [], hidden: 0 }, { status: 503 });
  }
}
