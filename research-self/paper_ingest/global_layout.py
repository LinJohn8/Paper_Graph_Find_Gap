"""Whole-corpus ("all years") semantic space and 2D layout.

WHY THIS MODULE EXISTS
----------------------
``graph_points`` holds one layout **per venue-year**. Each such layout was fitted
by its own TF-IDF + SVD model, so its 48 dimensions live in a basis of its own:
two points from different cells are not comparable, and overlaying the 29 cells
would stack 29 unrelated unit squares on top of each other. The "All years" view
therefore needs one model fitted over the whole corpus and one projection of it:

* ``SemanticModel`` (TF-IDF 1024 -> SVD 48) fitted on every paper's weighted
  title/keywords/abstract, alias-expanded, exactly like the per-cell models;
* a deterministic 2D projection (PCA on the 48 normalized dimensions) for the
  map; t-SNE is not attempted at 70k+ points because it would take hours;
* KMeans clusters on the same 48 dimensions for the map's colours and labels.

Everything is persisted so the API never refits at request time:

* ``Data/graph/all_all_semantic.pkl``  - the fitted ``SemanticModel``
* ``Data/graph/all_all_vectors.npz``   - ids + float32 (n, 48) matrix, so
  semantic search can rank tens of thousands of papers without re-reading every
  JSON vector from SQLite
* ``graph_points_global`` rows         - x, y, cluster, region per paper

Run it with::

    python -m paper_ingest.global_layout
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import db, dense as dense_module, retrieval, semantics

MODEL_FILENAME = "all_all_semantic.pkl"
VECTOR_FILENAME = "all_all_vectors.npz"
CLUSTERS = 40
# 128-dim matches semantics.SemanticModel's upgraded default: the ranking
# geometry is noticeably finer at corpus scale, and the search matrix stays a
# sub-second matrix-vector product (float32 73675x128).
DIMS = 128

_index_cache: dict[str, Any] = {}


def _graph_dir() -> Path:
    return db.DATA_ROOT / "graph"


def model_path() -> Path:
    return _graph_dir() / MODEL_FILENAME


def vector_path() -> Path:
    return _graph_dir() / VECTOR_FILENAME


def _unit(matrix: np.ndarray) -> np.ndarray:
    """Row-normalise; zero rows stay zero instead of producing NaN."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _load_rows() -> list[Any]:
    db.init_db()
    with db.connect() as con:
        return con.execute(
            """SELECT p.id, p.title, p.abstract, p.keywords_json
               FROM papers p
               WHERE EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id = p.id)
               ORDER BY p.id"""
        ).fetchall()


