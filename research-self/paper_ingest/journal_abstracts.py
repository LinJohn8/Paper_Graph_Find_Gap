"""Abstract backfill for journal papers.

WHY THIS MODULE EXISTS
----------------------
OpenAlex has no abstract for many Springer Nature titles (measured: Nature MI
2022 = 8/25, Cell = 13/25, Lancet = 9/25 with OpenAlex abstracts), and Crossref
deposits none for these DOIs (measured 0/8 on the same sample). So the
abstracts have to come from somewhere else.

Two channels, tried in this order:

1. **Europe PMC** (EBI's public REST API, no key). It indexes the biomed
   journals -- the whole Nature family, Science, Cell, PRL -- and returns the
   *full* abstract. Measured on the real pending corpus: **39/40 hits** at
   2.2 lookups/second. This is the fast path.
2. **The publisher's own landing page.** The article's abstract block
   (Springer Nature: ``<div id="Abs1-content">``, 1,666 chars measured) or
   ``og:description`` (a 243-char teaser). This covers what Europe PMC does
   not index, but it is slow and it rate-limits: the publisher answers a burst
   with a small "Client Challenge" page carrying no abstract. Measured success
   rate by fan-out: 2 workers -> 35%, 6 -> 7%, 10 -> 0%. The challenge page is
   detected and retried with a backoff instead of being recorded as a miss.

Both are legitimate, documented metadata services: publisher pages are the
publisher's own metadata for its own paper, and Europe PMC is a public
bibliographic index. No PDF, no paywall, no bypass -- same policy as the rest
of the collector.

Pipeline per journal-year:
1. list rows with empty abstracts for the venue-year;
2. for each, look the DOI up in Europe PMC, then fall back to the landing page;
3. accept only if it looks like an abstract; update the row and its FTS
   mirror (rowid-keyed, see db._align_fts_rowids);
4. pace the requests.

Resumable: re-running skips rows that already have an abstract.
"""
from __future__ import annotations

import html
import logging
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from . import db

__all__ = ["backfill_journal_abstracts", "fetch_landing_abstract"]

LOGGER = logging.getLogger("paper_ingest.journal_abstracts")

#: og:description appears in either attribute order depending on template.
_OG_RE_A = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"', re.IGNORECASE)
_OG_RE_B = re.compile(r'<meta\s+content="([^"]*)"\s+property="og:description"', re.IGNORECASE)

#: The full abstract, when the page carries it. Springer Nature renders it in
#: ``<div id="Abs1-content">`` (measured: 1,666 chars versus 243 for the
#: og:description teaser) and also in ``<section data-title="Abstract">``.
#: The full text is worth far more to retrieval than the teaser, so it wins
#: whenever both are present.
_ABS_DIV_RE = re.compile(r'<div[^>]*id="Abs1-content"[^>]*>(.*?)</div>', re.S | re.IGNORECASE)
_ABS_SECTION_RE = re.compile(r'<section[^>]*data-title="Abstract"[^>]*>(.*?)</section>',
                              re.S | re.IGNORECASE)
_ABS_ARTICLE_RE = re.compile(r'<div[^>]*class="[^"]*c-article-section__content[^"]*"[^>]*>(.*?)</div>',
                             re.S | re.IGNORECASE)

#: A bot-challenge page: a small HTML body with no abstract at all. Measured
#: on nature.com under load (200 with a 3 KB "Client Challenge" body), which
#: is *not* a missing abstract but a refusal to answer -- so it is retried
#: with a backoff instead of being recorded as a miss.
_CHALLENGE_RE = re.compile(r"Client Challenge|are you a robot|unusual traffic|"
                            r"enable JavaScript to proceed", re.IGNORECASE)

#: Below this length an og:description is a teaser blurb, not an abstract.
MIN_ABSTRACT_CHARS = 150

#: A full abstract is expected to be longer than the teaser.
MIN_FULL_ABSTRACT_CHARS = 400


