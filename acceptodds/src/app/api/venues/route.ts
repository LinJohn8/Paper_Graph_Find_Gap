import { NextResponse } from 'next/server';

const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export const dynamic = 'force-dynamic';

/** Venue registry + available years. Also the All-years sentinel the UI reads. */
export async function GET() {
  try {
    const response = await fetch(new URL('/api/venues', API), { cache: 'no-store' });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json(
      { default_year: new Date().getFullYear(), available_years: [], all_years: 'all', items: [] },
      { status: 503 },
    );
  }
}
