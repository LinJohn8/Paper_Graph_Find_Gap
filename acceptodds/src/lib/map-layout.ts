import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationNodeDatum,
} from 'd3-force';
import { UMAP } from 'umap-js';
import { MAP_SIZE, normalise } from './map';
import { cosineDistance } from './vectors';

/**
 * Re-lays out a subset of the paper map in the browser, for `/map`'s
 * "redraw": the matches of a search, spread over the screen and grouped
 * among themselves. Display only — never stored or sent anywhere; the map
 * itself stays the supplier's (`PUT /map`).
 *
 * A force layout over two kinds of edge: the similarity service's related
 * lists between matches (`GET /map/related`), and each match's nearest
 * matches on the supplied map, so a paper with no related list stays near
 * what it was near, and the overall shape stays recognisable. It starts
 * from the supplied positions, rescaled to fill the map.
 */

/** How hard a paper is pulled to its cluster's centre, and to the map's. */
const CLUSTER_PULL = 0.15;
const CENTRE_PULL = 0.03;
/** A link's rest length at the typical neighbour distance, in multiples of the even spacing. */
const LINK_LENGTH = 0.3;
/** Nearest matches on the supplied map each match is tied to. */
const NEAREST = 6;
/**
 * It starts from the supplied layout, which is already close, so few ticks
 * settle it. The many-body repulsion is the whole cost (d3's Barnes–Hut,
 * ~0.8 s for 4k nodes); the links are cheap.
 */
const TICKS = 120;
/** Ticks between frames yielded by `settle`. */
const TICKS_PER_SLICE = 4;

/**
 * More than this and the layout takes several seconds even off the main
 * thread (`map-layout.worker.ts`); the matches are shown in place instead.
 */
export const MAX_RELAYOUT = 10_000;

interface Node extends SimulationNodeDatum {
  x: number;
  y: number;
}

/**
 * Lay out a subset from `start`, its interleaved positions on the supplied
 * map, tied by `related` (directed pairs of indexes into the subset) and
 * drawn together by `groups` (the supplier's cluster per point, or -1).
 * Yields interleaved positions scaled to `[0, MAP_SIZE]` every few ticks, so
 * the layout can be drawn as it settles; the last is the settled layout.
 * Synchronous between yields: run it in a worker.
 */
export function* settle(
  start: Float32Array,
  related: readonly [number, number][],
  groups: Int32Array,
): Generator<Float32Array> {
  const n = start.length / 2;
  const from = normalise(Array.from({ length: n }, (_, k) => ({ x: start[2 * k], y: start[2 * k + 1] })));
  if (n < 3) {
    yield from;
    return;
  }
  const nodes: Node[] = Array.from({ length: n }, (_, k) => ({ x: from[2 * k], y: from[2 * k + 1] }));

  // Spacing that suits the number of nodes: about the gap they would have spread evenly over the map.
  const gap = MAP_SIZE / Math.sqrt(n);

  const seen = new Set<number>();
  const links: { source: number; target: number; strength: number; distance: number }[] = [];
  const link = (a: number, b: number, strength: number, distance: number) => {
    if (a === b) return;
    const key = Math.min(a, b) * n + Math.max(a, b);
    if (seen.has(key)) return;
    seen.add(key);
    links.push({ source: a, target: b, strength, distance });
  };
  for (const [a, b] of related) link(a, b, 0.6, gap * LINK_LENGTH);
  // Near neighbours keep their distances from the supplied map, relative to
  // the typical one and compressed (square root), so close stays closer
  // without a sparse subset keeping its gaps. Not their positions: the
  // subset is free to close up.
  const near = nearestNeighbours(start, NEAREST);
  const apart = (a: number, b: number) => Math.hypot(start[2 * a] - start[2 * b], start[2 * a + 1] - start[2 * b + 1]);
  const typical = median(near.flatMap((ns, a) => ns.map((b) => apart(a, b)))) || 1;
  near.forEach((ns, a) =>
    ns.forEach((b) => link(a, b, 0.3, gap * LINK_LENGTH * Math.min(Math.sqrt(apart(a, b) / typical), 3))),
  );
  const simulation = forceSimulation(nodes)
    .force(
      'link',
      forceLink<Node, (typeof links)[number]>(links)
        .distance((l) => l.distance)
        .strength((l) => l.strength),
    )
    // Gathers each cluster: what makes the subset read as groups rather than an even spread.
    .force('cluster', forceCluster(groups, CLUSTER_PULL))
    // No distanceMax: a hard cut-off leaves papers piled on circles of that radius around every dense spot.
    // A coarse Barnes–Hut (theta 1.5) is fine without one, and half the cost of the default.
    .force(
      'charge',
      forceManyBody<Node>()
        .strength(-gap * 0.12)
        .theta(1.5),
    )
    // A gentle pull to the centre, so groups with no tie between them still close up.
    .force('x', forceX<Node>(MAP_SIZE / 2).strength(CENTRE_PULL))
    .force('y', forceY<Node>(MAP_SIZE / 2).strength(CENTRE_PULL))
    .alphaDecay(1 - Math.pow(0.001, 1 / TICKS))
    .stop();

  for (let t = 0; t < TICKS; t += TICKS_PER_SLICE) {
    simulation.tick(TICKS_PER_SLICE);
    yield normalise(nodes);
  }
}

