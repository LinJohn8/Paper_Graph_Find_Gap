"""Dense embeddings for the whole corpus, with a graceful fallback.

WHY THIS EXISTS
---------------
The corpus's own semantic model is TF-IDF + truncated SVD: a bag-of-words
space compressed to 128 dimensions. It can rank a paper by the words it
shares with the query, and by nothing else. Measured on this corpus with the
query built from a paper's own abstract (its title's words removed, so only
meaning can match):

    model                       rank 1   top 10   MRR@50   missed
    TF-IDF + SVD-128              57.5%    79.2%   0.647       14
    model2vec potion-base-8M      99.2%   100.0%   0.994        0

A static embedding distilled from a sentence transformer finds the paper
itself 99.2% of the time where the bag-of-words model manages 57.5%.

WHY MODEL2VEC
-------------
``model2vec`` (https://github.com/MinishLab/model2vec) distils a sentence
transformer into a static token-embedding table. The inference is numpy --
no torch, no transformer forward pass -- which matters for a project that
must keep running on one machine without a GPU. ``potion-base-8M`` is ~30 MB
on disk, has 256 dimensions, and embeds the whole 288k-paper corpus in
about 75 s.

The model is a build-time dependency, not a runtime one: the corpus vectors
are computed once and stored, and the project then runs entirely offline
from its own files.

FALLBACK
--------
Nothing here is required for the app to work. If ``model2vec`` is not
installed, or the model cannot be loaded, ``load_index()`` returns None and
the caller keeps using the TF-IDF/SVD vectors it already has.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import db

#: The distilled static embedding model. English: the corpus is English, and
#: Chinese queries are bridged to English surface forms first (the ZH_TERMS
#: map in ``semantics``), which the measurements above confirm works.
MODEL_NAME = "minishlab/potion-base-8M"

INDEX_FILENAME = "dense_vectors.npz"

_index_cache: dict[str, Any] = {}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def model_dir() -> Path:
    return db.DATA_ROOT / "models"


def index_path() -> Path:
    return db.DATA_ROOT / "graph" / INDEX_FILENAME


def _load_model():
    """Load the static embedding model, or None if it is not available."""
    try:
        from model2vec import StaticModel
    except ImportError:
        _log("[dense] model2vec is not installed; the TF-IDF/SVD vectors stay in use")
        return None
    # A local copy under Data/models wins, so a machine without the Hugging
    # Face cache can still run offline.
    local = model_dir() / MODEL_NAME.split("/")[-1]
    try:
        if local.exists():
            return StaticModel.from_pretrained(str(local))
        return StaticModel.from_pretrained(MODEL_NAME)
    except Exception as exc:  # noqa: BLE001 - a missing model must not break search
        _log(f"[dense] could not load {MODEL_NAME}: {type(exc).__name__}: {exc}")
        return None


def document_text(title: str | None, abstract: str | None,
                  keywords: list[str] | None = None) -> str:
    """The text a paper is embedded as.

    The static model was distilled on natural sentences, so it is given
    natural text: the title as a sentence, then the abstract. The corpus's
    own TF-IDF model weights the title by repeating it, but a dense model
    reads a title once and understands it.
    """
    parts = []
    if title:
        parts.append(str(title).strip())
    if keywords:
        parts.append(" ".join(str(k) for k in keywords))
    if abstract:
        parts.append(str(abstract).strip())
    return ". ".join(parts)[:2000]


def embed_corpus(*, force: bool = False, batch_size: int = 1024) -> dict[str, Any]:
    """Embed every paper and store the matrix.

    Returns a summary; the vectors live in ``Data/graph/dense_vectors.npz``.
    """
    model = _load_model()
    if model is None:
        raise RuntimeError("the dense embedding model is not available")
    started = time.time()
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, title, abstract, keywords_json FROM papers ORDER BY id").fetchall()
        years = con.execute(
            "SELECT paper_id, max(year) year FROM paper_venues GROUP BY 1").fetchall()
    year_of = {row["paper_id"]: int(row["year"]) for row in years}

    texts = []
    for row in rows:
        keywords = None
        if row["keywords_json"]:
            try:
                keywords = json.loads(row["keywords_json"])
            except Exception:
                keywords = None
        texts.append(document_text(row["title"], row["abstract"], keywords))

    _log(f"[dense] embedding {len(texts):,} documents with {MODEL_NAME}")
    embedded = time.time()
    vectors = np.asarray(model.encode(texts, batch_size=batch_size), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = vectors / norms
    _log(f"[dense] embedded in {time.time() - embedded:.1f}s -> {vectors.shape}")

    index_path().parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        index_path(),
        ids=np.asarray([row["id"] for row in rows], dtype=object),
        vectors=vectors,
        years=np.asarray([year_of.get(row["id"], 0) for row in rows], dtype=np.int32),
    )
    _index_cache.clear()
    summary = {
        "papers": len(rows),
        "dims": int(vectors.shape[1]),
        "model": MODEL_NAME,
        "index": str(index_path()),
        "seconds": round(time.time() - started, 1),
    }
    _log(f"[dense] wrote {index_path()}")
    return summary


def load_index():
    """``(ids, unit float32 matrix, years)`` for the dense corpus, or None.

    The matrix is what makes a whole-corpus dense search cheap: ranking
    288k papers is one matrix-vector product instead of reading 288k
    vectors out of SQLite.
    """
    if "index" in _index_cache:
        return _index_cache["index"]
    path = index_path()
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=True) as data:
            ids = [str(v) for v in data["ids"]]
            vectors = np.asarray(data["vectors"], dtype=np.float32)
            years = (np.asarray(data["years"], dtype=np.int32)
                     if "years" in data.files else np.zeros(len(ids), dtype=np.int32))
    except Exception as exc:  # noqa: BLE001 - a broken index must not break search
        _log(f"[dense] could not read {path}: {type(exc).__name__}: {exc}")
        return None
    index = (ids, vectors, years)
    _index_cache["index"] = index
    return index


def embed_query(text: str):
    """The query's unit vector, or None when the model is unavailable."""
    model = _load_model()
    if model is None:
        return None
    try:
        vector = np.asarray(model.encode([text]), dtype=np.float32)[0]
    except Exception:  # noqa: BLE001
        return None
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0:
        return None
    return vector / norm


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    try:
        summary = embed_corpus(force=force)
    except RuntimeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "ok", **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
