"""Tests for dynamic year resolution (offline, temp DB)."""
from __future__ import annotations

import pytest

from paper_ingest import db, years


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "database" / "papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(years.db, "DB_PATH", db_path)
    monkeypatch.setattr(years.db, "DATA_ROOT", tmp_path)
    monkeypatch.delenv("PAPER_DEFAULT_YEAR", raising=False)
    db.init_db()
    return db_path


def _seed(db_path, venue_ids_years):
    db.init_db()
    with db.connect() as con:
        for venue, year in venue_ids_years:
            con.execute(
                "INSERT OR IGNORE INTO venues(id,name,year,provider,type,availability_status) "
                "VALUES(?,?,?,'test','conference','available')",
                (venue, venue.upper(), year),
            )
            con.execute(
                "INSERT OR IGNORE INTO papers(id,source,source_paper_id,title,abstract,"
                "authors_json,keywords_json,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,'[]','[]','2024-01-01','2024-01-01')",
                (f"p:{venue}:{year}", "test", f"s:{venue}:{year}",
                 f"Paper {venue} {year}", "abstract"),
            )
            con.execute(
                "INSERT OR IGNORE INTO paper_venues(paper_id,venue_id,relation_type,year) "
                "VALUES(?,?,'publication',?)",
                (f"p:{venue}:{year}", venue, year),
            )


def test_available_years_empty_on_fresh_db(temp_db):
    assert years.available_years() == []


def test_available_years_newest_first(temp_db):
    _seed(temp_db, [("icml", 2024), ("icml", 2025), ("neurips", 2023)])
    assert years.available_years() == [2025, 2024, 2023]


def test_default_year_is_newest_with_data(temp_db):
    """The UI must open on a year that actually has papers, never a placeholder."""
    _seed(temp_db, [("icml", 2024), ("icml", 2025)])
    assert years.default_year() == 2025


def test_default_year_falls_back_to_calendar_year_when_empty(temp_db):
    assert years.default_year() == years.current_year()


def test_default_year_env_override(temp_db, monkeypatch):
    _seed(temp_db, [("icml", 2024)])
    monkeypatch.setenv("PAPER_DEFAULT_YEAR", "2022")
    assert years.default_year() == 2022


def test_default_year_ignores_invalid_override(temp_db, monkeypatch):
    _seed(temp_db, [("icml", 2024)])
    monkeypatch.setenv("PAPER_DEFAULT_YEAR", "not-a-year")
    assert years.default_year() == 2024
    monkeypatch.setenv("PAPER_DEFAULT_YEAR", "9999")
    assert years.default_year() == 2024


def test_resolve_prefers_explicit_year(temp_db):
    _seed(temp_db, [("icml", 2024)])
    assert years.resolve(2023) == 2023
    assert years.resolve("2023") == 2023


def test_resolve_blank_uses_default(temp_db):
    _seed(temp_db, [("icml", 2024)])
    assert years.resolve(None) == 2024
    assert years.resolve("") == 2024
    assert years.resolve("   ") == 2024


def test_resolve_rejects_out_of_range(temp_db):
    _seed(temp_db, [("icml", 2024)])
    assert years.resolve(1500) == 2024
    assert years.resolve(3000) == 2024
    assert years.resolve("garbage") == 2024


def test_resolve_honours_explicit_fallback(temp_db):
    assert years.resolve(None, fallback=2020) == 2020


def test_current_year_is_sane():
    year = years.current_year()
    assert 2020 <= year <= 2100