/** `embedAround` works in multiples of a dot's room, `ROOM`: the caller rescales (`lib/map.ts` `frameAround`). */
const ROOM = 10;
/** Neighbours on the redrawn piece sit about this many `ROOM`s apart. */
const SPACING = 1.6;
/**
 * Neighbours each paper is tied to in the redraw's graph. Few, so the piece
 * breaks into its own groups: at 12 it came out as one even blob.
 */
const AROUND_NEIGHBOURS = 7;
/** A related pair's distance, as a share of the nearer end's distance to its nearest paper. */
const RELATED_NEAR = 0.9;
/** UMAP's: how tightly a neighbourhood packs (low, so groups read as groups), and how long it optimises. */
const MIN_DIST = 0.02;
const EPOCHS = 400;
/** Frames the redraw is animated over, from the map to the layout. */
const AROUND_FRAMES = 36;

/**
 * Lay out a paper's piece of the map again round it, for the paper page's
 * minimap, from the supplied vectors rather than the 2D map: the map
 * flattened 30k papers into a plane, and locally it is an even spread, while
 * the vectors still say which of these papers are close.
 *
 * UMAP (`umap-js`) over the piece alone, as the service's own map is built
 * (`../scraping/tags/map/build.py`): a k-nearest-neighbour graph by cosine
 * distance between `vectors`, with every `related` pair (indexes) and the
 * paper's own `ranked` related papers made neighbours, so the service's
 * related lists shape the neighbourhoods. It starts from the supplied positions, so the
 * piece keeps the map's orientation, and a fixed seed makes it the same
 * every time. Dots are then pushed apart where they would overlap.
 *
 * Yields frames easing from the map (compressed round the paper) to the
 * layout, so the redraw is animated; the last is the layout. Display only,
 * like `settle`. Not normalised: the paper sits at the centre and the caller
 * frames round it (`lib/map.ts` `frameAround`).
 */
