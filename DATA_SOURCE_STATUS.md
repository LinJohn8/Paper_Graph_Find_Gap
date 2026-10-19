# Data Source Status

Verified at 2026-10-14T14:07:02.145501+00:00. SQLite: `F:\AIWork\paper_graph\Data\database\papers.sqlite`. Actual unique real papers: **3390**. No fabricated reviews (0 stored review rows).

| Venue | Year | Source | Stored unique papers | With abstracts |
|---|---:|---|---:|---:|
| acl | 2025 | acl_anthology | 10 | 10 |
| cvpr | 2025 | cvf | 20 | 20 |
| cvpr | 2026 | cvf | 5 | 5 |
| icml | 2024 | pmlr | 5 | 5 |
| icml | 2025 | pmlr | 3330 | 20 |
| neurips | 2024 | neurips_proceedings | 5 | 5 |
| neurips | 2025 | neurips_proceedings | 15 | 15 |

ICML 2025 is a complete official directory import (3,330 records); abstract enrichment currently covers only the first 20. Other sources are intentionally limited samples, not full proceedings. NeurIPS 2025 includes 10 main-conference records and 5 Creative AI Track records; the source path/primary area preserves the distinction. ACL records are Main Long Papers; ARR review/commitment data has not been obtained.

## 2027 checks

| Venue | Source | Status | Reason |
|---|---|---|---|
| iclr | openreview | failed | HTTPStatusError: Client error '403 Forbidden' for url 'https://api2.openreview.net/notes?content.venueid=ICLR.cc%2F2027%2FConference%2FSubmission&limit=1000&offset=0&sort=cdate%3Aasc' |
| neurips | neurips_proceedings | not_open | HTTPStatusError: Client error '404 Not Found' for url 'https://proceedings.neurips.cc/paper_files/paper/2027' |
| icml | pmlr | not_open | LookupError: No ICML 2027 volume listed in official PMLR index |
| acl | acl_anthology | not_open | HTTPStatusError: Client error '404 Not Found' for url 'https://aclanthology.org/events/acl-2027/' |
| cvpr | cvf | not_open | HTTPStatusError: Client error '404 Not Found' for url 'https://openaccess.thecvf.com/CVPR2027?day=all' |
| tmlr | openreview_journal | failed | HTTPStatusError: Client error '403 Forbidden' for url 'https://api2.openreview.net/notes?content.venueid=TMLR%2FSubmission&limit=1000&offset=0&sort=cdate%3Aasc' |

`not_open` means the checked public official proceedings entry is missing/not yet listed, not that there are no submissions. No earlier year was substituted. For ICML/ACL/CVF/NeurIPS the default collector now checks official published proceedings; OpenReview submission access is a separate source concern.

## OpenReview diagnosis

Both the existing HTTP adapter and installed official `openreview-py` SDK can read the ICLR 2024 group. A real single-note ID from PMLR (`jYmGi1175R`) and a batch query return `ChallengeRequiredError`, HTTP 403, `Challenge verification required`. This is a server challenge refusal, not evidence of empty or rejected papers. No challenge bypass or private-data access was attempted. Logs: `Data/logs/openreview-current-diagnostic.json`, `Data/logs/openreview-sdk-diagnostic.json`.
