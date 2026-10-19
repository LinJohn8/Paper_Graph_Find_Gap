# Data ingestion report — verified continuation

| Venue | Year | Source | Stored unique papers | With abstracts |
|---|---:|---|---:|---:|
| acl | 2025 | acl_anthology | 10 | 10 |
| cvpr | 2025 | cvf | 20 | 20 |
| cvpr | 2026 | cvf | 5 | 5 |
| icml | 2024 | pmlr | 5 | 5 |
| icml | 2025 | pmlr | 3330 | 20 |
| neurips | 2024 | neurips_proceedings | 5 | 5 |
| neurips | 2025 | neurips_proceedings | 15 | 15 |

Total: **3390 unique real public papers**. The previous single-paper PMLR parser was incorrect: it swallowed the directory as one block. BeautifulSoup now parses each official paper block and discovers ICML volumes from the official index. Stable source URLs identify records; the original one-row PMLR ID was retained via its exact URL so it did not become a duplicate.

## Actual selected execution results

| Job | Venue | Year | Source | Fetched | Inserted | Updated | Skipped | Error | Status |
|---:|---|---:|---|---:|---:|---:|---:|---:|---|
| 7 | icml | 2025 | pmlr | 3330 | 3325 | 5 | 0 | 0 | success |
| 10 | cvpr | 2026 | cvf | 5 | 5 | 0 | 0 | 0 | partial |
| 11 | neurips | 2024 | neurips_proceedings | 5 | 5 | 0 | 0 | 0 | partial |
| 20 | icml | 2025 | pmlr | 3330 | 0 | 0 | 3330 | 0 | success |
| 22 | acl | 2025 | acl_anthology | 10 | 0 | 0 | 10 | 0 | partial |
| 24 | icml | 2024 | pmlr | 5 | 5 | 0 | 0 | 0 | partial |
| 25 | neurips | 2025 | neurips_proceedings | 10 | 10 | 0 | 0 | 0 | partial |
| 26 | cvpr | 2025 | cvf | 20 | 0 | 0 | 20 | 0 | partial |

Each row is one actual job; it is not a cumulative venue total. `partial` marks deliberate `--limit` runs. Entire job histories, errors and timestamps are in `Data/logs/current-ingestion-evidence.json` and SQLite `sync_jobs`.

Raw HTML plus retrieval URL/time sidecars are saved under `Data/raw/<provider>/<venue>/<year>`. Cached snapshots resume interrupted runs via idempotent rechecks; remove/refresh snapshots only intentionally if checking source revisions (automatic ETag freshness is not yet implemented). Timeout/429/server-error retries are bounded. Failure preserves already imported records. Existing nonempty abstracts/DOI/PDF/author data are preserved during metadata-only rechecks. Reviews are untouched. Source publication records are marked `published` and related through `paper_venues.relation_type=publication`, without inventing submission dates or decisions.

The first ACL sample accidentally included proceedings front matter ID `.0`. That one non-paper record was removed after the pre-ingest SQLite backup; subsequent parsing excludes `.0`. Backup: `Data/cache/before-next-ingest.sqlite`. No genuine paper corpus was reset. Foreign-key check: [].

Frontend fixes are limited to source labels, distinct Venue options, published status, encoded IDs and multi-year registry data. Namespaced IDs containing slashes now work through detail/review/related/minimap routes. Browser verification opens real ACL, ICML, CVPR and NeurIPS details, searches GraphNarrator and validates original source links. Evidence: `Data/cache/real-data-verification/results.json` and screenshots. Default year stays 2027.

All four core Graph hashes unchanged: True. Real papers have no newly generated graph points in this run, so the existing Graph displays its safe empty state. No Graph algorithm was changed.

Remaining: full ACL/CVF/NeurIPS runs, complete ICML abstract enrichment, ACL Findings/workshops/ARR mapping, automatic cache freshness, cross-provider identifier mapping beyond exact source URLs, OpenAlex/TMLR fallback and OpenReview challenge resolution. These are not claimed complete.
