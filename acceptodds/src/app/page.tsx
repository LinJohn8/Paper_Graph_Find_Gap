import type { Metadata } from 'next';
import { PaperExplorer } from '@/components/PaperExplorer';
import { load } from '@/lib/paper-api';
import { parseYearInfo, resolveYear } from '@/lib/years';

export const dynamic = 'force-dynamic';
export const metadata: Metadata = { title: 'Paper Graph', description: 'Local research paper explorer' };

type SearchParams = Promise<Record<string, string | string[] | undefined>>;
const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function Home({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  // Resolve the year from the backend's actual data instead of a hardcoded 2027:
  // a fresh install should open on the newest year that has papers.
  const registry = await load('/api/venues');
  const info = parseYearInfo(registry);
  return (
    <PaperExplorer
      venue={one(params.venue) ?? 'all'}
      year={resolveYear(one(params.year), info)}
      yearInfo={info}
      status={one(params.status) ?? ''}
      sort={one(params.sort) ?? 'recent'}
      keyword={one(params.q) ?? ''}
      page={Number(one(params.page) ?? 1)}
    />
  );
}
