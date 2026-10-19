'use client';

const RELATED_PREVIEW = 10;
import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode, type RefObject } from 'react';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import useSWR from 'swr';
import { z } from 'zod';
import { SearchSyntax } from '@/components/SearchSyntax';
import { ui } from '@/components/ui';

import { MAX_RELAYOUT } from '@/lib/map-layout';
import type { LayoutFrame, LayoutRequest } from '@/lib/map-layout.worker';

import { categoryRgb, labelTerms, normalise, topicLabels, type Rgb } from '@/lib/map';

import type { LabelMode } from './MapCanvas';

const MapCanvas = dynamic(() => import('./MapCanvas'), {
  ssr: false,
  loading: () => <div className="grid h-full place-items-center italic text-muted">Loading the map…</div>,
});

import type { PaperMap, MapTitles, Related, MapSearch, MapRelated } from '@/lib/paper-types';
const SEARCH_MAX_LENGTH = 1000;
/** One paper, from the map's columns. Its title is a separate, later fetch (`titleOf`). */
interface Point {
  slug: string;
  primaryArea: string | null;
  x: number;
  y: number;
  region: number | null;
  cluster: number | null;
}
type ColourBy = 'region' | 'cluster' | 'area';

const json = (credentials: RequestCredentials) => async (url: string) => {
  const r = await fetch(url, { credentials });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
};
/** Local paper reads require no account. */
const publicJson = json('omit');

const COLOURS: [ColourBy, string][] = [
  ['region', 'region'],
  ['cluster', 'cluster'],
  ['area', 'primary area'],
];
const LABELS: [LabelMode, string][] = [
  ['auto', 'by zoom'],
  ['region', 'regions'],
  ['cluster', 'clusters'],
  ['off', 'off'],
];
/** Smallest grouping that gets its name on the map. */
const MIN_LABELLED = { region: 40, cluster: 15 };
const SEARCH_HITS = 10;
/** On by default, a redraw restarts on every search, so wait for a pause in the typing. */
const SEARCH_DEBOUNCE_MS = 450;
/** A paper the search did not find: still there, barely. */
const UNMATCHED_ALPHA = 22;
const MAP_FILTERS_KEY = 'map-filters';
const subscribeToBrowser = () => () => {};
const browserSnapshot = () => true;
const serverSnapshot = () => false;
const MapFilters = z.object({
  colourBy: z.enum(['region', 'cluster', 'area']),
  labelMode: z.enum(['auto', 'region', 'cluster', 'off']),
  query: z.string().max(SEARCH_MAX_LENGTH),
  open: z.boolean(),
  syntax: z.boolean(),
  redraw: z.boolean(),
});

