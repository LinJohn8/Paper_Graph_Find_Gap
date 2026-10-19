"""TMLR ingest adapter backed by the journal's official index on jmlr.org.

Why this source
---------------
TMLR is a rolling journal hosted on OpenReview, but OpenReview cannot be used
from this host: ``api2.openreview.net`` answers 403 "Challenge verification
required" for every note query, and even the human-facing
``openreview.net/forum`` pages are behind a Cloudflare Turnstile check (confirmed
from this network: HTTP 200 HTML with ``<title>Verifying your browser</title>``).
DBLP and Semantic Scholar are blocked as well.

The publisher's own index is open, complete and authoritative:
``https://jmlr.org/tmlr/papers/`` -- one static page (about 3.5 MB) that lists
every accepted TMLR paper since the journal launched in 2022, newest first.

What the page exposes
---------------------
Each paper is one ``<li class="item ...">`` in a
``<ul class="list-papers">``::

    <li class="item nocertificate ">
      <h4><a class="paper-data-bs-title darkblue"
             href="https://openreview.net/pdf?id=aUQFMWmW0v"><b>TITLE</b></a></h4>
      <p><i>AUTHOR, AUTHOR</i>, October 2026  <br>
          [<a href="https://openreview.net/forum?id=aUQFMWmW0v">openreview</a>] ...
      </p>
    </li>

So title, complete author list, the publication month/year and the OpenReview
forum id (the stable id used here) are all present. Abstracts are *not* on the
index, there is no per-paper HTML page (``/tmlr/papers/<id>.html`` is a 404), the
per-paper bib file carries only BibTeX metadata, and the OpenReview forum is
Turnstile-gated -- so the listing is imported without source abstracts. As a
best-effort fallback closed abstracts are filled from the local corpus only on
an exact normalised-title + author-surname match (see ``_corpus_abstracts``).

Year assignment
---------------
TMLR is rolling, so there is no per-year page. The year is taken from each
item's own "Month Year" metadata (the month in which the paper was published /
accepted), never guessed. On 2026-10-19 the page held 4,915 items:
2022: 216, 2023: 611, 2024: 955, 2025: 1,432, 2026: 1,701.

Usage
-----
    from paper_ingest.tmlr import sync
    sync(2024)                        # one year, cached page
    sync(2024, force=True)            # refetch the index first

The runner lives in ``_run_tmlr.py`` at the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import httpx

from . import db

#: Authoritative single-page index for every TMLR year.
INDEX_URL = "https://jmlr.org/tmlr/papers/"

#: Rolling journal: every year the index is expected to know about.
YEARS = (2022, 2023, 2024, 2025, 2026)

#: Registry / provider labels used for this import (never "openreview").
VENUE_ID = "tmlr"
VENUE_NAME = "TMLR"
PROVIDER = "tmlr_jmlr"
SOURCE = "TMLR"
RELATION_TYPE = "publication"
STATUS = "published"
ID_SEP = "_"

#: Cache directory mandated by the task: Data/raw/tmlr/...
RAW_DIR = db.DATA_ROOT / "raw" / "tmlr"
INDEX_CACHE = RAW_DIR / "papers_index.html"
INDEX_META = RAW_DIR / "papers_index.meta.json"

#: jmlr.org sits behind a CDN that closes keep-alive connections; keep the
#: request count tiny and the pool per-request. Concurrency is capped at 6
#: wherever it is used so a fan-out can never hammer the publisher.
DEFAULT_WORKERS = min(6, max(1, int(os.environ.get("PAPER_HTTP_WORKERS", "6") or 6)))

#: A page this large is the index; anything much smaller is an error/block page.
MIN_INDEX_BYTES = 1_000_000

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

#: One <li> = one paper. The class list varies with certifications
#: ("item featured j2c event"), so only the stable ``item`` token is matched.
_ITEM_RE = re.compile(r'<li class="item[^"]*">(.*?)</li>', re.S)
_TITLE_RE = re.compile(r'<h4><a[^>]*href="([^"]+)"[^>]*><b>(.*?)</b></a></h4>', re.S)
_META_RE = re.compile(r"<p><i>(.*?)</i>,\s*([A-Z][a-z]+)\s+(\d{4})")
_FORUM_RE = re.compile(r"https://openreview\.net/forum\?id=([A-Za-z0-9_\-]+)")

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

_STOPWORDS = frozenset(
    "a an the of on in for and with to via from by at is are as using towards toward".split()
)


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------
def _new_client() -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=90.0, follow_redirects=True)


def fetch(*, force: bool = False, url: str = INDEX_URL, retries: int = 5,
          log=print) -> Path:
    """Return the path of the cached TMLR index page, downloading it if needed.

    A cached page is reused unless ``force`` is set, so repeated per-year runs
    and re-runs cost no network traffic. Every attempt uses a **fresh**
    ``httpx.Client``: a dropped keep-alive connection (``RemoteProtocolError``,
    a ``TransportError``) poisons a pooled client, and only a new client makes
    the retry useful. The body is written atomically next to a small metadata
    sidecar (url, retrieved_at, status, bytes, sha256) so a truncated download
    can never be mistaken for a complete index.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if INDEX_CACHE.exists() and not force:
        size = INDEX_CACHE.stat().st_size
        if size >= MIN_INDEX_BYTES:
            log(f"[tmlr] index cache hit: {INDEX_CACHE} ({size:,} bytes)")
            return INDEX_CACHE
        log(f"[tmlr] cached index too small ({size:,} bytes); refetching")

    last_error: Exception | None = None
    for attempt in range(retries):
        client = _new_client()
        try:
            response = client.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request,
                    response=response,
                )
            response.raise_for_status()
            # jmlr.org is UTF-8 (the page declares it in a <meta> tag but not
            # always in the Content-Type header).
            raw = response.content
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                response.encoding = "utf-8"
                text = response.text
            if len(raw) < MIN_INDEX_BYTES:
                raise httpx.TransportError(
                    f"index body too small ({len(raw):,} bytes); not assuming a complete page"
                )
            if "openreview.net/forum" not in text:
                raise httpx.TransportError("index body has no OpenReview forum links")
            tmp = INDEX_CACHE.with_suffix(".html.tmp")
            # Write the response bytes verbatim: Path.write_text would translate
            # LF to CRLF on Windows and inflate/pollute the cached snapshot.
            tmp.write_bytes(raw)
            tmp.replace(INDEX_CACHE)
            meta = {
                "url": url,
                "status": response.status_code,
                "bytes": len(raw),
                "content_type": response.headers.get("content-type"),
                "retrieved_at": db.now(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            INDEX_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            log(f"[tmlr] fetched {url} -> {INDEX_CACHE} ({meta['bytes']:,} bytes)")
            return INDEX_CACHE
        except (httpx.TransportError, httpx.HTTPStatusError, OSError) as exc:
            last_error = exc
            wait = min(2 ** attempt, 30)
            log(f"[tmlr] fetch attempt {attempt + 1}/{retries} failed: "
                f"{type(exc).__name__}: {exc}; retrying in {wait}s with a fresh client")
            time.sleep(wait)
        finally:
            client.close()
    raise RuntimeError(f"could not fetch {url} after {retries} attempts: {last_error}")


def fetch_many(urls: Iterable[str], *, workers: int = DEFAULT_WORKERS,
               retries: int = 3) -> dict[str, str | None]:
    """Bounded, polite parallel GET helper (max 6 workers).

    Only used for optional diagnostics / future per-paper enrichment: the index
    itself is a single request, so normal synchronization never fans out.
    """
    urls = list(urls)
    cap = max(1, min(int(workers), 6))
    out: dict[str, str | None] = {}

    def one(target: str) -> tuple[str, str | None]:
        for attempt in range(retries):
            client = _new_client()
            try:
                response = client.get(target)
                response.encoding = "utf-8"
                if response.status_code == 200:
                    return target, response.text
            except (httpx.TransportError, httpx.HTTPStatusError):
                pass
            finally:
                client.close()
            time.sleep(min(2 ** attempt, 10))
        return target, None

    with ThreadPoolExecutor(max_workers=cap) as pool:
        for target, body in pool.map(one, urls):
            out[target] = body
    return out


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------
def _clean(fragment: str) -> str:
    """Strip inline markup/entities from a title or author fragment."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse(text: str, *, raw_path: Path | None = None, log=print) -> list[dict[str, Any]]:
    """Parse the TMLR index page into one record per paper.

    Every record carries: OpenReview ``forum`` (stable id), ``title``,
    ``authors`` (list), ``year``/``month`` (from the item's own metadata),
    ``paper_url``/``pdf_url``. Records whose required pieces are missing are
    dropped and counted in the log; a layout change therefore shows up as a
    nonzero ``dropped`` count instead of silent data loss.
    """
    records: list[dict[str, Any]] = []
    dropped = 0
    for block in _ITEM_RE.findall(text):
        title_match = _TITLE_RE.search(block)
        meta_match = _META_RE.search(block)
        forum_match = _FORUM_RE.search(block)
        if not (title_match and meta_match and forum_match):
            dropped += 1
            continue
        title = _clean(title_match.group(2))
        authors = [a.strip() for a in _clean(meta_match.group(1)).split(",") if a.strip()]
        month_name = meta_match.group(2)
        if not title or month_name not in _MONTHS or not authors:
            dropped += 1
            continue
        forum = forum_match.group(1)
        year = int(meta_match.group(3))
        records.append({
            "forum": forum,
            "title": title,
            "authors": authors,
            "month": _MONTHS[month_name],
            "month_name": month_name,
            "year": year,
            "paper_url": f"https://openreview.net/forum?id={forum}",
            "pdf_url": f"https://openreview.net/pdf?id={forum}",
            "raw_data_path": str(raw_path) if raw_path else None,
        })
    if not records:
        raise RuntimeError("TMLR index parsed zero records; refusing to treat as empty")
    # Parse is deterministic and ids are the forum id; guard against a page that
    # repeats an item (which would silently inflate counts).
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        if record["forum"] in seen:
            dropped += 1
            continue
        seen.add(record["forum"])
        unique.append(record)
    if dropped:
        log(f"[tmlr] WARNING: dropped {dropped} malformed/duplicate item blocks")
    return unique


# ---------------------------------------------------------------------------
# abstract fallback (local corpus, exact match only)
# ---------------------------------------------------------------------------
def _norm_title(value: str) -> str:
    value = html.unescape(value or "").lower()
    value = re.sub(r"\\[a-z]+", " ", value)      # LaTeX commands such as $\pi$
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _surnames(value: Any) -> set[str]:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    out: set[str] = set()
    for part in re.split(r",| and ", str(value or "")):
        tokens = [t for t in re.findall(r"[a-z]+", part.lower())
                  if t not in _STOPWORDS and len(t) > 1]
        if tokens:
            out.add(tokens[-1])
    return out


def _corpus_abstracts(con, log=print) -> dict[str, tuple[str, set[str]]]:
    """Best-effort abstract lookup over the local corpus, exact title only.

    The TMLR listing has no abstracts and no reachable per-paper abstract page,
    so this recovers the ones already held by another source for the *same*
    paper. A hit requires an identical normalised title AND at least one shared
    author surname; anything weaker is ignored rather than guessed. Measured
    coverage on 2026-10-19: 68 / 4915 (1.4%).
    """
    index: dict[str, tuple[str, set[str]]] = {}
    rows = con.execute(
        "SELECT title, abstract, authors_json FROM papers "
        "WHERE abstract IS NOT NULL AND length(abstract) > 80"
    )
    for row in rows:
        key = _norm_title(row["title"])
        if len(key) < 20:
            continue
        index.setdefault(key, (row["abstract"], _surnames(row["authors_json"])))
    log(f"[tmlr] local-corpus abstract index: {len(index):,} titles")
    return index


def enrich_abstracts(records: list[dict[str, Any]], *, log=print) -> int:
    """Fill ``abstract`` on records that match the local corpus exactly."""
    with db.connect() as con:
        index = _corpus_abstracts(con, log=log)
    matched = 0
    for record in records:
        hit = index.get(_norm_title(record["title"]))
        if not hit:
            continue
        abstract, surnames = hit
        if _surnames(record["authors"]) & surnames:
            record["abstract"] = abstract
            matched += 1
    log(f"[tmlr] abstracts recovered from local corpus: {matched:,}/{len(records):,}")
    return matched


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def collect(*, years: Iterable[int] = YEARS, force: bool = False,
            enrich: bool = True, log=print) -> list[dict[str, Any]]:
    """Fetch (cached) + parse the index and return records for ``years``."""
    path = fetch(force=force, log=log)
    text = path.read_text(encoding="utf-8")
    records = parse(text, raw_path=path, log=log)
    wanted = set(years)
    selected = [r for r in records if r["year"] in wanted]
    log(f"[tmlr] parsed {len(records):,} papers from index; "
        f"selected {len(selected):,} for years {sorted(wanted)}")
    if enrich and selected:
        enrich_abstracts(selected, log=log)
    return selected


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def _paper_dict(record: dict[str, Any]) -> dict[str, Any]:
    """Map one parsed record to the ``papers`` row shape."""
    forum = record["forum"]
    return {
        "id": f"{VENUE_ID}{ID_SEP}{forum}",
        "source": SOURCE,
        "source_paper_id": forum,
        "forum_id": forum,
        "title": record["title"],
        "abstract": record.get("abstract"),
        "authors": record["authors"],
        "keywords": [],
        "primary_area": None,
        "pdf_url": record["pdf_url"],
        "paper_url": record["paper_url"],
        "doi": None,
        "arxiv_id": None,
        "submission_year": None,
        "publication_year": record["year"],
        "status": STATUS,
        "decision": None,
        "created_at_source": None,
        "updated_at_source": None,
        "raw_data_path": record.get("raw_data_path"),
    }


def _is_unchanged(paper: dict[str, Any], row) -> bool:
    return (
        paper["title"] == row["title"]
        and (paper.get("abstract") or None) == (row["abstract"] or None)
        and paper["pdf_url"] == row["pdf_url"]
        and paper["forum_id"] == row["forum_id"]
        and paper["publication_year"] == row["publication_year"]
        and paper["status"] == row["status"]
    )


def sync(year: int, *, force: bool = False, limit: int | None = None,
         dry_run: bool = False, enrich: bool = True, log=print) -> dict[str, Any]:
    """Import one TMLR year idempotently and record a ``sync_jobs`` row.

    The upsert is keyed on ``tmlr_<forum_id>`` (OpenReview forum id), so
    ``paper_venues`` gains exactly one row per paper and re-runs are no-ops.
    """
    if year not in YEARS:
        raise ValueError(f"unexpected TMLR year {year!r}; expected one of {YEARS}")
    started = db.now()
    counts = {"fetched_count": 0, "inserted_count": 0, "updated_count": 0,
              "skipped_count": 0, "error_count": 0}
    error: str | None = None
    status = "failed"
    job_id = None
    records: list[dict[str, Any]] = []
    with_abstract = 0
    db.init_db()

    if not dry_run:
        with db.connect() as con:
            job_id = con.execute(
                "INSERT INTO sync_jobs(venue,year,provider,status,started_at) "
                "VALUES(?,?,?,?,?)",
                (VENUE_ID, year, PROVIDER, "running", started),
            ).lastrowid

    try:
        records = collect(years=(year,), force=force, enrich=enrich, log=log)
        if limit:
            records = records[:limit]
        if not records:
            raise RuntimeError(f"no TMLR papers parsed for {year}; index layout may have changed")
        with_abstract = sum(1 for r in records if r.get("abstract"))

        con = None
        if not dry_run:
            con = db.connect()
            db.put_venue(
                con, venue_id=VENUE_ID, name=VENUE_NAME, year=year,
                provider=PROVIDER, source_venue_id=INDEX_URL,
                kind="rolling_review", source_url=INDEX_URL,
                availability="available",
            )
        try:
            for position, record in enumerate(records):
                paper = _paper_dict(record)
                counts["fetched_count"] += 1
                if dry_run:
                    continue
                row = con.execute(
                    "SELECT title,abstract,pdf_url,forum_id,publication_year,status "
                    "FROM papers WHERE id=?", (paper["id"],),
                ).fetchone()
                if row is not None and _is_unchanged(paper, row):
                    con.execute("UPDATE papers SET last_seen_at=? WHERE id=?",
                                (db.now(), paper["id"]))
                    counts["skipped_count"] += 1
                else:
                    db.upsert_paper(con, paper, VENUE_ID, year,
                                    relation_type=RELATION_TYPE)
                    counts["updated_count" if row is not None else "inserted_count"] += 1
                # Batch commits: one transaction for ~5k rows would hold the
                # SQLite write lock for minutes and starve concurrent importers.
                if position % 200 == 199:
                    con.commit()
            if con is not None:
                con.commit()
        finally:
            if con is not None:
                con.close()
        status = "partial" if limit else "success"
    except Exception as exc:  # noqa: BLE001 - surfaced in the job row and return value
        error = f"{type(exc).__name__}: {exc}"
        counts["error_count"] += 1
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
            status = "restricted"
        log(f"[tmlr] {year} FAILED: {error}")
    finally:
        if not dry_run:
            with db.connect() as con:
                if not con.execute("SELECT 1 FROM venues WHERE id=? AND year=?",
                                   (VENUE_ID, year)).fetchone():
                    db.put_venue(
                        con, venue_id=VENUE_ID, name=VENUE_NAME, year=year,
                        provider=PROVIDER, source_venue_id=INDEX_URL,
                        kind="rolling_review", source_url=INDEX_URL,
                        availability=status,
                    )
                con.execute(
                    "UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,"
                    "inserted_count=?,updated_count=?,skipped_count=?,error_count=?,"
                    "error_message=? WHERE id=?",
                    (status, db.now(), *counts.values(), error, job_id),
                )
                if status in ("success", "partial"):
                    con.execute(
                        "UPDATE venues SET last_sync_at=?,availability_status='available' "
                        "WHERE id=? AND year=?", (db.now(), VENUE_ID, year))

    result = {"venue": VENUE_ID, "year": year, "provider": PROVIDER,
              "status": status, **counts, "error": error,
              "with_abstract": with_abstract, "abstract_coverage": (
                  round(with_abstract / len(records), 4) if records else 0.0)}
    if counts["fetched_count"]:
        log(f"[tmlr] {year}: {status} fetched={counts['fetched_count']} "
            f"ins={counts['inserted_count']} upd={counts['updated_count']} "
            f"skip={counts['skipped_count']} abstracts={with_abstract} "
            f"({result['abstract_coverage']:.1%})")
    return result


def sync_all(*, years: Iterable[int] = YEARS, force: bool = False,
             enrich: bool = True, log=print) -> list[dict[str, Any]]:
    """Import every year sequentially, sharing one cached index fetch."""
    return [sync(year, force=force, enrich=enrich, log=log) for year in years]


# ---------------------------------------------------------------------------
# abstract availability diagnostics
# ---------------------------------------------------------------------------
def probe_abstract_source(sample: int = 3) -> dict[str, Any]:
    """Check whether a per-paper abstract page is reachable (it currently is not).

    Documents *why* abstracts are absent instead of silently leaving them out:
    the OpenReview forum/PDF/API endpoints and the guessed jmlr per-paper page
    are probed once. Bounded to ``sample`` => at most 3 requests, sequential.
    """
    forum = "aUQFMWmW0v"
    candidates = [
        f"https://openreview.net/forum?id={forum}",
        f"https://openreview.net/pdf?id={forum}",
        "https://api2.openreview.net/notes?forum=" + forum,
        "https://jmlr.org/tmlr/papers/" + forum + ".html",
    ][:max(1, min(sample, 4))]
    report = {}
    for url in candidates:
        try:
            client = _new_client()
            try:
                response = client.get(url)
                body = response.text
            finally:
                client.close()
            challenge = "Verifying your browser" in body or "cf-turnstile" in body
            report[url] = {
                "status": response.status_code,
                "bytes": len(response.content),
                "challenge": challenge,
                "content_type": response.headers.get("content-type"),
            }
        except Exception as exc:  # noqa: BLE001
            report[url] = {"error": f"{type(exc).__name__}: {exc}"}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import TMLR papers from jmlr.org")
    parser.add_argument("--years", default=",".join(str(y) for y in YEARS),
                        help="comma-separated years (default: 2022..2026)")
    parser.add_argument("--force", action="store_true", help="refetch the index page")
    parser.add_argument("--no-abstracts", action="store_true",
                        help="skip the local-corpus abstract fallback")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-abstracts", action="store_true",
                        help="report reachability of per-paper abstract pages")
    args = parser.parse_args(argv)
    years = [int(y) for y in args.years.split(",") if y.strip()]
    if args.probe_abstracts:
        print(json.dumps(probe_abstract_source(), indent=2))
    results = [sync(year, force=args.force, limit=args.limit,
                    dry_run=args.dry_run, enrich=not args.no_abstracts)
               for year in years]
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if all(r["status"] in ("success", "partial") for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
