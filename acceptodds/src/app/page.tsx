import type { Metadata } from 'next';
import { PaperExplorer } from '@/components/PaperExplorer';

export const dynamic = 'force-dynamic';
export const metadata: Metadata = { title: 'Paper Graph', description: 'Local research paper explorer' };

type SearchParams = Promise<Record<string, string | string[] | undefined>>;
const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function Home({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  return (
    <PaperExplorer
      venue={one(params.venue) ?? 'all'}
      year={Number(one(params.year) ?? 2027)}
      status={one(params.status) ?? ''}
      sort={one(params.sort) ?? 'recent'}
      keyword={one(params.q) ?? ''}
      page={Number(one(params.page) ?? 1)}
    />
  );
}
