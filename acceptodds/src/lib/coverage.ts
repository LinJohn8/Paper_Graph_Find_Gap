/**
 * How a harvest's coverage verdict is shown.
 *
 * The backend keeps an expected-size manifest: every venue-year has a floor
 * and a ceiling measured from the corpus, and a harvest that lands below the
 * floor is reported as "short". Before that existed, a venue-year that
 * quietly lost most of its papers looked exactly like one that was complete,
 * because nothing compared the two.
 *
 * The verdicts are:
 *
 * - `ok`       -- within the expected range
 * - `short`    -- fewer papers than the floor (the silent-failure case)
 * - `over`     -- more than the ceiling (usually a ceiling that is now stale)
 * - `unknown`  -- no expectation is recorded for this venue-year
 *
 * `unknown` is not a problem, so it renders nothing at all: a venue-year
 * without a recorded expectation should not look like a warning.
 */

export type CoverageStatus = 'ok' | 'short' | 'over' | 'unknown';

export type CoverageLabel = { text: string; className: string };

/**
 * The label for a coverage status, or `null` when there is nothing to say.
 *
 * The class names are the project's own palette: `text-accent` is its
 * "this needs attention" red, `text-muted` is the neutral grey and
 * `text-subtle` the quieter one.
 */
export function coverageLabel(status: string | null | undefined): CoverageLabel | null {
  switch (status) {
    case 'ok':
      return { text: 'Complete', className: 'text-muted' };
    case 'short':
      return { text: 'Below expected', className: 'text-accent' };
    case 'over':
      return { text: 'Above expected', className: 'text-subtle' };
    default:
      return null;
  }
}

/**
 * The detail line for a coverage verdict, or `null` when there is no detail.
 *
 * A "short" verdict is the only one that needs a number: the reader wants to
 * know how far below the floor the harvest landed, not merely that it did.
 */
export function coverageDetail(
  status: string | null | undefined,
  expectedCount: number | null | undefined,
): string | null {
  if (status !== 'short') return null;
  if (expectedCount == null) return null;
  return `expected at least ${expectedCount}`;
}