def _strip_markup(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    # The rendering injects reference markers like " 1 , 2 , 3 " around
    # citations; collapse the stray punctuation they leave behind.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_landing_abstract(client: httpx.Client, paper_url: str) -> str | None:
    """The paper's abstract from its publisher landing page.

    Prefers the article's own abstract block (Springer Nature: 1,666 chars)
    and falls back to ``og:description`` (243 chars -- a teaser). Transport
    failures of every kind return ``None``: this is a per-paper enhancement
    pass, and one unreachable page must never abort the batch (the broad
    except is deliberate; the runner counts misses).
    """
    try:
        response = client.get(paper_url)
        if response.status_code != 200:
            return None
        text = response.text
        if _CHALLENGE_RE.search(text):
            # A refusal, not a missing abstract. Tell the caller to retry.
            raise _ChallengePage(text[:80])
        for pattern in (_ABS_DIV_RE, _ABS_SECTION_RE, _ABS_ARTICLE_RE):
            match = pattern.search(text)
            if not match:
                continue
            abstract = _strip_markup(match.group(1))
            if len(abstract) >= MIN_FULL_ABSTRACT_CHARS:
                return abstract
        match = _OG_RE_A.search(text) or _OG_RE_B.search(text)
        if not match:
            return None
        abstract = html.unescape(match.group(1)).strip()
        if len(abstract) < MIN_ABSTRACT_CHARS:
            return None
        return abstract
    except _ChallengePage:
        raise
    except Exception:  # noqa: BLE001 - resilience is the point here
        LOGGER.debug("landing page fetch failed for %s", paper_url)
        return None


class _ChallengePage(Exception):
    """The publisher answered with a bot-challenge page instead of the article."""


# --------------------------------------------------------------------------- #
# Europe PMC: the fast channel                                                #
# --------------------------------------------------------------------------- #

#: EBI's public search API. No key, documented, and it indexes the biomed
#: journals this corpus needs (the whole Nature family, Science, Cell, PRL).
EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: ``10.xxxx/yyyy`` anywhere in the URL or bare-DOI field.
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)

#: Europe PMC renders citation markers as ``<sup>1-3</sup>`` and encodes
#: section headings; they are stripped so the abstract is plain text.
_EPMC_TAG_RE = re.compile(r"<[^>]+>")


def doi_of(paper_url: str | None) -> str | None:
    """The DOI inside a paper URL (or a bare DOI), if there is one."""
    if not paper_url:
        return None
    match = _DOI_RE.search(paper_url)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)")
    return doi.lower()


#: DOI prefixes Europe PMC does not index, so the fast channel is skipped for
#: them and the publisher page is used directly. ``10.1038/d41586-`` is
#: Nature's *news* DOI prefix (News & Views, news stories) -- those are not
#: research articles and Europe PMC does not carry them, so asking costs a
#: request that cannot succeed. (The research prefix is ``10.1038/s41586-``.)
EUROPEPMC_EXCLUDED_PREFIXES = ("10.1038/d41586-",)


def europepmc_eligible(doi: str | None) -> bool:
    """Whether the Europe PMC channel is worth trying for this DOI."""
    if not doi:
        return False
    return not doi.startswith(EUROPEPMC_EXCLUDED_PREFIXES)


