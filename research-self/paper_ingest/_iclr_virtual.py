"""Reusable official adapter for ICLR proceedings published on ``iclr.cc``.

WHY THIS EXISTS
---------------
ICLR has no PMLR volume, so ``official.PROVIDERS`` (icml/acl/cvpr/neurips) cannot
cover it, and every API route was unavailable when this was written:

* arXiv            -> HTTP 429 (rate/politeness wall).
* OpenAlex         -> per-IP daily budget exhausted; ICLR coverage there was
                      single digits per year even when it answered.
* OpenReview API   -> 403 ``ChallengeRequiredError`` ("Challenge verification
                      required") on every /notes query, v1 and v2, filtered or
                      unfiltered. A bot-verification wall, not a rate limit --
                      deliberately not circumvented.
* DBLP             -> "Making sure you're not a bot!" interstitial.
* Semantic Scholar -> 429.

The official conference virtual site is plain static HTML with no challenge:
``/virtual/<year>/papers.html`` ships a ``<noscript>`` list of every poster as
``<a href="/virtual/<year>/poster/<id>">Title</a>``, and each poster page carries
the title (``h1.event-title``), authors (``div.event-organizers``,
``⋅``-separated), the full abstract (``div.abstract-text``) and the OpenReview
forum link. That is an *official publisher index* -- the same class of source as
the ACL Anthology or CVF open access -- so it is ingested the same way.

DESIGN NOTES
------------
* Pages are cached under ``Data/raw/iclr_virtual/<venue>/<year>/`` so a rerun
  resumes instead of refetching (matching ``official.PublicSource`` behaviour).
* ``httpx.TransportError`` is retried on a *fresh* connection: the Anthology
  drops keep-alive connections with ``RemoteProtocolError``, and the same
  defence is cheap insurance here.
* The year index links to a login redirect whose ``nextp`` query embeds a poster
  path; the href must therefore *end* with ``/poster/<id>``, otherwise a record
  titled "Login" gets harvested as a paper.
* One bad page never aborts a year; it is counted in ``error_count``.

PUBLIC ENTRY POINTS
-------------------
``sync_iclr(year, limit=None, dry_run=False)``
    DB-writing harvest of one year. Returns the same dict shape as
    ``official.sync_official`` so it can drop straight into that dispatch table.
``harvest_years(years=DEFAULT_YEARS, ...)``
    Sequential multi-year convenience wrapper (returns a list of those dicts).
``collect_year(year, limit=None, workers=6)``
    Pure extraction, no DB writes -- use for inspection/validation.
``paper_links(year)`` / ``parse_poster(html, fallback_title)``
    The two testable primitives.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import httpx
from bs4 import BeautifulSoup

#: Provider label recorded on the venue row / sync job.
PROVIDER = "iclr_virtual"
#: Display name written to the ``venues`` table.
VENUE_NAME = "ICLR"
#: Years this adapter is known to cover.
DEFAULT_YEARS: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)
#: Default worker count. Static pages, so a small pool is polite and safe.
DEFAULT_WORKERS = 6

HEADERS = {
    "User-Agent": "PaperGraph/0.1 academic metadata harvester (mailto:research@example.org)"
}

_INDEX_URL = "https://iclr.cc/virtual/{year}/papers.html"
_POSTER_HREF_RE = re.compile(r"/virtual/(\d+)/poster/(\d+)$")
_AUTHOR_SPLIT_RE = re.compile(r"[⋅|]")
_ABSTRACT_LEAD_RE = re.compile(r"^Abstract\s*")


# --------------------------------------------------------------------------- #
# Fetching (cached)                                                            #
# --------------------------------------------------------------------------- #
def cache_dir(venue: str, year: int) -> Path:
    """Directory that holds the cached HTML for one venue-year."""
    from . import db  # local import keeps the module importable without a DB

    return db.DATA_ROOT / "raw" / PROVIDER / venue / str(year)


def fetch_html(url: str, dest: Path, *, attempts: int = 6) -> tuple[str | None, str | None]:
    """Return ``(html, cache_path)`` for ``url``, caching to ``dest``.

    Returns ``(None, None)`` when the page could not be retrieved after retries,
    so one dead page cannot abort a whole year.
    """
    from . import db

    dest.mkdir(parents=True, exist_ok=True)
    path = dest / (hashlib.sha256(url.encode()).hexdigest() + ".html")
    if path.exists():
        return path.read_text(encoding="utf-8"), str(path)

    for attempt in range(attempts):
        client = httpx.Client(timeout=45, follow_redirects=True, headers=HEADERS)
        try:
            response = client.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(2 ** attempt, 20))
                continue
            response.raise_for_status()
            response.encoding = "utf-8"
            path.write_text(response.text, encoding="utf-8")
            path.with_suffix(".json").write_text(
                json.dumps({"url": url, "retrieved_at": db.now(),
                            "status": response.status_code}),
                encoding="utf-8",
            )
            return response.text, str(path)
        except (httpx.TransportError, httpx.TimeoutException):
            # Fresh client next iteration: a dropped keep-alive cannot poison it.
            time.sleep(min(2 ** attempt, 20))
        finally:
            client.close()
    return None, None


# --------------------------------------------------------------------------- #
# Parsing primitives                                                           #
# --------------------------------------------------------------------------- #
def paper_links(html: str, year: int) -> list[tuple[str, str]]:
    """Return de-duplicated ``[(poster_url, title)]`` from one year's index page."""
    soup = BeautifulSoup(html, "html.parser")
    expected = str(year)
    found: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        match = _POSTER_HREF_RE.search(href)
        # The login redirect ("/accounts/login?nextp=/virtual/<year>/poster/<id>")
        # must not become a paper titled "Login": its href does not END in the
        # poster path, so the anchored regex already rejects it -- the explicit
        # year/title guards below are defence in depth.
        if not match or match.group(1) != expected:
            continue
        title = anchor.get_text(" ", strip=True)
        if not title or title.lower() == "login":
            continue
        url = href if href.startswith("http") else "https://iclr.cc" + href
        found.setdefault(url, title)
    return sorted(found.items())


