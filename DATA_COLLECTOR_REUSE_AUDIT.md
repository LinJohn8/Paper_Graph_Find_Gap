# Data collector reuse audit

The collector uses official public sources as the data authority and keeps all
responses under `Data/raw`. GitHub projects were reviewed for adapter ideas and
diagnostics; third party scraper code was not copied into this repository.

| Project | Decision | Reason |
|---|---|---|
| [openreview-py](https://github.com/openreview/openreview-py) | Installed dependency | Official SDK, Apache-2.0; used for ICLR/TMLR and ARR discovery. Public Notes currently return Challenge/403 and are recorded as restricted. |
| [acl-org/acl-anthology](https://github.com/acl-org/acl-anthology) | Reference only | Official Anthology data/model; the lightweight HTML adapter avoids cloning the large repository. |
| [J535D165/pyalex](https://github.com/J535D165/pyalex) | Not added | OpenAlex is enrichment, not a replacement for official proceedings; no need for this phase. |
| [pranftw/openreview_scraper](https://github.com/pranftw/openreview_scraper) | Reference only | Useful pagination ideas, but OpenReview access controls must not be bypassed. |
| [fedebotu/ICLR2023-OpenReviewData](https://github.com/fedebotu/ICLR2023-OpenReviewData) | Reference only | Historical snapshot, not a live source and no clear reusable license metadata. |
| CVF/NeurIPS/PMLR official sites | Implemented adapters | Stable public proceedings pages and metadata are fetched with caching, retries and idempotent upserts. |

The new `sync-years` command covers 2022 through 2026 (and the registry lists
2027 for future retries). It does not fabricate a year when an official index is
not published. Abstracts are fetched from detail pages and can be resumed with
`enrich-abstracts`.