/** Global paper map with local search, topic colours and the original layout worker. */
export function PaperMapView({
  kind,
  year = 2027,
  initialPaper,
}: {
  kind: string;
  year?: number;
  initialPaper: string | null;
}) {
  const router = useRouter();
  const venue = `venue=${encodeURIComponent(kind)}&year=${year}`;
  const { data, error } = useSWR<PaperMap>(`/api/graph?${venue}`, publicJson, { revalidateOnFocus: false });
  const [colourBy, setColourBy] = useState<ColourBy>('region');
  const [labelMode, setLabelMode] = useState<LabelMode>('auto');
  const [selected, setSelected] = useState<number | null>(null);
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(null);
  const [focus, setFocus] = useState<{ index: number } | null>(null);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(true);
  const [syntax, setSyntax] = useState(false);
  const [redraw, setRedraw] = useState(true);
  const [filtersRestored, setFiltersRestored] = useState(false);
  const inBrowser = useSyncExternalStore(subscribeToBrowser, browserSnapshot, serverSnapshot);
  // Restore once during rendering, after hydration makes browser storage available.
  if (inBrowser && !filtersRestored) {
    try {
      const raw = sessionStorage.getItem(MAP_FILTERS_KEY);
      const saved = raw ? MapFilters.safeParse(JSON.parse(raw)) : null;
      if (saved?.success) {
        setColourBy(saved.data.colourBy);
        setLabelMode(saved.data.labelMode);
        setQuery(saved.data.query);
        setOpen(saved.data.open);
        setSyntax(saved.data.syntax);
        setRedraw(saved.data.redraw);
      }
    } catch {
      // Storage may be unavailable; the map still works with its defaults.
    }
    setFiltersRestored(true);
  }
  useEffect(() => {
    // Wait for restoration so the first render's defaults never overwrite the saved filters.
    if (!filtersRestored) return;
    try {
      sessionStorage.setItem(MAP_FILTERS_KEY, JSON.stringify({ colourBy, labelMode, query, open, syntax, redraw }));
    } catch {
      // Remembering filters is optional when browser storage is blocked or full.
    }
  }, [filtersRestored, colourBy, labelMode, query, open, syntax, redraw]);
  const frame = useRef<HTMLDivElement>(null);
  const height = useFillViewport(frame);

  const derived = useMemo(() => (data ? derive(data) : null), [data]);
  const points = derived?.points;
  // Asked for only once the map is drawable, so the two never compete for the line.
  const { data: titles } = useSWR<MapTitles>(data ? `/api/graph/titles?${venue}` : null, publicJson, {
    revalidateOnFocus: false,
  });
  const titleOf = (i: number): ReactNode => titles?.titles[points![i].slug] ?? <span className="italic">…</span>;
  const colours = useMemo(
    () => (points && derived ? paint(points, colourBy, derived.areaIndex) : null),
    [points, colourBy, derived],
  );

  const none: number[] = useMemo(() => [], []);

  const selectedPoint = selected !== null ? points?.[selected] : undefined;
  const { data: related } = useSWR<Related>(
    selectedPoint ? `/api/papers/${encodeURIComponent(selectedPoint.slug)}/related` : null,
    publicJson,
    { revalidateOnFocus: false },
  );
  // The panel lists the best few, and the lines go to those.
  const relatedTop = useMemo(() => related?.related.slice(0, RELATED_PREVIEW), [related]);
  const relatedHere = useMemo(
    () =>
      (relatedTop ?? []).flatMap((r) => {
        const i = derived?.bySlug.get(r.slug);
        return i === undefined ? [] : [i];
      }),
    [relatedTop, derived],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setSelected(null);
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, []);

  // The home page's search, run by the server (full text and filters); the map lights up what it finds.
  const [asked, setAsked] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setAsked(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);
  const {
    data: found,
    error: searchFailed,
    isLoading: searching,
  } = useSWR<MapSearch>(asked ? `/api/graph/search?${venue}&q=${encodeURIComponent(asked)}` : null, publicJson, {
    revalidateOnFocus: false,
    keepPreviousData: true,
  });
  const matches = useMemo(() => {
    if (!asked || !found || searchFailed || !derived) return null;
    return found.slugs.flatMap((slug) => {
      const i = derived.bySlug.get(slug);
      return i === undefined ? [] : [i];
    });
  }, [asked, found, searchFailed, derived]);
  // The topic labels the search's words name are drawn in the accent.
  const highlight = useMemo(() => (asked ? labelTerms(asked) : []), [asked]);
  const lit = matches;
  const shown = useMemo(() => (colours && lit ? spotlight(colours, lit) : colours), [colours, lit]);
  const onlyMatches = useMemo(() => {
    if (!redraw || !matches || !points) return null;
    const mask = new Float32Array(points.length);
    for (const i of matches) mask[i] = 1;
    return mask;
  }, [redraw, matches, points]);

  // Redrawn, the rest is hidden and the matches are laid out again among themselves, in a worker, and drawn as they settle.
  const relaySubset = onlyMatches && matches && matches.length <= MAX_RELAYOUT ? matches : null;
  // Fetched only once a search needs it: most visits never search.
  const { data: relatedEdges } = useSWR<MapRelated>(relaySubset ? `/api/graph/related?${venue}` : null, publicJson, {
    revalidateOnFocus: false,
  });
  const [layout, setLayout] = useState<{ of: number[]; positions: Float32Array; settled: boolean } | null>(null);
  // One worker for the page, made on first use; a new job supersedes the last.
  const worker = useRef<Worker | null>(null);
  const jobs = useRef(0);
  useEffect(() => () => worker.current?.terminate(), []);
  useEffect(() => {
    if (!relaySubset || !derived || !relatedEdges) return;
    const local = new Map(relaySubset.map((i, k) => [i, k]));
    const at = relatedEdges.slugs.map((slug) => local.get(derived.bySlug.get(slug) ?? -1));
    const related: [number, number][] = [];
    for (let k = 0; k < relatedEdges.edges.length; k += 2) {
      const a = at[relatedEdges.edges[k]];
      const b = at[relatedEdges.edges[k + 1]];
      if (a !== undefined && b !== undefined) related.push([a, b]);
    }
    const groups = Int32Array.from(relaySubset, (i) => derived.points[i].cluster ?? -1);
    const start = new Float32Array(relaySubset.length * 2);
    relaySubset.forEach((i, k) => {
      start[2 * k] = derived.positions[2 * i];
      start[2 * k + 1] = derived.positions[2 * i + 1];
    });

    worker.current ??= new Worker(new URL('../../lib/map-layout.worker.ts', import.meta.url), { type: 'module' });
    // The counter itself, not its value: the cleanup bumps it to make every frame of this job stale.
    const counter = jobs;
    const id = ++counter.current;
    worker.current.onmessage = (e: MessageEvent<LayoutFrame>) => {
      if (e.data.id !== counter.current) return;
      const all = Float32Array.from(derived.positions);
      relaySubset.forEach((i, k) => {
        all[2 * i] = e.data.positions[2 * k];
        all[2 * i + 1] = e.data.positions[2 * k + 1];
      });
      setLayout({ of: relaySubset, positions: all, settled: e.data.settled });
    };
    worker.current.postMessage({ id, start, related, groups } satisfies LayoutRequest, {
      transfer: [start.buffer, groups.buffer],
    });
    const running = worker.current;
    return () => running.postMessage({ id: ++counter.current, stop: true } satisfies LayoutRequest);
  }, [relaySubset, derived, relatedEdges]);
  // A layout of another search (or of none) is stale.
  const relaid = layout && relaySubset && layout.of === relaySubset ? layout : null;
  const positions = relaid?.positions ?? derived?.positions;
  const relayoutNote = !onlyMatches
    ? ''
    : !relaySubset
      ? `, too many to lay out again (over ${MAX_RELAYOUT.toLocaleString('en-US')})`
      : relaid?.settled
        ? ', laid out again'
        : ', laying out…';
  // Labels follow a re-laid-out map once it settles, named only from the papers on it.
  const labels = useMemo(() => {
    if (!derived) return null;
    if (!relaid) return { regions: derived.regionLabels, clusters: derived.clusterLabels };
    if (!relaid.settled || !matches) return { regions: [], clusters: [] };
    const on = new Set(matches);
    const only = (g: (p: Point) => number | null) => derived.points.map((p, i) => (on.has(i) ? g(p) : null));
    return {
      regions: topicLabels(
        relaid.positions,
        only((p) => p.region),
        derived.regionNames,
        MIN_LABELLED.region / 4,
      ),
      clusters: topicLabels(
        relaid.positions,
        only((p) => p.cluster),
        derived.clusterNames,
        MIN_LABELLED.cluster / 3,
      ),
    };
  }, [derived, relaid, matches]);

  const go = (i: number) => {
    setSelected(i);
    setFocus({ index: i });
  };

  // `?paper=` once the map has loaded, then the selection back into the URL (without a navigation).
  // Adjusted while rendering, once, as React advises for state that follows other state.
  const [opened, setOpened] = useState(initialPaper === null);
  if (!opened && derived) {
    setOpened(true);
    const i = derived.bySlug.get(initialPaper!);
    if (i !== undefined) {
      setSelected(i);
      setFocus({ index: i });
    }
  }
  useEffect(() => {
    if (!opened) return;
    const url = new URL(location.href);
    if (selectedPoint) url.searchParams.set('paper', selectedPoint.slug);
    else url.searchParams.delete('paper');
    if (url.href !== location.href) history.replaceState(history.state, '', url);
  }, [opened, selectedPoint]);

  const regionName = (p: Point) => (p.region !== null ? derived?.regionNames.get(p.region) : undefined);
  const clusterName = (p: Point) => (p.cluster !== null ? derived?.clusterNames.get(p.cluster) : undefined);
  const empty = error
    ? 'The map could not be loaded.'
    : data?.slugs.length === 0
      ? 'No papers available. Sync a conference to populate the map.'
      : null;

  return (
    <div ref={frame} className="relative overflow-hidden bg-bg" style={{ height }}>
      {derived && shown && !empty && (
        <MapCanvas
          positions={positions!}
          colours={shown}
          shown={onlyMatches}
          starred={none}
          held={none}
          regions={labels!.regions}
          clusters={labels!.clusters}
          labelMode={labelMode}
          highlight={highlight}
          selected={selected}
          neighbours={relatedHere}
          focus={focus}
          onHover={setHover}
          onSelect={(i) => {
            if (i !== null && i === selected) router.push(`/papers/${encodeURIComponent(points![i].slug)}`);
            else setSelected(i);
          }}
          onOpen={(i) => window.open(`/papers/${encodeURIComponent(points![i].slug)}`, '_blank', 'noopener')}
        />
      )}
      {(empty || !data) && (
        <div className="grid h-full place-items-center italic text-muted">{empty ?? 'Loading the map…'}</div>
      )}
      {hover && points && (
        <div
          className="pointer-events-none absolute z-20 max-w-[380px] bg-ink px-2 py-1 font-sans text-xs text-white"
          style={{ left: hover.x + 14, top: hover.y + 10 }}
        >
          {titleOf(hover.index)}
          <span className="block opacity-70">{[clusterName(points[hover.index])].filter(Boolean).join(' · ')}</span>
          {/* Only ever rendered in the browser (a hover), so `navigator` is there. */}
          <span className="mt-0.5 block opacity-50">
            {hover.index === selected ? 'Click again to open the paper · ' : ''}
            {/Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl'}-click to open in a new tab
          </span>
        </div>
      )}

      {/* The controls float over the map; on a phone, along its foot, and they fold away. */}
      <div className="absolute top-3 left-3 z-10 flex max-h-[calc(100%-24px)] w-[340px] flex-col border border-frame bg-card/95 font-sans text-[13px] narrow:top-auto narrow:right-3 narrow:bottom-3 narrow:w-auto narrow:max-h-[45%]">
        <div className="flex items-baseline justify-between gap-3 border-b border-rule-soft px-3 py-2">
          <span className="text-subtle">
            {points ? `${points.length.toLocaleString('en-US')} papers` : 'Papers'}, near the papers most like them
          </span>
          <button type="button" className={ui.linkBtn} onClick={() => setOpen(!open)}>
            {open ? 'hide' : 'show'}
          </button>
        </div>
        {open && (
          <div className="space-y-3 overflow-y-auto px-3 py-2.5">
            <div className="space-y-1 text-subtle">
              <Toggle label="Colour" options={COLOURS} value={colourBy} onChange={setColourBy} />
              <Toggle label="Labels" options={LABELS} value={labelMode} onChange={setLabelMode} />
              <Legend colourBy={colourBy} areas={derived?.areas ?? []} />
            </div>

            <div>
              <input
                className={ui.input}
                type="search"
                placeholder="Search titles, authors, abstracts and keywords"
                maxLength={SEARCH_MAX_LENGTH}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <div className="flex items-baseline justify-between gap-3 text-xs text-muted">
                <span>
                  {!asked
                    ? 'Words search titles, authors and abstracts.'
                    : searchFailed
                      ? 'The search failed.'
                      : searching && !found
                        ? 'Searching…'
                        : matches &&
                          `${matches.length.toLocaleString('en-US')} ${matches.length === 1 ? 'match' : 'matches'}${relayoutNote}`}
                </span>
                <span className="flex gap-3">
                  {asked && (
                    <label className="flex cursor-pointer items-center gap-1">
                      <input type="checkbox" checked={redraw} onChange={(e) => setRedraw(e.target.checked)} />
                      redraw
                    </label>
                  )}
                  <button type="button" className={ui.linkBtn} onClick={() => setSyntax(!syntax)}>
                    {syntax ? 'hide syntax' : 'syntax'}
                  </button>
                </span>
              </div>
              {asked && !searchFailed && found && found.errors.length > 0 && (
                <div className="mt-1 text-xs text-down">Ignored: {found.errors.join('; ')}</div>
              )}
              {syntax && (
                <div className="mt-2 text-xs leading-normal text-muted">
                  <SearchSyntax />
                </div>
              )}
              {matches && matches.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {matches.slice(0, SEARCH_HITS).map((i) => (
                    <li key={i}>
                      <button type="button" className={`${ui.linkBtn} text-left`} onClick={() => go(i)}>
                        {titleOf(i)}
                      </button>{' '}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {selectedPoint && (
              <div className="border-t border-rule-soft pt-2.5">
                <div className="font-serif text-[15px] leading-snug">
                  <Link href={`/papers/${encodeURIComponent(selectedPoint.slug)}`}>{titleOf(selected!)}</Link>
                </div>
                <p className="mt-1 text-xs text-subtle">
                  {[selectedPoint.primaryArea, clusterName(selectedPoint) ?? regionName(selectedPoint)]
                    .filter(Boolean)
                    .map((t) => ` · ${t}`)}
                </p>
                <div className="mt-2.5 mb-1 font-serif text-sm font-semibold">Related papers</div>
                {relatedTop === undefined ? (
                  <p className="italic text-muted">Loading…</p>
                ) : relatedTop.length === 0 ? (
                  <p className="italic text-muted">None named yet.</p>
                ) : (
                  <ol className="list-decimal space-y-1 pl-5">
                    {relatedTop.map((r) => {
                      const i = derived?.bySlug.get(r.slug);
                      return (
                        <li key={r.id}>
                          {i !== undefined ? (
                            <button type="button" className={`${ui.linkBtn} text-left`} onClick={() => go(i)}>
                              {r.title}
                            </button>
                          ) : (
                            <Link href={`/papers/${encodeURIComponent(r.slug)}`}>{r.title}</Link>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** The height that takes an element from where it starts to the foot of the window, kept on resize. */
function useFillViewport(ref: RefObject<HTMLElement | null>): string {
  const [height, setHeight] = useState('calc(100dvh - 56px)');
  useEffect(() => {
    const fit = () =>
      ref.current && setHeight(`${window.innerHeight - ref.current.getBoundingClientRect().top - scrollY}px`);
    fit();
    addEventListener('resize', fit);
    return () => removeEventListener('resize', fit);
  }, [ref]);
  return height;
}

function Toggle<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: [T, string][];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div>
      {label}:{' '}
      {options.map(([v, text], k) => (
        <span key={v}>
          {k > 0 && ' · '}
          {v === value ? (
            <span className={ui.on}>{text}</span>
          ) : (
            <button type="button" className={ui.linkBtn} onClick={() => onChange(v)}>
              {text}
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

function Legend({ colourBy, areas }: { colourBy: ColourBy; areas: string[] }) {
  const items: [string, Rgb][] =
    colourBy === 'area' ? areas.map((a, i) => [a || 'none given', categoryRgb(a ? i : null)]) : [];
  if (items.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-0.5 pt-0.5 text-xs text-subtle">
      {items.map(([text, rgb]) => (
        <li key={text}>
          <span className="mr-1 inline-block size-2" style={{ background: `rgb(${rgb.join(',')})` }} />
          {text}
        </li>
      ))}
    </ul>
  );
}

/** Everything that depends only on the map, computed once per fetch. */
function derive(data: PaperMap) {
  const points: Point[] = data.slugs.map((slug, i) => ({
    slug,
    primaryArea: data.area[i] === null ? null : data.areas[data.area[i]],
    x: data.x[i],
    y: data.y[i],
    region: data.region[i],
    cluster: data.cluster[i],
  }));
  const positions = normalise(points);
  const regionNames = new Map(data.regions.map((t) => [t.number, t.label]));
  const clusterNames = new Map(data.clusters.map((t) => [t.number, t.label]));
  // '' is "none given" in the legend, first.
  const areas = data.area.includes(null) ? ['', ...data.areas] : data.areas;
  return {
    points,
    positions,
    regionNames,
    clusterNames,
    areas,
    areaIndex: new Map(areas.map((a, i) => [a, i])),
    bySlug: new Map(data.slugs.map((slug, i) => [slug, i])),
    regionLabels: topicLabels(positions, data.region, regionNames, MIN_LABELLED.region),
    clusterLabels: topicLabels(positions, data.cluster, clusterNames, MIN_LABELLED.cluster),
  };
}

/** The colouring with every paper but `matches` faded. */
function spotlight(colours: Uint8Array, matches: number[]): Uint8Array {
  const out = Uint8Array.from(colours);
  for (let i = 3; i < out.length; i += 4) out[i] = UNMATCHED_ALPHA;
  for (const i of matches) out[4 * i + 3] = colours[4 * i + 3];
  return out;
}

/** RGBA per point for a colouring. */
function paint(points: Point[], by: ColourBy, areaIndex: Map<string, number>): Uint8Array {
  const out = new Uint8Array(points.length * 4);
  points.forEach((p, i) => {
    const rgb =
      by === 'area'
        ? categoryRgb(p.primaryArea ? areaIndex.get(p.primaryArea)! : null)
        : categoryRgb(by === 'region' ? p.region : p.cluster);
    out.set(rgb, 4 * i);
    out[4 * i + 3] = 200;
  });
  return out;
}
