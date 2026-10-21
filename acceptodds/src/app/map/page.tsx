import type { Metadata } from 'next';
import { load } from '@/lib/paper-api';
import { PaperMapView } from '@/components/map/PaperMapView';
import {
  ALL_YEARS,
  isAllYears,
  parseYearInfo,
  resolveYear,
  yearLabel,
  yearSelectValues,
  type YearParam,
} from '@/lib/years';
import { groupedVenues } from '@/lib/venues';

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
  // `year=all` plots the whole corpus in one globally fitted semantic space; it is
  // a string sentinel, so it must never be coerced to a number.
  const requested = first(sp.year) ?? undefined;
  const year: YearParam = isAllYears(requested) ? ALL_YEARS : resolveYear(requested, info);
  return (
    <main>
      <form action="/map" className="flex gap-3 px-6 py-3">
        <label>
          Venue{' '}
          <select name="venue" defaultValue={kind}>
            <option value="all">All venues</option>
            {groupedVenues(registry?.items).map((group) => (
              <optgroup key={group.label} label={group.label}>
                {group.venues.map((v) => (
                  <option value={v.id} key={v.id}>
                    {v.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label>
          Year{' '}
          <select name="year" defaultValue={String(year)} className="border border-rule">
            {yearSelectValues(info, year).map((y) => (
              <option key={y} value={y}>
                {yearLabel(y)}
                {typeof y === 'number' && info && !info.availableYears.includes(y) ? ' (no data)' : ''}
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
