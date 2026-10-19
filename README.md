# Paper Graph

Paper Graph is a local, login-free academic paper browser. It stores source metadata in `Data/database/papers.sqlite`, serves it through the FastAPI service in `research-self`, and keeps the AcceptOdds Paper Map renderer, minimap and layout worker in the Next.js app (`acceptodds`). The prediction-market product has been removed.

## Screenshots

Papers page — the UI opens on the newest year that actually has data. The repaired 2022–2026 corpus holds **73,675 papers** (5 conferences + TMLR journal):

![Papers page showing 20,412 papers for 2026](docs/screenshots/papers-2026.png)

Paper map — one semantic coordinate per paper (TF-IDF/SVD + t-SNE neighbourhood projection + KMeans), rebuilt for every venue-year:

![Paper map for 2026 with 20,412 points](docs/screenshots/map-2026.png)

Data Sources — per-venue, per-year local paper counts and last sync status:

![Data Sources page with every venue-year count](docs/screenshots/data-sources.png)

Paper detail — abstract, extracted research questions, stated gaps and related papers:

![Paper detail page](docs/screenshots/paper-detail.png)

## Data coverage (2022–2026)

Every venue-year was rebuilt from the publishers' own indexes (ICLR virtual site, PMLR, NeurIPS Proceedings, ACL Anthology, CVF Open Access, jmlr.org for TMLR) and then deduplicated across sources. Counts now grow strictly year over year:

| Year | Papers | Distinct titles | Abstract coverage |
|---:|---:|---:|---:|
| 2022 | 8,294 | 8,294 | 97% |
| 2023 | 11,291 | 11,291 | 95% |
| 2024 | 14,227 | 14,224 | 93% |
| 2025 | 19,451 | 19,451 | 93% |
| 2026 | 20,412 | 20,409 | 92% |
| **Total** | **73,675** | **73,524** | **93%** |

Known gaps: NeurIPS 2026 proceedings are not published yet (December 2026); TMLR abstracts are behind an OpenReview Cloudflare challenge (metadata is complete); 122 measured title-variant cross-source twins are retained rather than deleted. Root causes, fixes, per-cell expected-vs-actual numbers, and the rollback files are documented in [DATA_COVERAGE_REPAIR_REPORT.md](DATA_COVERAGE_REPAIR_REPORT.md).

## Start on Windows

Requirements: Python 3.11+, Node.js 20.9+ and PowerShell.

```powershell
cd F:\AIWork\paper_graph
.\start-paper-graph.ps1 -InstallDependencies
# later starts: .\start-paper-graph.ps1
```

The script checks dependencies, refuses occupied ports, writes logs to `Data/logs`, waits for `/api/health` and opens services at <http://127.0.0.1:3000> and <http://127.0.0.1:8000/docs>. Use `-Production` after `npm run build`; stop with the printed `Stop-Process` command.

Manual development:

```powershell
cd F:\AIWork\paper_graph\research-self
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
python -m uvicorn paper_ingest.api:app --host 127.0.0.1 --port 8000
# second terminal
cd F:\AIWork\paper_graph\acceptodds
npm ci
npm run dev
```

Pages: Papers `/`, Graph `/map`, Data Sources `/sources`, and paper details `/papers/{id}`. The six configured venues remain ICLR, NeurIPS, ICML, ACL/ARR, CVPR and TMLR, and the UI opens on the newest year that actually has data (2026 at the time of writing). Collection commands remain documented in `research-self/README.md`; this phase does not change source access.

## Verification

```powershell
cd F:\AIWork\paper_graph\acceptodds
npm test; npm run typecheck; npm run lint -- src tests; npm run build
cd ..\research-self
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\audit_check.py
.\.venv\Scripts\python.exe tests\browser_check.py
```

See [DATA_COVERAGE_REPAIR_REPORT.md](DATA_COVERAGE_REPAIR_REPORT.md), [MARKET_REMOVAL_AUDIT.md](MARKET_REMOVAL_AUDIT.md), [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md), [TEST_REPORT.md](TEST_REPORT.md) and [PROJECT_AUDIT.md](PROJECT_AUDIT.md). The upstream repository has no clear license; keep this checkout for personal local research.