export function* embedAround(
  start: Float32Array,
  vectors: readonly (readonly number[])[],
  self: number,
  ranked: readonly number[],
  related: readonly [number, number][],
): Generator<Float32Array> {
  const n = start.length / 2;
  const cx = MAP_SIZE / 2;
  const k = Math.min(AROUND_NEIGHBOURS, n - 2);
  if (k < 2) {
    yield normalise(Array.from({ length: n }, (_, i) => ({ x: start[2 * i], y: start[2 * i + 1] })));
    return;
  }

  const distance = Array.from({ length: n }, (_, a) =>
    Float64Array.from({ length: n }, (_, b) => (a === b ? 0 : cosineDistance(vectors[a], vectors[b]))),
  );
  // A related pair counts as near as either end's nearest paper, so it is always in the graph, whatever the
  // vectors say: the service's related lists are the better judge, as they are on its own map.
  const nearest = distance.map((row, a) => Math.min(...row.filter((_, b) => b !== a)));
  for (const [a, b] of [...related, ...ranked.map((r) => [self, r] as const)]) {
    if (a === b) continue;
    distance[a][b] = distance[b][a] = Math.min(distance[a][b], RELATED_NEAR * Math.min(nearest[a], nearest[b]));
  }
  // UMAP's convention: each point is its own first neighbour, at distance 0.
  const knn = distance.map((row, a) =>
    [a, ...Array.from({ length: n }, (_, b) => b).filter((b) => b !== a)]
      .sort((x, y) => (x === a ? -1 : y === a ? 1 : row[x] - row[y] || x - y))
      .slice(0, k + 1),
  );
  const umap = new UMAP({ nComponents: 2, nNeighbors: k + 1, minDist: MIN_DIST, nEpochs: EPOCHS, random: seeded(1) });
  umap.setPrecomputedKNN(
    knn,
    knn.map((ns, a) => ns.map((b) => distance[a][b])),
  );
  umap.initializeFit(vectors.map((v) => [...v]));
  // Start from the map, round the paper, at about UMAP's own scale ([-10, 10]), in place of its random start.
  const far = Math.max(...Array.from({ length: n }, (_, i) => apartIn(start, i, self)), 1e-9);
  umap.getEmbedding().forEach((e, i) => {
    e[0] = ((start[2 * i] - start[2 * self]) / far) * 10;
    e[1] = ((start[2 * i + 1] - start[2 * self + 1]) / far) * 10;
  });
  for (let e = 0; e < EPOCHS; e++) umap.step();
  const embedded = umap.getEmbedding();

  // Rescaled so neighbours sit `SPACING` rooms apart, the paper at the centre, then overlaps pushed apart.
  const flat = Float32Array.from(embedded.flat());
  const typical = median(nearestNeighbours(flat, 1).map(([b], a) => apartIn(flat, a, b))) || 1;
  const unit = (SPACING * ROOM) / typical;
  const nodes: (Node & { tx: number; ty: number })[] = embedded.map(([x, y]) => {
    const tx = (x - embedded[self][0]) * unit;
    const ty = (y - embedded[self][1]) * unit;
    return { x: tx, y: ty, tx, ty };
  });
  nodes[self].fx = 0;
  nodes[self].fy = 0;
  forceSimulation(nodes)
    .force('collide', forceCollide<Node>(ROOM / 2).strength(0.9))
    .force('x', forceX<(typeof nodes)[number]>((d) => d.tx).strength(0.05))
    .force('y', forceY<(typeof nodes)[number]>((d) => d.ty).strength(0.05))
    .stop()
    .tick(120);
  const end = new Float32Array(2 * n);
  nodes.forEach((d, i) => {
    end[2 * i] = cx + d.x;
    end[2 * i + 1] = cx + d.y;
  });

  // From the map, compressed round the paper to about the layout's size, eased to the layout.
  const reach = Math.max(...Array.from({ length: n }, (_, i) => Math.hypot(end[2 * i] - cx, end[2 * i + 1] - cx)), 1);
  const begin = new Float32Array(2 * n);
  for (let i = 0; i < n; i++) {
    const d = apartIn(start, i, self);
    const f = d === 0 ? 0 : (reach * Math.sqrt(d / far)) / d;
    begin[2 * i] = cx + (start[2 * i] - start[2 * self]) * f;
    begin[2 * i + 1] = cx + (start[2 * i + 1] - start[2 * self + 1]) * f;
  }
  for (let f = 1; f <= AROUND_FRAMES; f++) {
    const t = f / AROUND_FRAMES;
    const e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
    yield begin.map((b, j) => b + (end[j] - b) * e);
  }
}

