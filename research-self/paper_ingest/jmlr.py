"""JMLR ingest adapter backed by the journal's own index on jmlr.org.

Why this source
---------------
OpenAlex does not carry JMLR for the years this project covers: measured
2026-10-21, filtering works by JMLR's OpenAlex source id
(``S118988714``) returns **0** works per year for 2022, 2023, 2024 and 2026
and 1 for 2025, while the source's overall ``works_count`` is 1,269 -- all
older papers. The corpus therefore had no JMLR papers at all, while every
other journal in the registry comes from OpenAlex.

The publisher's own site is open, complete and authoritative, and it is the
same host that already serves TMLR (``paper_ingest/tmlr.py``), so no new
network surface is introduced.

What the site exposes
---------------------
``https://jmlr.org/papers/`` lists every volume (v1 .. vN). A volume page
(``/papers/v24/``) lists its papers as definition lists::

    <dl>
    <dt>Approximation Bounds for Hierarchical Clustering: ...</dt>
    <dd><b><i>Benjamin Moseley, Joshua R. Wang</i></b>; (1):1&minus;36, 2023.
    <br>[<a href='/papers/v24/18-080.html'>abs</a>][...pdf...][...bib...]
    </dl>

and each paper's own page (``/papers/v24/18-080.html``) carries the full
abstract in ``<p class="abstract">`` plus standard ``citation_*`` metadata
(title, one meta tag per author, volume, issue, pages, ISSN, year).

So title, complete author list, year, pages, DOI-less identifier, PDF link
and abstract are all available from the publisher, per paper.

Year assignment
---------------
JMLR publishes one volume per year, but the mapping is *derived from the
data*, never assumed: every entry carries its own year ("..., 2023."), and the
harvester reads it from the entry. A volume page is scanned once (and cached),
and only the papers whose own year matches the requested year are imported.
That way a future volume (v27 = 2026) is picked up automatically as soon as
the publisher posts it, without touching this module.

Abstracts
---------
Fetched from the paper page at a polite rate (see :data:`DEFAULT_WORKERS`).
A paper whose page cannot be read is imported without an abstract rather than
being dropped -- the metadata is still worth having.

Usage
-----
    from paper_ingest.jmlr import sync
    sync(2024)                        # one year, cached volume pages
    sync(2024, force=True)            # refetch the volume pages first

The runner lives in ``_run_jmlr.py`` at the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import httpx

from . import db

__all__ = [
    "INDEX_URL",
    "YEARS",
    "VENUE_ID",
    "VENUE_NAME",
    "PROVIDER",
    "SOURCE",
    "discover_volumes",
    "volume_entries",
    "fetch_paper",
    "sync",
]

#: The volume index.
INDEX_URL = "https://jmlr.org/papers/"

#: Years this project covers. 2026 has no JMLR volume yet (the publisher posts
#: the next volume in December), so that year imports zero papers -- recorded
#: honestly rather than filled with another venue's papers.
YEARS = (2022, 2023, 2024, 2025, 2026)

VENUE_ID = "jmlr"
VENUE_NAME = "JMLR"
PROVIDER = "jmlr_site"
SOURCE = "JMLR"
RELATION_TYPE = "publication"
STATUS = "published"

RAW_DIR = db.DATA_ROOT / "raw" / "jmlr"

#: jmlr.org closes pooled keep-alive connections reliably; each request uses a
#: fresh client and the fan-out is capped so the publisher is never hammered.
DEFAULT_WORKERS = min(4, max(1, int(os.environ.get("PAPER_HTTP_WORKERS", "4") or 4)))

#: A volume page with fewer bytes than this is an error page, not a listing.
MIN_VOLUME_BYTES = 20_000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

#: ``<a href='/papers/v24/18-080.html'>abs</a>`` -- note the single quotes.
_ENTRY_RE = re.compile(r"<dl>(.*?)</dl>", re.S)
_TITLE_RE = re.compile(r"<dt>(.*?)</dt>", re.S)
_AUTHORS_RE = re.compile(r"<b><i>(.*?)</i></b>", re.S)
_CITATION_RE = re.compile(r"\(.*?\):\s*([\d&minus;\-–]+)\s*,\s*(\d{4})")
_ABS_RE = re.compile(r"href=['\"](/papers/v\d+/[^'\"]+\.html)['\"]")
_PDF_RE = re.compile(r"href=['\"](/papers/volume\d+/[^'\"]+\.pdf)['\"]")
_BIB_RE = re.compile(r"href=['\"](/papers/v\d+/[^'\"]+\.bib)['\"]")

_VOLUME_RE = re.compile(r"href=['\"]v(\d+)['\"]")

#: On a paper page.
_ABSTRACT_RE = re.compile(r'<p class="abstract">(.*?)</p>', re.S)
_META_RE = re.compile(r'<meta\s+name="([^"]+)"\s+content="([^"]*)"\s*/?>')
_TITLE_TAG_RE = re.compile(r"<h2>(.*?)</h2>", re.S)
_AUTHOR_LINE_RE = re.compile(r"<p><b><i>(.*?)</i></b>(.*?)</p>", re.S)


def _clean(text: str) -> str:
    """Strip tags and unescape entities, collapsing whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _new_client() -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=60.0, follow_redirects=True)


