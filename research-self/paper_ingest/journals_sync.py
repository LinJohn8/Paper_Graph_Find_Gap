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

__all__ = ["import_journal_year", "import_epmc_journal_year", "normalize_title_key",
           "prune_abstractless"]

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


def prune_abstractless(*, source: str = "europepmc", dry_run: bool = False) -> dict[str, Any]:
    """Remove the records of one source that carry no abstract.

    WHY THIS EXISTS
    ----------------
    The Europe PMC collector now asks the service only for the records that
    have an abstract (``HAS_ABSTRACT:Y``), because the rest is editorial
    matter -- news items, letters, obituaries -- that carries a title and a
    DOI and nothing else. The first full run predated that filter, so it
    left behind a tail of abstract-less records: BMJ 2022 arrived with 2,003
    rows of which 189 had an abstract.

    Such a row is nearly inert here. The semantic model is fitted on
    ``title * 3 + keywords * 2 + abstract``
    (:func:`paper_ingest.semantics.make_doc`), so an abstract-less record
    contributes a title and nothing else; it lands in a cluster defined by
    its title words alone and drags that cluster's centroid. The idea finder
    reads abstracts and cannot use it at all.

    Only rows whose *source* matches are touched, so a record another
    collector delivered is never removed on the strength of a rule about
    this one. The record is removed *as a record*: a source's abstract-less
    paper has no value in any venue it appears in, so every link to it goes
    with it.

    The FTS mirror is maintained too -- ``papers_fts`` is keyed by
    ``papers.rowid``, so the row is deleted by that rowid.

    HOW IT IS DONE
    --------------
    As one set-based transaction, not row by row. Two measurements shaped it:

    * The row-by-row version asked ``SELECT count(*) FROM paper_venues WHERE
      paper_id=?`` for every candidate, and ``paper_venues`` has no index on
      ``paper_id`` -- so each question was a full scan of a 325,000-row
      table. Measured: 220 rows per minute, which is 90 minutes for 19,712
      rows.
    * Deleting the FTS rows one by one is just as slow: FTS5 merges a
      segment per delete, and 15,312 of them were still running after ten
      minutes.

    So the prune answers the question once, in the database, and rebuilds the
    FTS mirror in one pass -- the same rebuild ``db._align_fts_rowids``
    performs.
    """
    db.init_db()
    with db.connect() as con:
        rows = con.execute(
            "SELECT p.id, p.rowid FROM papers p WHERE p.source=? "
            "AND (p.abstract IS NULL OR trim(p.abstract)='')", (source,)).fetchall()
        total = len(rows)
        if dry_run:
            per_venue = {
                r["venue_id"]: r["n"] for r in con.execute(
                    "SELECT pv.venue_id, count(*) n FROM paper_venues pv "
                    "JOIN papers p ON p.id=pv.paper_id WHERE p.source=? "
                    "AND (p.abstract IS NULL OR trim(p.abstract)='') GROUP BY 1",
                    (source,))
            }
            return {"source": source, "would_remove": total, "by_venue": per_venue,
                    "dry_run": True}

        # One transaction: the FTS mirror and the papers table must not drift
        # apart, and a half-applied prune would be worse than none.
        #
        # ``isolation_level = None`` hands the transaction control to SQLite.
        # The default (implicit transactions) would refuse ``BEGIN IMMEDIATE``
        # with "cannot start a transaction within a transaction".
        previous_isolation = con.isolation_level
        con.isolation_level = None
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TEMP TABLE IF NOT EXISTS doomed_papers "
                        "(id TEXT PRIMARY KEY, rowid_ INTEGER)")
            con.execute("DELETE FROM doomed_papers")
            con.executemany(
                "INSERT OR IGNORE INTO doomed_papers(id,rowid_) VALUES(?,?)",
                [(r["id"], r["rowid"]) for r in rows])

            # The paper row goes first. ``paper_venues`` has ON DELETE CASCADE
            # from ``papers``, so the links follow on their own -- and the
            # cascade needs an index on ``paper_relations.target_paper_id``,
            # which is why that index exists (see db.init_db).
            removed_papers = con.execute(
                "DELETE FROM papers WHERE id IN (SELECT id FROM doomed_papers)"
            ).rowcount
            removed_links = con.execute(
                "DELETE FROM paper_venues WHERE paper_id IN "
                "(SELECT id FROM doomed_papers)").rowcount

            # The FTS mirror is rebuilt rather than patched row by row.
            #
            # Deleting N rows from an FTS5 table is not N cheap statements:
            # every delete merges a segment, and 15,312 of them took over ten
            # minutes and were still running. Dropping the table's contents
            # and refilling it from ``papers`` is one pass over the corpus
            # (the same rebuild ``db._align_fts_rowids`` performs, and the
            # same one the index was migrated with), and it cannot leave the
            # mirror out of step with the papers it mirrors.
            con.execute("DELETE FROM papers_fts")
            con.execute(
                """INSERT INTO papers_fts(rowid,paper_id,title,abstract,authors,keywords,venue)
                   SELECT p.rowid,p.id,p.title,coalesce(p.abstract,''),p.authors_json,
                          p.keywords_json,
                          coalesce((SELECT v.name FROM paper_venues pv
                                    JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year
                                    WHERE pv.paper_id=p.id LIMIT 1),'')
                   FROM papers p"""
            )
            removed_fts = len(rows)
            con.execute("DROP TABLE doomed_papers")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.isolation_level = previous_isolation
    return {"source": source, "removed_papers": removed_papers,
            "removed_links": removed_links, "removed_fts": removed_fts,
            "dry_run": False}


