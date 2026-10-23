"""Tests for the venue-year expectation manifest (offline, temp DB)."""
from __future__ import annotations

import json

import pytest

from paper_ingest import db, manifest, service


MANIFEST_YAML = """
venues:
  neurips:
    2024:
      min_papers: 4000
      max_papers: 6000
      note: "NeurIPS 2024 main track"
  jmlr:
    2025: {min_papers: 250}
"""


@pytest.fixture()
def manifest_file(tmp_path):
    """A manifest on disk, in the documented YAML shape."""
    path = tmp_path / "expected_counts.yaml"
    path.write_text(MANIFEST_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """An empty database in a temporary data root."""
    db_path = tmp_path / "database" / "papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    db.init_db()
    return db_path


def _seed(db_path, venue, year, count):
    """Insert ``count`` papers into one venue-year."""
    db.init_db()
    with db.connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO venues(id,name,year,provider,type,availability_status) "
            "VALUES(?,?,?,'test','conference','available')",
            (venue, venue.upper(), year),
        )
        for index in range(count):
            paper_id = f"p:{venue}:{year}:{index}"
            con.execute(
                "INSERT OR IGNORE INTO papers(id,source,source_paper_id,title,abstract,"
                "authors_json,keywords_json,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,'abstract','[]','[]','2024-01-01','2024-01-01')",
                (paper_id, "test", f"s:{venue}:{year}:{index}", f"Paper {index}"),
            )
            con.execute(
                "INSERT OR IGNORE INTO paper_venues(paper_id,venue_id,relation_type,year) "
                "VALUES(?,?,'publication',?)",
                (paper_id, venue, year),
            )


def test_load_manifest_reads_the_expectations(manifest_file):
    loaded = manifest.load_manifest(manifest_file)
    assert set(loaded) == {("neurips", 2024), ("jmlr", 2025)}
    neurips = loaded[("neurips", 2024)]
    assert isinstance(neurips, manifest.Expectation)
    assert neurips.venue == "neurips"
    assert neurips.year == 2024
    assert neurips.min_papers == 4000
    assert neurips.max_papers == 6000
    assert neurips.note == "NeurIPS 2024 main track"
    # A shorthand entry keeps its floor and leaves the ceiling open.
    assert loaded[("jmlr", 2025)].min_papers == 250
    assert loaded[("jmlr", 2025)].max_papers is None


def test_load_manifest_missing_file_is_not_an_error(tmp_path):
    assert manifest.load_manifest(tmp_path / "does-not-exist.yaml") == {}


def test_load_manifest_accepts_json(tmp_path):
    path = tmp_path / "expected_counts.json"
    path.write_text(json.dumps({"venues": {"icml": {"2024": {"min_papers": 10}}}}), encoding="utf-8")
    loaded = manifest.load_manifest(path)
    assert loaded[("icml", 2024)].min_papers == 10


def test_load_manifest_reads_the_expected_key(tmp_path):
    """The tracked registry template carries the expectations under `expected:`."""
    path = tmp_path / "venues.yaml"
    path.write_text(
        "venues:\n  icml:\n    name: ICML\n    provider: openreview\n"
        "expected:\n  icml:\n    2024: {min_papers: 2000, max_papers: 3000}\n",
        encoding="utf-8",
    )
    loaded = manifest.load_manifest(path)
    assert loaded[("icml", 2024)].min_papers == 2000
    assert loaded[("icml", 2024)].max_papers == 3000


def test_expectation_as_dict_round_trips_the_fields():
    expectation = manifest.Expectation(
        venue="icml", year=2024, min_papers=2000, max_papers=3000,
        note="note", measured_at="2026-10-21", source="sqlite:paper_venues",
    )
    as_dict = expectation.as_dict()
    assert as_dict["venue"] == "icml"
    assert as_dict["year"] == 2024
    assert as_dict["min_papers"] == 2000
    assert as_dict["max_papers"] == 3000
    assert as_dict["note"] == "note"
    assert as_dict["measured_at"] == "2026-10-21"
    assert as_dict["source"] == "sqlite:paper_venues"


