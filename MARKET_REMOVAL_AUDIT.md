# Market removal audit

## Removed

The application no longer contains prediction, trading, LMSR, odds, positions, portfolio, settlement, wallet, credits, reputation, leaderboard, user account, login/registration, email verification, API-key, agent trading, market comments, market administration or trading-notification functionality. The exact deleted source list and pre/post core hashes are in `Data/cache/phase2-removal-manifest.json`.

Deleted route totals are 17 page routes and 69 route handlers, including old `/markets`, `/listings`, `/positions`, `/portfolio`, `/leaderboard`, `/signin`, `/verify-email`, `/api/v1/*`, market/order/quote/settlement routes and authentication routes. 21 package dependencies were removed, including `better-auth`, `drizzle-orm`, `drizzle-kit`, `pg`, `resend`, `canvas-confetti`, `@better-auth/api-key` and market OpenAPI helpers.

## Retained and decoupled

The Paper Explorer, Paper Detail, six Venue registry, SQLite queries, reviews, related papers, `/api/graph`, `/api/graph/search`, `/api/graph/related`, MapCanvas, Minimap, UMAP/d3-force worker and map controller remain. Graph receives paper IDs, coordinates, regions, clusters and relation edges only. Its four core files have identical SHA-256 hashes before and after cleanup. The old `starred`/`held` renderer props are neutral empty arrays required by the unchanged rendering contract.

## Evidence and remaining hits

`tests/audit_check.py` scans the live application source and found zero market/auth business-word hits. Historical `Data/cache` manifests and backup copies intentionally contain old names for rollback evidence. Generic `authors`/`forum_id` database fields are research metadata, not account or market features. No market-related runtime code remains in `acceptodds/src` or `research-self/paper_ingest`.

Formal HTTP audit checked 155 generated retired route/method combinations: all returned 404. The SQLite file was backed up read-only to `Data/cache/phase2-tests/papers-backup.sqlite`; the formal paper count remained zero. The exact audit result is `Data/cache/phase2-tests/audit-results.json`.

Build, typecheck, lint, frontend tests, backend tests, startup health checks and Playwright browser checks passed. The browser evidence covers empty Papers/Map/Data Sources, fixture search and filters, paper details, public review visibility, minimap canvas and interaction, full-map navigation, search relayout, and selection back to details. `DATA_SOURCE_STATUS.md` was not modified.