def fetch_europepmc_abstract(client: httpx.Client, doi: str) -> str | None:
    """The full abstract for ``doi`` from Europe PMC, or ``None``.

    The API returns a record list; only the first record for an exact DOI
    match is considered. A record without an abstract counts as a miss, not
    an error.
    """
    try:
        response = client.get(
            EUROPEPMC_API,
            params={"query": f'DOI:"{doi}"', "resultType": "core",
                    "format": "json", "pageSize": 1},
        )
        if response.status_code != 200:
            return None
        results = response.json().get("resultList", {}).get("result", [])
        if not results:
            return None
        abstract = results[0].get("abstractText") or ""
        if not abstract:
            return None
        abstract = html.unescape(_EPMC_TAG_RE.sub(" ", abstract))
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if len(abstract) < MIN_ABSTRACT_CHARS:
            return None
        return abstract
    except Exception:  # noqa: BLE001 - a miss is the correct outcome here
        LOGGER.debug("europepmc lookup failed for %s", doi)
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
                               sleep_s: float = 0.8, workers: int = 2,
                               epmc_workers: int = 8, epmc_sleep_s: float = 0.05) -> dict[str, Any]:
    """Fill empty abstracts of one journal-year from publisher landing pages.

    Only rows whose ``paper_url`` resolves to the publisher (OpenAlex sets it
    from the landing page) are touched; rows with an existing abstract are
    skipped, so the pass is resumable and idempotent. Non-backfillable hosts
    (Elsevier's interstitial wall) are counted as ``skipped_host`` instead of
    being attempted — each would otherwise cost a request that cannot succeed.

    Concurrency model
    -----------------
    The **network** part is parallel (a small, capped fan-out against the
    publisher), the **database** part is serial and stays on the calling
    thread: a SQLite connection is not safe to use from several threads at
    once, and the write lock is held for the whole batch anyway. Measured
    serial rate was ~1,700 abstracts/hour; the fan-out raises that roughly
    fourfold without changing what the publisher sees beyond a few concurrent
    requests.
    """
    db.init_db()
    with db.connect() as con:
        # No LIMIT here: the priority order is applied in Python (it depends on
        # the DOI prefix, which only the rows themselves know), so slicing
        # before sorting would hand back the wrong rows. The candidate set is
        # a few thousand rows per venue-year -- cheap to hold.
        rows = con.execute(
            """SELECT p.id, p.paper_url FROM papers p
               JOIN paper_venues pv ON pv.paper_id=p.id
               WHERE pv.venue_id=? AND pv.year=?
                 AND (p.abstract IS NULL OR trim(p.abstract)='')
                 AND p.paper_url IS NOT NULL AND p.paper_url != ''
               ORDER BY p.id""",
            (venue, year),
        ).fetchall()

    stats: dict[str, Any] = {"venue": venue, "year": year, "candidates": len(rows),
                             "recovered": 0, "failed": 0, "no_meta": 0, "skipped_host": 0,
                             "from_europepmc": 0, "from_landing_page": 0}
    if not rows:
        return stats

    # Filter first: a non-backfillable host can never succeed through the
    # landing-page channel, so it must not occupy a worker slot. A row that
    # has a DOI can still succeed through Europe PMC, so those are kept.
    todo = [row for row in rows
            if _backfillable(row["paper_url"]) or doi_of(row["paper_url"])]
    stats["skipped_host"] = len(rows) - len(todo)
    if not todo:
        return stats

    # Two channels, run separately because their rate limits differ by an
    # order of magnitude:
    #   * Europe PMC -- fast, generous (stable at 10 concurrent requests),
    #     and it carries the research articles (measured 39/40 hits);
    #   * the publisher page -- the publisher answers a burst with a
    #     "Client Challenge" page, so this channel needs a small fan-out
    #     (measured: 2 workers -> 35% success, 10 -> 0%).
    # Europe PMC does not index Nature's news DOIs, so those papers can only
    # come from the publisher page -- they are the slow lane by necessity.
    fast = [r for r in todo if europepmc_eligible(doi_of(r["paper_url"]))]
    slow = [r for r in todo if not europepmc_eligible(doi_of(r["paper_url"]))]
    if limit:
        # A limited run takes the fast lane first; only when it is empty does
        # the slow lane get a turn.
        fast, slow = fast[:limit], []
        if not fast:
            slow = slow[:limit]

    def _fetch_epmc(row: sqlite3.Row) -> tuple[str | None, str]:
        client = httpx.Client(
            timeout=45, follow_redirects=True,
            headers={"User-Agent": "PaperGraph/0.2 (academic metadata; mailto:research@example.org)"},
        )
        try:
            abstract = fetch_europepmc_abstract(client, doi_of(row["paper_url"]) or "")
            return abstract, ("europepmc" if abstract else "none")
        finally:
            client.close()
            if epmc_sleep_s:
                time.sleep(epmc_sleep_s)

    def _fetch_page(row: sqlite3.Row) -> tuple[str | None, str]:
        client = httpx.Client(
            timeout=45, follow_redirects=True,
            headers={"User-Agent": "PaperGraph/0.2 (academic metadata; mailto:research@example.org)"},
        )
        try:
            for attempt in range(3):
                try:
                    abstract = fetch_landing_abstract(client, row["paper_url"])
                    return abstract, ("landing_page" if abstract else "none")
                except _ChallengePage:
                    if attempt == 2:
                        return None, "none"
                    time.sleep(2.0 * (attempt + 1) + random.random())
            return None, "none"
        finally:
            client.close()
            if sleep_s:
                time.sleep(sleep_s)

    con = db.connect()
    try:
        # Streaming: each result is written as it arrives, so a long year-long
        # batch shows progress and a crash loses at most the in-flight window.
        # The database work stays on this thread (one connection, one writer).
        def _run(batch, fn, pool_size):
            if not batch:
                return
            with ThreadPoolExecutor(max_workers=max(1, pool_size)) as pool:
                futures = {pool.submit(fn, row): row for row in batch}
                for position, future in enumerate(as_completed(futures)):
                    row = futures[future]
                    try:
                        abstract, source = future.result()
                    except Exception:  # noqa: BLE001 - one bad page must not stop the batch
                        abstract, source = None, "none"
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
                        if source == "europepmc":
                            stats["from_europepmc"] += 1
                        else:
                            stats["from_landing_page"] += 1
                    else:
                        stats["no_meta"] += 1
                    if position % 50 == 49:
                        # Commit in batches: a year-long batch would otherwise be
                        # invisible to any other reader until it finished, and a
                        # crash would lose the whole batch.
                        con.commit()

        # The fast lane first, so the papers that carry real abstracts are in
        # the corpus even if the slow lane is still running.
        _run(fast, _fetch_epmc, epmc_workers)
        con.commit()
        _run(slow, _fetch_page, workers)
        con.commit()
    finally:
        con.close()
    return stats
