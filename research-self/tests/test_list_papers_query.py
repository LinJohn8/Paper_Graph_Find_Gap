"""Performance + correctness guards for the venue/year-filtered paper list.

The venue filter used to be a correlated EXISTS subquery, which SQLite answers
by scanning every paper row and probing paper_venues for each one. At the
195k-row corpus (73k conference + 122k journal papers) one venue-year page
took 236s. The JOIN rewrite drives the query from paper_venues instead; this
module pins both the shape (no per-paper correlated subquery) and the result
correctness (no duplicates, no dropped rows, stable totals) so the slow shape
cannot come back unnoticed.

The timing assertion is deliberately loose -- it exists to catch an
order-of-magnitude regression, not to benchmark the machine.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import db, service  # noqa: E402


def _paper(pid: str, year: int) -> dict:
    """Minimal paper dict accepted by db.upsert_paper."""
    return {
        "id": pid, "source": "test", "source_paper_id": pid, "title": f"Paper {pid}",
        "status": "published", "publication_year": year, "submission_year": year,
        "updated_at_source": "2026-01-01",
    }


def _venue(con, venue: str, year: int) -> None:
    con.execute(
        "INSERT OR IGNORE INTO venues(id,name,year,provider,type,availability_status) "
        "VALUES(?,?,?,'test','conference','available')",
        (venue, venue.upper(), year),
    )


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """A corpus with a venue/year grid, several papers per cell."""
    path = tmp_path / "list_papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    db.init_db()
    with db.connect() as con:
        for venue, year in {("iclr", 2025), ("nature", 2025), ("iclr", 2024)}:
            _venue(con, venue, year)
        for i in range(300):
            db.upsert_paper(con, _paper(f"a{i}", 2025), "iclr", 2025)
            db.upsert_paper(con, _paper(f"b{i}", 2025), "nature", 2025)
            if i % 2 == 0:  # multi-venue papers must not be duplicated
                db.upsert_paper(con, _paper(f"c{i}", 2024), "iclr", 2024)
        con.commit()
    return db


def test_venue_year_totals_match_the_seeded_grid(seeded):
    assert service.list_papers(venue="iclr", year=2025)["total"] == 300
    assert service.list_papers(venue="nature", year=2025)["total"] == 300
    # iclr spans two years -> the all-years query must not double count.
    assert service.list_papers(venue="iclr", year="all")["total"] == 450
    assert service.list_papers(year=2024)["total"] == 150
    # 300 iclr/2025 + 300 nature/2025 + 150 iclr/2024, each paper counted once.
    assert service.list_papers(year="all")["total"] == 750


def test_no_duplicate_papers_within_a_page(seeded):
    for kwargs in ({"venue": "iclr", "year": 2025}, {"venue": "iclr", "year": "all"},
                   {"year": 2025}, {"year": "all"}):
        page = service.list_papers(**kwargs, page=1, page_size=100)
        ids = [item["id"] for item in page["items"]]
        assert len(ids) == len(set(ids)), f"duplicate rows for {kwargs}"


def test_pages_and_offsets_stay_consistent(seeded):
    p1 = service.list_papers(venue="iclr", year=2025, page=1, page_size=100)
    p2 = service.list_papers(venue="iclr", year=2025, page=2, page_size=100)
    p3 = service.list_papers(venue="iclr", year=2025, page=3, page_size=100)
    assert p1["pages"] == 3
    assert len(p1["items"]) == 100 and len(p2["items"]) == 100 and len(p3["items"]) == 100
    seen = {i["id"] for page in (p1, p2, p3) for i in page["items"]}
    assert len(seen) == 300


def test_status_and_keyword_filters_compose_with_the_join(seeded):
    with db.connect() as con:
        # An exact-id slice (LIKE 'b1%' would match b1, b10..b19, b100..b199).
        con.execute("UPDATE papers SET status='rejected' "
                    "WHERE id IN ('b1','b2','b3','b4','b5')")
        con.commit()
    assert service.list_papers(venue="nature", year=2025, status="rejected")["total"] == 5
    assert service.list_papers(venue="nature", year=2025, status="published")["total"] == 295
    # A keyword that matches nothing must return zero rows, not everything.
    assert service.list_papers(venue="nature", year=2025,
                               keyword="zzzznotpresent")["total"] == 0
    assert service.list_papers(venue="nature", year=2025, keyword="Paper")["total"] == 300


def test_venue_filter_is_not_a_correlated_subquery():
    """The slow shape is banned outright, not merely timed.

    A per-paper ``EXISTS (SELECT 1 FROM paper_venues ...)`` can only be answered
    by probing once per row; the driving-join form is the fix. Guarding the SQL
    text keeps a future refactor from silently restoring the quadratic plan
    (the timing test below would also catch it, but only on the live corpus).
    """
    source = Path(service.__file__).read_text(encoding="utf-8")
    body = source.split("def list_papers", 1)[1].split("\ndef ", 1)[0]
    assert "EXISTS (SELECT 1 FROM paper_venues" not in body, (
        "list_papers must drive venue/year filtering from paper_venues, "
        "not filter papers row by row"
    )
    assert "JOIN paper_venues pv ON pv.paper_id=p.id" in body


def test_venue_year_page_stays_fast_on_a_large_corpus(tmp_path, monkeypatch):
    """Build enough rows that a per-row probe would be plainly visible."""
    path = tmp_path / "perf.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    db.init_db()
    with db.connect() as con:
        for venue in ("nature", "iclr"):
            _venue(con, venue, 2025)
        # 200 matching rows for the queried cell out of 20,000 papers: the ratio
        # that made the correlated form cost 236s at corpus scale.
        for i in range(20_000):
            venue = "nature" if i < 200 else "iclr"
            db.upsert_paper(con, _paper(f"p{i}", 2025), venue, 2025)
        con.commit()

    started = time.time()
    result = service.list_papers(venue="nature", year=2025, page=1, page_size=20)
    elapsed = time.time() - started
    assert result["total"] == 200
    # The correlated form needed ~100s here (measured 112s at 195k rows);
    # the join form is ~0.02s. 5s catches the regression without being flaky.
    assert elapsed < 5.0, f"venue-year page took {elapsed:.1f}s; expected well under 5s"