# Implementation report — Phase 2

The frontend is now an independent Paper Graph application. The retained home, paper detail, map, minimap and data-source pages use only the local FastAPI/SQLite paper API. Paper records, venues, reviews, relations and graph points have no Market or User foreign keys. `venue=all` works for list and graph queries, status/sort/year filters are available, and empty maps render the required message.

The cleanup removed 312 obsolete source files from the application closure, including 17 page routes and 69 route handlers (the manifest records the exact paths), plus 21 market/auth/database dependencies. It removed market, order, position, wallet, account, authentication, notification and administration entry points. The four core renderer/layout/controller files (`MapCanvas.tsx`, `map-layout.ts`, `map-layout.worker.ts`, `map-controller.ts`) are byte-identical to their pre-cleanup hashes. Renderer neutral inputs (`starred` and `held`) are empty arrays; no market data is fetched.

Backend API responses no longer contain market/headline fields. Graph related edges now match the existing worker contract (`slugs` plus flat `edges`). Venue status includes local paper counts and latest sync result. The start script checks Node/Python requirements, uses local loopback services, waits for health, and writes logs.

The formal SQLite corpus was not reset or populated; it remains zero papers. The test suite creates `[TEST FIXTURE]` records in a disposable database only. No OpenReview or collector behavior was changed in this phase.

Known limitations: the SQLite schema remains the Phase 1 schema for backward compatibility (including its existing venue uniqueness constraint); OpenReview 403 and source availability remain documented in `DATA_SOURCE_STATUS.md`. Upstream licensing remains unclear, so this is personal local use.
