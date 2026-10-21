/**
 * Venue helpers shared by the paper list, the map and the data-sources page.
 *
 * The registry grew from 6 conference venues to 17 once the journal corpus
 * arrived (Nature, Science, Cell, TPAMI, JMLR, ...). A flat <select> with 17
 * entries is hard to scan, and the two families are genuinely different kinds
 * of thing: the conference venues are rolling peer-review venues, the journal
 * ones are archival titles. Grouping them keeps the control usable and makes
 * the corpus's two halves legible.
 */

export type VenueItem = {
  id: string;
  name: string;
  year: number;
  type?: string | null;
  paper_count?: number;
};

/** The two families the registry distinguishes, in display order. */
const VENUE_GROUPS: { label: string; types: string[] }[] = [
  { label: 'Conferences & rolling venues', types: ['conference', 'rolling_review'] },
  { label: 'Journals', types: ['journal'] },
];

/**
 * The unique venues of a registry, in first-seen order.
 *
 * `/api/venues` returns one row per venue *year*, so the same venue appears
 * several times; the select only needs it once.
 */
export function uniqueVenues<T extends VenueItem>(items: T[] | undefined | null): T[] {
  if (!items) return [];
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of items) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
  }
  return out;
}

/**
 * The unique venues, split into labelled groups.
 *
 * A venue whose type the registry does not know lands in `Other` rather than
 * disappearing, so a new provider type is visible instead of silently
 * missing from the picker.
 */
export function groupedVenues<T extends VenueItem>(items: T[] | undefined | null): { label: string; venues: T[] }[] {
  const unique = uniqueVenues(items);
  const groups: { label: string; venues: T[] }[] = [];
  const used = new Set<string>();

  for (const group of VENUE_GROUPS) {
    const venues = unique.filter((v) => v.type != null && group.types.includes(v.type));
    if (venues.length) {
      groups.push({ label: group.label, venues });
      venues.forEach((v) => used.add(v.id));
    }
  }
  const other = unique.filter((v) => !used.has(v.id));
  if (other.length) groups.push({ label: 'Other', venues: other });
  return groups;
}
