import type { Metadata } from 'next';
import { PaperExplorer } from '@/components/PaperExplorer';
import { load } from '@/lib/paper-api';
import { ALL_YEARS, isAllYears, parseYearInfo, resolveYear } from '@/lib/years';

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
  // `year=all` is a string sentinel, not a year: pass it through untouched so the
  // explorer can query the whole corpus. Anything else resolves as before.
  const requestedYear = one(params.year);
  return (
    <PaperExplorer
      venue={one(params.venue) ?? 'all'}
      year={isAllYears(requestedYear) ? ALL_YEARS : resolveYear(requestedYear, info)}
      yearInfo={info}
      status={one(params.status) ?? ''}
      sort={one(params.sort) ?? 'recent'}
      keyword={one(params.q) ?? ''}
      page={Number(one(params.page) ?? 1)}
    />
  );
}