def test_check_expectation_ok_inside_the_range():
    expectation = manifest.Expectation(venue="icml", year=2024, min_papers=2000, max_papers=3000)
    verdict = manifest.check_expectation(expectation, 2500)
    assert verdict["status"] == "ok"
    assert verdict["venue"] == "icml"
    assert verdict["year"] == 2024
    assert verdict["actual"] == 2500
    assert verdict["min_papers"] == 2000
    assert verdict["max_papers"] == 3000
    assert verdict["message"]


def test_check_expectation_short_below_the_minimum():
    expectation = manifest.Expectation(venue="icml", year=2024, min_papers=2000, max_papers=3000)
    verdict = manifest.check_expectation(expectation, 1200)
    assert verdict["status"] == "short"
    assert "1200" in verdict["message"]


def test_check_expectation_over_above_the_maximum():
    expectation = manifest.Expectation(venue="icml", year=2024, min_papers=2000, max_papers=3000)
    verdict = manifest.check_expectation(expectation, 4200)
    assert verdict["status"] == "over"
    assert "4200" in verdict["message"]


def test_check_expectation_unknown_without_bounds():
    expectation = manifest.Expectation(venue="icml", year=2024)
    verdict = manifest.check_expectation(expectation, 42)
    assert verdict["status"] == "unknown"
    assert verdict["min_papers"] is None
    assert verdict["max_papers"] is None


def test_check_venue_finds_the_expectation(manifest_file):
    loaded = manifest.load_manifest(manifest_file)
    verdict = manifest.check_venue("neurips", 2024, 4200, manifest=loaded)
    assert verdict["status"] == "ok"
    assert verdict["min_papers"] == 4000


def test_check_venue_without_an_expectation_is_unknown(manifest_file):
    loaded = manifest.load_manifest(manifest_file)
    verdict = manifest.check_venue("icml", 1999, 10, manifest=loaded)
    assert verdict["status"] == "unknown"
    assert verdict["venue"] == "icml"
    assert verdict["year"] == 1999
    assert verdict["actual"] == 10


def test_check_venue_loads_the_manifest_itself(manifest_file, tmp_path, monkeypatch):
    """Without an explicit manifest the default file is read."""
    monkeypatch.setattr(db, "DATA_ROOT", manifest_file.parent)
    verdict = manifest.check_venue("jmlr", 2025, 300)
    assert verdict["status"] == "ok"
    assert verdict["min_papers"] == 250


def test_manifest_round_trip(tmp_path):
    """Write a manifest, load it back and check a verdict against it."""
    path = tmp_path / "expected_counts.yaml"
    path.write_text(MANIFEST_YAML, encoding="utf-8")
    loaded = manifest.load_manifest(path)
    verdict = manifest.check_venue("neurips", 2024, 1500, manifest=loaded)
    assert verdict["status"] == "short"
    assert verdict["actual"] == 1500
    assert verdict["min_papers"] == 4000


def test_verify_corpus_reports_a_short_venue_year(temp_db, tmp_path):
    _seed(temp_db, "neurips", 2024, 10)
    _seed(temp_db, "jmlr", 2025, 300)
    path = tmp_path / "expected_counts.yaml"
    path.write_text(MANIFEST_YAML, encoding="utf-8")
    loaded = manifest.load_manifest(path)
    verdicts = manifest.verify_corpus(loaded)
    summary = verdicts[-1]
    assert summary == {"checked": 2, "ok": 1, "short": 1, "over": 0, "unknown": 0}
    problems = verdicts[:-1]
    assert len(problems) == 1
    assert problems[0]["venue"] == "neurips"
    assert problems[0]["year"] == 2024
    assert problems[0]["actual"] == 10
    assert problems[0]["status"] == "short"


def test_verify_corpus_summary_only(temp_db, tmp_path):
    _seed(temp_db, "neurips", 2024, 5000)
    path = tmp_path / "expected_counts.yaml"
    path.write_text(MANIFEST_YAML, encoding="utf-8")
    summary = manifest.verify_corpus_summary(manifest.load_manifest(path))
    assert summary == {"checked": 2, "ok": 1, "short": 1, "over": 0, "unknown": 0}


