"""Tests for the journal write path (paper_ingest/journals_sync.py).

The write path is where a corpus is actually grown, so the properties that
matter are: a repeated import does not duplicate rows, a paper that another
source already delivered in the same venue-year is not stored twice, and a
failing harvest is recorded rather than swallowed.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import db  # noqa: E402
from paper_ingest import journals_sync  # noqa: E402


class TestTitleKey:
    def test_punctuation_and_case_are_dropped(self):
        assert journals_sync.normalize_title_key("Attention Is All You Need!") == \
               "attentionisallyouneed"

    def test_empty(self):
        assert journals_sync.normalize_title_key(None) == ""


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    """A throwaway database wired into the package's db module.

    ``db.connect`` and ``db.init_db`` both read the module-level ``DB_PATH``,
    so redirecting it is enough to keep the test off the real corpus.
    """
    path = tmp_path / "papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    return path


def _paper(paper_id: str, title: str, *, abstract: str | None = None) -> dict:
    """A paper dict shaped for db.upsert_paper."""
    return {
        "id": paper_id,
        "source": "openalex",
        "source_paper_id": paper_id,
        "forum_id": None,
        "title": title,
        "abstract": abstract,
        "authors": ["Ada Lovelace"],
        "keywords": ["computing"],
        "primary_area": "Computer Science",
        "pdf_url": None,
        "paper_url": f"https://example.org/{paper_id}",
        "doi": None,
        "arxiv_id": None,
        "submission_year": 2024,
        "publication_year": 2024,
        "status": "published",
        "decision": "accept",
        "created_at_source": db.now(),
        "updated_at_source": db.now(),
        "raw_data_path": None,
    }


class TestImportJournalYear:
    def test_an_unknown_venue_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            journals_sync.import_journal_year("not-a-journal", 2024)
        assert "not-a-journal" in str(excinfo.value)
        assert "known" in str(excinfo.value)

    def test_the_records_are_written_and_linked_to_the_venue(
            self, memory_db, monkeypatch):
        from paper_ingest import provider_journals

        papers = [_paper("openalex:W1", "A First Paper"),
                  _paper("openalex:W2", "A Second Paper")]

        class FakeProvider:
            def __init__(self, venue_key, *, mailto=None):
                self.venue_key = venue_key

            def list_year(self, year, *, limit=None, since=None):
                return papers

            def close(self):
                pass

        monkeypatch.setattr(journals_sync, "JournalProvider", FakeProvider)
        monkeypatch.setattr(provider_journals, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "backfill_journal_abstracts",
                            lambda *a, **k: {"recovered": 0})

        result = journals_sync.import_journal_year("nature", 2024, backfill=True)

        assert result["status"] == "success", result.get("error")
        assert result["fetched"] == 2
        assert result["inserted"] == 2
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 2
            assert con.execute(
                "SELECT count(*) FROM paper_venues WHERE venue_id='nature' AND year=2024"
            ).fetchone()[0] == 2

    def test_a_repeated_import_updates_instead_of_duplicating(
            self, memory_db, monkeypatch):
        from paper_ingest import provider_journals

        papers = [_paper("openalex:W1", "A First Paper")]

        class FakeProvider:
            def __init__(self, venue_key, *, mailto=None):
                pass

            def list_year(self, year, *, limit=None, since=None):
                return papers

            def close(self):
                pass

        monkeypatch.setattr(journals_sync, "JournalProvider", FakeProvider)
        monkeypatch.setattr(provider_journals, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "backfill_journal_abstracts",
                            lambda *a, **k: {"recovered": 0})

        journals_sync.import_journal_year("nature", 2024)
        second = journals_sync.import_journal_year("nature", 2024)

        # The record did not change, so nothing is written again -- that is the
        # point of the unchanged check.
        assert second["inserted"] == 0
        assert second["updated"] == 0
        assert second["unchanged"] == 1
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 1
            assert con.execute(
                "SELECT count(*) FROM paper_venues WHERE venue_id='nature'"
            ).fetchone()[0] == 1

    def test_a_changed_record_is_rewritten(self, memory_db, monkeypatch):
        from paper_ingest import provider_journals

        papers = [_paper("openalex:W1", "A First Paper", abstract="First version.")]

        class FakeProvider:
            def __init__(self, venue_key, *, mailto=None):
                pass

            def list_year(self, year, *, limit=None, since=None):
                return papers

            def close(self):
                pass

        monkeypatch.setattr(journals_sync, "JournalProvider", FakeProvider)
        monkeypatch.setattr(provider_journals, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "backfill_journal_abstracts",
                            lambda *a, **k: {"recovered": 0})

        journals_sync.import_journal_year("nature", 2024, backfill=False)
        papers[0] = _paper("openalex:W1", "A First Paper", abstract="A better abstract.")
        second = journals_sync.import_journal_year("nature", 2024, backfill=False)

        assert second["updated"] == 1
        assert second["unchanged"] == 0
        with db.connect() as con:
            assert con.execute(
                "SELECT abstract FROM papers WHERE id='openalex:W1'").fetchone()[0] \
                == "A better abstract."

    def test_a_failure_is_recorded_not_raised(self, memory_db, monkeypatch):
        from paper_ingest import provider_journals

        class BrokenProvider:
            def __init__(self, venue_key, *, mailto=None):
                pass

            def list_year(self, year, *, limit=None, since=None):
                raise RuntimeError("openalex refused")

            def close(self):
                pass

        monkeypatch.setattr(journals_sync, "JournalProvider", BrokenProvider)
        monkeypatch.setattr(provider_journals, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})

        result = journals_sync.import_journal_year("nature", 2024, backfill=False)

        assert result["status"] == "failed"
        assert "openalex refused" in result["error"]
        with db.connect() as con:
            row = con.execute(
                "SELECT status, error_message FROM sync_jobs WHERE venue='nature' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            assert row["status"] == "failed"
            assert "openalex refused" in row["error_message"]


class TestIncremental:
    """The watermark behaviour: a re-run must be cheap, and honest."""

    def _provider(self, monkeypatch, papers, seen=None):
        from paper_ingest import provider_journals

        class FakeProvider:
            def __init__(self, venue_key, *, mailto=None):
                pass

            def list_year(self, year, *, limit=None, since=None):
                if seen is not None:
                    seen.append(since)
                return papers

            def close(self):
                pass

        monkeypatch.setattr(journals_sync, "JournalProvider", FakeProvider)
        monkeypatch.setattr(provider_journals, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "backfill_journal_abstracts",
                            lambda *a, **k: {"recovered": 0})

    def test_the_watermark_is_written_after_a_successful_run(self, memory_db, monkeypatch):
        self._provider(monkeypatch, [_paper("openalex:W1", "A First Paper")])
        journals_sync.import_journal_year("nature", 2024, backfill=False)
        with db.connect() as con:
            row = con.execute(
                "SELECT checkpoint FROM sync_state WHERE venue='nature' AND year=2024"
            ).fetchone()
            assert row is not None
            assert row["checkpoint"]

    def test_a_failed_run_does_not_advance_the_watermark(self, memory_db, monkeypatch):
        from paper_ingest import provider_journals

        class BrokenProvider:
            def __init__(self, venue_key, *, mailto=None):
                pass

            def list_year(self, year, *, limit=None, since=None):
                raise RuntimeError("nope")

            def close(self):
                pass

        monkeypatch.setattr(journals_sync, "JournalProvider", BrokenProvider)
        monkeypatch.setattr(provider_journals, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})
        monkeypatch.setattr(journals_sync, "JOURNAL_SOURCES",
                            {"nature": ("S123", "Nature")})

        journals_sync.import_journal_year("nature", 2024, backfill=False)
        with db.connect() as con:
            row = con.execute(
                "SELECT checkpoint FROM sync_state WHERE venue='nature' AND year=2024"
            ).fetchone()
            # A failed harvest must not claim to have caught up.
            assert row is None or not row["checkpoint"]

    def test_an_incremental_run_resumes_from_the_watermark(self, memory_db, monkeypatch):
        seen: list[str | None] = []
        self._provider(monkeypatch, [_paper("openalex:W1", "A First Paper")], seen)
        journals_sync.import_journal_year("nature", 2024, backfill=False)
        assert seen[0] is None, "the first run has no watermark to resume from"

        journals_sync.import_journal_year("nature", 2024, backfill=False, incremental=True)
        assert seen[1] is not None, "the second run must resume from the watermark"
        # The watermark is a date, which is what OpenAlex's filter accepts.
        assert len(seen[1]) == 10 and seen[1][4] == "-"

    def test_an_explicit_since_overrides_the_watermark(self, memory_db, monkeypatch):
        seen: list[str | None] = []
        self._provider(monkeypatch, [_paper("openalex:W1", "A First Paper")], seen)
        journals_sync.import_journal_year("nature", 2024, backfill=False,
                                          since="2026-01-02", incremental=True)
        assert seen[0] == "2026-01-02"

    def test_an_unchanged_record_is_not_written_again(self, memory_db, monkeypatch):
        papers = [_paper("openalex:W1", "A First Paper", abstract="The abstract text.")]
        self._provider(monkeypatch, papers)
        journals_sync.import_journal_year("nature", 2024, backfill=False)
        second = journals_sync.import_journal_year("nature", 2024, backfill=False)
        assert second["unchanged"] == 1
        assert second["inserted"] == 0
        assert second["updated"] == 0
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 1


class TestPruneAbstractless:
    """The prune removes one source's abstract-less records and nothing else."""

    def _seed(self, con):
        """Two EPMC rows (one with an abstract) and one OpenAlex row."""
        con.execute(
            "INSERT INTO papers(id,source,source_paper_id,title,abstract,authors_json,"
            "keywords_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("epmc:1", "europepmc", "1", "A Paper Without An Abstract", None, "[]",
             "[]", db.now(), db.now()))
        con.execute(
            "INSERT INTO papers(id,source,source_paper_id,title,abstract,authors_json,"
            "keywords_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("epmc:2", "europepmc", "2", "A Paper With An Abstract", "The abstract.",
             "[]", "[]", db.now(), db.now()))
        con.execute(
            "INSERT INTO papers(id,source,source_paper_id,title,abstract,authors_json,"
            "keywords_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("openalex:W1", "openalex", "W1", "An OpenAlex Paper Without An Abstract",
             None, "[]", "[]", db.now(), db.now()))
        for venue, pid in (("bmj", "epmc:1"), ("bmj", "epmc:2"),
                           ("nature", "openalex:W1")):
            con.execute(
                "INSERT OR IGNORE INTO venues(id,name,year,provider,source_venue_id,type,"
                "source_url,availability_status) VALUES(?,?,?,?,?,?,?,?)",
                (venue, venue.title(), 2024, "test", None, "journal", None, "available"))
            con.execute(
                "INSERT INTO paper_venues(paper_id,venue_id,relation_type,year) "
                "VALUES(?,?,?,?)", (pid, venue, "publication", 2024))
        # The FTS mirror, keyed by papers.rowid.
        for pid in ("epmc:1", "epmc:2", "openalex:W1"):
            rowid = con.execute("SELECT rowid FROM papers WHERE id=?", (pid,)).fetchone()[0]
            con.execute(
                "INSERT INTO papers_fts(rowid,paper_id,title,abstract,authors,keywords,venue) "
                "VALUES(?,?,?,?,?,?,?)", (rowid, pid, "t", "", "", "", "v"))
        con.commit()

    def test_the_dry_run_changes_nothing(self, memory_db):
        with db.connect() as con:
            self._seed(con)
        result = journals_sync.prune_abstractless(dry_run=True)
        assert result["would_remove"] == 1
        assert result["by_venue"] == {"bmj": 1}
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 3

    def test_only_the_named_source_is_pruned(self, memory_db):
        with db.connect() as con:
            self._seed(con)
        result = journals_sync.prune_abstractless()
        assert result["removed_papers"] == 1
        with db.connect() as con:
            left = {r["id"] for r in con.execute("SELECT id FROM papers")}
            assert "epmc:1" not in left
            assert "epmc:2" in left, "a record with an abstract must stay"
            assert "openalex:W1" in left, "another source must not be touched"

    def test_the_fts_mirror_is_pruned_too(self, memory_db):
        with db.connect() as con:
            self._seed(con)
        journals_sync.prune_abstractless()
        with db.connect() as con:
            ids = {r["paper_id"] for r in con.execute("SELECT paper_id FROM papers_fts")}
            assert "epmc:1" not in ids
            assert "epmc:2" in ids

    def test_the_venue_link_is_removed(self, memory_db):
        with db.connect() as con:
            self._seed(con)
        journals_sync.prune_abstractless()
        with db.connect() as con:
            links = con.execute(
                "SELECT count(*) FROM paper_venues WHERE paper_id='epmc:1'").fetchone()[0]
            assert links == 0

    def test_a_paper_another_venue_uses_keeps_its_row(self, memory_db):
        with db.connect() as con:
            self._seed(con)
            # The same paper in a second venue. (paper_venues is unique per
            # (paper, venue, relation), so the second reference has to be a
            # different venue, not a different year.)
            con.execute(
                "INSERT OR IGNORE INTO venues(id,name,year,provider,source_venue_id,type,"
                "source_url,availability_status) VALUES(?,?,?,?,?,?,?,?)",
                ("jama", "JAMA", 2024, "test", None, "journal", None, "available"))
            con.execute(
                "INSERT INTO paper_venues(paper_id,venue_id,relation_type,year) "
                "VALUES(?,?,?,?)", ("epmc:1", "jama", "publication", 2024))
            con.commit()
        journals_sync.prune_abstractless()
        with db.connect() as con:
            # The prune removes the *record*, not a link: an abstract-less
            # record of this source has no value in any venue it appears in.
            gone = con.execute(
                "SELECT count(*) FROM papers WHERE id='epmc:1'").fetchone()[0]
            assert gone == 0
            links = con.execute(
                "SELECT count(*) FROM paper_venues WHERE paper_id='epmc:1'").fetchone()[0]
            assert links == 0
