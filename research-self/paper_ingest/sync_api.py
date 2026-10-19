"""Persist multi-source API harvests into the local database, incrementally.

This is the bridge between the official-API providers (`provider_chain`) and the
existing SQLite schema. It mirrors `official.sync_official`'s bookkeeping (venue
row, sync_jobs row, insert/update/skip counters) so the rest of the app -- and the
existing UI -- see a normal sync, while the data now comes from machine-readable
APIs instead of HTML scraping.

Incremental semantics
---------------------
A full re-harvest of a venue/year is expensive (arXiv alone needs ~3s per
request). Re-syncing must therefore be cheap and safe:

* A paper already present with an *identical* content fingerprint is counted as
  ``skipped`` and not rewritten (no churn in ``updated_at_source``, no needless
  graph rebuilds).
* A paper whose content changed (title/abstract/authors/keywords/doi/pdf) is
  updated and counted as ``updated``.
* A genuinely new paper is inserted and counted as ``inserted``.

``--since`` support: ``db.upsert_paper`` records ``last_seen_at``; we also look at
``papers.updated_at_source``. For an incremental run we can restrict *which* DB
rows are considered for update, but the upstream APIs cannot reliably filter by
"changed since X", so the practical optimisation is to cap how many records we
fetch (``--limit``) and to skip unchanged ones cheaply. What actually makes
incremental runs fast is (a) not rewriting unchanged rows and (b) not rebuilding
the graph unless something changed -- both implemented here.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from . import db
from .provider_chain import harvest_venue

#: Display names used for the `venues` table.
NAMES = {
    "icml": "ICML",
    "neurips": "NeurIPS",
    "iclr": "ICLR",
    "cvpr": "CVPR",
    "acl": "ACL",
    "tmlr": "TMLR",
}

#: Provider label recorded on the venue row: this is a multi-source harvest,
#: not any single upstream, so report it as such.
PROVIDER_LABEL = "api-multi"

#: Fields that, when changed, mean the stored record is stale.
_CONTENT_FIELDS = ("title", "abstract", "authors", "keywords", "doi", "pdf_url",
                   "primary_area", "status", "decision", "publication_year")


def _fingerprint(paper: dict[str, Any]) -> str:
    """Stable hash of the fields we care about, for cheap change detection."""
    payload = {
        "title": (paper.get("title") or "").strip(),
        "abstract": (paper.get("abstract") or "").strip(),
        "authors": list(paper.get("authors") or []),
        "keywords": list(paper.get("keywords") or []),
        "doi": (paper.get("doi") or "").strip().lower(),
        "pdf_url": paper.get("pdf_url"),
        "primary_area": paper.get("primary_area"),
        "status": paper.get("status"),
        "decision": paper.get("decision"),
        "publication_year": paper.get("publication_year"),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _row_fingerprint(row: Any) -> str:
    """Fingerprint an existing DB row using the same field set as a harvest."""
    def _json_list(value: Any) -> list[Any]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []

    paper = {
        "title": row["title"],
        "abstract": row["abstract"],
        "authors": _json_list(row["authors_json"]),
        "keywords": _json_list(row["keywords_json"]),
        "doi": row["doi"],
        "pdf_url": row["pdf_url"],
        "primary_area": row["primary_area"],
        "status": row["status"],
        "decision": row["decision"],
        "publication_year": row["publication_year"],
    }
    return _fingerprint(paper)


#: Non-paper records that proceedings metadata includes: front matter, indexes and
#: committee lists are deposited with titles and DOIs like real articles, so they
#: otherwise enter the corpus and pollute topic labels and search results.
_JUNK_TITLES = frozenset(
    {
        "tableofcontents", "organizingcommittee", "authorindex", "subjectindex",
        "preface", "frontmatter", "committees", "index", "editorial",
        "reviewers", "programcommittee", "conferenceprogram", "sponsors",
        "welcome", "foreword", "proceedingsof", "miscellaneous",
    }
)


def _is_junk(paper: dict[str, Any]) -> bool:
    """True for front-matter / index / committee rows masquerading as papers."""
    title = (paper.get("title") or "").strip().lower()
    key = re.sub(r"[^a-z0-9]+", "", title)
    if not key:
        return True
    if key in _JUNK_TITLES:
        return True
    # "Table of Contents" style headings with a year/number suffix.
    if re.fullmatch(r"(tableofcontents|organizingcommittee|authorindex|preface)\d*", key):
        return True
    return False


def _find_existing(con: Any, paper: dict[str, Any]) -> Any | None:
    """Locate an already-stored version of this paper.

    Tries, in order: exact id, same (source, source_paper_id), same DOI. The DOI
    check is what lets a paper first harvested from arXiv later be recognised when
    it appears in OpenAlex under a different id -- without it, every source switch
    would duplicate the corpus.
    """
    row = con.execute("SELECT * FROM papers WHERE id=?", (paper["id"],)).fetchone()
    if row is not None:
        return row
    row = con.execute(
        "SELECT * FROM papers WHERE source=? AND source_paper_id=?",
        (paper.get("source"), paper.get("source_paper_id")),
    ).fetchone()
    if row is not None:
        return row
    doi = (paper.get("doi") or "").strip()
    if doi:
        for candidate in {doi, doi.lower(), doi.replace("https://doi.org/", "")}:
            row = con.execute(
                "SELECT * FROM papers WHERE lower(doi)=lower(?)", (candidate,)
            ).fetchone()
            if row is not None:
                return row
    return None


def sync_api(venue: str, year: int, *, limit: int | None = None,
             target: int | None = None, dry_run: bool = False,
             mailto: str | None = None, api_key: str | None = None,
             arxiv_delay: float = 3.0) -> dict[str, Any]:
    """Harvest ``venue``/``year`` from official APIs and upsert into the DB.

    Incremental by nature: unchanged records are skipped, changed ones updated,
    new ones inserted. Returns a result dict shaped like the other sync functions
    so callers (CLI, API) do not need special cases. ``providers`` carries the
    per-source diagnostics -- when a source is blocked or quota-limited this is
    where you can see it, instead of silently getting an empty corpus.
    """
    started = datetime.now(timezone.utc).isoformat()
    counters = {
        "fetched_count": 0,
        "inserted_count": 0,
        "updated_count": 0,
        "skipped_count": 0,
        "error_count": 0,
    }
    name = NAMES.get(venue, venue.upper())

    job_id = None
    if not dry_run:
        db.init_db()
        with db.connect() as con:
            job_id = con.execute(
                "INSERT INTO sync_jobs(venue,year,provider,status,started_at) VALUES(?,?,?,?,?)",
                (venue, year, PROVIDER_LABEL, "running", started),
            ).lastrowid

    # Start as "no_data" rather than "failed": an empty harvest is a legitimate
    # outcome (no coverage yet / all sources rate-limited) and must not be
    # reported as a crash. The exception path overwrites this to "failed".
    status = "no_data"
    error: str | None = None
    result: dict[str, Any] = {}
    try:
        result = harvest_venue(
            venue, year, limit=limit, target=target,
            mailto=mailto, api_key=api_key, arxiv_delay=arxiv_delay,
        )
        papers = result.papers
        counters["fetched_count"] = len(papers)
        # Drop front-matter / index / committee rows before they reach the DB.
        # Proceedings deposits include these as if they were articles ("Table of
        # Contents", "Organizing Committee"), and they were visibly polluting the
        # corpus (duplicate title groups, meaningless topic labels).
        filtered = [p for p in papers if not _is_junk(p)]
        if len(filtered) != len(papers):
            counters["skipped_count"] += len(papers) - len(filtered)
            papers = filtered
        if not papers:
            # Explain *why* nothing came back (blocked source, no coverage, quota)
            # instead of leaving a bare empty result. `attempts` is optional so a
            # provider chain that reports differently cannot crash the sync.
            attempts = getattr(result, "attempts", None) or []
            error = "; ".join(
                f"{getattr(a, 'provider', '?')}:{getattr(a, 'status', '?')}"
                + (f"({getattr(a, 'message', None)})" if getattr(a, "message", None) else "")
                for a in attempts
            ) or "no providers returned papers"

        # One connection for the whole batch. Opening a fresh SQLite handle per
        # paper was correct but pathological at real corpus sizes: a 4000-paper
        # year meant 4000 connect/close cycles (plus WAL setup each time), which
        # dominated the run. The venue row is also written once, not per paper.
        if papers and not dry_run:
            db.init_db()
            with db.connect() as con:
                db.put_venue(
                    con, venue_id=venue, name=name, year=year,
                    provider=PROVIDER_LABEL,
                    source_venue_id=papers[0].get("source"),
                    kind="conference",
                    source_url=papers[0].get("paper_url"),
                    availability="available",
                )
                for paper in papers:
                    existing = _find_existing(con, paper)
                    if existing is not None:
                        if _row_fingerprint(existing) == _fingerprint(paper):
                            # Unchanged: touch last_seen_at only, keep row stable.
                            con.execute(
                                "UPDATE papers SET last_seen_at=? WHERE id=?",
                                (db.now(), existing["id"]),
                            )
                            counters["skipped_count"] += 1
                            continue
                        # Same paper, changed metadata: keep the original id so any
                        # graph_points/paper_relations rows stay attached.
                        paper = dict(paper)
                        paper["id"] = existing["id"]
                        paper["source"] = existing["source"]
                        paper["source_paper_id"] = existing["source_paper_id"]
                    db.upsert_paper(con, paper, venue, year, relation_type="publication")
                    counters["updated_count" if existing is not None else "inserted_count"] += 1
                con.commit()

        if papers:
            status = "partial" if limit is not None and len(papers) >= limit else "success"
        elif status != "no_data":
            # Defensive: harvest returned nothing but did not classify itself.
            status = "no_data"
    except Exception as exc:  # noqa: BLE001 - record any failure in the job row
        error = f"{type(exc).__name__}: {exc}"
        counters["error_count"] += 1
        status = "failed"
    finally:
        if not dry_run:
            with db.connect() as con:
                if not con.execute(
                    "SELECT 1 FROM venues WHERE id=? AND year=?", (venue, year)
                ).fetchone():
                    db.put_venue(
                        con, venue_id=venue, name=name, year=year,
                        provider=PROVIDER_LABEL, source_venue_id=None,
                        kind="conference", source_url=None, availability=status,
                    )
                con.execute(
                    "UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,"
                    "inserted_count=?,updated_count=?,skipped_count=?,error_count=?,"
                    "error_message=? WHERE id=?",
                    (status, db.now(), *counters.values(), error, job_id),
                )

    summary = result.summary() if result else {}
    return {
        "venue": venue,
        "year": year,
        "provider": PROVIDER_LABEL,
        "status": status,
        **counters,
        "changed": counters["inserted_count"] + counters["updated_count"],
        "error": error,
        "providers": summary.get("providers", []),
        "with_abstract": summary.get("with_abstract", 0),
    }