/** A small seeded generator (mulberry32), so the same piece always lays out the same. */
function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function apartIn(p: Float32Array, a: number, b: number): number {
  return Math.hypot(p[2 * a] - p[2 * b], p[2 * a + 1] - p[2 * b + 1]);
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[sorted.length >> 1];
}

/**
 * Pulls each node toward the centre of its group (-1: none), recomputed
 * every tick. d3 has no such force; this is the usual one, linear in the
 * nodes.
 */
function forceCluster(groups: Int32Array, strength: number) {
  let nodes: Node[] = [];
  const force = (alpha: number) => {
    const sum = new Map<number, { x: number; y: number; n: number }>();
    nodes.forEach((d, k) => {
      const g = groups[k];
      if (g < 0) return;
      const c = sum.get(g) ?? sum.set(g, { x: 0, y: 0, n: 0 }).get(g)!;
      c.x += d.x;
      c.y += d.y;
      c.n++;
    });
    nodes.forEach((d, k) => {
      const c = groups[k] < 0 ? undefined : sum.get(groups[k]);
      if (!c || c.n < 2) return;
      d.vx! += (c.x / c.n - d.x) * strength * alpha;
      d.vy! += (c.y / c.n - d.y) * strength * alpha;
    });
  };
  force.initialize = (n: Node[]) => {
    nodes = n;
  };
  return force;
}

/**
 * Each point's `k` nearest others (interleaved positions), by a uniform grid
 * sized so a cell holds about `k` points, searched ring by ring outward.
 */
export function nearestNeighbours(positions: Float32Array, k: number): number[][] {
  const n = positions.length / 2;
  if (n <= 1) return Array.from({ length: n }, () => []);
  const xs = positions.filter((_, j) => j % 2 === 0);
  const ys = positions.filter((_, j) => j % 2 === 1);
  const [minX, maxX] = [xs.reduce((a, v) => Math.min(a, v)), xs.reduce((a, v) => Math.max(a, v))];
  const [minY, maxY] = [ys.reduce((a, v) => Math.min(a, v)), ys.reduce((a, v) => Math.max(a, v))];
  const cell = Math.max(Math.sqrt(((maxX - minX || 1) * (maxY - minY || 1) * k) / n), 1e-9);
  const cols = Math.floor((maxX - minX) / cell) + 1;
  const rows = Math.floor((maxY - minY) / cell) + 1;
  const grid = new Map<number, number[]>();
  const cx = (i: number) => Math.floor((positions[2 * i] - minX) / cell);
  const cy = (i: number) => Math.floor((positions[2 * i + 1] - minY) / cell);
  for (let i = 0; i < n; i++) {
    const key = cy(i) * cols + cx(i);
    (grid.get(key) ?? grid.set(key, []).get(key)!).push(i);
  }
  const want = Math.min(k, n - 1);
  return Array.from({ length: n }, (_, i) => {
    const x = positions[2 * i];
    const y = positions[2 * i + 1];
    const found: [number, number][] = [];
    // Grow the ring until it holds enough points and one more ring could hold none nearer.
    for (let r = 0; r <= Math.max(cols, rows); r++) {
      for (let gy = cy(i) - r; gy <= cy(i) + r; gy++) {
        for (let gx = cx(i) - r; gx <= cx(i) + r; gx++) {
          if (Math.max(Math.abs(gx - cx(i)), Math.abs(gy - cy(i))) !== r) continue;
          if (gx < 0 || gy < 0 || gx >= cols || gy >= rows) continue;
          for (const j of grid.get(gy * cols + gx) ?? []) {
            if (j !== i) found.push([(positions[2 * j] - x) ** 2 + (positions[2 * j + 1] - y) ** 2, j]);
          }
        }
      }
      if (found.length >= want) {
        found.sort((a, b) => a[0] - b[0]);
        if (found[want - 1][0] <= (r * cell) ** 2) break;
      }
    }
    found.sort((a, b) => a[0] - b[0]);
    return found.slice(0, want).map(([, j]) => j);
  });
}