## Origin And Scope

Paper Graph is derived from the open-source [AcceptOdds](https://github.com/benedict-armstrong/acceptodds) codebase. AcceptOdds supplied the initial Next.js paper-map experience and reusable graph components such as `MapCanvas`, `Minimap`, graph interaction, local layout, UMAP/d3-force integration, and paper navigation. The upstream checkout did not provide a clear license statement, so this project is intended for local personal research and validation; review licensing before redistribution.

The application has been converted into a paper browser and graph explorer. Removed or replaced areas include prediction markets, Accept/Reject trading, LMSR pricing, odds, positions, portfolios, wallets, settlement, leaderboards, accounts, authentication, email verification, trading API keys, market administration, trading notifications, and market-only database/API modules. Paper list, detail, search, related-paper, review, venue-status, and graph capabilities were retained and connected to the local Python/FastAPI research database. This cleanup is ongoing and future releases may remove more legacy compatibility code. See `MARKET_REMOVAL_AUDIT.md` and `PROJECT_AUDIT.md`.

## Open-Access Collection Policy

The collector is a metadata crawler for publicly accessible scholarly sources. It focuses on title, authors, abstract, venue, year, DOI and public source/PDF links from official proceedings, ACL Anthology, PMLR, CVF Open Access, NeurIPS Proceedings, and public OpenReview records when the API permits access. It does not bypass authentication, anti-bot challenges, paywalls, private reviews, or access controls, and does not download paywalled papers. A missing future proceedings page is recorded as `not_open`; a public source blocked by an access challenge is recorded as `restricted`.

## Repository Hygiene

The public repository excludes `Data/`, SQLite databases, raw snapshots, graph caches, `.env` files, local venue configuration, virtual environments, `node_modules`, `.next`, logs and the locally built Windows EXE. These files can contain large datasets, machine-specific paths, or local configuration. Use the example files and run the collector locally to recreate them.

## Double-click EXE viewer

The repository root contains `PaperGraph.exe`. Double-click it, or run `.\PaperGraph.exe` from PowerShell. It starts the existing local FastAPI and production Next.js services, checks both health endpoints, opens the ICML 2025 Paper Map, and shows buttons for Papers and Data Sources. The map uses the existing Graph renderer and the generated per-venue coordinates; current corpus counts are in [DATA_COVERAGE_REPAIR_REPORT.md](DATA_COVERAGE_REPAIR_REPORT.md). Close the launcher window to stop only the processes it started. If the EXE is rebuilt, run `.\launcher\build-launcher.ps1` after `npm run build`.

The launcher expects this repository layout, the Python virtual environment at `research-self\.venv`, frontend `node_modules`, `.next\BUILD_ID`, and `Data\database\papers.sqlite`. It never downloads data or changes the Graph algorithm. Startup logs are written to `Data\logs\launcher.log`.

## Real data import commands

```powershell
cd F:\AIWork\paper_graph\research-self
.\.venv\Scripts\python.exe -m paper_ingest sync --venue icml --year 2025
.\.venv\Scripts\python.exe -m paper_ingest sync --venue acl --year 2025 --limit 10
.\.venv\Scripts\python.exe -m paper_ingest sync --venue cvpr --year 2026 --limit 5
.\.venv\Scripts\python.exe -m paper_ingest sync --venue neurips --year 2025 --limit 10
.\.venv\Scripts\python.exe -m paper_ingest status
```

Browse <http://127.0.0.1:3000/?venue=icml&year=2025> or change the Year control to 2024/2025/2026. The default is the newest year with data (2026 at the time of writing). Latest actual counts and limitations are in [DATA_COVERAGE_REPAIR_REPORT.md](DATA_COVERAGE_REPAIR_REPORT.md), `DATA_INGESTION_REPORT.md` and `DATA_SOURCE_STATUS.md`. Official SDK diagnostics are optional: `pip install -e ".[openreview-sdk]"`, then `python tests/sdk_openreview_probe.py`. No PDF files are downloaded.
