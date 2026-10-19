/**
 * Cosine distance between the map's supplied vectors (`map_points.vector`):
 * 0 for the same direction, 2 for opposite. Scale-free, so the service may
 * send them in any units. A zero vector is 1 from everything.
 */
export function cosineDistance(a: ArrayLike<number>, b: ArrayLike<number>): number {
  let dot = 0;
  let aa = 0;
  let bb = 0;
  for (let k = 0; k < a.length; k++) {
    dot += a[k] * b[k];
    aa += a[k] * a[k];
    bb += b[k] * b[k];
  }
  return aa === 0 || bb === 0 ? 1 : 1 - dot / Math.sqrt(aa * bb);
}
