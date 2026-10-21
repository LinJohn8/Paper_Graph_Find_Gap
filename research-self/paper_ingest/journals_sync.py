"""Writing harvested journal papers into the corpus.

WHY THIS MODULE EXISTS
-----------------------
Collecting the records and *storing* them were two different pieces of code:
the provider returned paper dicts, and a script in the repository root turned
them into rows. The script could not be imported, could not be tested, and
every caller had to know its calling convention.

This module owns the write path for journals:

* it upserts through :func:`paper_ingest.db.upsert_paper`, so the FTS mirror
  and the ``paper_venues`` link are maintained the same way as for every
  other source;
* it records a ``sync_jobs`` row per venue-year, so a failed journal-year is
  visible in the status list instead of being lost;
* it reuses an existing paper when a different source already delivered the
  same title in that venue-year (the same cross-source dedup rule the
  official harvester uses), so a journal paper that also exists on arXiv is
  stored once;
* it can then fill the missing abstracts through
  :mod:`paper_ingest.journal_abstracts`.

The result is one call -- :func:`import_journal_year` -- that any caller (the
CLI, a test, a notebook) can use without knowing the internals.
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import db
from .journal_abstracts import backfill_journal_abstracts
from .provider_journals import JOURNAL_SOURCES, JournalProvider

__all__ = ["import_journal_year", "normalize_title_key"]

#: How many papers are written before the transaction is committed. A journal
#: year is a few thousand rows; committing in batches keeps the write lock
#: short enough that the API stays responsive during a harvest.
COMMIT_BATCH = 200


def normalize_title_key(title: str | None) -> str:
    """The comparison key for cross-source title dedup.

    Same rule as the official harvester: lowercased, punctuation removed, and
    only trusted above 20 characters (shorter titles collide too easily).
    """
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def _read_watermark(con, venue_key: str, year: int) -> str | None:
    """The date this venue-year was last harvested, as ``YYYY-MM-DD``.

    The watermark is what makes an incremental run cheap: OpenAlex can be
    asked only for the works it indexed after that date, so a re-run over a
    settled year fetches a handful of records instead of thousands.
    """
    row = con.execute(
        "SELECT checkpoint FROM sync_state WHERE venue=? AND year=?",
        (venue_key, year)).fetchone()
    if row is None or not row["checkpoint"]:
        return None
    return str(row["checkpoint"])[:10] or None


def _write_watermark(con, venue_key: str, year: int, when: str) -> None:
    """Record the harvest date so the next run can start from it."""
    con.execute(
        "INSERT OR REPLACE INTO sync_state(venue,year,cursor,checkpoint,updated_at) "
        "VALUES(?,?,?,?,?)",
        (venue_key, year, None, when[:10], db.now()))


def import_journal_year(
    venue_key: str,
    year: int,
    *,
    limit: int | None = None,
    backfill: bool = True,
    mailto: str | None = None,
    api_key: str | None = None,
    workers: int = 2,
    epmc_workers: int = 8,
    since: str | None = None,
    incremental: bool = False,
) -> dict[str, Any]:
    """Harvest one journal-year and write it into the corpus.

    Returns a report dict: the counts, where the abstracts came from, and how
    long it took. A failure is recorded on the ``sync_jobs`` row and reported
    in the result (``status: "failed"``) rather than raised -- a harvest that
    cannot reach one journal must not abort a plan that covers fifteen.

    ``incremental=True`` reuses the stored watermark as the starting point and
    records a new one, so a settled year costs one request instead of fifty.
    ``since`` overrides the watermark explicitly.
    """
    if venue_key not in JOURNAL_SOURCES:
        raise ValueError(
            f"unknown journal {venue_key!r}; known: {', '.join(sorted(JOURNAL_SOURCES))}")
    source_id, journal_name = JOURNAL_SOURCES[venue_key]

    started = time.time()
    counts: dict[str, Any] = {
        "venue": venue_key, "year": year, "status": "success",
        "fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0, "no_abstract": 0,
        "recovered": 0, "error": None, "since": None,
    }
    provider = JournalProvider(venue_key, mailto=mailto)
    if api_key:
        provider.http.api_key = api_key

    job_id = None
    with db.connect() as con:
        job_id = con.execute(
            "INSERT INTO sync_jobs(venue,year,provider,status,started_at) VALUES(?,?,?,?,?)",
            (venue_key, year, "journal_openalex", "running", db.now()),
        ).lastrowid
        db.put_venue(
            con, venue_id=venue_key, name=journal_name, year=year,
            provider="journal_openalex", source_venue_id=f"openalex:{source_id}",
            kind="journal", source_url=f"https://api.openalex.org/sources/{source_id}",
            availability="available",
        )
        con.commit()

    try:
        with db.connect() as con:
            # Cross-source dedup: a paper this venue-year already has (from
            # arXiv, from the official harvester) is updated, not duplicated.
            existing: dict[str, str] = {}
            for row in con.execute(
                "SELECT p.id, p.title FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id "
                "WHERE pv.venue_id=? AND pv.year=?", (venue_key, year),
            ):
                key = normalize_title_key(row["title"])
                if len(key) >= 20 and key not in existing:
                    existing[key] = row["id"]
            # The records already stored, with the abstract they carry, so a
            # record that has not changed is not written again.
            stored_abstract: dict[str, str | None] = {}
            for row in con.execute(
                "SELECT p.id, p.abstract FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id "
                "WHERE pv.venue_id=? AND pv.year=?", (venue_key, year),
            ):
                stored_abstract[row["id"]] = row["abstract"]

            watermark = since
            if incremental and watermark is None:
                watermark = _read_watermark(con, venue_key, year)
            counts["since"] = watermark
            con.commit()

            papers = provider.list_year(year, limit=limit, since=watermark)
            counts["fetched"] = len(papers)
            for position, paper in enumerate(papers):
                if not paper.get("abstract"):
                    counts["no_abstract"] += 1
                title_key = normalize_title_key(paper.get("title"))
                reuse = existing.get(title_key) if len(title_key) >= 20 else None
                if reuse:
                    paper["id"] = reuse
                if paper["id"] in stored_abstract:
                    # Already stored. An unchanged record is skipped outright:
                    # rewriting it would touch every FTS row and every
                    # ``last_seen_at`` for nothing.
                    if stored_abstract[paper["id"]] == (paper.get("abstract") or None):
                        counts["unchanged"] += 1
                        continue
                before = con.execute(
                    "SELECT id FROM papers WHERE id=?", (paper["id"],)).fetchone()
                db.upsert_paper(con, paper, venue_key, year, relation_type="publication")
                counts["updated" if before else "inserted"] += 1
                if len(title_key) >= 20 and title_key not in existing:
                    existing[title_key] = paper["id"]
                if position % COMMIT_BATCH == COMMIT_BATCH - 1:
                    con.commit()
            con.commit()
    except Exception as exc:  # noqa: BLE001 - recorded on the job row
        counts["status"] = "failed"
        counts["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        provider.close()

    if backfill and counts["status"] == "success":
        fill = backfill_journal_abstracts(venue_key, year, limit=limit,
                                          workers=workers, epmc_workers=epmc_workers)
        counts["recovered"] = fill.get("recovered", 0)
        counts["from_europepmc"] = fill.get("from_europepmc", 0)
        counts["from_landing_page"] = fill.get("from_landing_page", 0)
        counts["lane_gave_up"] = fill.get("lane_gave_up", 0)

    counts["seconds"] = round(time.time() - started, 1)
    with db.connect() as con:
        if counts["status"] == "success":
            # The watermark advances only after a successful run: a failed
            # harvest must not claim to have caught up.
            _write_watermark(con, venue_key, year, db.now())
        con.execute(
            "UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,inserted_count=?,"
            "updated_count=?,skipped_count=?,error_count=?,error_message=? WHERE id=?",
            (counts["status"], db.now(), counts["fetched"], counts["inserted"],
             counts["updated"], counts["unchanged"], 0 if counts["status"] == "success" else 1,
             counts["error"], job_id),
        )
        con.commit()
    return counts