def test_verify_corpus_without_a_manifest_is_empty(temp_db):
    verdicts = manifest.verify_corpus({})
    assert verdicts[-1] == {"checked": 0, "ok": 0, "short": 0, "over": 0, "unknown": 0}


def test_registry_template_expectations_are_loadable():
    """The tracked template must stay parseable by the loader it documents.

    The numbers are regenerated from the measured corpus, so this asserts the
    *semantics* of the block rather than a particular count -- pinning 4050
    here meant every legitimate re-measurement broke the test, which teaches
    nobody anything about the manifest.
    """
    from pathlib import Path

    template = Path(__file__).resolve().parents[1] / "venues.example.yaml"
    loaded = manifest.load_manifest(template)
    assert len(loaded) > 50, "the template carries an expectation per venue-year"
    assert ("neurips", 2024) in loaded

    neurips = loaded[("neurips", 2024)]
    assert neurips.min_papers is not None and neurips.min_papers > 0
    assert neurips.max_papers is not None
    assert neurips.min_papers < neurips.max_papers, "a floor below its ceiling"

    # Every entry must be a usable range: floor <= ceiling, both positive
    # (JMLR 2026 is the documented exception, handled below).
    for (venue, year), expectation in loaded.items():
        if (venue, year) == ("jmlr", 2026):
            continue
        assert expectation.min_papers is not None, f"{venue} {year} has no floor"
        assert expectation.min_papers >= 0
        if expectation.max_papers is not None:
            assert expectation.min_papers <= expectation.max_papers, \
                f"{venue} {year}: floor above ceiling"

    # JMLR 2026 has no volume yet: the floor is zero and there is no ceiling.
    assert loaded[("jmlr", 2026)].min_papers == 0
    assert loaded[("jmlr", 2026)].max_papers is None


def test_short_official_harvest_is_reported_not_raised(tmp_path, monkeypatch):
    """The whole point: a short harvest is a verdict, never an exception."""
    from paper_ingest import official

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "database" / "papers.sqlite")
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    (tmp_path / "expected_counts.yaml").write_text(
        "venues:\n  icml:\n    2025: {min_papers: 5, max_papers: 10}\n", encoding="utf-8"
    )
    # Keep the test offline: no detail pages are fetched.
    monkeypatch.setattr(official, "enrich", lambda source, venue, paper: paper)

    def records(*args):
        return "https://test.invalid/", "test.html", [
            {"title": "[TEST FIXTURE] Paper", "authors": ["A"],
             "paper_url": "https://test.invalid/v1/test.html", "abstract": None},
        ]

    monkeypatch.setattr(official, "index_records", records)
    result = service.sync_official("icml", 2025)
    assert result["status"] == "success"
    assert result["coverage"]["status"] == "short"
    assert result["coverage"]["actual"] == 1
    assert result["coverage"]["min_papers"] == 5
    with db.connect() as con:
        job = con.execute(
            "SELECT expected_count,coverage_status FROM sync_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert job["expected_count"] == 5
    assert job["coverage_status"] == "short"


def test_api_venues_reports_the_coverage(tmp_path, monkeypatch):
    """The API surfaces the verdict of the last harvest of every venue-year."""
    from fastapi.testclient import TestClient

    from paper_ingest.api import app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "database" / "papers.sqlite")
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    db.init_db()
    with db.connect() as con:
        db.put_venue(con, venue_id="icml", name="ICML", year=2025, provider="pmlr",
                     source_venue_id=None, kind="conference", source_url=None, availability="available")
        con.execute(
            "INSERT INTO sync_jobs(venue,year,provider,status,started_at,expected_count,coverage_status) "
            "VALUES('icml',2025,'pmlr','success','2026-10-21T00:00:00+00:00',2376,'ok')"
        )
    with TestClient(app) as client:
        items = client.get("/api/venues").json()["items"]
    row = next(item for item in items if item["id"] == "icml" and item["year"] == 2025)
    assert row["expected_count"] == 2376
    assert row["coverage_status"] == "ok"
    # The pre-existing keys are untouched.
    assert row["paper_count"] == 0
    assert row["latest_result"] == "success"
