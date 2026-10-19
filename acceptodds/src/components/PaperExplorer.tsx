import Link from 'next/link';
import { yearOptions, type YearInfo } from '@/lib/years';

type Item = {
  id: string;
  title: string;
  abstract: string | null;
  authors: string[];
  keywords: string[];
  primary_area: string | null;
  pdf_url: string | null;
  paper_url: string | null;
  status: string;
  venues: { id: string; name: string; year: number }[];
};

const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';

async function load(path: string) {
  try {
    const response = await fetch(`${API}${path}`, { cache: 'no-store' });
    return response.ok ? response.json() : null;
  } catch {
    return null;
  }
}

export async function PaperExplorer({
  venue,
  year,
  yearInfo,
  keyword,
  page,
  status,
  sort,
}: {
  venue: string;
  year: number;
  yearInfo?: YearInfo | null;
  keyword: string;
  page: number;
  status: string;
  sort: string;
}) {
  const [result, registry] = await Promise.all([
    load(
      `/api/papers?${new URLSearchParams({ ...(venue !== 'all' ? { venue } : {}), year: String(year), ...(keyword ? { keyword } : {}), page: String(page), page_size: '40', status, sort })}`,
    ),
    load('/api/venues'),
  ]);
  const items: Item[] = result?.items ?? [];
  const venues = registry?.items ?? [];
  const params = (patch: Record<string, string>) => {
    const p = new URLSearchParams({
      venue,
      year: String(year),
      status,
      sort,
      ...(keyword ? { q: keyword } : {}),
      ...patch,
    });
    return `/?${p}`;
  };
  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="font-serif text-3xl">Papers</h1>
      <form
        action="/"
        method="get"
        className="mt-5 grid grid-cols-1 gap-3 border-b border-rule pb-5 sm:grid-cols-[minmax(220px,1fr)_180px_130px_auto]"
      >
        <input
          className="border border-frame bg-card px-3 py-2"
          name="q"
          type="search"
          defaultValue={keyword}
          placeholder="Title, abstract, authors, keywords"
        />
        <select className="border border-frame bg-card px-3 py-2" name="venue" defaultValue={venue}>
          <option value="all">All venues</option>
          {venues
            .filter((v: { id: string }, i: number) => venues.findIndex((x: { id: string }) => x.id === v.id) === i)
            .map((v: { id: string; name: string; year: number }) => (
              <option key={`${v.id}-${v.year}`} value={v.id}>
                {v.name}
              </option>
            ))}
        </select>
        {/* Offer the years the backend actually has, instead of a bare number box
            that defaults to a year with no data. */}
        <select
          aria-label="Year"
          className="border border-frame bg-card px-3 py-2"
          name="year"
          defaultValue={String(year)}
        >
          {yearOptions(yearInfo, year).map((y) => (
            <option key={y} value={y}>
              {y}
              {yearInfo?.availableYears.includes(y) ? '' : ' (no data)'}
            </option>
          ))}
        </select>
        <select
          aria-label="Status"
          name="status"
          defaultValue={status}
          className="border border-frame bg-card px-3 py-2"
        >
          <option value="">All statuses</option>
          {['published', 'unknown', 'submitted', 'accepted', 'rejected', 'withdrawn'].map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select aria-label="Sort" name="sort" defaultValue={sort} className="border border-frame bg-card px-3 py-2">
          <option value="recent">Recently updated</option>
          <option value="title">Title</option>
          <option value="year">Year</option>
        </select>
        <button className="border border-ink bg-ink px-5 py-2 text-white" type="submit">
          Search
        </button>
      </form>
      <div className="flex items-center justify-between py-3 text-sm text-subtle">
        <span>{result ? `${result.total.toLocaleString()} papers` : 'Paper API is unavailable'}</span>
        <span>Showing {year} data</span>
      </div>
      <section className="divide-y divide-rule border-y border-rule">
        {items.map((paper) => (
          <article key={paper.id} className="py-5">
            <h2 className="font-serif text-xl leading-snug">
              <Link href={`/papers/${encodeURIComponent(paper.id)}`} className="hover:underline">
                {paper.title}
              </Link>
            </h2>
            <p className="mt-1 text-sm text-subtle">
              {paper.authors.join(', ') || 'Authors not provided'} ·{' '}
              {paper.venues.map((v) => `${v.name} ${v.year}`).join(' / ')} · {paper.status}
            </p>
            {!!paper.keywords.length && <p className="mt-1 text-xs text-muted">{paper.keywords.join(' · ')}</p>}
            {paper.primary_area && <p className="mt-1 text-xs text-muted">{paper.primary_area}</p>}
            {paper.abstract && <p className="mt-2 line-clamp-3 text-sm leading-relaxed">{paper.abstract}</p>}
            <div className="mt-2 flex gap-4 text-sm">
              {paper.paper_url && (
                <a href={paper.paper_url} target="_blank" rel="noreferrer">
                  Original source
                </a>
              )}
              {paper.pdf_url && (
                <a href={paper.pdf_url} target="_blank" rel="noreferrer">
                  PDF
                </a>
              )}
            </div>
          </article>
        ))}
        {!items.length && (
          <p className="py-12 text-center text-sm text-muted">
            {result ? 'No public papers match these filters.' : 'Start the local API to browse the paper database.'}
          </p>
        )}
      </section>
      <footer className="flex justify-between py-5 text-sm">
        {page > 1 ? <Link href={params({ page: String(page - 1) })}>Previous</Link> : <span>Previous</span>}
        <span>
          Page {result?.page ?? page} of {result?.pages ?? 1}
        </span>
        {result && page < result.pages ? (
          <Link href={params({ page: String(page + 1) })}>Next</Link>
        ) : (
          <span>Next</span>
        )}
      </footer>
      <section className="mt-7 border-t border-rule pt-5">
        <h2 className="font-serif text-lg">Venue sync status</h2>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {venues.map((v: { id: string; name: string; year: number; status: string; last_sync_at?: string | null }) => (
            <div key={`${v.id}-${v.year}`} className="flex justify-between border-b border-rule py-2 text-sm">
              <span>
                {v.name} {v.year}
              </span>
              <span className="text-subtle">
                {v.status}
                {v.last_sync_at ? ` · ${new Date(v.last_sync_at).toLocaleDateString()}` : ''}
              </span>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