def build(*, dims: int = DIMS, clusters: int = CLUSTERS, log=print) -> dict[str, Any]:
    """Project the corpus to 2D and persist everything.

    The space being projected is the dense static embedding when it has been
    built, and the fitted TF-IDF/SVD model otherwise. The dense space is the
    better one: it places papers by meaning, so the map's regions and
    clusters are topics a reader recognises, and the search that shares the
    index ranks by meaning too.
    """
    started = time.time()
    rows = _load_rows()
    if not rows:
        raise RuntimeError("no papers in the database; nothing to fit")
    log(f"[global] papers={len(rows)}")

    model = None
    vectors = None
    docs: list[str] = []
    dense = dense_module.load_index()
    if dense is not None:
        dense_ids, dense_vectors, _dense_years = dense
        wanted = [row["id"] for row in rows]
        position = {pid: i for i, pid in enumerate(dense_ids)}
        if all(pid in position for pid in wanted):
            vectors = np.asarray([dense_vectors[position[pid]] for pid in wanted],
                                 dtype=np.float32)
            vectors = _unit(vectors)
            log(f"[global] dense vectors {vectors.shape}")
        else:
            log("[global] the dense index does not cover every paper; "
                "fitting TF-IDF/SVD instead")

    # The documents are always built: they are what the cluster labels are
    # read from, whichever space the clusters came from.
    for row in rows:
        try:
            keywords = json.loads(row["keywords_json"]) if row["keywords_json"] else None
        except Exception:
            keywords = None
        docs.append(semantics.expand_aliases(
            semantics.make_doc(row["title"], keywords, row["abstract"])))
    log(f"[global] documents built in {time.time() - started:.1f}s")

    if vectors is None:
        fitted = time.time()
        model = semantics.SemanticModel(dims=dims)
        model.fit(docs)
        log(f"[global] TF-IDF + SVD fitted in {time.time() - fitted:.1f}s")

        vectors = np.asarray(model.transform(docs), dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(rows):
            raise RuntimeError(f"model returned {vectors.shape}; expected ({len(rows)}, {dims})")
        vectors = _unit(vectors)
    log(f"[global] vectors {vectors.shape}")

    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

    projected = time.time()
    coords = PCA(n_components=2, random_state=17).fit_transform(vectors)
    low, high = coords.min(axis=0), coords.max(axis=0)
    span = np.where(high > low, high - low, 1.0)
    coords = (coords - low) / span
    log(f"[global] PCA-2 projection in {time.time() - projected:.1f}s")

    labels = np.zeros(len(rows), dtype=int)
    k = max(2, min(int(clusters), len(rows)))
    if k > 1 and len(np.unique(vectors, axis=0)) > 1:
        clustered = time.time()
        labels = KMeans(n_clusters=k, random_state=17, n_init=5).fit_predict(vectors).astype(int)
        log(f"[global] KMeans k={k} in {time.time() - clustered:.1f}s")

    topics = _topic_labels(model, docs, labels)
    years = _paper_years()
    _graph_dir().mkdir(parents=True, exist_ok=True)
    if model is not None:
        model.save(model_path())
    np.savez_compressed(
        vector_path(),
        ids=np.asarray([row["id"] for row in rows], dtype=object),
        vectors=vectors,
        years=np.asarray([years.get(row["id"], 0) for row in rows], dtype=np.int32),
    )

    built_at = db.now()
    with db.connect() as con:
        con.execute("DELETE FROM graph_points_global")
        con.executemany(
            """INSERT OR REPLACE INTO graph_points_global(paper_id,x,y,cluster,region,vector_json,built_at)
               VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    row["id"],
                    float(coords[i, 0]),
                    float(coords[i, 1]),
                    int(labels[i]),
                    int(labels[i]),
                    json.dumps([round(float(v), 5) for v in vectors[i]]),
                    built_at,
                )
                for i, row in enumerate(rows)
            ],
        )
        con.execute(
            "INSERT OR REPLACE INTO sync_state(venue,year,cursor,checkpoint,updated_at) VALUES(?,?,?,?,?)",
            ("__meta__", 0, "global-layout", json.dumps({"papers": len(rows), "clusters": k}), built_at),
        )
        for number, label in topics.items():
            con.execute(
                "INSERT OR REPLACE INTO graph_topics(venue_id,level,number,label) VALUES(?,?,?,?)",
                ("all", "cluster", number, label),
            )
            con.execute(
                "INSERT OR REPLACE INTO graph_topics(venue_id,level,number,label) VALUES(?,?,?,?)",
                ("all", "region", number, label),
            )
    _index_cache.clear()
    summary = {
        "papers": len(rows),
        "dims": int(vectors.shape[1]),
        "clusters": int(k),
        "model": str(model_path()),
        "vectors": str(vector_path()),
        "seconds": round(time.time() - started, 1),
    }
    log(f"[global] done: {summary}")
    return summary


def _paper_years() -> dict[str, int]:
    """paper_id -> publication year (max over its venue rows), in ONE query.

    A per-paper lookup would issue 73k queries; this is a single grouped scan.
    """
    try:
        with db.connect() as con:
            return {
                row["paper_id"]: int(row["y"])
                for row in con.execute(
                    "SELECT paper_id, max(year) AS y FROM paper_venues GROUP BY paper_id"
                )
                if row["y"] is not None
            }
    except Exception:
        return {}


def _topic_labels(model, docs: list[str], labels: np.ndarray) -> dict[int, str]:
    """Cluster labels from the fitted vocabulary, falling back to 'Topic N'.

    The clusters may come from the dense space while the labels come from a
    TF-IDF vocabulary -- that is deliberate. Naming a cluster is a matter of
    picking the words its papers use most distinctively, which is what a
    TF-IDF matrix is for; where the clusters sit does not change that.
    """
    try:
        if model is None:
            # The dense path: borrow the saved model's vectorizer, which is a
            # TF-IDF over the corpus's vocabulary.
            model = semantics.SemanticModel.load(model_path())
            if model is None:
                raise RuntimeError("no vocabulary for the topic labels")
        tfidf = model.vectorizer.transform(docs)
        labelled = semantics.label_clusters_from_tfidf(tfidf, labels, model.feature_names_, top_k=3)
    except Exception:
        labelled = None
    if labelled:
        return {int(key): str(value) for key, value in labelled.items()}
    return {int(c): f"Topic {int(c) + 1}" for c in np.unique(labels)}


def load_index(*, refresh: bool = False):
    """Return ``(ids, normalized float32 matrix, years, model)``, cached in-process.

    The matrix is what makes whole-corpus semantic search cheap: ranking 70k
    papers is one matrix-vector product instead of reading 70k JSON vectors.
    """
    if not refresh and "index" in _index_cache:
        return _index_cache["index"]
    path = vector_path()
    if not path.exists():
        return None
    model = semantics.SemanticModel.load(model_path())
    if model is None:
        return None
    with np.load(path, allow_pickle=True) as data:
        ids = [str(v) for v in data["ids"]]
        vectors = np.asarray(data["vectors"], dtype=np.float32)
        years = np.asarray(data["years"], dtype=np.int32)
    vectors = _unit(vectors)
    index = (ids, vectors, years, model)
    _index_cache["index"] = index
    return index


def _vocabulary() -> list[str]:
    """The corpus's word list, for the "did you mean" suggestions.

    The dense static model has no features of its own, so the fitted
    TF-IDF model's vocabulary is used: it is a list of the corpus's words,
    which is all the spelling suggestions need.
    """
    model = semantics.SemanticModel.load(model_path())
    if model is None or getattr(model, "feature_names_", None) is None:
        return []
    return [str(v) for v in model.feature_names_]


def search(query: str, *, limit: int = 20, venue: str | None = None,
           suggest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Rank the whole corpus against ``query``; returns id/score/year dicts.

    Hybrid retrieval, fused by Reciprocal Rank Fusion:

    * the **semantic** cosine ranking -- the dense static embeddings when
      they are built, the TF-IDF/SVD vectors otherwise;
    * an **exact phrase** ranking (the whole query inside a title);
    * an **FTS** ranking (all the query's terms, relaxing to the rarest);
    * a **substring** ranking over titles (catches acronyms inside longer
      words, usernames, code links -- the channel that finds "Secilia-Cxy"
      for SECI).

    Every row carries ``mode``: ``semantic`` when the cosine channel ranked at
    all (fused or pure), ``keyword`` when only lexical channels answered, and
    ``unavailable`` when the global index has not been built. ``suggest``, when
    passed, receives vocabulary terms close to the query spelling plus PRF
    expansion words, so the API can offer "did you mean" refinements.
    """
    # The dense static embeddings are the better semantic space; the
    # TF-IDF/SVD vectors are the fallback when they have not been built.
    # The dense index stands on its own, so a missing SVD model does not stop
    # the search.
    bridged = semantics.query_document(query)
    dense = dense_module.load_index()
    model = None
    query_vector = None
    if dense is not None:
        ids, vectors, years = dense
        query_vector = dense_module.embed_query(bridged)
    if query_vector is None:
        index = load_index()
        if index is None:
            return [{"mode": "unavailable"}]
        ids, vectors, years, model = index
        query_vector = np.asarray(model.transform([bridged])[0], dtype=np.float32).ravel()
    year_of = {pid: int(y) for pid, y in zip(ids, years)}
    if query_vector.size != vectors.shape[1] or not np.isfinite(query_vector).all():
        return [{"mode": "unavailable"}]
    norm = float(np.linalg.norm(query_vector))

    terms = [t for t in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(t) >= 2]
    with db.connect() as con:
        # Four lexical channels, each answering a different shape of query:
        #   phrase - the whole query is (part of) a title       [exact]
        #   fts    - every term, relaxing to the rarest ones   [AND]
        #   like   - the term inside a longer word             [substring]
        # plus the semantic channel above.
        phrase_ids = retrieval.phrase_ranking(con, query, limit=max(limit, 30),
                                               venue=venue)
        fts_ids = retrieval.fts_ranking(con, terms, limit=max(limit * 3, 60), venue=venue)
        like_ids = retrieval.like_ranking(con, query, limit=max(limit, 30), venue=venue)

    semantic_ids: list[str] = []
    scores_by_id: dict[str, float] = {}
    if norm > 0:
        scores = vectors @ (query_vector / norm)
        order = np.argsort(-scores)
        for i in order:
            pid = ids[int(i)]
            if venue and not _paper_in_venue(pid, venue):
                continue
            semantic_ids.append(pid)
            scores_by_id[pid] = float(scores[int(i)])
            if len(semantic_ids) >= max(limit * 3, 60):
                break

    # The phrase channel is exact, so it outweighs the channels that merely
    # match some of the terms; the OR-ish FTS channel outweighs the substring
    # guess, which is the vaguest of the four.
    fused = retrieval.rank_fuse(
        [phrase_ids, semantic_ids, fts_ids, like_ids],
        weights=[3.0, 1.0, 1.2, 0.6])
    out: list[dict[str, Any]] = []
    for pid, fused_score in fused[:limit]:
        out.append({"id": pid,
                    "score": round(scores_by_id.get(pid, 0.0), 6),
                    "rrf": round(fused_score, 6),
                    "year": year_of.get(pid, 0),
                    "mode": "semantic" if norm > 0 else "keyword"})

    if suggest is not None:
        try:
            # The "did you mean" row needs a vocabulary to suggest from. The
            # dense model has no features, so the fitted TF-IDF model's
            # vocabulary is used for this -- it is a word list, nothing more.
            vocabulary = [str(v) for v in _vocabulary()]
            suggest["spelling"] = retrieval.suggest_terms(query, vocabulary)
            with db.connect() as con:
                suggest["related"] = retrieval.pseudo_relevance_feedback(
                    terms, [h["id"] for h in out],
                    lambda i: retrieval.fetch_titles(con, i))
        except Exception:
            suggest["spelling"] = []
            suggest["related"] = []
    return out


_venue_membership: dict[str, set[str]] = {}


def _paper_in_venue(paper_id: str, venue: str) -> bool:
    """Membership test with one cached set per venue (73k ids, built once)."""
    members = _venue_membership.get(venue)
    if members is None:
        with db.connect() as con:
            members = {
                row["paper_id"]
                for row in con.execute("SELECT paper_id FROM paper_venues WHERE venue_id=?", (venue,))
            }
        _venue_membership[venue] = members
    return paper_id in members


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    clusters = int(argv[0]) if argv else CLUSTERS
    build(clusters=clusters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())