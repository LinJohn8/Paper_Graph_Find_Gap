# AcceptOdds Project Audit

## Repository state and license

- Existing source is `acceptodds`, clean on branch `master`, at upstream commit `a6f439ab07027d2438cfff57a5f1c4ab3e51161c` (`Drop the affiliation from the about page's byline`). No working-tree edits existed at audit time. No new branch was created because this checkout is an existing repository; its current branch and commit provide the rollback point.
- `LICENSE` is absent. The package is marked private in `package.json`; dependency licenses were not exhaustively audited. Keep this work for local personal research and validation unless source-code reuse rights are independently established.
- The design documents describe a live prediction-market service. They explain original invariants but do not describe the requested local paper-exploration product.

## Keep

- `src/components/map/MapCanvas.tsx`, `PaperMapView.tsx`, `Minimap.tsx`, `src/lib/map.ts`, `map-layout.ts`, and `map-layout.worker.ts`. These implement WebGL map rendering, interaction, search highlighting and relayout, region/cluster colouring, local UMAP and d3-force layout. The layout algorithms are retained unchanged.
- Paper presentation primitives and the existing markdown/bibliography/link utilities where independent of market state.
- Existing unit tests that cover pure map geometry/layout and paper links.

## Remove or retire from the local product

- Prediction-market routes and UI: markets, order/quote/trade flows, outcomes, portfolio, positions, trader leaderboard, reputation, wallets, comments/backings, market-specific JEV and settlement.
- Authentication, registration, email verification, API keys, bot accounts, admin permissions, mail/digest, trade notifications, and trading-only API endpoints.
- Postgres/Drizzle as the source of local paper data. The new local collector owns durable data in `Data/database/papers.sqlite`.

The old modules are not bulk-deleted during the initial integration because route imports are tightly coupled. The public local product is routed through standalone paper-explorer pages and local API handlers; removing legacy internals can follow once regression coverage covers all preserved paths.

## Refactor and database coupling

- Current schema uses Postgres/Drizzle. `listings` contains title, summary, authors, keywords, primary area, opaque `kind`; `listing_texts` stores full text; `listing_references` and `listing_related` contain citation/related links.
- Graph tables are `map_points(kind, slug, x, y, region, cluster, vector)` and `map_topics(kind, level, number, label)`. They are joined to `listings` by slug. Related edges come from `listing_related`.
- `server/views.ts` has read models `paperMap`, `mapTitles`, `listingMinimap`, `listingRelatedTo`, `listingVenue`, `mapKinds`, `mapRelated`, and `mapSearch`. It also imports markets for headline probabilities and listing read helpers for follower/trade state. The current map page and root layout require auth/cookie/market data even though the canvas itself does not.
- `server/map.ts` accepts a complete precomputed map via `setMap`; it does no embedding or layout generation. `PaperMapView` calls `/api/v1/map`, `/map/titles`, `/map/search`, related/listing APIs. `Minimap` expects map points, vectors, nearest/related indices and cluster topics.
- The original research service and generated `data/iclr2027.sqlite` are absent from this workspace. No external embedding/map generator is present.

## New data boundary

`research-self` will own provider discovery/collection, raw response archives, normalized SQLite rows, FTS search, sync job/state, and local API. Graph export will use the existing columns/semantics (slug, x/y, region/cluster, vectors, topics, related edges), with no new browser layout algorithm.

## Risks and rollback

- High coupling: original pages import market and auth models at layout and route level. Keep legacy source intact while selecting a separate local explorer experience; this allows rollback by restoring the original entry routes.
- OpenReview venue IDs and access state change over time. Discovery and availability must be recorded separately from empty results; failed syncs must never delete stored papers.
- 2027 conference content may not yet be public. The tool reports verified source status and never substitutes another year.
- Rollback point is the recorded upstream commit and `master`; new work is limited to added local project files and explicit route integration. Data remains outside Git under `Data`.
