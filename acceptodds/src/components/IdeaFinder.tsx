'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Gap, GapsResult, SemanticItem, SemanticResult, TopicSummary } from '@/lib/paper-types';
import { formatCitations, type CopyFormat } from '@/lib/copy-export';
import { ALL_YEARS, yearLabel, yearSelectValues } from '@/lib/years';

// Browser components must call same-origin `/api/...`: non-NEXT_PUBLIC env vars
// are not inlined into the client bundle, so this used to become `undefined` and
// every request went to `undefined/api/...` (the year picker showed "(no data)"
// for every year). During SSR the module still sees the server env; next.config
// proxies the backend-only endpoints (/api/venues, /api/papers/semantic, /api/gaps).
const API = typeof window === 'undefined' ? (process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000') : '';

type Tab = 'search' | 'gaps';
/** A search scope is a concrete year, or the whole corpus. */
type Scope = number | typeof ALL_YEARS;

const COPY_ACTIONS: [CopyFormat, string][] = [
  ['markdown', 'Markdown 链接'],
  ['titles', '标题'],
  ['urls', '链接'],
  ['bibtex', 'BibTeX'],
];

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
  const [year, setYear] = useState<Scope>(() => new Date().getFullYear());
  const [yearChoices, setYearChoices] = useState<number[]>([]);

  const [activeTab, setActiveTab] = useState<Tab>('search');

  const [results, setResults] = useState<SemanticItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  /** Slugs the reader has ticked, and the transient "copied N" confirmation. */
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);

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
          // `all` asks the backend for the whole corpus, which it ranks against a
          // single globally fitted model; a number keeps the per-year behaviour.
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
      setSelected(new Set());
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  }

  /** Items a copy action applies to: the ticked ones, or every result when none are ticked. */
  const chosen = useMemo(
    () => (selected.size > 0 ? results.filter((item) => selected.has(item.slug || item.id)) : results),
    [results, selected],
  );

  const toggle = useCallback((slug: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }, []);

  const copy = useCallback(
    async (format: CopyFormat, label: string) => {
      setCopyError(null);
      if (!chosen.length) return;
      const text = formatCitations(chosen, format, typeof window === 'undefined' ? '' : location.origin);
      const done = `${label} · 已复制 ${chosen.length} 条`;
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          // Older browsers (and some non-secure contexts) have no async clipboard.
          const area = document.createElement('textarea');
          area.value = text;
          area.setAttribute('readonly', '');
          area.style.position = 'fixed';
          area.style.opacity = '0';
          document.body.appendChild(area);
          area.select();
          const ok = document.execCommand('copy');
          document.body.removeChild(area);
          if (!ok) throw new Error('execCommand copy refused');
        }
        setCopied(done);
        setTimeout(() => setCopied(null), 2500);
      } catch {
        setCopyError('无法访问剪贴板，请手动选择文本复制。');
      }
    },
    [chosen],
  );

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
      if (data.note) setGapsError(data.note);
    } catch {
      setGaps([]);
      setTopics([]);
      setGapsError('Gap data is unavailable.');
    } finally {
      setGapsLoading(false);
    }
  }

  useEffect(() => {
    // Only scan once the reader actually opens the tab. The scan takes 20-30 s on
    // a full year, and it used to fire on mount: the page looked frozen while the
    // user was still typing their idea, which read as "the button does nothing".
    if (year === ALL_YEARS || activeTab !== 'gaps') return;
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      await loadGaps(year);
    })();
    return () => {
      cancelled = true;
    };
    // loadGaps is re-created per render; keying on the scope is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [year, activeTab]);

  const gapsDisabled = year === ALL_YEARS;
  const options = useMemo(
    () => yearSelectValues({ defaultYear: 0, availableYears: yearChoices, currentYear: 0 }, year),
    [yearChoices, year],
  );

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
          disabled={gapsDisabled}
          title={gapsDisabled ? 'Gap scanning needs a single year' : undefined}
          className={
            gapsDisabled
              ? 'cursor-not-allowed text-muted'
              : activeTab === 'gaps'
                ? 'border-b-2 border-ink pb-1 font-medium'
                : 'text-subtle'
          }
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
                aria-label="Year scope"
                value={String(year)}
                onChange={(e) => setYear(e.target.value === ALL_YEARS ? ALL_YEARS : Number(e.target.value))}
                className="mt-1 border border-frame bg-card px-3 py-2"
              >
                {options.map((y) => (
                  <option key={y} value={String(y)}>
                    {yearLabel(y)}
                    {typeof y === 'number' && !yearChoices.includes(y) ? ' (no data)' : ''}
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
          {note && note !== 'keyword fallback' && <p className="mt-3 text-xs text-subtle">{note}</p>}

          {results.length > 0 && (
            <div className="sticky top-0 z-10 mt-5 border-y border-rule bg-bg/95 py-2 backdrop-blur">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
                <span className="text-subtle">
                  {selected.size > 0
                    ? `已选 ${selected.size} / ${results.length} 条`
                    : `${results.length} 条结果 · 未选中时复制全部`}
                </span>
                <button
                  type="button"
                  className="text-subtle underline hover:text-ink"
                  onClick={() =>
                    setSelected(
                      selected.size === results.length ? new Set() : new Set(results.map((r) => r.slug || r.id)),
                    )
                  }
                >
                  {selected.size === results.length ? '清除选择' : '全选'}
                </button>
                {selected.size > 0 && (
                  <button
                    type="button"
                    className="text-subtle underline hover:text-ink"
                    onClick={() => setSelected(new Set())}
                  >
                    清除
                  </button>
                )}
                <span className="text-muted">复制为：</span>
                {COPY_ACTIONS.map(([format, label]) => (
                  <button
                    key={format}
                    type="button"
                    disabled={!chosen.length}
                    onClick={() => void copy(format, label)}
                    className="border border-frame bg-card px-2 py-1 text-xs hover:border-ink disabled:opacity-50"
                  >
                    {label}
                  </button>
                ))}
                {copied && <span className="text-xs text-accent">{copied}</span>}
                {copyError && <span className="text-xs text-accent">{copyError}</span>}
              </div>
            </div>
          )}

          <div className="mt-2 divide-y divide-rule border-y border-rule">
            {results.map((item) => {
              const slug = item.slug || item.id;
              const ticked = selected.has(slug);
              return (
                <article key={item.id} className="flex items-start gap-3 py-5">
                  <input
                    type="checkbox"
                    className="mt-2 shrink-0"
                    aria-label={`Select ${item.title}`}
                    checked={ticked}
                    onChange={() => toggle(slug)}
                  />
                  <div className="min-w-0 flex-1">
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
                    {(item.paper_url || item.doi) && (
                      <p className="mt-2 flex gap-4 text-xs">
                        {item.paper_url && (
                          <a href={item.paper_url} target="_blank" rel="noreferrer" className="underline">
                            Original source
                          </a>
                        )}
                        {item.doi && (
                          <a
                            href={item.doi.startsWith('http') ? item.doi : `https://doi.org/${item.doi}`}
                            target="_blank"
                            rel="noreferrer"
                            className="underline"
                          >
                            DOI
                          </a>
                        )}
                      </p>
                    )}
                  </div>
                </article>
              );
            })}
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
            <button
              type="button"
              onClick={() => year !== ALL_YEARS && void loadGaps(year)}
              className="text-sm text-subtle hover:underline"
            >
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
