'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import type { Gap, GapsResult, SemanticItem, SemanticResult, TopicSummary } from '@/lib/paper-types';

// Browser components must call same-origin `/api/...`: non-NEXT_PUBLIC env vars
// are not inlined into the client bundle, so this used to become `undefined` and
// every request went to `undefined/api/...` (the year picker showed "(no data)"
// for every year). During SSR the module still sees the server env; next.config
// proxies the backend-only endpoints (/api/venues, /api/papers/semantic, /api/gaps).
const API = typeof window === 'undefined' ? (process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000') : '';

type Tab = 'search' | 'gaps';

const GAP_LABEL: Record<Gap['type'], string> = {
  bridge: 'Bridge',
  sparse: 'Sparse',
  cue: 'Cue',
};

const GAP_BADGE: Record<Gap['type'], string> = {
  bridge: 'border-accent text-accent',
  sparse: 'border-frame text-subtle',
  cue: 'border-ink text-ink',
};

export function IdeaFinder() {
  const [query, setQuery] = useState('');
  const [venue, setVenue] = useState('');
  // Start on the viewer's current year and replace it with the backend's real
  // default as soon as `/api/venues` answers. Hardcoding a placeholder year meant
  // this panel opened on a year with no papers.
  const [year, setYear] = useState<number>(() => new Date().getFullYear());
  const [yearChoices, setYearChoices] = useState<number[]>([]);

  const [activeTab, setActiveTab] = useState<Tab>('search');

  const [results, setResults] = useState<SemanticItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const [gaps, setGaps] = useState<Gap[]>([]);
  const [topics, setTopics] = useState<TopicSummary[]>([]);
  const [gapsLoading, setGapsLoading] = useState(false);
  const [gapsError, setGapsError] = useState<string | null>(null);

  // Load the years that actually contain data so the picker is never empty/misleading.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch(`${API}/api/venues`, { cache: 'no-store' });
        if (!response.ok) return;
        const body = await response.json();
        if (cancelled) return;
        const years: number[] = Array.isArray(body?.available_years)
          ? body.available_years.filter((y: unknown) => Number.isFinite(Number(y))).map(Number)
          : [];
        const fallback = Number(body?.default_year);
        setYearChoices(years);
        if (Number.isFinite(fallback)) setYear(fallback);
      } catch {
        /* keep the current-year default */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function findPapers() {
    if (!query.trim()) return;
    setSearching(true);
    setNote(null);
    try {
      const response = await fetch(`${API}/api/papers/semantic`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: query.trim(),
          ...(venue.trim() ? { venue: venue.trim() } : {}),
          year,
          limit: 20,
        }),
      });
      if (!response.ok) {
        setResults([]);
        return;
      }
      const data = (await response.json()) as SemanticResult;
      setResults(data.items ?? []);
      setNote(data.note ?? null);
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  }

  async function loadGaps(targetYear: number) {
    setGapsLoading(true);
    setGapsError(null);
    try {
      const response = await fetch(`${API}/api/gaps?${new URLSearchParams({ year: String(targetYear) })}`, {
        cache: 'no-store',
      });
      if (!response.ok) {
        setGaps([]);
        setTopics([]);
        setGapsError('Gap data is unavailable.');
        return;
      }
      const data = (await response.json()) as GapsResult;
      setGaps(data.gaps ?? []);
      setTopics(data.topics ?? []);
    } catch {
      setGaps([]);
      setTopics([]);
      setGapsError('Gap data is unavailable.');
    } finally {
      setGapsLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      await loadGaps(year);
    })();
    return () => {
      cancelled = true;
    };
  }, [year]);

  const venueLine = (item: SemanticItem) =>
    (item.venues ?? [])
      .map((v) => `${v.name} ${v.year}${v.relation_type ? ` (${v.relation_type})` : ''}`)
      .join(' / ') || 'Venue not recorded';

  return (
    <div className="mt-6">
      <div className="flex gap-4 border-b border-rule pb-3 text-sm">
        <button
          type="button"
          onClick={() => setActiveTab('search')}
          className={activeTab === 'search' ? 'border-b-2 border-ink pb-1 font-medium' : 'text-subtle'}
        >
          Semantic search
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('gaps')}
          className={activeTab === 'gaps' ? 'border-b-2 border-ink pb-1 font-medium' : 'text-subtle'}
        >
          Research gaps
        </button>
      </div>

      {activeTab === 'search' && (
        <section className="mt-5">
          <label htmlFor="idea-query" className="font-serif text-lg">
            Your idea or research question
          </label>
          <textarea
            id="idea-query"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={4}
            placeholder="e.g. How can diffusion models be made more sample-efficient for 3D scene generation?"
            className="mt-2 w-full border border-frame bg-card px-3 py-2 text-sm leading-relaxed"
          />
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col text-sm">
              <span className="text-subtle">Venue (optional)</span>
              <input
                value={venue}
                onChange={(e) => setVenue(e.target.value)}
                placeholder="e.g. neurips"
                className="mt-1 border border-frame bg-card px-3 py-2"
              />
            </label>
            <label className="flex flex-col text-sm">
              <span className="text-subtle">Year</span>
              <select
                value={year}
                onChange={(e) => setYear(Number(e.target.value))}
                className="mt-1 border border-frame bg-card px-3 py-2"
              >
                {(yearChoices.includes(year) ? yearChoices : [year, ...yearChoices]).map((y) => (
                  <option key={y} value={y}>
                    {y}
                    {yearChoices.includes(y) ? '' : ' (no data)'}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => void findPapers()}
              disabled={searching || !query.trim()}
              className="border border-ink bg-ink px-5 py-2 text-sm text-white disabled:opacity-50"
            >
              {searching ? 'Searching…' : 'Find related papers'}
            </button>
          </div>

          {note === 'keyword fallback' && (
            <p className="mt-3 text-xs text-subtle">keyword fallback (no semantic model yet)</p>
          )}

          <div className="mt-5 divide-y divide-rule border-y border-rule">
            {results.map((item) => (
              <article key={item.id} className="py-5">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="font-serif text-xl leading-snug">
                    <Link href={`/papers/${encodeURIComponent(item.id)}`} className="hover:underline">
                      {item.title}
                    </Link>
                  </h2>
                  <span className="shrink-0 rounded border border-frame px-2 py-1 text-xs text-subtle">
                    {item.score.toFixed(2)}
                  </span>
                </div>
                <p className="mt-1 text-sm text-subtle">
                  {item.authors.join(', ') || 'Authors not provided'} · {venueLine(item)} · {item.year}
                </p>
                {item.abstract && <p className="mt-2 line-clamp-3 text-sm leading-relaxed">{item.abstract}</p>}
              </article>
            ))}
          </div>

          {!searching && !results.length && (
            <p className="py-12 text-center text-sm text-muted">
              Describe your idea / research question to find the most relevant paper abstracts.
            </p>
          )}
        </section>
      )}

      {activeTab === 'gaps' && (
        <section className="mt-5">
          <div className="flex items-center justify-between">
            <h2 className="font-serif text-lg">Research gaps for {year}</h2>
            <button type="button" onClick={() => void loadGaps(year)} className="text-sm text-subtle hover:underline">
              Refresh
            </button>
          </div>

          {gapsLoading && <p className="mt-3 text-sm text-muted">Loading gaps…</p>}
          {gapsError && <p className="mt-3 text-sm text-muted">{gapsError}</p>}

          {!gapsLoading && gaps.length > 0 && (
            <div className="mt-4 space-y-4">
              {gaps.map((gap, i) => (
                <article key={`${gap.type}-${i}`} className="border border-rule p-4">
                  <div className="flex items-center gap-2">
                    <span className={`rounded border px-2 py-0.5 text-xs uppercase ${GAP_BADGE[gap.type]}`}>
                      {GAP_LABEL[gap.type]}
                    </span>
                    <h3 className="font-serif text-lg leading-snug">{gap.title}</h3>
                  </div>
                  <p className="mt-2 text-sm leading-relaxed">{gap.explanation}</p>
                  {gap.papers.length > 0 && (
                    <details className="mt-3">
                      <summary className="cursor-pointer text-sm text-subtle">
                        {gap.papers.length} example paper{gap.papers.length === 1 ? '' : 's'}
                      </summary>
                      <ul className="mt-2 list-disc space-y-2 pl-5">
                        {gap.papers.slice(0, 5).map((p) => (
                          <li key={p.id}>
                            <Link href={`/papers/${encodeURIComponent(p.id)}`} className="text-sm hover:underline">
                              {p.title}
                            </Link>
                            {p.abstract && <p className="mt-1 line-clamp-2 text-sm text-subtle">{p.abstract}</p>}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </article>
              ))}
            </div>
          )}

          {!gapsLoading && !gapsError && gaps.length === 0 && (
            <p className="mt-3 text-sm text-muted">No gaps detected for {year} yet.</p>
          )}

          <section className="mt-8 border-t border-rule pt-5">
            <h2 className="font-serif text-lg">Map vocabulary</h2>
            <p className="mt-1 text-sm text-subtle">
              The terms the graph resolves papers into — a sense of what the corpus covers.
            </p>
            <ul className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {topics.map((t) => (
                <li key={t.number} className="flex items-center justify-between border-b border-rule py-2 text-sm">
                  <span>
                    <span className="text-subtle">{t.number}.</span> {t.label}
                  </span>
                  <span className="text-xs text-muted">{t.size}</span>
                </li>
              ))}
            </ul>
            {!topics.length && <p className="mt-3 text-sm text-muted">No topic vocabulary available yet.</p>}
          </section>
        </section>
      )}
    </div>
  );
}
