import json
from pathlib import Path

from fastapi.testclient import TestClient

from paper_ingest import db
from paper_ingest.api import app
from paper_ingest.openreview import normalize
from paper_ingest.service import list_papers


def fixture_db(tmp_path: Path, monkeypatch):
    path=tmp_path/"papers.sqlite"
    monkeypatch.setattr(db,"DATA_ROOT",tmp_path)
    db.init_db(path)
    return path


def test_openreview_normalization_and_idempotent_upsert(tmp_path,monkeypatch):
    path=fixture_db(tmp_path,monkeypatch)
    note={"id":"abc123","forum":"abc123","cdate":10,"mdate":20,"content":{
        "title":{"value":"Graph retrieval"},"abstract":{"value":"A searchable graph."},
        "authors":{"value":["A. Researcher"]},"keywords":{"value":["graphs","retrieval"]},
        "pdf":{"value":"/pdf?id=abc123"}}}
    paper=normalize(note,2025)
    assert paper["paper_url"].endswith("id=abc123")
    with db.connect(path) as con:
        db.put_venue(con,venue_id="iclr",name="ICLR",year=2025,provider="openreview",source_venue_id="ICLR.cc/2025/Conference",kind="conference",source_url=None,availability="available")
        assert db.upsert_paper(con,paper,"iclr",2025)=="inserted"
        assert db.upsert_paper(con,paper,"iclr",2025)=="updated"
        assert con.execute("SELECT count(*) FROM papers").fetchone()[0]==1
        assert con.execute("SELECT count(*) FROM paper_venues").fetchone()[0]==1
    monkeypatch.setattr(db,"DB_PATH",path)
    result=list_papers(venue="iclr",year=2025,keyword="retrieval",page=1,page_size=10)
    assert result["total"]==1
    assert result["items"][0]["authors"]==["A. Researcher"]


def test_api_venues_and_paper_search(tmp_path,monkeypatch):
    path=fixture_db(tmp_path,monkeypatch)
    monkeypatch.setattr(db,"DB_PATH",path)
    with TestClient(app) as client:
        venues=client.get("/api/venues").json()
        assert {v["id"] for v in venues["items"]}=={"iclr","neurips","icml","acl","cvpr","tmlr"}
        assert venues["default_year"]==2027
        assert client.get("/api/health").status_code==200
