"""Journal harvesting: high-impact journal papers via OpenAlex.

WHY THIS MODULE EXISTS
----------------------
The corpus covers 5 conference venues + TMLR, which biases retrieval toward
conference literature. The user asked for journal papers (Springer Nature
family, Science Citation Index titles, etc.) with the same metadata scope:
title + abstract + authors + DOI + year. No PDFs.

OpenAlex indexes journal papers from every major publisher, so one provider can
serve every journal uniformly, and the API is legitimate, documented and polite-
pool friendly (exactly what :mod:`paper_ingest.providers_openalex` already
built). We reuse that machinery rather than reinventing HTTP behaviour:

* :class:`PoliteHttp` + :class:`OpenAlexProvider.normalize` via a subclass --
  the paper-dict contract (``id, source, title, abstract, ...``) is identical,
  so rows upsert through :func:`paper_ingest.db.upsert_paper` unchanged;
* venue identity comes from OpenAlex *source ids* (``S...``), NOT free-text
  container search: same lesson as the conference sources.

Coverage verified live (see ``_probe_journals.py`` output in the repo history):
every journal below answers per-year ``type:article`` counts for 2022-2026 with
abstracts available in ``abstract_inverted_index`` (rebuilt by
:func:`rebuild_abstract`).

Scope decision: ``type:article`` only. Journals also index editorials,
corrections and news; those are not papers for our graph. A year with an empty
count is *expected* for future years and must not fabricate rows.

Journal registry
----------------
:data:`JOURNAL_SOURCES` maps this project's venue keys to OpenAlex source ids.
The set is curated for impact and breadth across publishers (Springer Nature,
AAAS/Cell Press, IEEE, JMLR, APS). Add a journal by finding its source id:
``GET /sources?search=<name>`` and copy the ``S...`` id.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from .providers_openalex import (
    OPENALEX_API,
    OPENALEX_MAX_PER_PAGE,
    OPENALEX_SELECT,
    MalformedWorkError,
    OpenAlexProvider,
    PoliteHttp,
    PoliteHttpError,
    rebuild_abstract,
)

__all__ = [
    "JOURNAL_SOURCES",
    "JOURNAL_YEARS",
    "JournalProvider",
    "harvest_journal",
    "normalize_title_key",
]

LOGGER = logging.getLogger("paper_ingest.provider_journals")


def normalize_title_key(title: str | None) -> str:
    """Comparison key for cross-source title dedup (same rule as official.py)."""
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]+", "", title.lower())


#: Verified OpenAlex source ids for curated high-impact journals.
#:
#: The value tuple is (source_id, display_name). Verified against the live API
#: in October 2026 (see module docstring); re-verify with /sources?search= if a
#: count ever looks wrong.
JOURNAL_SOURCES: dict[str, tuple[str, str]] = {
    # Springer Nature family
    "nature": ("S137773608", "Nature"),
    "naturemi": ("S2912241403", "Nature Machine Intelligence"),
    "naturecomms": ("S64187185", "Nature Communications"),
    "naturemethods": ("S127827428", "Nature Methods"),
    "natureneuro": ("S126953839", "Nature Neuroscience"),
    "npjqi": ("S2738600312", "npj Quantum Information"),
    # AAAS / Cell Press (the classic "SCI" core)
    "science": ("S3880285", "Science"),
    "cell": ("S110447773", "Cell"),
    "neuron": ("S45757444", "Neuron"),
    # IEEE
    "tpami": ("S199944782", "IEEE Transactions on Pattern Analysis and Machine Intelligence"),
    "ijcv": ("S25538012", "International Journal of Computer Vision"),
    # JMLR (the ML journal the conference corpus always lacked)
    # KNOWN GAP: OpenAlex attaches almost no works to this source for recent
    # years (measured 2026-10-21: per-year count 0 for 2022-2024/2026, 1 for
    # 2025, while the source's total works_count is 1269 — all older papers).
    # Recent JMLR papers are published at jmlr.org, which OpenAlex does not
    # index under this source; a future harvester could scrape jmlr.org/papers/
    # the way tmlr.py scrapes jmlr.org/tmlr/papers/. Until then this key
    # harvests 0 rows, which the runner records as a truthful empty year.
    "jmlr": ("S118988714", "Journal of Machine Learning Research"),
    # APS
    "prl": ("S24807848", "Physical Review Letters"),
    # Chemistry/medicine breadth
    "jacs": ("S111155417", "Journal of the American Chemical Society"),
    "lancet": ("S49861241", "The Lancet"),
}

#: Years the journal corpus covers (same span as the conference corpus).
JOURNAL_YEARS: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)


class JournalProvider(OpenAlexProvider):
    """OpenAlex provider restricted to one journal (source id) per instance.

    Subclassing keeps the whole wire protocol (politeness, retries, select
    projection, normalization) and overrides only the discovery: works are
    filtered by ``locations.source.id`` + ``publication_year`` + ``type:article``
    instead of the conference source map.
    """

    def __init__(self, venue_key: str, *, mailto: str | None = None):
        super().__init__(mailto=mailto)
        if venue_key not in JOURNAL_SOURCES:
            raise ValueError(
                f"unknown journal {venue_key!r}; known: {', '.join(sorted(JOURNAL_SOURCES))}"
            )
        self.venue_key = venue_key
        self.source_id, self.journal_name = JOURNAL_SOURCES[venue_key]

    def harvest(self, venue: str, year: int, *, limit: int | None = None) -> list[dict[str, Any]]:
        """List one journal-year's articles.

        ``venue`` is accepted for provider-protocol compatibility and must equal
        this instance's key (guards against a mis-wired registry).
        """
        if venue not in ("all", self.venue_key):
            raise ValueError(f"this provider serves {self.venue_key!r}, not {venue!r}")
        return self.list_year(year, limit=limit)

    def list_year(self, year: int, *, limit: int | None = None) -> list[dict[str, Any]]:
        """All articles of this journal for ``year`` (paginated, offset-based).

        Offset paging (``page=N``), never cursor paging: OpenAlex gates cursor
        requests behind an API key (HTTP 401 even on page 1 — re-measured
        2026-10-21: a plain request answers 200 while the same request plus
        ``cursor=*`` answers 401 without a key). The conference harvester hit
        the same wall and documented the same fix; ``page=N`` is ungated.
        Journal years are at most a few thousand articles, far below the
        ~10k deep-paging ceiling, so offset paging reaches everything.
        """
        works: list[dict[str, Any]] = []
        per_page = OPENALEX_MAX_PER_PAGE if limit is None else min(int(limit), OPENALEX_MAX_PER_PAGE)
        remaining = limit
        page = 1
        # Offset paging is capped at 50 pages (10,000 works) without an API key
        # — measured 2026-10-21: page 50 answers 200, page 51 answers 400. A
        # journal-year larger than that (Nature Communications 2024+) is
        # imported as its first 10,000 articles; the year is still recorded and
        # the runner reports the fetched count, so the partial coverage is
        # visible rather than silent. An OPENALEX_API_KEY lifts the cap (cursor
        # paging becomes available).
        while page <= 50:
            params = {
                "filter": (
                    f"locations.source.id:{self.source_id},"
                    f"publication_year:{year},"
                    "type:article"
                ),
                "select": OPENALEX_SELECT,
                "per-page": per_page,
                "mailto": self.http.mailto,
                "page": page,
            }
            try:
                payload = self.http.get_json(f"{OPENALEX_API}/works", params)
            except PoliteHttpError as exc:
                if getattr(exc, "status_code", None) == 400 and page > 1:
                    # The unkeyed deep-paging ceiling: stop cleanly instead of
                    # failing the year, which would discard everything fetched.
                    LOGGER.info(
                        "journal %s %s: offset page %d rejected (unkeyed ceiling); "
                        "importing the first %d articles as partial coverage",
                        self.venue_key, year, page, len(works),
                    )
                    break
                raise
            batch = payload.get("results", [])
            if not batch:
                break  # documented end of results
            for work in batch:
                try:
                    normalized = self.normalize(work, self.venue_key, year)
                except MalformedWorkError as exc:
                    LOGGER.debug("skipping malformed journal work: %s", exc)
                    continue
                works.append(normalized)
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return works
            if len(batch) < per_page:
                break  # short page: reached the end
            page += 1
        return works


def harvest_journal(venue_key: str, year: int, *, limit: int | None = None,
                    mailto: str | None = None) -> list[dict[str, Any]]:
    """One-shot convenience wrapper for scripts and tests."""
    provider = JournalProvider(venue_key, mailto=mailto)
    return provider.list_year(year, limit=limit)
