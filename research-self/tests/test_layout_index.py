"""The layout tables must answer "which venue-years have no layout?" quickly.

WHY THIS TEST EXISTS
--------------------
The query is

    SELECT pv.venue_id, pv.year, count(DISTINCT pv.paper_id)
    FROM paper_venues pv
    WHERE NOT EXISTS (SELECT 1 FROM graph_points g
                      WHERE g.venue_id = pv.venue_id AND g.year = pv.year)
    GROUP BY 1,2

``graph_points`` is keyed by ``paper_id``, so ``venue_id`` had no index and
SQLite answered this by scanning the whole layout table once per
``paper_venues`` row: 288k rows x 75k rows. Measured on the real corpus,
the query **did not finish in ten minutes**; with the composite index it
returns in 0.9 s.

A test that only checks the index exists would pass on an index that does
not cover the year column -- which is what the first attempt did, and the
query was still slow. So this asserts the plan, not just the schema.
"""
import sqlite3
import time

import pytest

from paper_ingest import db


@pytest.fixture()
def corpus(tmp_path):
    """A miniature corpus with the same table shapes."""
    db.init_db(tmp_path / "layout.sqlite")
    with db.connect(tmp_path / "layout.sqlite") as con:
        # venues is keyed by (id, year) and paper_venues references that pair.
        for venue, year in (("aa", 2024), ("aa", 2025), ("bb", 2024), ("bb", 2025)):
            db.put_venue(con, venue_id=venue, name=venue.upper(), year=year,
                         provider="test", source_venue_id=None, kind="journal",
                         source_url=None, availability="open")
        # Each paper belongs to exactly one venue-year, as in the real corpus
        # (measured: 0 papers belong to more than one venue-year).
        for i in range(40):
            paper_id = f"p{i}"
            con.execute(
                "INSERT INTO papers(id, title, abstract, source, source_paper_id, "
                "status, submission_year, first_seen_at, last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (paper_id, f"Paper {i}", "An abstract.", "test", str(i),
                 "accepted", 2025, "2025-01-01T00:00:00+00:00",
                 "2025-01-01T00:00:00+00:00"))
        for offset, (venue, year) in enumerate(
                (("aa", 2024), ("aa", 2025), ("bb", 2024), ("bb", 2025))):
            for i in range(10):
                con.execute(
                    "INSERT OR IGNORE INTO paper_venues"
                    "(paper_id, venue_id, year, relation_type) VALUES(?,?,?,?)",
                    (f"p{offset * 10 + i}", venue, year, "main"))
        con.commit()
    return tmp_path / "layout.sqlite"


def test_the_layout_index_covers_both_columns(corpus):
    with db.connect(corpus) as con:
        indexes = {
            row[1]: [c[2] for c in con.execute(f"PRAGMA index_info('{row[1]}')")]
            for row in con.execute("PRAGMA index_list('graph_points')")
        }
    covered = [cols for cols in indexes.values() if cols == ["venue_id", "year"]]
    assert covered, (
        "graph_points needs an index on (venue_id, year): an index on "
        f"venue_id alone still reads every row of the venue to test the "
        f"year, and the layout-absence query was still slow. Found: {indexes}")


def test_the_layout_absence_query_uses_the_index(corpus):
    """The query must be an index probe per row, not a scan of the layout."""
    with db.connect(corpus) as con:
        sql = (
            "SELECT pv.venue_id, pv.year, count(DISTINCT pv.paper_id) n "
            "FROM paper_venues pv "
            "WHERE NOT EXISTS (SELECT 1 FROM graph_points g "
            "                  WHERE g.venue_id = pv.venue_id AND g.year = pv.year) "
            "GROUP BY 1,2 HAVING n > 0")
        # EXPLAIN QUERY PLAN returns (id, parent, notused, detail) since
        # SQLite 3.36; the detail of every step is in the fourth column.
        steps = con.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        plan = "\n".join(str(step[-1]) for step in steps)
    assert "graph_points_venue_year_idx" in plan, (
        f"the layout-absence query does not use the composite index:\n{plan}")


def test_a_layout_is_found_for_the_venue_year_that_has_one(corpus):
    with db.connect(corpus) as con:
        con.execute(
            "INSERT INTO graph_points(paper_id, venue_id, x, y, region, cluster, "
            "vector_json, year) VALUES('p0','aa',0.1,0.2,0,0,'[1]',2025)")
        con.commit()
        sql = (
            "SELECT pv.venue_id, pv.year FROM paper_venues pv "
            "WHERE NOT EXISTS (SELECT 1 FROM graph_points g "
            "                  WHERE g.venue_id = pv.venue_id AND g.year = pv.year) "
            "GROUP BY 1,2")
        missing = {(r[0], r[1]) for r in con.execute(sql)}
    # aa 2025 now has a layout, so it must not be reported as missing.
    assert ("aa", 2025) not in missing
    assert ("aa", 2024) in missing
