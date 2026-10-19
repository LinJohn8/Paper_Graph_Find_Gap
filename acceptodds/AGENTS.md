# Paper Graph local frontend
Next.js App Router, React, Tailwind, deck.gl, UMAP and d3-force. FastAPI/SQLite is the only paper-data source.
Preserve MapCanvas rendering and map layout algorithms. Do not add trading, accounts or authentication.
Keep all corpora outside the frontend in ../Data. Test fixtures must use isolated databases.
Run npm test, npm run typecheck and npm run build. Format edited code with Prettier.
This checkout has no upstream license; local personal research only.

<!-- BEGIN:nextjs-agent-rules -->

## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->
