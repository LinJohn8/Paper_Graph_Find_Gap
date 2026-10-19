import type { NextConfig } from 'next';

// The browser calls same-origin `/api/...` (see IdeaFinder). Local route handlers
// under src/app/api already proxy most endpoints; these three exist only on the
// FastAPI backend and must be forwarded. `beforeFiles` is required for
// /api/papers/semantic, otherwise it would be captured by the local
// /api/papers/[paperId] route handler.
const backend = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';

const config: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    return {
      beforeFiles: [
        { source: '/api/venues', destination: `${backend}/api/venues` },
        { source: '/api/papers/semantic', destination: `${backend}/api/papers/semantic` },
        { source: '/api/gaps', destination: `${backend}/api/gaps` },
      ],
      afterFiles: [],
      fallback: [],
    };
  },
};

export default config;
