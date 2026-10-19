import type { Metadata } from 'next';
import { load } from '@/lib/paper-api';
import { PaperMapView } from '@/components/map/PaperMapView';
import { parseYearInfo, resolveYear, yearOptions } from '@/lib/years';

export const dynamic = 'force-dynamic';
export const metadata: Metadata = { title: 'Paper map | Paper Graph' };
type SearchParams = Promise<Record<string, string | string[] | undefined>>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? null;

export default async function MapPage({ searchParams }: { searchParams: SearchParams }) {
  const sp = await searchParams;
  const kind = first(sp.venue) ?? first(sp.kind) ?? 'all';
  const registry = await load('/api/venues');
  // Use the backend's real default year rather than a literal that has no data.
  const info = parseYearInfo(registry);
  const year = resolveYear(first(sp.year) ?? undefined, info);
  return (
    <main>
      <form action="/map" className="flex gap-3 px-6 py-3">
        <label>
          Venue{' '}
          <select name="venue" defaultValue={kind}>
            <option value="all">All venues</option>
            {registry?.items
              .filter(
                (v: { id: string }, i: number) => registry.items.findIndex((x: { id: string }) => x.id === v.id) === i,
              )
              .map((v: { id: string; name: string }) => (
                <option value={v.id} key={v.id}>
                  {v.name}
                </option>
              ))}
          </select>
        </label>
        <label>
          Year{' '}
          <select name="year" defaultValue={String(year)} className="border border-rule">
            {yearOptions(info, year).map((y) => (
              <option key={y} value={y}>
                {y}
                {info?.availableYears.includes(y) ? '' : ' (no data)'}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">Apply</button>
      </form>
      <PaperMapView key={`${kind}-${year}`} kind={kind} year={year} initialPaper={first(sp.paper)} />
    </main>
  );
}
