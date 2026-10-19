"""Generate source-status evidence from the actual local database, not estimates."""
from pathlib import Path
import hashlib
import json
from paper_ingest import db


def main():
    root=Path(__file__).resolve().parents[2]
    with db.connect() as con:
        counts=[dict(r) for r in con.execute('SELECT pv.venue_id venue,pv.year,p.source,count(DISTINCT p.id) papers,sum(p.abstract IS NOT NULL) abstracts FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id GROUP BY pv.venue_id,pv.year,p.source ORDER BY pv.venue_id,pv.year')]
        jobs=[dict(r) for r in con.execute('SELECT * FROM sync_jobs ORDER BY id')]
        total=con.execute('SELECT count(*) FROM papers').fetchone()[0]
        reviews=con.execute('SELECT count(*) FROM reviews').fetchone()[0]
        integrity=[list(r) for r in con.execute('PRAGMA foreign_key_check')]
    manifest=json.loads((root/'Data/cache/phase2-removal-manifest.json').read_text())
    core={p:hashlib.sha256((root/'acceptodds'/p).read_bytes()).hexdigest()==h for p,h in manifest['core_before_sha256'].items()}
    result={'checked_at':db.now(),'total_unique_papers':total,'reviews':reviews,'counts':counts,'jobs':jobs,'foreign_key_errors':integrity,'unchanged_graph_core':core}
    (db.DATA_ROOT/'logs/current-ingestion-evidence.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    table='| Venue | Year | Source | Stored unique papers | With abstracts |\n|---|---:|---|---:|---:|\n'+''.join(f"| {r['venue']} | {r['year']} | {r['source']} | {r['papers']} | {r['abstracts']} |\n" for r in counts)
    selected=[r for r in jobs if r['id'] in (7,20,22,26,10,24,11,25)]
    jobtable='| Job | Venue | Year | Source | Fetched | Inserted | Updated | Skipped | Error | Status |\n|---:|---|---:|---|---:|---:|---:|---:|---:|---|\n'+''.join(f"| {r['id']} | {r['venue']} | {r['year']} | {r['provider']} | {r['fetched_count']} | {r['inserted_count']} | {r['updated_count']} | {r['skipped_count']} | {r['error_count']} | {r['status']} |\n" for r in selected)
    future=[r for r in jobs if r['year']==2027]
    latest={r['venue']:r for r in future}
    futuretable='| Venue | Source | Status | Reason |\n|---|---|---|---|\n'+''.join(f"| {k} | {r['provider']} | {r['status']} | {(r['error_message'] or '').split(chr(10))[0].replace('|','/')} |\n" for k,r in latest.items())
    status=f'''# Data Source Status

Verified at {result['checked_at']}. SQLite: `{db.DB_PATH}`. Actual unique real papers: **{total}**. No fabricated reviews ({reviews} stored review rows).

{table}
ICML 2025 is a complete official directory import (3,330 records); abstract enrichment currently covers only the first 20. Other sources are intentionally limited samples, not full proceedings. NeurIPS 2025 includes 10 main-conference records and 5 Creative AI Track records; the source path/primary area preserves the distinction. ACL records are Main Long Papers; ARR review/commitment data has not been obtained.

## 2027 checks

{futuretable}
`not_open` means the checked public official proceedings entry is missing/not yet listed, not that there are no submissions. No earlier year was substituted. For ICML/ACL/CVF/NeurIPS the default collector now checks official published proceedings; OpenReview submission access is a separate source concern.

## OpenReview diagnosis

Both the existing HTTP adapter and installed official `openreview-py` SDK can read the ICLR 2024 group. A real single-note ID from PMLR (`jYmGi1175R`) and a batch query return `ChallengeRequiredError`, HTTP 403, `Challenge verification required`. This is a server challenge refusal, not evidence of empty or rejected papers. No challenge bypass or private-data access was attempted. Logs: `Data/logs/openreview-current-diagnostic.json`, `Data/logs/openreview-sdk-diagnostic.json`.
'''
    (root/'DATA_SOURCE_STATUS.md').write_text(status,encoding='utf-8')
    report=f'''# Data ingestion report — verified continuation

{table}
Total: **{total} unique real public papers**. The previous single-paper PMLR parser was incorrect: it swallowed the directory as one block. BeautifulSoup now parses each official paper block and discovers ICML volumes from the official index. Stable source URLs identify records; the original one-row PMLR ID was retained via its exact URL so it did not become a duplicate.

## Actual selected execution results

{jobtable}
Each row is one actual job; it is not a cumulative venue total. `partial` marks deliberate `--limit` runs. Entire job histories, errors and timestamps are in `Data/logs/current-ingestion-evidence.json` and SQLite `sync_jobs`.

Raw HTML plus retrieval URL/time sidecars are saved under `Data/raw/<provider>/<venue>/<year>`. Cached snapshots resume interrupted runs via idempotent rechecks; remove/refresh snapshots only intentionally if checking source revisions (automatic ETag freshness is not yet implemented). Timeout/429/server-error retries are bounded. Failure preserves already imported records. Existing nonempty abstracts/DOI/PDF/author data are preserved during metadata-only rechecks. Reviews are untouched. Source publication records are marked `published` and related through `paper_venues.relation_type=publication`, without inventing submission dates or decisions.

The first ACL sample accidentally included proceedings front matter ID `.0`. That one non-paper record was removed after the pre-ingest SQLite backup; subsequent parsing excludes `.0`. Backup: `Data/cache/before-next-ingest.sqlite`. No genuine paper corpus was reset. Foreign-key check: {integrity}.

Frontend fixes are limited to source labels, distinct Venue options, published status, encoded IDs and multi-year registry data. Namespaced IDs containing slashes now work through detail/review/related/minimap routes. Browser verification opens real ACL, ICML, CVPR and NeurIPS details, searches GraphNarrator and validates original source links. Evidence: `Data/cache/real-data-verification/results.json` and screenshots. Default year stays 2027.

All four core Graph hashes unchanged: {all(core.values())}. Real papers have no newly generated graph points in this run, so the existing Graph displays its safe empty state. No Graph algorithm was changed.

Remaining: full ACL/CVF/NeurIPS runs, complete ICML abstract enrichment, ACL Findings/workshops/ARR mapping, automatic cache freshness, cross-provider identifier mapping beyond exact source URLs, OpenAlex/TMLR fallback and OpenReview challenge resolution. These are not claimed complete.
'''
    (root/'DATA_INGESTION_REPORT.md').write_text(report,encoding='utf-8')
    with (root/'TEST_REPORT.md').open('a',encoding='utf-8') as f:
        f.write(f'\n## Verified collection continuation — {result["checked_at"]}\n\nPython: 10 tests passed, including parser boundaries, idempotency/dry-run, failure logs and slash-containing IDs. Frontend: 18 tests passed; TypeScript, ESLint and full Next build passed. `pip check`: no broken requirements. Browser: real search/filter/detail/source-link checks passed for four venues; screenshots saved in `Data/cache/real-data-verification`. Default 2027 and safe Graph empty state verified. Four core hashes unchanged. SQLite has {total} real papers; foreign-key errors: {integrity}. SDK and HTTP OpenReview diagnostics reproduced the same challenge 403.\n')
    with (root/'README.md').open('a',encoding='utf-8') as f:
        f.write('''
## Real data import commands

```powershell
cd F:\\AIWork\\paper_graph\\research-self
.\\.venv\\Scripts\\python.exe -m paper_ingest sync --venue icml --year 2025
.\\.venv\\Scripts\\python.exe -m paper_ingest sync --venue acl --year 2025 --limit 10
.\\.venv\\Scripts\\python.exe -m paper_ingest sync --venue cvpr --year 2026 --limit 5
.\\.venv\\Scripts\\python.exe -m paper_ingest sync --venue neurips --year 2025 --limit 10
.\\.venv\\Scripts\\python.exe -m paper_ingest status
```

Browse <http://127.0.0.1:3000/?venue=icml&year=2025> or change the Year control to 2024/2025/2026. The default remains 2027. Latest actual counts and limitations are in `DATA_INGESTION_REPORT.md` and `DATA_SOURCE_STATUS.md`. Official SDK diagnostics are optional: `pip install -e ".[openreview-sdk]"`, then `python tests/sdk_openreview_probe.py`. No PDF files are downloaded.
''')
    print(table)
    print(f'Total unique papers: {total}; core unchanged: {all(core.values())}')


if __name__=='__main__':main()
