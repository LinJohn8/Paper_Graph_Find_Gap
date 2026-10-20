"""Hybrid retrieval: RRF fusion, FTS prefix channel, substring channel, suggestions.

These tests share one temporary database so the SQL channels are exercised for
real (bm25() placement, LIKE shapes, venue/year filters) instead of being
mocked. The fixtures insert a handful of papers whose text mirrors the failure
that motivated the module: an acronym (SECI) that appears once as a real token,
once inside a username ("Secilia-Cxy") and once inside a code link ("baseCIL").
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import db as pdb  # noqa: E402
from paper_ingest import retrieval  # noqa: E402
from paper_ingest.db import init_db  # noqa: E402

PAPERS = [
    ("p1", "Discriminative Reasoning for Event-Event Relations",
     "It challenges the conventional sentence-level task (SECI) with long-text understanding."),
    ("p2", "SOFTS: Multivariate Time Series Forecasting",
     "Code at https://github.com/Secilia-Cxy/SOFTS is publicly available."),
    ("p3", "Class-Incremental Learning with Pretrained Models",
     "Our code is available at https://github.com/HdTgon/baseCIL."),
    ("p4", "Semi-Supervised Graph Learning",
     "semi supervised learning on graphs, a series of experiments."),
    ("p5", "Social Network Analysis", "social structures studied with statistics."),
]


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_DB_PATH", str(tmp_path / "test.sqlite"))
    pdb.DB_PATH = tmp_path / "test.sqlite"
    init_db()
    connection = pdb.connect()
    # LocalConnection closes itself on `with` exit, so seed rows with the
    # connection kept open and hand the same open connection to the test.
    seed = pdb.connect()
    with seed:
        seed.executemany(
            "INSERT INTO papers(id,source,source_paper_id,title,abstract,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [(pid, "test", pid, title, abstract, "2026-01-01", "2026-01-01")
             for pid, title, abstract in PAPERS],
        )
        for pid, title, abstract in PAPERS:
            seed.execute(
                "INSERT INTO papers_fts(rowid,paper_id,title,abstract,authors,keywords,venue) "
                "SELECT rowid,?,?,?,?,?,? FROM papers WHERE id=?",
                (pid, title, abstract, "", "", "", pid),
            )
        seed.executemany(
            "INSERT INTO venues(id,name,year,provider,type,availability_status) VALUES(?,?,?,?,?,?)",
            [("icml", "ICML", 2024, "pmlr", "conference", "available")],
        )
        seed.executemany(
            "INSERT INTO paper_venues(paper_id,venue_id,relation_type,year) VALUES(?,?,?,?)",
            [("p1", "icml", "publication", 2024), ("p4", "icml", "publication", 2024)],
        )
    yield connection
    connection.close()


def test_rrf_fusion_prefers_agreeing_channels():
    fused = retrieval.rank_fuse([["a", "b", "c"], ["b", "a"]])
    ids = [pid for pid, _ in fused]
    # 'a' and 'b' appear in both channels; 'c' only in one, so it ranks last.
    assert set(ids[:2]) == {"a", "b"}
    assert ids[2] == "c"
    # A paper ranked high everywhere must outscore one ranked high once.
    scores = dict(fused)
    assert scores["a"] > scores["c"]


def test_fts_channel_matches_prefix_tokens(con):
    # "learn" as a prefix must find "learning" documents.
    hits = retrieval.fts_ranking(con, ["learn"], limit=5)
    assert "p4" in hits


def test_fts_channel_matches_acronym_token(con):
    hits = retrieval.fts_ranking(con, ["seci"], limit=5)
    # The paper that literally contains the token SECI must rank; the prefix
    # expansion may legitimately surface additional sec*-prefixed tokens.
    assert "p1" in hits


def test_fts_channel_venue_filter(con):
    hits = retrieval.fts_ranking(con, ["graph"], limit=5, venue="icml", year=2024)
    assert "p4" in hits
    assert "p5" not in hits  # p5 is in no venue


def test_like_channel_finds_substrings(con):
    hits = retrieval.like_ranking(con, "SECI", limit=5)
    # All three SECI occurrences: the acronym token, the username, the code link.
    assert set(hits) >= {"p1", "p2", "p3"}


def test_like_channel_year_filter(con):
    hits = retrieval.like_ranking(con, "graph", limit=5, year=2024)
    assert "p4" in hits
    assert "p5" not in hits


def test_suggest_terms_finds_vocabulary_neighbours():
    vocab = ["semi", "social", "series", "speech", "selection", "specific"]
    out = retrieval.suggest_terms("SECI", vocab)
    assert "semi" in out
    assert all(t in vocab for t in out)


def test_prf_extracts_frequent_words():
    titles = [("p1", "diffusion models for image generation"),
              ("p2", "diffusion models for audio generation")]
    out = retrieval.pseudo_relevance_feedback(["diffusion"], ["p1", "p2"], lambda ids: titles)
    assert "diffusion" not in out  # query terms are not suggested back
    assert "models" in out or "generation" in out
