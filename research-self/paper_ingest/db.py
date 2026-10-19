from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH, DATA_ROOT


class LocalConnection(sqlite3.Connection):
    """Commit/rollback and release Windows file handles when leaving a query."""
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, factory=LocalConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(path: Path | None = None) -> None:
    path = path or DB_PATH
    for folder in ("raw/openreview", "raw/acl_anthology", "raw/cvf", "processed", "graph", "cache", "sync_state", "logs", "database"):
        (DATA_ROOT / folder).mkdir(parents=True, exist_ok=True)
    with connect(path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS venues (
          id TEXT NOT NULL, name TEXT NOT NULL, year INTEGER NOT NULL, provider TEXT NOT NULL,
          source_venue_id TEXT, type TEXT NOT NULL, source_url TEXT, availability_status TEXT NOT NULL DEFAULT 'unknown',
          last_sync_at TEXT, PRIMARY KEY(id, year), UNIQUE(id)
        );
        CREATE TABLE IF NOT EXISTS papers (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, source_paper_id TEXT NOT NULL,
          forum_id TEXT, title TEXT NOT NULL, abstract TEXT, authors_json TEXT NOT NULL DEFAULT '[]',
          keywords_json TEXT NOT NULL DEFAULT '[]', primary_area TEXT, pdf_url TEXT, paper_url TEXT,
          doi TEXT, arxiv_id TEXT, submission_year INTEGER, publication_year INTEGER, status TEXT NOT NULL DEFAULT 'unknown',
          decision TEXT, created_at_source TEXT, updated_at_source TEXT, first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL, raw_data_path TEXT, UNIQUE(source, source_paper_id)
        );
        CREATE TABLE IF NOT EXISTS paper_venues (
          paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          venue_id TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
          relation_type TEXT NOT NULL DEFAULT 'submission', year INTEGER NOT NULL,
          PRIMARY KEY(paper_id, venue_id, relation_type)
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          source_review_id TEXT NOT NULL, review_type TEXT NOT NULL, content TEXT, scores_json TEXT,
          source_created_at TEXT, source_updated_at TEXT, visibility TEXT NOT NULL, raw_data_path TEXT,
          UNIQUE(paper_id, source_review_id)
        );
        CREATE TABLE IF NOT EXISTS paper_relations (
          source_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          target_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          relation_type TEXT NOT NULL, score REAL, provenance TEXT,
          PRIMARY KEY(source_paper_id, target_paper_id, relation_type)
        );
        CREATE TABLE IF NOT EXISTS graph_points (
          paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
          venue_id TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL, region INTEGER, cluster INTEGER, vector_json TEXT
        );
        CREATE TABLE IF NOT EXISTS graph_topics (
          venue_id TEXT NOT NULL, level TEXT NOT NULL, number INTEGER NOT NULL, label TEXT NOT NULL,
          PRIMARY KEY(venue_id, level, number)
        );
        CREATE TABLE IF NOT EXISTS sync_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL, year INTEGER NOT NULL, provider TEXT NOT NULL,
          status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, fetched_count INTEGER DEFAULT 0,
          inserted_count INTEGER DEFAULT 0, updated_count INTEGER DEFAULT 0, skipped_count INTEGER DEFAULT 0,
          error_count INTEGER DEFAULT 0, error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_state (
          venue TEXT NOT NULL, year INTEGER NOT NULL, cursor TEXT, checkpoint TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY(venue, year)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(paper_id UNINDEXED, title, abstract, authors, keywords, venue, tokenize='unicode61');
        CREATE INDEX IF NOT EXISTS paper_venues_year_idx ON paper_venues(venue_id, year);
        CREATE INDEX IF NOT EXISTS papers_status_idx ON papers(status);
        """)
        # Phase 1 accidentally added UNIQUE(id), preventing the configured
        # registry from retaining more than one year. Rebuild only that small
        # table when the legacy index is present; existing rows are preserved.
        unique_id = any(
            row[2] == 1 and [c[2] for c in con.execute(f"PRAGMA index_info('{row[1]}')")] == ['id']
            for row in con.execute("PRAGMA index_list('venues')")
        )
        if unique_id:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("ALTER TABLE venues RENAME TO venues_legacy")
            con.execute("ALTER TABLE paper_venues RENAME TO paper_venues_legacy")
            con.execute("""CREATE TABLE venues (
              id TEXT NOT NULL, name TEXT NOT NULL, year INTEGER NOT NULL, provider TEXT NOT NULL,
              source_venue_id TEXT, type TEXT NOT NULL, source_url TEXT, availability_status TEXT NOT NULL DEFAULT 'unknown',
              last_sync_at TEXT, PRIMARY KEY(id, year))""")
            con.execute("""CREATE TABLE paper_venues (
              paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
              venue_id TEXT NOT NULL, relation_type TEXT NOT NULL DEFAULT 'submission', year INTEGER NOT NULL,
              PRIMARY KEY(paper_id, venue_id, relation_type), FOREIGN KEY(venue_id,year) REFERENCES venues(id,year) ON DELETE CASCADE)""")
            con.execute("INSERT INTO venues SELECT * FROM venues_legacy")
            con.execute("INSERT INTO paper_venues SELECT * FROM paper_venues_legacy")
            con.execute("DROP TABLE paper_venues_legacy")
            con.execute("DROP TABLE venues_legacy")
            con.execute("PRAGMA foreign_keys=ON")


def put_venue(con: sqlite3.Connection, *, venue_id: str, name: str, year: int, provider: str,
              source_venue_id: str | None, kind: str, source_url: str | None, availability: str) -> None:
    con.execute("""INSERT INTO venues(id,name,year,provider,source_venue_id,type,source_url,availability_status)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id,year) DO UPDATE SET name=excluded.name, provider=excluded.provider,
      source_venue_id=excluded.source_venue_id,type=excluded.type,source_url=excluded.source_url,
      availability_status=excluded.availability_status""", (venue_id,name,year,provider,source_venue_id,kind,source_url,availability))


def paper_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["authors"] = json.loads(out.pop("authors_json"))
    out["keywords"] = json.loads(out.pop("keywords_json"))
    return out


def upsert_paper(con: sqlite3.Connection, paper: dict[str, Any], venue_id: str, year: int,
                 relation_type: str = "submission") -> str:
    paper_id = paper["id"]
    existing = con.execute("SELECT id FROM papers WHERE id=?", (paper_id,)).fetchone()
    timestamp = now()
    con.execute("""INSERT INTO papers(id,source,source_paper_id,forum_id,title,abstract,authors_json,keywords_json,
      primary_area,pdf_url,paper_url,doi,arxiv_id,submission_year,publication_year,status,decision,created_at_source,
      updated_at_source,first_seen_at,last_seen_at,raw_data_path)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
      forum_id=excluded.forum_id,title=excluded.title,abstract=excluded.abstract,authors_json=excluded.authors_json,
      keywords_json=excluded.keywords_json,primary_area=excluded.primary_area,pdf_url=excluded.pdf_url,
      paper_url=excluded.paper_url,doi=excluded.doi,arxiv_id=excluded.arxiv_id,submission_year=excluded.submission_year,
      publication_year=coalesce(excluded.publication_year,papers.publication_year),status=excluded.status,
      decision=excluded.decision,updated_at_source=excluded.updated_at_source,last_seen_at=excluded.last_seen_at,
      raw_data_path=excluded.raw_data_path""", (
        paper_id,paper["source"],paper["source_paper_id"],paper.get("forum_id"),paper["title"],paper.get("abstract"),
        json.dumps(paper.get("authors",[])),json.dumps(paper.get("keywords",[])),paper.get("primary_area"),
        paper.get("pdf_url"),paper.get("paper_url"),paper.get("doi"),paper.get("arxiv_id"),paper.get("submission_year"),
        paper.get("publication_year"),paper.get("status","unknown"),paper.get("decision"),paper.get("created_at_source"),
        paper.get("updated_at_source"),timestamp,timestamp,paper.get("raw_data_path")))
    con.execute("INSERT OR IGNORE INTO paper_venues(paper_id,venue_id,relation_type,year) VALUES(?,?,?,?)",
                (paper_id,venue_id,relation_type,year))
    venue = con.execute("SELECT name FROM venues WHERE id=? AND year=?", (venue_id,year)).fetchone()
    con.execute("DELETE FROM papers_fts WHERE paper_id=?", (paper_id,))
    con.execute("INSERT INTO papers_fts(paper_id,title,abstract,authors,keywords,venue) VALUES(?,?,?,?,?,?)",
                (paper_id,paper["title"],paper.get("abstract") or "", " ".join(paper.get("authors",[])),
                 " ".join(paper.get("keywords",[])), venue["name"] if venue else ""))
    return "updated" if existing else "inserted"
