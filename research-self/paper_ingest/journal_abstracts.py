"""Abstract backfill for journal papers from publisher landing pages.

WHY THIS MODULE EXISTS
----------------------
OpenAlex has no abstract for many Springer Nature titles (measured: Nature MI
2022 = 8/25, Cell = 13/25, Lancet = 9/25 with OpenAlex abstracts), and Crossref
deposits none for these DOIs (measured 0/8 on the same sample). But the
publisher's own landing page ships the abstract as
``<meta property="og:description">`` — 12/12 recovered in the probe, ~1s per
DOI, ~150-450 chars, exactly the official abstract text.

That is the publisher's own metadata for its own paper, fetched at a polite
rate, no PDF, no bypass — same policy as the rest of the collector.

Pipeline per journal-year:
1. list rows with empty abstracts for the venue-year;
2. for each, fetch the DOI's landing page once (follow redirects), parse
   og:description (both attribute orders), HTML-unescape;
3. accept only if it looks like an abstract (>150 chars); update the row and its
   FTS mirror (rowid-keyed, see db._align_fts_rowids);
4. sleep between requests (default 1.0s — 73k-scale safety, journals are ~k/yr).

Resumable: re-running skips rows that already have an abstract.
"""
from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import db

__all__ = ["backfill_journal_abstracts", "fetch_landing_abstract"]

LOGGER = logging.getLogger("paper_ingest.journal_abstracts")

#: og:description appears in either attribute order depending on template.
_OG_RE_A = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"', re.IGNORECASE)
_OG_RE_B = re.compile(r'<meta\s+content="([^"]*)"\s+property="og:description"', re.IGNORECASE)

#: Below this length an og:description is a teaser blurb, not an abstract.
MIN_ABSTRACT_CHARS = 150


def fetch_landing_abstract(client: httpx.Client, paper_url: str) -> str | None:
    """The landing page's og:description, if it looks like a real abstract.

    Transport failures of every kind return ``None`` — this is a per-paper
    enhancement pass, and one unreachable page must never abort the batch (the
    broad except is deliberate; the runner counts misses).
    """
    try:
        response = client.get(paper_url)
        if response.status_code != 200:
            return None
        match = _OG_RE_A.search(response.text) or _OG_RE_B.search(response.text)
        if not match:
            return None
        text = html.unescape(match.group(1)).strip()
        if len(text) < MIN_ABSTRACT_CHARS:
            return None
        return text
    except Exception:  # noqa: BLE001 - resilience is the point here
        LOGGER.debug("landing page fetch failed for %s", paper_url)
        return None


#: Publishers whose landing pages ship og:description (measured 2026-10-21:
#: Springer/Nature 12/12 recovered). Elsevier (Cell, The Lancet) serves a
#: meta-refresh interstitial via doi.org and 403s direct article URLs, so the
#: og:description channel cannot reach it — those journals keep their OpenAlex
#: metadata as-is rather than burning a timeout per paper.
BACKFILLABLE_HOSTS = (
    "nature.com",
    "link.springer.com",
    "springer.com",
    "biomedcentral.com",
    "pnas.org",
)

#: DOI prefixes that resolve into the backfillable publishers. OpenAlex stores
#: ``paper_url`` as the bare doi.org link, so the host check alone never fires;
#: the DOI prefix is the reliable signal (10.1038 = Springer Nature, 10.1007 =
#: Springer, 10.1109 = IEEE, 10.1126 = AAAS, 10.1073 = PNAS).
BACKFILLABLE_DOI_PREFIXES = (
    "10.1038/",   # Springer Nature
    "10.1007/",   # Springer
    "10.1186/",   # BMC / Springer Nature
    "10.1109/",   # IEEE (tpami/ijcv)
    "10.1126/",   # AAAS (science)
    "10.1073/",   # PNAS
)


def _backfillable(paper_url: str) -> bool:
    """Whether the og:description channel can plausibly reach this paper.

    doi.org URLs are judged by their DOI prefix (see
    :data:`BACKFILLABLE_DOI_PREFIXES`); direct publisher hosts by domain.
    """
    host = paper_url.split("/")[2] if paper_url.count("/") >= 2 else ""
    if any(host == h or host.endswith("." + h) for h in BACKFILLABLE_HOSTS):
        return True
    for prefix in BACKFILLABLE_DOI_PREFIXES:
        if prefix in paper_url:
            return True
    return False


def backfill_journal_abstracts(venue: str, year: int, *, limit: int | None = None,
                               sleep_s: float = 1.0) -> dict[str, Any]:
    """Fill empty abstracts of one journal-year from publisher landing pages.

    Only rows whose ``paper_url`` resolves to the publisher (OpenAlex sets it
    from the landing page) are touched; rows with an existing abstract are
    skipped, so the pass is resumable and idempotent. Non-backfillable hosts
    (Elsevier's interstitial wall) are counted as ``skipped_host`` instead of
    being attempted — each would otherwise cost a request that cannot succeed.
    """
    db.init_db()
    with db.connect() as con:
        rows = con.execute(
            """SELECT p.id, p.paper_url FROM papers p
               JOIN paper_venues pv ON pv.paper_id=p.id
               WHERE pv.venue_id=? AND pv.year=?
                 AND (p.abstract IS NULL OR trim(p.abstract)='')
                 AND p.paper_url IS NOT NULL AND p.paper_url != ''
               ORDER BY p.id LIMIT ?""",
            (venue, year, limit or -1),
        ).fetchall()

    stats: dict[str, Any] = {"venue": venue, "year": year, "candidates": len(rows),
                             "recovered": 0, "failed": 0, "no_meta": 0, "skipped_host": 0}
    if not rows:
        return stats

    client = httpx.Client(
        timeout=45,
        follow_redirects=True,
        headers={"User-Agent": "PaperGraph/0.2 (academic metadata; mailto:research@example.org)"},
    )
    try:
        con = db.connect()
        for position, row in enumerate(rows):
            if not _backfillable(row["paper_url"]):
                stats["skipped_host"] += 1
                continue
            abstract = fetch_landing_abstract(client, row["paper_url"])
            if abstract:
                con.execute(
                    "UPDATE papers SET abstract=?,updated_at_source=?,last_seen_at=? WHERE id=?",
                    (abstract, db.now(), db.now(), row["id"]),
                )
                con.execute(
                    "INSERT OR REPLACE INTO papers_fts(rowid,paper_id,title,abstract,authors,keywords,venue) "
                    "SELECT rowid, id, title, abstract, authors_json, keywords_json, ? FROM papers WHERE id=?",
                    (venue, row["id"]),
                )
                stats["recovered"] += 1
            else:
                stats["no_meta"] += 1
            if position % 50 == 49:
                con.commit()
            time.sleep(sleep_s)
        con.commit()
        con.close()
    finally:
        client.close()
    return stats
