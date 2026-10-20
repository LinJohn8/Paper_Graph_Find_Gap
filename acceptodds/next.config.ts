import type { NextConfig } from 'next';

// The browser calls same-origin `/api/...`. Most endpoints have a local route
// handler under src/app/api that forwards to FastAPI; the slow ones (/api/gaps,
// /api/papers/semantic) have their own handlers too, because Next's rewrite proxy
// closes the connection ("socket hang up") on requests that run past ~30 s, while
// a semantic search or a gap scan legitimately takes 20-40 s. No rewrites are
// needed for those any more; they would shadow the route handlers in beforeFiles,
// so the list is intentionally empty.
const config: NextConfig = {
  poweredByHeader: false,
};

export default config;
