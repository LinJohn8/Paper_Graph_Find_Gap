"""The dense embedding index, and the fallback when it is not there.

The dense static embeddings are a build-time convenience, not a
requirement: the app must work without them, and with a stale or broken
index file it must fall back rather than fail. These tests pin that
behaviour, so nobody has to rebuild a 288k-paper index to check it.
"""
import numpy as np
import pytest

from paper_ingest import dense, global_layout


def test_without_an_index_the_loader_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(dense, "index_path", lambda: tmp_path / "missing.npz")
    dense._index_cache.clear()
    assert dense.load_index() is None


def test_a_broken_index_file_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "broken.npz"
    path.write_bytes(b"this is not an npz file")
    monkeypatch.setattr(dense, "index_path", lambda: path)
    dense._index_cache.clear()
    assert dense.load_index() is None


def test_an_index_is_loaded_with_its_years(tmp_path, monkeypatch):
    path = tmp_path / "dense.npz"
    np.savez_compressed(
        path,
        ids=np.asarray(["p1", "p2"], dtype=object),
        vectors=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        years=np.asarray([2024, 2025], dtype=np.int32),
    )
    monkeypatch.setattr(dense, "index_path", lambda: path)
    dense._index_cache.clear()
    try:
        index = dense.load_index()
        assert index is not None
        ids, vectors, years = index
        assert ids == ["p1", "p2"]
        assert vectors.shape == (2, 2)
        assert list(years) == [2024, 2025]
    finally:
        dense._index_cache.clear()


def test_the_document_text_is_natural_text():
    text = dense.document_text("A title", "The abstract.", ["kw1", "kw2"])
    assert "A title" in text and "The abstract." in text and "kw1" in text


def test_the_search_falls_back_when_the_dense_model_is_missing(monkeypatch):
    """No dense index and no usable SVD model: the search says so, rather
    than crashing."""
    import paper_ingest.dense as dense_module

    monkeypatch.setattr(dense_module, "load_index", lambda: None)
    monkeypatch.setattr(global_layout, "load_index", lambda: None)
    assert global_layout.search("anything") == [{"mode": "unavailable"}]


def test_the_search_uses_the_dense_index_when_there_is_one(monkeypatch, tmp_path):
    """The dense vectors are the semantic channel when they exist."""
    import paper_ingest.dense as dense_module
    from paper_ingest import db

    # The lexical channels read the database, so the search needs an isolated
    # one -- otherwise it would rank against the real corpus.
    db.init_db(tmp_path / "dense_test.sqlite")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dense_test.sqlite")
    monkeypatch.setattr(db, "DATA_ROOT", tmp_path)

    ids = ["p1", "p2"]
    vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    years = np.asarray([2024, 2025], dtype=np.int32)
    monkeypatch.setattr(dense_module, "load_index", lambda: (ids, vectors, years))
    monkeypatch.setattr(dense_module, "embed_query",
                        lambda text: np.asarray([1.0, 0.0], dtype=np.float32))
    monkeypatch.setattr(global_layout, "load_index",
                        lambda: (_ for _ in ()).throw(AssertionError(
                            "the SVD index must not be loaded when the dense one exists")))

    hits = global_layout.search("a query", limit=2)
    assert hits and hits[0]["id"] == "p1"
    assert hits[0]["mode"] == "semantic"
    assert hits[0]["year"] == 2024