def _cache_path(volume: int) -> Path:
    return RAW_DIR / f"volume{volume}.html"


def _fetch(url: str, cache: Path | None = None, *, force: bool = False,
           retries: int = 4, log=print) -> str:
    """Fetch a page, using ``cache`` when it holds a plausible response.

    Every attempt uses a fresh client: jmlr.org drops pooled keep-alive
    connections, and only a new client makes the retry useful.
    """
    if cache is not None and cache.exists() and not force:
        text = cache.read_text(encoding="utf-8", errors="replace")
        if len(text) >= MIN_VOLUME_BYTES or ".html" in url:
            return text
    last: Exception | None = None
    for attempt in range(retries):
        client = _new_client()
        try:
            response = client.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response)
            response.raise_for_status()
            text = response.text
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text, encoding="utf-8")
            return text
        except (httpx.TransportError, httpx.HTTPStatusError, OSError) as exc:
            last = exc
            wait = min(2 ** attempt, 30)
            log(f"[jmlr] fetch {url} attempt {attempt + 1}/{retries} failed: "
                f"{type(exc).__name__}: {exc}; retry in {wait}s")
            time.sleep(wait)
        finally:
            client.close()
    raise RuntimeError(f"could not fetch {url}: {last}")


def discover_volumes(*, force: bool = False, log=print) -> list[int]:
    """Every volume number the publisher lists (``/papers/`` -> v1..vN)."""
    text = _fetch(INDEX_URL, RAW_DIR / "index.html", force=force, log=log)
    volumes = sorted({int(v) for v in _VOLUME_RE.findall(text)})
    if not volumes:
        raise RuntimeError("no JMLR volumes found in the index page")
    log(f"[jmlr] {len(volumes)} volumes listed ({volumes[0]}..{volumes[-1]})")
    return volumes


def volume_entries(volume: int, *, force: bool = False, log=print) -> list[dict[str, Any]]:
    """Parse one volume page into paper entries.

    Each entry carries the year **as printed by the publisher**; the importer
    uses that value, never a volume->year guess.
    """
    url = f"https://jmlr.org/papers/v{volume}/"
    text = _fetch(url, _cache_path(volume), force=force, log=log)
    entries: list[dict[str, Any]] = []
    for block in _ENTRY_RE.findall(text):
        title_match = _TITLE_RE.search(block)
        if not title_match:
            continue
        title = _clean(title_match.group(1))
        if not title:
            continue
        abs_match = _ABS_RE.search(block)
        if not abs_match:
            continue
        year_match = _CITATION_RE.search(block)
        if not year_match:
            continue
        year = int(year_match.group(2))
        pages = year_match.group(1).replace("&minus;", "-").replace("–", "-")
        authors_match = _AUTHORS_RE.search(block)
        authors = []
        if authors_match:
            authors = [a.strip() for a in _clean(authors_match.group(1)).split(",") if a.strip()]
        pdf_match = _PDF_RE.search(block)
        bib_match = _BIB_RE.search(block)
        entries.append({
            "title": title,
            "authors": authors,
            "year": year,
            "pages": pages,
            "abs_url": "https://jmlr.org" + abs_match.group(1),
            "paper_id": abs_match.group(1).rsplit("/", 1)[-1].rsplit(".", 1)[0],
            "pdf_url": ("https://jmlr.org" + pdf_match.group(1)) if pdf_match else None,
            "bib_url": ("https://jmlr.org" + bib_match.group(1)) if bib_match else None,
            "volume": volume,
        })
    return entries


def fetch_paper(abs_url: str, *, retries: int = 3) -> dict[str, Any]:
    """Read one paper page: abstract + citation metadata.

    Returns a dict with at least ``abstract`` (possibly empty). A page that
    cannot be read yields an empty abstract rather than an exception, so one
    bad page never aborts a volume.
    """
    client = _new_client()
    try:
        for attempt in range(retries):
            try:
                response = client.get(abs_url)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response)
                if response.status_code == 404:
                    return {"abstract": "", "meta": {}}
                response.raise_for_status()
                text = response.text
                abstract_match = _ABSTRACT_RE.search(text)
                abstract = _clean(abstract_match.group(1)) if abstract_match else ""
                meta = {name: html_mod.unescape(value) for name, value in _META_RE.findall(text)}
                return {"abstract": abstract, "meta": meta}
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == retries - 1:
                    return {"abstract": "", "meta": {}}
                time.sleep(min(2 ** attempt, 15))
        return {"abstract": "", "meta": {}}
    finally:
        client.close()


