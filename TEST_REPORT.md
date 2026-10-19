# Test report — Phase 2

Executed on Windows PowerShell, Node 24.13, Python 3.11, Next 16.4.

- `npm test`: 3 files, 18 tests passed.
- `npm run typecheck`: passed.
- `npm run lint -- src tests`: passed.
- `npm run build`: passed; all retained App Router routes compiled.
- `python -m pytest -q`: 6 tests passed (normalization/upsert, FTS/API, empty API, fixture filters/details/reviews/relations/graph/minimap).
- `tests/audit_check.py`: passed; 155 retired HTTP route/method checks returned 404, zero live market/auth hits, four Graph core hashes unchanged, formal SQLite count 0.
- `tests/browser_check.py`: passed. Production services were checked over HTTP; a disposable fixture API/frontend pair was checked with Playwright Chromium. Empty state, six sources, filters, details, public review, minimap canvas, node interaction, full map, search relayout and no uncaught JavaScript errors were verified.
- `start-paper-graph.ps1 -Production`: passed dependency, port, readiness and health checks for ports 8000/3000.

The three FastAPI warnings are upstream deprecation warnings (`on_event` and Starlette/httpx), not failures. Phase 2 fixtures were isolated; Phase 3 intentionally added one real PMLR record as documented below.

Phase 3 ran `python -m paper_ingest sync --venue icml --year 2025 --limit 3` twice: first run `fetched=1, inserted=1`; repeat `fetched=1, updated=1`. The database contains one unique ICML 2025 paper. `python -m compileall research-self/paper_ingest` passed. OpenReview diagnosis is available through `python -m paper_ingest.diagnose_openreview`.

## EXE visualization verification — 2026-10-15

`launcher/build-launcher.ps1` compiled `PaperGraph.exe` with the installed .NET Framework C# compiler. `PaperGraph.exe --self-test` returned exit code 0 and started/stopped the local API and production frontend. The browser verification then started the EXE normally and passed: `/map?venue=icml&year=2025` returned 3,330 graph nodes, the existing canvas rendered, graph search selected a real paper, navigation opened its detail page, and the detail Minimap rendered. No JavaScript errors occurred. Screenshots/results: `Data/cache/exe-verification/results.json`, `icml-2025-map.png`, `real-paper-minimap.png`.

## Verified collection continuation — 2026-10-14T14:07:02.145501+00:00

Python: 10 tests passed, including parser boundaries, idempotency/dry-run, failure logs and slash-containing IDs. Frontend: 18 tests passed; TypeScript, ESLint and full Next build passed. `pip check`: no broken requirements. Browser: real search/filter/detail/source-link checks passed for four venues; screenshots saved in `Data/cache/real-data-verification`. Default 2027 and safe Graph empty state verified. Four core hashes unchanged. SQLite has 3390 real papers; foreign-key errors: []. SDK and HTTP OpenReview diagnostics reproduced the same challenge 403.
