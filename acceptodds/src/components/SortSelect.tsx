'use client';

import { useState } from 'react';

/**
 * The sort control, with one extra rule: when it is left at the order the
 * server chose, it submits nothing.
 *
 * WHY
 * ---
 * The order a search returns depends on whether a query was typed. With a
 * query, the results are ordered by relevance; without one, by date. That
 * decision belongs to the server, which knows the query.
 *
 * But a `<select name="sort">` always submits its value, so the first
 * search sent `sort=recent` -- the value the control happened to show when
 * the page was rendered, before the reader had typed anything -- and the
 * server took that as an explicit choice. A paper the reader asked for by
 * name could then sit below papers that merely shared a word with it.
 *
 * So the control only carries a name when the reader has actually changed
 * it. At its default it is not submitted, and the server's own default
 * applies.
 */
export function SortSelect({
  value,
  fallback,
  options,
}: {
  /** The order currently in force. */
  value: string;
  /** The order the server would pick on its own. */
  fallback: string;
  options: { value: string; label: string }[];
}) {
  const [chosen, setChosen] = useState(value);
  const atDefault = chosen === fallback;
  return (
    <select
      aria-label="Sort"
      // An empty name means the browser leaves the field out of the form.
      name={atDefault ? '' : 'sort'}
      value={chosen}
      onChange={(event) => setChosen(event.target.value)}
      className="border border-frame bg-card px-3 py-2"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}