def parse_poster(html: str, fallback_title: str = "") -> dict[str, Any]:
    """Extract ``{title, authors, abstract, forum_id}`` from a poster page."""
    soup = BeautifulSoup(html, "html.parser")
    card = soup.select_one("div.hero-card")

    title = fallback_title
    authors: list[str] = []
    abstract: str | None = None

    if card is not None:
        heading = card.select_one("h1.event-title") or card.select_one("h1")
        if heading is not None and heading.get_text(strip=True):
            title = heading.get_text(" ", strip=True)

        organizers = card.select_one("div.event-organizers")
        if organizers is not None:
            authors = [name.strip()
                       for name in _AUTHOR_SPLIT_RE.split(organizers.get_text(" ", strip=True))
                       if name.strip()]

        node = card.select_one("div.abstract-text") or card.select_one("[id*=abstract]")
        if node is not None:
            text = _ABSTRACT_LEAD_RE.sub("", node.get_text(" ", strip=True)).strip()
            abstract = text or None

    forum_id = None
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "openreview.net/forum" in href:
            match = re.search(r"[?&]id=([^&]+)", href)
            if match:
                forum_id = match.group(1)
                break

    return {"title": title, "authors": authors, "abstract": abstract,
            "forum_id": forum_id}


# --------------------------------------------------------------------------- #
# Collection                                                                  #
# --------------------------------------------------------------------------- #
def collect_year(
    year: int,
    *,
    venue: str = "iclr",
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Extract every ICLR paper for ``year`` without touching the database."""
    dest = cache_dir(venue, year)
    index_html, _ = fetch_html(_INDEX_URL.format(year=year), dest)
    if index_html is None:
        raise RuntimeError(f"could not fetch the ICLR {year} paper index")

    links = paper_links(index_html, year)
    if limit is not None:
        links = links[:limit]
    if not links:
        raise RuntimeError(f"ICLR {year} index parsed but contained no poster links")

    def work(item: tuple[str, str]) -> dict[str, Any] | None:
        url, title = item
        page, raw = fetch_html(url, dest)
        if page is None:
            return None
        record = parse_poster(page, title)
        match = re.search(r"/poster/(\d+)", url)
        poster_id = match.group(1) if match else url
        identifier = f"{year}/{poster_id}"
        return {
            "id": f"{PROVIDER}:{identifier}",
            "source": PROVIDER,
            "source_paper_id": identifier,
            "forum_id": record["forum_id"],
            "title": record["title"],
            "abstract": record["abstract"],
            "authors": record["authors"],
            "keywords": [],
            "primary_area": None,
            "pdf_url": None,
            "paper_url": url,
            "doi": None,
            "arxiv_id": None,
            "submission_year": year,
            "publication_year": year,
            "status": "published",
            "decision": None,
            "created_at_source": None,
            "updated_at_source": None,
            "raw_data_path": raw,
        }

    papers: list[dict[str, Any]] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(work, item) for item in links]
        for done, future in enumerate(as_completed(futures), 1):
            try:
                record = future.result()
            except Exception:  # noqa: BLE001 - skip one bad page, keep the year
                errors += 1
                continue
            if record is None:
                errors += 1
                continue
            papers.append(record)
            if progress is not None and done % 250 == 0:
                progress(done, len(links))

    collect_year.last_errors = errors  # lightweight diagnostics for callers
    return sorted(papers, key=lambda p: p["source_paper_id"])


# --------------------------------------------------------------------------- #
# DB-writing sync (sync_official-compatible shape)                            #
# --------------------------------------------------------------------------- #
def sync_iclr(year: int, limit: int | None = None, dry_run: bool = False,
              *, venue: str = "iclr", workers: int = DEFAULT_WORKERS) -> dict[str, Any]:
    """Harvest one ICLR year into the local DB.

    Returns the same dict shape as ``official.sync_official`` so this can be
    registered in its dispatch table without special-casing.
    """
    from . import db

    started = db.now()
    counts = {"fetched_count": 0, "inserted_count": 0, "updated_count": 0,
              "skipped_count": 0, "error_count": 0}
    job_id = None
    status = "failed"
    error: str | None = None
    index_url = _INDEX_URL.format(year=year)

    db.init_db()
    if not dry_run:
        with db.connect() as con:
            job_id = con.execute(
                "INSERT INTO sync_jobs(venue,year,provider,status,started_at) VALUES(?,?,?,?,?)",
                (venue, year, PROVIDER, "running", started),
            ).lastrowid

    try:
        papers = collect_year(year, venue=venue, limit=limit, workers=workers)
        counts["error_count"] += getattr(collect_year, "last_errors", 0)
        if not papers:
            raise RuntimeError("ICLR index parsed but produced no usable records")

        con = None if dry_run else db.connect()
        try:
            if con is not None:
                db.put_venue(con, venue_id=venue, name=VENUE_NAME, year=year,
                             provider=PROVIDER, source_venue_id=index_url,
                             kind="conference", source_url=index_url,
                             availability="available")
            for position, paper in enumerate(papers):
                counts["fetched_count"] += 1
                if dry_run:
                    continue
                # One connection for the whole year, committed in batches. The
                # old per-paper `with db.connect()` opened and closed 5,468 SQLite
                # connections for ICLR 2026, which is unusable next to the other
                # venue harvests running on the same WAL file.
                existing = con.execute(
                    "SELECT id FROM papers WHERE id=?", (paper["id"],)
                ).fetchone()
                db.upsert_paper(con, paper, venue, year, relation_type="publication")
                counts["updated_count" if existing else "inserted_count"] += 1
                if position % 200 == 199:
                    con.commit()
            if con is not None:
                con.commit()
        finally:
            if con is not None:
                con.close()
        status = "partial" if limit is not None and len(papers) >= limit else "success"
    except Exception as exc:  # noqa: BLE001 - recorded on the job row
        error = f"{type(exc).__name__}: {exc}"
        counts["error_count"] += 1
        status = "failed"
    finally:
        if not dry_run:
            with db.connect() as con:
                con.execute(
                    "UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,"
                    "inserted_count=?,updated_count=?,skipped_count=?,error_count=?,"
                    "error_message=? WHERE id=?",
                    (status, db.now(), *counts.values(), error, job_id),
                )
                con.execute(
                    "UPDATE venues SET last_sync_at=?,availability_status=? WHERE id=? AND year=?",
                    (db.now(), "available" if counts["fetched_count"] else status,
                     venue, year),
                )

    return {"venue": venue, "year": year, "provider": PROVIDER, "status": status,
            **counts, "error": error}


def harvest_years(years: Iterable[int] = DEFAULT_YEARS, *,
                  workers: int = DEFAULT_WORKERS) -> list[dict[str, Any]]:
    """Sync several ICLR years sequentially (each year is internally parallel)."""
    return [sync_iclr(year, workers=workers) for year in years]
