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

            def list_year(self, year, *, limit=None):
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

        assert result["status"] == "success"
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

            def list_year(self, year, *, limit=None):
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

        assert second["inserted"] == 0
        assert second["updated"] == 1
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 1
            assert con.execute(
                "SELECT count(*) FROM paper_venues WHERE venue_id='nature'"
            ).fetchone()[0] == 1

    def test_a_failure_is_recorded_not_raised(self, memory_db, monkeypatch):
        from paper_ingest import provider_journals

        class BrokenProvider:
            def __init__(self, venue_key, *, mailto=None):
                pass

            def list_year(self, year, *, limit=None):
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
