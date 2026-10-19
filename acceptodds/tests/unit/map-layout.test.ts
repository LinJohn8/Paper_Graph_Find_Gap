import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import { embedAround } from '@/lib/map-layout';
import { MAP_SIZE } from '@/lib/map';
import { cosineDistance } from '@/lib/vectors';

const last = <T>(frames: Generator<T>): T => {
  let out: T | undefined;
  for (const f of frames) out = f;
  return out!;
};

/** A seeded stream in [0, 1). */
function stream(seed: number): () => number {
  let s = seed;
  return () => (s = (s * 16807) % 2147483647) / 2147483647;
}

/** A piece of a map: `n` points scattered by `seed`. */
function piece(n: number, seed: number): Float32Array {
  const r = stream(seed);
  return Float32Array.from({ length: 2 * n }, () => r() * 100);
}

/** `n` vectors in `dims` dimensions, each near one of `topics` random directions (`topicOf`). */
function vectors(n: number, dims: number, topicOf: (i: number) => number, seed: number): number[][] {
  const r = stream(seed);
  const topics = Array.from({ length: 8 }, () => Array.from({ length: dims }, () => r() - 0.5));
  return Array.from({ length: n }, (_, i) => topics[topicOf(i)].map((v) => v + (r() - 0.5) * 0.15));
}

describe('cosineDistance', () => {
  it('is 0 for one direction at any scale, 2 for opposite, 1 for a zero vector', () => {
    expect(cosineDistance([1, 2], [3, 6])).toBeCloseTo(0);
    expect(cosineDistance([1, 2], [-2, -4])).toBeCloseTo(2);
    expect(cosineDistance([1, 0], [0, 1])).toBeCloseTo(1);
    expect(cosineDistance([0, 0], [1, 1])).toBe(1);
  });
});

describe('embedAround', () => {
  // 0 is the paper; 1–20 share its topic, 21–40 and 41–60 two others. The map scatters them all evenly.
  const n = 61;
  const topic = (i: number) => (i <= 20 ? 0 : i <= 40 ? 1 : 2);
  const vs = vectors(n, 32, topic, 5);
  const start = piece(n, 7);
  const d = (out: Float32Array, a: number, b: number) =>
    Math.hypot(out[2 * a] - out[2 * b], out[2 * a + 1] - out[2 * b + 1]);
  const mean = (xs: number[]) => xs.reduce((s, x) => s + x, 0) / xs.length;

  it('keeps the paper at the centre, gathers what its vectors gather, deterministically', () => {
    const out = last(embedAround(start, vs, 0, [], []));
    expect([out[0], out[1]]).toEqual([MAP_SIZE / 2, MAP_SIZE / 2]);
    const own = Array.from({ length: 20 }, (_, k) => d(out, 0, k + 1));
    const others = Array.from({ length: 40 }, (_, k) => d(out, 0, k + 21));
    expect(mean(own)).toBeLessThan(mean(others) / 2);
    expect(last(embedAround(start, vs, 0, [], []))).toEqual(out);
  });

  it('draws the paper’s related papers in, even from another topic', () => {
    const ranked = [45, 50, 55];
    const plain = last(embedAround(start, vs, 0, [], []));
    const drawn = last(
      embedAround(
        start,
        vs,
        0,
        ranked,
        ranked.map((r) => [0, r]),
      ),
    );
    expect(mean(ranked.map((r) => d(drawn, 0, r)))).toBeLessThan(mean(ranked.map((r) => d(plain, 0, r))));
  });

  it('gives every paper a finite place of its own, whatever the ties', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 80 }),
        fc.integer({ min: 1, max: 1e6 }),
        fc.array(fc.tuple(fc.nat(), fc.nat()), { maxLength: 200 }),
        (count, seed, pairs) => {
          const related = pairs.map(([a, b]) => [a % count, b % count] as [number, number]);
          const out = last(
            embedAround(
              piece(count, seed),
              vectors(count, 8, (i) => i % 3, seed),
              0,
              [1, 2].filter((i) => i < count),
              related,
            ),
          );
          expect(out).toHaveLength(2 * count);
          expect(out.every(Number.isFinite)).toBe(true);
          if (count < 4) return;
          for (let a = 0; a < count; a++) for (let b = a + 1; b < count; b++) expect(d(out, a, b)).toBeGreaterThan(1);
        },
      ),
      { numRuns: 30 },
    );
  });
});