def import_epmc_journal_year(
    venue_key: str,
    year: int,
    *,
    limit: int | None = None,
    has_abstract_only: bool = True,
    mailto: str | None = None,
    since: str | None = None,
    incremental: bool = False,
) -> dict[str, Any]:
    """Harvest one journal-year from Europe PMC and write it into the corpus.

    This is the path for the journals Europe PMC covers and OpenAlex never
    indexed at all (PLOS ONE, eLife, Cell Reports, PNAS, NEJM, ...), and the
    path that keeps working when the OpenAlex daily budget is spent.

    Unlike the OpenAlex path, no separate backfill pass is needed: Europe
    PMC serves the abstract with the record, so the rows arrive complete.

    ``has_abstract_only`` asks the service for the records that carry an
    abstract. It is on by default, and the reason is measured: BMJ 2024 has
    1,178 records of which only 132 have an abstract, JAMA 947 of which 202
    do. The rest is editorial matter -- news items, letters, obituaries --
    that carries a title and a DOI and nothing else. Such a record is nearly
    useless here: the semantic model is fitted on title *and* abstract, the
    idea finder reads abstracts, and a cluster of news items is noise on the
    map. Where Europe PMC indexes a journal well the filter costs nothing
    (PLOS ONE 16,688 -> 16,617, Cell Reports 1,376 -> 1,374, IEEE TPAMI
    753 -> 753).

    ``incremental=True`` reuses the stored watermark and asks Europe PMC only
    for the records it created since, so a settled year costs one request
    instead of many. ``since`` overrides the watermark explicitly. The
    watermark advances only after a successful run -- a failed harvest must
    not claim to have caught up.
    """
    from .provider_epmc import EPMC_JOURNALS, EpmcProvider

    if venue_key not in EPMC_JOURNALS:
        raise ValueError(
            f"unknown journal {venue_key!r}; known: {', '.join(sorted(EPMC_JOURNALS))}")
    journals, journal_name = EPMC_JOURNALS[venue_key]

    started = time.time()
    counts: dict[str, Any] = {
        "venue": venue_key, "year": year, "status": "success",
        "fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0,
        "no_abstract": 0, "recovered": 0, "error": None, "since": None,
    }
    provider = EpmcProvider(venue_key, mailto=mailto)
    job_id = None
    with db.connect() as con:
        job_id = con.execute(
            "INSERT INTO sync_jobs(venue,year,provider,status,started_at) VALUES(?,?,?,?,?)",
            (venue_key, year, "journal_europepmc", "running", db.now()),
        ).lastrowid
        db.put_venue(
            con, venue_id=venue_key, name=journal_name, year=year,
            provider="journal_europepmc", source_venue_id=f"europepmc:{journals[0]}",
            kind="journal", source_url="https://europepmc.org",
            availability="available",
        )
        con.commit()

    try:
        with db.connect() as con:
            existing: dict[str, str] = {}
            for row in con.execute(
                "SELECT p.id, p.title FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id "
                "WHERE pv.venue_id=? AND pv.year=?", (venue_key, year),
            ):
                key = normalize_title_key(row["title"])
                if len(key) >= 20 and key not in existing:
                    existing[key] = row["id"]
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

            for position, paper in enumerate(provider.iter_year(
                    year, limit=limit, has_abstract_only=has_abstract_only,
                    since=watermark)):
                counts["fetched"] += 1
                if not paper.get("abstract"):
                    counts["no_abstract"] += 1
                title_key = normalize_title_key(paper.get("title"))
                reuse = existing.get(title_key) if len(title_key) >= 20 else None
                if reuse:
                    paper["id"] = reuse
                if paper["id"] in stored_abstract and \
                        stored_abstract[paper["id"]] == (paper.get("abstract") or None):
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

    counts["seconds"] = round(time.time() - started, 1)
    with db.connect() as con:
        if counts["status"] == "success":
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
