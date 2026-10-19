'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Deck, LinearInterpolator, OrthographicView, type OrthographicViewState } from '@deck.gl/core';
import { LineLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import {
  CollisionFilterExtension,
  DataFilterExtension,
  type CollisionFilterExtensionProps,
  type DataFilterExtensionProps,
} from '@deck.gl/extensions';
import { EmbeddedController, PanController } from '@/lib/map-controller';
import { dotScale, labelMatches, MAP_SIZE, sparseScale, type TopicLabel } from '@/lib/map';

export type LabelMode = 'auto' | 'region' | 'cluster' | 'off';

/** Below this zoom (past the opening view) `auto` labels regions, above it clusters. */
const CLUSTER_LABELS_FROM = 1;
const ACCENT: [number, number, number] = [179, 27, 27];
const INK: [number, number, number, number] = [29, 29, 29, 255];
const PAGE: [number, number, number, number] = [251, 250, 247, 230];
/**
 * The labels' SDF atlas. deck's `outlineWidth` is a fraction of the glyph's
 * distance field (0–1), and the halo can reach no further than `buffer`: the
 * field is wide enough here (`radius`) for the halo below, with a soft edge
 * (`smoothing`) under half a pixel.
 */
const LABEL_FONT = { sdf: true, fontSize: 96, buffer: 32, radius: 48, smoothing: 0.2 };
/**
 * The halo round a label, so it reads over dense dots: the outline starts at
 * `0.75 · (1 − LABEL_HALO)` of the field, which here is about 2.5px at label
 * sizes (0.2 was barely 1px). Past ~0.85 it would run into the atlas padding.
 */
const LABEL_HALO = 0.45;
/** Label sizes in pixels: a region's a size larger than a cluster's. */
const REGION_LABEL_PX = 15;
const CLUSTER_LABEL_PX = 14;
/**
 * A label the search names: a vivid blue, more saturated than every dot colour (the accent was lost
 * over reject's red), this much bigger, and a ~4px halo.
 */
const LIT_LABEL: [number, number, number, number] = [21, 52, 178, 255];
const LIT_LABEL_EXTRA_PX = 2;
const LIT_LABEL_HALO = 0.7;
/** How far the view may drift from where it opened before "Reset view" shows: a fraction of a pixel. */
const HOME_TOLERANCE = 0.5;
/**
 * Dots on the whole map are this much smaller on a phone: the map opens fitted to a narrower
 * screen, so the papers sit closer together while a dot stays the same size in pixels.
 */
const NARROW_DOTS = 0.6;
const FILTER = new DataFilterExtension({ filterSize: 1 });
const COLLISIONS = new CollisionFilterExtension();
const SAFARI_GESTURES = ['gesturestart', 'gesturechange', 'gestureend'];

/**
 * The map itself, in WebGL (deck.gl). Client-only: `PaperMapView` loads it
 * with `ssr: false`. Draws what it is given and reports hovers and clicks;
 * it fetches nothing and knows nothing about papers.
 */
export default function MapCanvas({
  positions,
  colours,
  shown,
  starred,
  held,
  regions,
  clusters,
  labelMode,
  selected,
  neighbours,
  focus,
  onHover,
  onSelect,
  onOpen,
  dotRadius = 1.6,
  highlight = [],
  dimmed: dimmedOpacity = 0.2,
  embedded = false,
}: {
  /** Interleaved x, y in `[0, MAP_SIZE]` (`lib/map.ts` `normalise`). */
  positions: Float32Array;
  /** RGBA per point. */
  colours: Uint8Array;
  /** 1 per point drawn, 0 per point hidden (neither drawn nor pickable); `null` draws every point. */
  shown: Float32Array | null;
  /** The viewer's starred papers, drawn as a star; held ones, ringed. Indexes into the points. */
  starred: number[];
  held: number[];
  regions: TopicLabel[];
  clusters: TopicLabel[];
  labelMode: LabelMode;
  selected: number | null;
  neighbours: number[];
  /** Fly to a point; a new object each time, so the same point can be flown to twice. */
  focus: { index: number } | null;
  onHover: (hover: { index: number; x: number; y: number } | null) => void;
  onSelect: (index: number | null) => void;
  /** A paper clicked with ⌘ or Ctrl held, as a link would open in a new tab; without it, such a click selects. */
  onOpen?: (index: number) => void;
  /** A search's words (`lib/map.ts` `labelTerms`): topic labels holding one are drawn in `LIT_LABEL`. */
  highlight?: readonly string[];
  /** A dot's radius in pixels at the opening view: small for the whole map, bigger for a piece of it. */
  dotRadius?: number;
  /** The other papers' opacity while one is selected. */
  dimmed?: number;
  /**
   * Set in a page that scrolls (the paper page's minimap): the wheel scrolls the page, and zooms only with ⌘ or Ctrl.
   */
  embedded?: boolean;
}) {
  const parent = useRef<HTMLDivElement>(null);
  const deck = useRef<Deck<OrthographicView> | null>(null);
  const opening = useRef(0);
  const [zoom, setZoom] = useState<number | null>(null);
  const [closeUp, setCloseUp] = useState(false);
  const [moved, setMoved] = useState(false);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const media = matchMedia('(max-width: 720px)');
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  const pointsAt = useRef(positions);
  useEffect(() => {
    pointsAt.current = positions;
  });
  const everyPoint = useMemo(() => new Float32Array(positions.length / 2).fill(1), [positions.length]);
  // A new data object makes deck rebuild its attributes. Zoom changes sizes,
  // not the points or glyphs; keep their data stable throughout a gesture.
  const paperData = useMemo(
    () => ({
      length: positions.length / 2,
      attributes: {
        getPosition: { value: positions, size: 2 },
        getFillColor: { value: colours, size: 4 },
        getFilterValue: { value: shown ?? everyPoint, size: 1 },
      },
    }),
    [positions, colours, shown, everyPoint],
  );
  const shownCount = useMemo(
    () => (shown ? shown.reduce((a, v) => a + v, 0) : positions.length / 2),
    [shown, positions.length],
  );
  const level = labelMode === 'auto' ? (closeUp ? 'cluster' : 'region') : labelMode;
  const topics = level === 'region' ? regions : level === 'cluster' ? clusters : null;
  // The labels the search names, regions and clusters alike, shown at every zoom whatever the level.
  const lit = useMemo(
    () =>
      highlight.length === 0 || labelMode === 'off'
        ? []
        : [...regions, ...clusters].filter((t) => labelMatches(t.text, highlight)),
    [regions, clusters, highlight, labelMode],
  );
  // A region's label is set a size larger than a cluster's.
  const regionSet = useMemo(() => new Set(regions), [regions]);
  // The level's other labels; on phones three quarters of them, favouring the largest topics.
  const labels = useMemo(() => {
    const rest = (topics ?? []).filter((t) => !lit.includes(t));
    return narrow
      ? rest.toSorted((a, b) => b.size - a.size || a.number - b.number).slice(0, Math.round(rest.length * 0.75))
      : rest;
  }, [topics, narrow, lit]);
  // Collide the lit labels with the rest (invisible, at the top priority) so they clear a space for themselves;
  // drawn in a layer of their own, which no collision hides.
  const colliding = useMemo(() => [...lit, ...labels], [lit, labels]);
  const visibleHeld = useMemo(() => (shown ? held.filter((i) => shown[i] > 0) : held), [held, shown]);
  const visibleStarred = useMemo(() => (shown ? starred.filter((i) => shown[i] > 0) : starred), [starred, shown]);
  const selection = useMemo(() => (selected === null ? [] : [...neighbours, selected]), [neighbours, selected]);
  const handlers = useRef({ onHover, onSelect, onOpen });
  useEffect(() => {
    handlers.current = { onHover, onSelect, onOpen };
  });

  // ⌘- or Ctrl-click opens, like a link; a plain click selects.
  const click = (index: number, e: Event) => {
    const { onOpen, onSelect } = handlers.current;
    if (onOpen && e instanceof MouseEvent && (e.metaKey || e.ctrlKey)) onOpen(index);
    else onSelect(index);
  };

  useEffect(() => {
    const el = parent.current!;
    opening.current = Math.log2((Math.min(el.clientWidth, el.clientHeight) / MAP_SIZE) * 0.95);
    const initialViewState = homeView(opening.current);
    deck.current = new Deck({
      parent: el,
      views: new OrthographicView({ flipY: false }),
      initialViewState,
      // A 3x phone screen otherwise draws nine fragments per CSS pixel,
      // including the collision and picking passes. Keep mobile buffers bounded.
      useDevicePixels: matchMedia('(pointer: coarse)').matches ? Math.min(devicePixelRatio, 2) : true,
      controller: { type: embedded ? EmbeddedController : PanController, doubleClickZoom: true, inertia: true },
      touchAction: embedded ? 'pan-x pan-y' : 'none',
      getCursor: ({ isHovering, isDragging }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab'),
      onViewStateChange: ({ viewState }) => {
        const view = viewState as OrthographicViewState;
        setZoom(view.zoom as number);
        setCloseUp((view.zoom as number) - opening.current >= CLUSTER_LABELS_FROM);
        setMoved(awayFromHome(view, opening.current, el));
      },
      onClick: (info) => {
        if (!info.picked) handlers.current.onSelect(null);
      },
    });
    setZoom(opening.current);
    // iOS Safari can still zoom the page on a pinch over the map; refusing its own gesture events stops it.
    const refuse = (e: Event) => e.preventDefault();
    for (const type of SAFARI_GESTURES) el.addEventListener(type, refuse, { passive: false });
    return () => {
      for (const type of SAFARI_GESTURES) el.removeEventListener(type, refuse);
      deck.current?.finalize();
      deck.current = null;
      // finalize() leaves the canvas it made in `parent`; a remount (Strict Mode's, in dev) would stack below it.
      el.replaceChildren();
    };
    // `embedded` is fixed for a figure's life.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Embedded, the wheel scrolls the page and zooms only with ⌘ or Ctrl held (a trackpad pinch
  // arrives as Ctrl + wheel), as an embedded Google map does. Caught on the way down, before
  // deck's own listener on the canvas: a plain wheel is stopped there, and not prevented, so the page scrolls.
  useEffect(() => {
    if (!embedded) return;
    const el = parent.current!;
    const onWheel = (e: WheelEvent) => {
      if (!e.metaKey && !e.ctrlKey) e.stopPropagation();
    };
    el.addEventListener('wheel', onWheel, { capture: true });
    return () => el.removeEventListener('wheel', onWheel, { capture: true });
  }, [embedded]);

  const reset = () => {
    const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    deck.current?.setProps({
      initialViewState: {
        ...homeView(opening.current),
        transitionDuration: still ? 0 : 400,
        transitionInterpolator: new LinearInterpolator(['target', 'zoom']),
      },
    });
  };

  useEffect(() => {
    if (!focus || !deck.current) return;
    const i = focus.index;
    const at = pointsAt.current;
    deck.current.setProps({
      initialViewState: {
        target: [at[2 * i], at[2 * i + 1], 0],
        zoom: Math.max(zoom ?? opening.current, opening.current + 3),
        minZoom: opening.current - 1,
        maxZoom: opening.current + 9,
        transitionDuration: 600,
        transitionInterpolator: new LinearInterpolator(['target', 'zoom']),
      },
    });
    // Only a new focus flies. Not new positions either: a redraw sends them every frame as it settles,
    // and each one flew the view back, so the map could not be panned or zoomed until it stopped.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus]);

  useEffect(() => {
    if (!deck.current || zoom === null) return;
    const n = positions.length / 2;
    const radius = narrow && !embedded ? dotRadius * NARROW_DOTS : dotRadius;
    // Zoom and sparseness both grow the dots; together never past 8x, or zoomed-in sparse dots swamp the map.
    const scale = Math.min(8, dotScale(zoom, opening.current) * sparseScale(shownCount, n));
    const dimmed = selected !== null;
    const biggest = Math.max(1, ...colliding.map((t) => t.size));
    const at = (i: number): [number, number] => [positions[2 * i], positions[2 * i + 1]];
    const layers = [
      new ScatterplotLayer<unknown, DataFilterExtensionProps>({
        id: 'papers',
        data: paperData,
        // Always on the layer, so turning the filter on and off never rebuilds it.
        extensions: [FILTER],
        filterEnabled: shown !== null,
        filterRange: [0.5, 1],
        radiusUnits: 'pixels',
        getRadius: radius,
        radiusScale: scale,
        radiusMinPixels: 1,
        opacity: dimmed ? dimmedOpacity : 1,
        pickable: true,
        onHover: ({ index, x, y }) => handlers.current.onHover(index >= 0 ? { index, x, y } : null),
        onClick: ({ index }, event) => click(index, event.srcEvent),
      }),
      visibleHeld.length > 0 &&
        new ScatterplotLayer<number>({
          id: 'held',
          data: visibleHeld,
          getPosition: (j) => at(j),
          filled: false,
          stroked: true,
          getLineColor: [...ACCENT, 230],
          lineWidthUnits: 'pixels',
          getLineWidth: 1.5,
          radiusUnits: 'pixels',
          getRadius: radius * scale + 3,
          updateTriggers: { getPosition: positions, getRadius: [scale, radius] },
        }),
      visibleStarred.length > 0 &&
        new TextLayer<number>({
          id: 'starred',
          data: visibleStarred,
          getPosition: (j) => at(j),
          getText: () => '★',
          characterSet: ['★'],
          getSize: 9 + 2.5 * scale,
          getColor: (j) => [colours[4 * j], colours[4 * j + 1], colours[4 * j + 2], 255],
          fontFamily: 'Georgia, "Times New Roman", serif',
          fontSettings: { sdf: true },
          outlineWidth: 3,
          outlineColor: PAGE,
          updateTriggers: { getPosition: positions, getColor: colours, getSize: scale },
        }),
      selected !== null &&
        new LineLayer<number>({
          id: 'neighbour-lines',
          data: neighbours,
          getSourcePosition: () => at(selected),
          getTargetPosition: (j) => at(j),
          getColor: [...ACCENT, 140],
          getWidth: 1.2,
          // The data is the same array while a redraw moves the points: without these the lines stay where they were.
          updateTriggers: { getSourcePosition: [positions, selected], getTargetPosition: positions },
        }),
      selected !== null &&
        new ScatterplotLayer<number>({
          id: 'selection',
          data: selection,
          getPosition: (j) => at(j),
          getFillColor: (j) =>
            j === selected ? [...ACCENT, 255] : [colours[4 * j], colours[4 * j + 1], colours[4 * j + 2], 255],
          getLineColor: [255, 255, 255, 255],
          stroked: true,
          lineWidthUnits: 'pixels',
          getLineWidth: 1,
          radiusUnits: 'pixels',
          getRadius: (j) => (j === selected ? 5 : 3.5),
          radiusScale: Math.sqrt(scale),
          pickable: true,
          onHover: ({ object, x, y }) =>
            handlers.current.onHover(object !== undefined ? { index: object, x, y } : null),
          onClick: ({ object }, event) =>
            object === undefined ? handlers.current.onSelect(null) : click(object, event.srcEvent),
          updateTriggers: { getPosition: positions, getFillColor: [selected, colours], getRadius: selected },
        }),
      colliding.length > 0 &&
        new TextLayer<TopicLabel, CollisionFilterExtensionProps<TopicLabel>>({
          id: `labels-${level}`,
          data: colliding,
          getPosition: (d) => [d.x, d.y],
          getText: (d) => d.text,
          // A lit label's invisible copy at its drawn size, so it clears all the room it takes.
          getSize: (d) =>
            (regionSet.has(d) ? REGION_LABEL_PX : CLUSTER_LABEL_PX) + (lit.includes(d) ? LIT_LABEL_EXTRA_PX : 0),
          getColor: (d) => (lit.includes(d) ? [0, 0, 0, 0] : INK),
          fontFamily: 'Georgia, "Times New Roman", serif',
          fontWeight: 'bold',
          characterSet: 'auto',
          // A halo in the page's colour, not a box: dots stay visible round the letters.
          fontSettings: LABEL_FONT,
          outlineWidth: LABEL_HALO,
          outlineColor: [251, 250, 247, 255],
          extensions: [COLLISIONS],
          // deck takes -1000..1000: a label the search names wins every collision, then the bigger topic.
          getCollisionPriority: (d) => (lit.includes(d) ? 500 : -500) + (499 * d.size) / biggest,
          updateTriggers: { getColor: lit, getSize: [regionSet, lit], getCollisionPriority: lit },
        }),
      lit.length > 0 &&
        new TextLayer<TopicLabel>({
          id: 'labels-lit',
          data: lit,
          getPosition: (d) => [d.x, d.y],
          getText: (d) => d.text,
          // Bigger, and with a wider halo than the rest, so no dot touches the letters.
          getSize: (d) => (regionSet.has(d) ? REGION_LABEL_PX : CLUSTER_LABEL_PX) + LIT_LABEL_EXTRA_PX,
          getColor: LIT_LABEL,
          fontFamily: 'Georgia, "Times New Roman", serif',
          fontWeight: 'bold',
          characterSet: 'auto',
          fontSettings: LABEL_FONT,
          outlineWidth: LIT_LABEL_HALO,
          outlineColor: [251, 250, 247, 255],
          updateTriggers: { getSize: regionSet },
        }),
    ];
    deck.current.setProps({ layers });
  }, [
    positions,
    colours,
    shown,
    paperData,
    shownCount,
    visibleStarred,
    visibleHeld,
    selection,
    labels,
    lit,
    regionSet,
    colliding,
    level,
    selected,
    neighbours,
    zoom,
    dotRadius,
    narrow,
    embedded,
    dimmedOpacity,
  ]);

  return (
    <div className="relative h-full w-full">
      <div ref={parent} className="relative h-full w-full" />
      {moved && (
        <button
          type="button"
          className="absolute top-2 right-2 z-10 cursor-pointer border border-frame bg-card/95 px-2 py-0.5 font-sans text-xs hover:border-ink"
          onClick={reset}
        >
          Reset view
        </button>
      )}
    </div>
  );
}

/** Where the map opens: the whole of it, centred. */
function homeView(opening: number): OrthographicViewState {
  return {
    target: [MAP_SIZE / 2, MAP_SIZE / 2, 0],
    zoom: opening,
    minZoom: opening - 1,
    maxZoom: opening + 9,
  };
}

/** Whether the view has been panned or zoomed off where it opened, by more than a fraction of a pixel. */
function awayFromHome(view: OrthographicViewState, opening: number, el: HTMLElement): boolean {
  const zoom = view.zoom as number;
  const [x, y] = view.target as number[];
  const pixels = 2 ** zoom * Math.hypot(x - MAP_SIZE / 2, y - MAP_SIZE / 2);
  const grown = Math.abs(2 ** (zoom - opening) - 1) * Math.min(el.clientWidth, el.clientHeight);
  return pixels > HOME_TOLERANCE || grown > HOME_TOLERANCE;
}