def _to_paper(entry: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Map one entry + page detail onto the paper-dict contract."""
    meta = detail.get("meta", {})
    year = entry.get("year")
    volume = entry.get("volume")
    number = None
    pages = entry.get("pages")
    if pages and "-" in pages:
        number, _, page_range = pages.partition("-")
        number = number.strip() or None
        pages = page_range.strip() or pages
    return {
        "id": f"jmlr:{entry['paper_id']}",
        "source": SOURCE,
        "source_paper_id": entry["paper_id"],
        "forum_id": None,
        "title": entry["title"],
        "abstract": detail.get("abstract") or "",
        "authors": entry.get("authors") or [],
        "keywords": [],
        "primary_area": meta.get("citation_journal_title") or VENUE_NAME,
        "pdf_url": entry.get("pdf_url"),
        "paper_url": entry.get("abs_url"),
        "doi": None,
        "arxiv_id": None,
        "submission_year": year,
        "publication_year": year,
        "status": STATUS,
        "decision": None,
        "created_at_source": None,
        "updated_at_source": f"{year}-01-01",
        "raw_data_path": None,
        # Extra fields kept out of the DB contract but useful in reports.
        "_volume": volume,
        "_issue": number,
        "_pages": pages,
        "_issn": meta.get("citation_issn"),
    }


def sync(year: int, *, force: bool = False, limit: int | None = None,
         workers: int | None = None, log=print) -> dict[str, Any]:
    """Import one JMLR year into the corpus.

    Steps: discover volumes, read each cached volume page, keep the entries
    whose own year matches, fetch each paper page for the abstract, then
    upsert. Re-running is idempotent (paper id = ``jmlr:<jmlr-id>``).
    """
    if year not in YEARS:
        raise ValueError(f"year {year} is outside the supported range {YEARS}")
    workers = workers or DEFAULT_WORKERS
    volumes = discover_volumes(force=force, log=log)
    entries: list[dict[str, Any]] = []
    for volume in volumes:
        volume_papers = volume_entries(volume, force=force, log=log)
        entries.extend(p for p in volume_papers if p["year"] == year)
    log(f"[jmlr] {year}: {len(entries)} entries across {len(volumes)} volumes")
    if limit:
        entries = entries[:limit]

    started = db.now()
    db.init_db()
    with db.connect() as con:
        # 2026 has no volume yet: the venue row records that honestly instead of
        # pretending the year is missing.
        has_papers = any(e["year"] == year for e in entries)
        db.put_venue(con, venue_id=VENUE_ID, name=VENUE_NAME, year=year,
                     provider=PROVIDER, source_venue_id="jmlr.org/papers",
                     kind="journal", source_url="https://jmlr.org/papers/",
                     availability="available" if has_papers else "not_published")
        con.commit()

    def _detail(entry: dict[str, Any]) -> dict[str, Any]:
        return fetch_paper(entry["abs_url"])

    details: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for detail in pool.map(_detail, entries):
            details.append(detail)

    stats = {"year": year, "fetched": len(entries), "inserted": 0, "updated": 0,
             "with_abstract": 0, "no_abstract": 0}
    with db.connect() as con:
        for entry, detail in zip(entries, details):
            paper = _to_paper(entry, detail)
            if paper["abstract"]:
                stats["with_abstract"] += 1
            else:
                stats["no_abstract"] += 1
            exists = con.execute("SELECT id FROM papers WHERE id=?", (paper["id"],)).fetchone()
            db.upsert_paper(con, paper, VENUE_ID, year, relation_type=RELATION_TYPE)
            stats["updated" if exists else "inserted"] += 1
        con.commit()
    stats["seconds"] = round(time.time() - started, 1) if False else None
    log(f"[jmlr] {year}: {stats}")
    return stats


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import JMLR papers from jmlr.org")
    parser.add_argument("--year", type=int, action="append", help="year(s) to import")
    parser.add_argument("--force", action="store_true", help="refetch cached volume pages")
    parser.add_argument("--limit", type=int, help="cap papers per year (smoke test)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    years = args.year or list(YEARS)
    for year in years:
        sync(year, force=args.force, limit=args.limit)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
