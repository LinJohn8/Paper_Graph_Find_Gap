"""Tests for incremental API sync (offline: temp DB, stubbed provider)."""
from __future__ import annotations

import json

import pytest

from paper_ingest import config, db, provider_chain, sync_api


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the package at an isolated SQLite database."""
    db_path = tmp_path / "database" / "papers.sqlite"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(sync_api.db, "DB_PATH", db_path)
    monkeypatch.setattr(sync_api.db, "DATA_ROOT", tmp_path)
    db.init_db()
    return db_path


def _paper(pid="openalex:W1", title="A Stable Paper Title For Testing",
           abstract="An abstract of reasonable length for testing purposes.",
           **kw):
    base = {
        "id": pid,
        "source": "openalex",
        "source_paper_id": pid.split(":")[-1],
        "title": title,
        "abstract": abstract,
        "authors": ["A. Author"],
        "keywords": ["kw1"],
        "primary_area": "cs.LG",
        "pdf_url": "https://example.org/p.pdf",
        "paper_url": "https://example.org/p",
        "doi": "10.1/test",
        "arxiv_id": None,
        "submission_year": 2024,
        "publication_year": 2024,
        "status": "published",
        "decision": None,
        "created_at_source": None,
        "updated_at_source": None,
        "raw_data_path": None,
    }
    base.update(kw)
    return base


class StubHarvest:
    def __init__(self, papers):
        self.papers = papers

    def summary(self):
        return {"papers": len(self.papers), "with_abstract": len(self.papers),
                "status": "ok", "providers": [{"provider": "openalex",
                                               "status": "ok", "fetched": len(self.papers),
                                               "kept": len(self.papers),
                                               "message": None, "elapsed_s": 0.0}]}


def _stub(monkeypatch, papers):
    monkeypatch.setattr(
        sync_api, "harvest_venue",
        lambda *a, **kw: StubHarvest(papers),
    )


def _count(con, where="1=1", args=()):
    return con.execute(f"SELECT count(*) FROM papers WHERE {where}", args).fetchone()[0]


def test_first_sync_inserts(temp_db, monkeypatch):
    _stub(monkeypatch, [_paper()])
    res = sync_api.sync_api("icml", 2024)
    assert res["status"] == "success"
    assert res["inserted_count"] == 1
    assert res["updated_count"] == 0
    assert res["skipped_count"] == 0
    with db.connect() as con:
        assert _count(con) == 1


def test_second_sync_skips_unchanged(temp_db, monkeypatch):
    """Re-syncing identical data must not rewrite rows (cheap incremental runs)."""
    _stub(monkeypatch, [_paper()])
    first = sync_api.sync_api("icml", 2024)
    assert first["inserted_count"] == 1

    second = sync_api.sync_api("icml", 2024)
    assert second["inserted_count"] == 0
    assert second["updated_count"] == 0
    assert second["skipped_count"] == 1
    assert second["changed"] == 0
    with db.connect() as con:
        assert _count(con) == 1


def test_sync_updates_changed_paper(temp_db, monkeypatch):
    _stub(monkeypatch, [_paper(abstract="Original abstract text that is long enough.")])
    sync_api.sync_api("icml", 2024)

    _stub(monkeypatch, [_paper(abstract="A completely revised abstract with new findings.")])
    res = sync_api.sync_api("icml", 2024)
    assert res["updated_count"] == 1
    assert res["inserted_count"] == 0
    assert res["changed"] == 1
    with db.connect() as con:
        row = con.execute("SELECT abstract FROM papers").fetchone()
        assert "revised" in row["abstract"]
        assert _count(con) == 1  # still one row, not duplicated


def test_incremental_adds_only_new_papers(temp_db, monkeypatch):
    _stub(monkeypatch, [_paper()])
    sync_api.sync_api("icml", 2024)

    new_paper = _paper(pid="openalex:W2", title="Another Distinct Paper Title",
                      doi="10.1/other")
    _stub(monkeypatch, [_paper(), new_paper])
    res = sync_api.sync_api("icml", 2024)
    assert res["inserted_count"] == 1
    assert res["skipped_count"] == 1
    with db.connect() as con:
        assert _count(con) == 2


def test_doi_match_prevents_duplicate_across_sources(temp_db, monkeypatch):
    """A paper seen from arXiv, then from OpenAlex under a different id, is one row."""
    arxiv_paper = _paper(id="arxiv:2401.00001", source="arxiv",
                        source_paper_id="2401.00001", doi="10.1/same",
                        title="Same Paper Appearing In Two Sources")
    _stub(monkeypatch, [arxiv_paper])
    sync_api.sync_api("icml", 2024)

    openalex_paper = _paper(id="openalex:W9", source="openalex",
                           source_paper_id="W9", doi="10.1/same",
                           title="Same Paper Appearing In Two Sources")
    _stub(monkeypatch, [openalex_paper])
    res = sync_api.sync_api("icml", 2024)
    with db.connect() as con:
        assert _count(con) == 1, "duplicate row created for the same DOI"
    # It is the same logical paper, so it should be skipped or updated, never inserted.
    assert res["inserted_count"] == 0


def test_unchanged_paper_keeps_id_stable_across_source_switch(temp_db, monkeypatch):
    _stub(monkeypatch, [_paper(id="arxiv:2401.1", source="arxiv",
                              source_paper_id="2401.1")])
    sync_api.sync_api("icml", 2024)
    with db.connect() as con:
        original_id = con.execute("SELECT id FROM papers").fetchone()["id"]

    _stub(monkeypatch, [_paper(id="openalex:W5", source="openalex",
                              source_paper_id="W5")])
    sync_api.sync_api("icml", 2024)
    with db.connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM papers")]
    assert ids == [original_id], "id changed, which would orphan graph/relations"


def test_dry_run_writes_nothing(temp_db, monkeypatch):
    _stub(monkeypatch, [_paper()])
    res = sync_api.sync_api("icml", 2024, dry_run=True)
    assert res["fetched_count"] == 1
    assert res["inserted_count"] == 0
    with db.connect() as con:
        assert _count(con) == 0


def test_no_data_records_provider_reasons(temp_db, monkeypatch):
    from paper_ingest.harvest import Attempt

    class Empty:
        papers = []
        attempts = [Attempt("openalex", "error", message="HTTP 401")]

        def summary(self):
            return {"papers": 0, "with_abstract": 0, "status": "no_data",
                    "providers": [{"provider": "openalex", "status": "error",
                                   "fetched": 0, "kept": 0,
                                   "message": "HTTP 401", "elapsed_s": 0.0}]}

    monkeypatch.setattr(sync_api, "harvest_venue", lambda *a, **kw: Empty())
    res = sync_api.sync_api("icml", 2026)
    assert res["status"] == "no_data"
    assert "openalex" in (res["error"] or "")
    assert "401" in (res["error"] or "")


def test_no_data_without_attempts_still_reports_no_data(temp_db, monkeypatch):
    """A bare empty result must be 'no_data', never a crash/failure."""

    class Bare:
        papers = []

        def summary(self):
            return {"papers": 0, "with_abstract": 0, "status": "no_data",
                    "providers": []}

    monkeypatch.setattr(sync_api, "harvest_venue", lambda *a, **kw: Bare())
    res = sync_api.sync_api("icml", 2026)
    assert res["status"] == "no_data"
    assert res["error"]


def test_sync_job_recorded(temp_db, monkeypatch):
    _stub(monkeypatch, [_paper()])
    sync_api.sync_api("icml", 2025)
    with db.connect() as con:
        row = con.execute(
            "SELECT venue,year,status,fetched_count,inserted_count "
            "FROM sync_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["venue"] == "icml"
    assert row["year"] == 2025
    assert row["status"] == "success"
    assert row["inserted_count"] == 1
