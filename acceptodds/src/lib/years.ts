/**
 * Year helpers shared by pages and components.
 *
 * The app used to hardcode `2027` wherever a year was needed. That is wrong as
 * soon as the calendar moves on: the backend has no 2027 data, so every page
 * opened on an empty year and every link carried a meaningless parameter.
 *
 * The backend now reports the years it actually has:
 *   GET /api/venues -> { default_year, available_years, current_year, items }
 *
 * These helpers keep the fallback chain in one place instead of repeating
 * `?? 2027` in a dozen components.
 */

export type YearInfo = {
  defaultYear: number;
  availableYears: number[];
  currentYear: number;
};

/** The calendar year in the viewer's timezone (used before any data loads). */
export function currentYear(): number {
  return new Date().getFullYear();
}

/**
 * Pick the year a page should show when the URL does not specify one.
 *
 * Order: explicitly requested -> newest year with data -> current calendar year.
 * Never a hardcoded literal.
 */
export function resolveYear(requested: string | string[] | undefined, info?: YearInfo | null): number {
  const raw = Array.isArray(requested) ? requested[0] : requested;
  if (raw && /^\d{4}$/.test(raw.trim())) {
    const parsed = Number(raw.trim());
    if (parsed >= 1900 && parsed <= 2200) return parsed;
  }
  if (info && Number.isFinite(info.defaultYear)) return info.defaultYear;
  return currentYear();
}

/** Build the option list for a year selector, newest first, no duplicates. */
export function yearOptions(info?: YearInfo | null, selected?: number): number[] {
  const set = new Set<number>();
  if (selected && Number.isFinite(selected)) set.add(selected);
  for (const y of info?.availableYears ?? []) {
    if (Number.isFinite(y)) set.add(y);
  }
  if (info && Number.isFinite(info.currentYear)) set.add(info.currentYear);
  if (set.size === 0) set.add(currentYear());
  return [...set].sort((a, b) => b - a);
}

/** Normalise the `year` field the backend returns on `/api/venues`. */
export function parseYearInfo(registry: unknown): YearInfo | null {
  if (!registry || typeof registry !== 'object') return null;
  const r = registry as Record<string, unknown>;
  const items = Array.isArray(r.items) ? (r.items as Array<Record<string, unknown>>) : [];
  const available = new Set<number>();
  for (const raw of Array.isArray(r.available_years) ? r.available_years : []) {
    const n = Number(raw);
    if (Number.isFinite(n)) available.add(n);
  }
  // Fall back to the years present on the venue rows if the backend did not send
  // `available_years` (older server versions).
  for (const item of items) {
    const n = Number(item.year);
    if (Number.isFinite(n)) available.add(n);
  }
  const defaultYear = Number(r.default_year);
  const current = Number(r.current_year);
  return {
    defaultYear: Number.isFinite(defaultYear) ? defaultYear : currentYear(),
    availableYears: [...available].sort((a, b) => b - a),
    currentYear: Number.isFinite(current) ? current : currentYear(),
  };
}
