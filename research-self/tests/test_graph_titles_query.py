"""Pin the scope query of ``graph_titles`` to the form that scales.

The endpoint used two correlated ``EXISTS`` subqueries over ``paper_venues``.
The plan SQLite chose for them was::

    SCAN p
    SEARCH pv USING INDEX paper_venues_year_only_idx (year=?)

-- one scan of ``paper_venues`` per paper. With 288,261 papers in the corpus
that is ~1.7x10^10 comparisons for a query returning 3,395 titles: the
browser timed out and the backend accumulated 1,758 CPU-seconds.

Driving from ``paper_venues`` (whose (venue_id, year) index answers the
scope directly) takes 0.26 s. These tests pin the shape, because the
slow form *works* -- it just never finishes -- so a correctness test alone
would not notice a regression.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import db  # noqa: E402
from paper_ingest import service  # noqa: E402


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    """A small corpus: two venues, two years, plus a paper in both years."""
    path = tmp_path / "papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)

    def paper(pid: str, title: str, venue: str, year: int) -> None:
        con.execute(
            "INSERT OR IGNORE INTO papers(id,source,source_paper_id,title,"
            "authors_json,keywords_json,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, "openalex", pid, title, "[]", "[]", db.now(), db.now()))
        con.execute(
            "INSERT OR IGNORE INTO venues(id,name,year,provider,source_venue_id,"
            "type,source_url,availability_status) VALUES(?,?,?,?,?,?,?,?)",
            (venue, venue.title(), year, "test", None, "conference", None, "available"))
        con.execute(
            "INSERT OR IGNORE INTO paper_venues(paper_id,venue_id,relation_type,year) "
            "VALUES(?,?,?,?)", (pid, venue, "publication", year))

    with db.connect() as con:
        paper("p1", "A Paper In Icml 2025", "icml", 2025)
        paper("p2", "Another Icml Paper", "icml", 2025)
        paper("p3", "A Cvpr Paper", "cvpr", 2025)
        paper("p4", "An Older Icml Paper", "icml", 2024)
        con.commit()
    return path


def _captured_sql(monkeypatch) -> list[str]:
    """Record every SQL statement the endpoint runs."""
    seen: list[str] = []
    real_connect = db.connect

    class RecordingConnection:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args, **kwargs):
            seen.append(sql)
            return self._con.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._con, name)

        def __enter__(self):
            self._con.__enter__()
            return self

        def __exit__(self, *exc):
            return self._con.__exit__(*exc)

    monkeypatch.setattr(
        service.db, "connect",
        lambda *a, **k: RecordingConnection(real_connect(*a, **k)))
    return seen


class TestScopeShape:
    def test_the_scope_is_not_a_correlated_exists(self, memory_db, monkeypatch):
        seen = _captured_sql(monkeypatch)
        service.graph_titles("icml", 2025)
        title_sql = [s for s in seen if "p.title" in s]
        assert title_sql, "the endpoint ran no title query"
        for sql in title_sql:
            assert "EXISTS (SELECT 1 FROM paper_venues" not in sql, (
                "the correlated EXISTS form is back: it scans paper_venues "
                "once per paper and never finishes on the real corpus")

    def test_the_scope_drives_from_paper_venues(self, memory_db, monkeypatch):
        seen = _captured_sql(monkeypatch)
        service.graph_titles("icml", 2025)
        title_sql = [s for s in seen if "p.title" in s][0]
        assert "FROM paper_venues" in title_sql
        assert "IN (" in title_sql


class TestResults:
    def test_only_the_requested_venue_year_is_returned(self, memory_db):
        titles = service.graph_titles("icml", 2025)
        assert set(titles) == {"p1", "p2"}
        assert titles["p1"] == "A Paper In Icml 2025"

    def test_a_year_without_a_venue_filter_returns_the_whole_year(self, memory_db):
        titles = service.graph_titles(None, 2025)
        assert set(titles) == {"p1", "p2", "p3"}

    def test_the_slug_filter_is_applied(self, memory_db):
        titles = service.graph_titles("icml", 2025, slugs=["p1"])
        assert set(titles) == {"p1"}

    def test_a_venue_year_with_no_papers_is_empty(self, memory_db):
        assert service.graph_titles("cvpr", 2024) == {}

    def test_an_unknown_venue_is_empty_not_an_error(self, memory_db):
        assert service.graph_titles("no-such-venue", 2025) == {}


class TestUnfilteredBound:
    """A whole-corpus title map is bounded, not an 18-second hang.

    The map always sends slugs; the unbounded form returned 288,261 titles
    in a 36 MB body, which a caller reads as a hang.
    """

    def _seed_many(self, count: int, *, venue: str = "icml", year: int = 2025) -> None:
        with db.connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO venues(id,name,year,provider,source_venue_id,"
                "type,source_url,availability_status) VALUES(?,?,?,?,?,?,?,?)",
                (venue, venue.title(), year, "test", None, "conference",
                 None, "available"))
            for i in range(count):
                pid = f"bulk:{i}"
                con.execute(
                    "INSERT OR IGNORE INTO papers(id,source,source_paper_id,title,"
                    "authors_json,keywords_json,first_seen_at,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (pid, "openalex", pid, f"Bulk Paper {i}", "[]", "[]",
                     db.now(), db.now()))
                con.execute(
                    "INSERT OR IGNORE INTO paper_venues(paper_id,venue_id,"
                    "relation_type,year) VALUES(?,?,?,?)",
                    (pid, venue, "publication", year))
            con.commit()

    def test_the_unfiltered_slice_is_bounded(self, memory_db):
        self._seed_many(service.MAX_UNFILTERED_TITLES + 50)
        titles = service.graph_titles("icml", 2025)
        assert len(titles) == service.MAX_UNFILTERED_TITLES

    def test_the_bound_does_not_apply_when_slugs_are_given(self, memory_db):
        self._seed_many(10)
        wanted = [f"bulk:{i}" for i in range(10)]
        titles = service.graph_titles("icml", 2025, slugs=wanted)
        assert len(titles) == 10

    def test_the_slice_is_deterministic(self, memory_db):
        self._seed_many(service.MAX_UNFILTERED_TITLES + 20)
        first = list(service.graph_titles("icml", 2025))
        second = list(service.graph_titles("icml", 2025))
        assert first == second, "repeated calls must agree on the slice"
