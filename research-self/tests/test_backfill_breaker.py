"""The backfill's circuit breaker must measure service health, not hits.

The breaker's job is to notice that a server stopped answering. It once
measured "did this paper have an abstract" instead, which stopped the
backfill short: the batch index covers 12% of The Lancet's 2022 papers, so
the lane tripped the 25% threshold after 50 papers and recovered 3 papers
instead of 158.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import db  # noqa: E402
from paper_ingest import journal_abstracts  # noqa: E402


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    path = tmp_path / "papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    return path


def _seed(con, count: int, *, venue: str = "lancet", year: int = 2022) -> None:
    """``count`` pending papers of one venue-year."""
    con.execute(
        "INSERT OR IGNORE INTO venues(id,name,year,provider,source_venue_id,type,"
        "source_url,availability_status) VALUES(?,?,?,?,?,?,?,?)",
        (venue, venue.title(), year, "test", None, "journal", None, "available"))
    for i in range(count):
        con.execute(
            "INSERT INTO papers(id,source,source_paper_id,title,paper_url,authors_json,"
            "keywords_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"epmc:{i}", "europepmc", str(i), f"Paper {i}",
             f"https://doi.org/10.1016/test-{i}", "[]", "[]",
             db.now(), db.now()))
        con.execute(
            "INSERT INTO paper_venues(paper_id,venue_id,relation_type,year) "
            "VALUES(?,?,?,?)", (f"epmc:{i}", venue, "publication", year))
    con.commit()


class TestBreakerSemantics:
    def test_a_low_abstract_rate_does_not_trip_the_breaker(self, memory_db, monkeypatch):
        """A service that answers "no abstract" is a healthy service."""
        with db.connect() as con:
            _seed(con, 120)

        # The index knows nothing: every paper is a legitimate miss. The
        # per-DOI lookup is stubbed too, so the test never touches the
        # network -- it is about the breaker's bookkeeping, not about
        # Europe PMC.
        monkeypatch.setattr(journal_abstracts, "build_epmc_index",
                            lambda *a, **k: journal_abstracts.EpmcIndex())
        monkeypatch.setattr(journal_abstracts, "fetch_europepmc_abstract",
                            lambda client, doi: None)

        result = journal_abstracts.backfill_journal_abstracts(
            "lancet", 2022, use_epmc_index=False, epmc_workers=2, workers=1)

        # The lane must have tried every candidate, not given up at 50.
        assert result["candidates"] == 120
        assert result.get("lane_gave_up", 0) == 0, \
            "the lane gave up even though the service was answering"

    def test_a_failing_service_trips_the_breaker(self, memory_db, monkeypatch):
        """A service that errors is not healthy, and the lane should stop."""
        with db.connect() as con:
            _seed(con, 120)

        def boom(*args, **kwargs):
            raise RuntimeError("the service is refusing us")

        # The index cannot be built (the service is down) and every per-DOI
        # request fails: the lane must give up rather than grind through.
        monkeypatch.setattr(journal_abstracts, "build_epmc_index", boom)
        monkeypatch.setattr(journal_abstracts, "fetch_europepmc_abstract", boom)

        result = journal_abstracts.backfill_journal_abstracts(
            "lancet", 2022, use_epmc_index=False, epmc_workers=2, workers=1)
        assert result["candidates"] == 120
        assert result.get("lane_gave_up", 0) >= 1, \
            "the lane kept going although every request failed"

    def test_the_index_is_authoritative_for_the_journal_year(self, memory_db, monkeypatch):
        """A paper the index does not have costs no further request."""
        with db.connect() as con:
            _seed(con, 3)

        index = journal_abstracts.EpmcIndex()
        index.by_doi["10.1016/test-1"] = "The abstract of paper one."
        index.records = 1
        monkeypatch.setattr(journal_abstracts, "build_epmc_index",
                            lambda *a, **k: index)

        called = {"n": 0}

        def counting_fetch(client, doi):
            called["n"] += 1
            return "Should not be reached."

        monkeypatch.setattr(journal_abstracts, "fetch_europepmc_abstract", counting_fetch)

        result = journal_abstracts.backfill_journal_abstracts(
            "lancet", 2022, epmc_workers=2, workers=1)

        assert result["recovered"] == 1
        assert called["n"] == 0, "the index is authoritative; no request was needed"
        with db.connect() as con:
            stored = con.execute(
                "SELECT abstract FROM papers WHERE id='epmc:1'").fetchone()[0]
            assert stored == "The abstract of paper one."
