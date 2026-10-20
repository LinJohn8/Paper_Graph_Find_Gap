/**
 * Turning semantic-search results into text the user can paste elsewhere.
 *
 * The Idea Finder's whole point is to get a reader from "here is my idea" to
 * "here are the papers about it" quickly. Copying is the step after that: the
 * user wants the hits in their notes, a reference manager, or a chat, without
 * re-typing titles and links. These are pure functions so they can be tested
 * without a browser.
 */

import type { SemanticItem } from './paper-types';

export type CopyFormat = 'markdown' | 'titles' | 'urls' | 'bibtex';

/**
 * The public URL for a paper.
 *
 * Preference order matters: the publisher's own landing page is the most durable
 * link, then the DOI resolver (which survives the publisher reorganising), and
 * only then this app's own `/papers/<slug>` page, which needs the local server.
 */
export function paperUrl(item: SemanticItem, origin: string): string {
  const direct = (item.paper_url ?? '').trim();
  if (direct) return direct;
  const doi = (item.doi ?? '').trim();
  if (doi) return doi.startsWith('http') ? doi : `https://doi.org/${doi.replace(/^doi:\s*/i, '')}`;
  return `${origin.replace(/\/$/, '')}/papers/${encodeURIComponent(item.slug || item.id)}`;
}

/** Venue and year, e.g. `ICML 2026`; empty when neither is known. */
export function venueLabel(item: SemanticItem): string {
  const venues = item.venues ?? [];
  if (venues.length) return venues.map((v) => `${v.name}${v.year ? ` ${v.year}` : ''}`).join(' / ');
  return item.year ? String(item.year) : '';
}

/** Escape the characters BibTeX treats specially. */
function bibtexEscape(text: string): string {
  return text
    .replace(/([&%$#_{}])/g, '\\$1')
    .replace(/\s+/g, ' ')
    .trim();
}

/** A citation key: the slug reduced to `[A-Za-z0-9_]`, never empty. */
function bibtexKey(item: SemanticItem): string {
  const cleaned = (item.slug || item.id || 'paper').replace(/[^A-Za-z0-9_]/g, '_');
  return /^[0-9]/.test(cleaned) ? `p_${cleaned}` : cleaned;
}

function bibtexEntry(item: SemanticItem): string {
  const fields: [string, string][] = [['title', bibtexEscape(item.title)]];
  const authors = (item.authors ?? []).filter(Boolean);
  if (authors.length) fields.push(['author', bibtexEscape(authors.join(' and '))]);
  if (item.year) fields.push(['year', String(item.year)]);
  const url = paperUrl(item, '');
  // `paperUrl` invents a local `/papers/...` URL when the item has no links; a
  // relative path is useless in a bibliography, so only real links are recorded.
  if (url && /^https?:\/\//.test(url)) fields.push(['url', url]);
  const doi = (item.doi ?? '').trim();
  if (doi) fields.push(['doi', bibtexEscape(doi)]);
  const venue = venueLabel(item);
  if (venue) fields.push(['note', bibtexEscape(venue)]);
  const body = fields.map(([key, value]) => `  ${key} = {${value}}`).join(',\n');
  return `@article{${bibtexKey(item)},\n${body}\n}`;
}

/**
 * The text for `items` in `format`.
 *
 * `origin` is only used for items with no publisher link and no DOI, where the
 * app's own page is the last resort.
 */
export function formatCitations(items: readonly SemanticItem[], format: CopyFormat, origin: string): string {
  if (format === 'bibtex') return items.map(bibtexEntry).join('\n\n');
  if (format === 'titles') return items.map((item) => item.title.trim()).join('\n');
  if (format === 'urls') return items.map((item) => paperUrl(item, origin)).join('\n');
  return items
    .map((item) => {
      const venue = venueLabel(item);
      const details = [venue, `score ${item.score.toFixed(2)}`].filter(Boolean).join(', ');
      return `- [${item.title.trim()}](${paperUrl(item, origin)})${details ? ` — ${details}` : ''}`;
    })
    .join('\n');
}

/** File-friendly name for a downloaded copy of the results. */
export function exportFilename(format: CopyFormat): string {
  const extension = format === 'bibtex' ? 'bib' : format === 'markdown' ? 'md' : 'txt';
  return `papers-${format}.${extension}`;
}
