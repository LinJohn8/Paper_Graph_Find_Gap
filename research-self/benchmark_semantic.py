"""Quantify semantic discrimination quality on the real corpus.

Metrics that matter for the user's goal (find related papers fast when
brainstorming an idea):

1. kNN purity      -- for each paper, how often are its nearest neighbours in
                      the same KMeans cluster? (higher = clusters are meaningful)
2. Intra/inter ratio -- mean within-cluster cosine vs mean across-cluster cosine.
                      (higher = better separated)
3. Query probe     -- for a handful of hand-written "idea" queries, report the
                      top-5 retrieved titles so a human can judge relevance.
4. Alias probe     -- same query with acronym vs canonical form should rank
                      near-identically if alias expansion works.
5. Gap sanity      -- number of gaps by type and a sample of titles.

Run:
    cd research-self
    $env:PYTHONPATH=(Resolve-Path .).Path
    & 'E:\\ESP\\_ENV\\tools\\python\\python.exe' benchmark_semantic.py
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np

from paper_ingest import semantics
from paper_ingest.config import DB_PATH


def load_corpus(venue="icml", year=2024):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT p.id,p.title,p.abstract,p.keywords_json
           FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id
           WHERE pv.venue_id=? AND pv.year=? ORDER BY p.id""",
        (venue, year),
    ).fetchall()
    ids, titles, docs = [], [], []
    for r in rows:
        try:
            kws = json.loads(r["keywords_json"]) if r["keywords_json"] else None
        except Exception:
            kws = None
        ids.append(r["id"])
        titles.append(r["title"])
        docs.append(semantics.expand_aliases(semantics.make_doc(r["title"], kws, r["abstract"])))
    return ids, titles, docs


def kmeans_labels(vectors, n_clusters):
    from sklearn.cluster import KMeans

    n_clusters = max(1, min(n_clusters, len(vectors)))
    if n_clusters <= 1:
        return np.zeros(len(vectors), dtype=int)
    return KMeans(n_clusters=n_clusters, random_state=17, n_init=10).fit_predict(vectors)


def knn_purity(vectors, labels, k=5):
    sims = vectors @ vectors.T
    np.fill_diagonal(sims, -np.inf)
    k = min(k, len(vectors) - 1)
    if k < 1:
        return 0.0
    same = 0
    total = 0
    for i in range(len(vectors)):
        nn = np.argsort(-sims[i])[:k]
        same += sum(1 for j in nn if labels[j] == labels[i])
        total += k
    return same / total if total else 0.0


def intra_inter(vectors, labels):
    sims = vectors @ vectors.T
    iu = np.triu_indices(len(vectors), k=1)
    within, across = [], []
    for a, b in zip(*iu):
        (within if labels[a] == labels[b] else across).append(sims[a, b])
    return (float(np.mean(within)) if within else 0.0,
            float(np.mean(across)) if across else 0.0)


PROBES = [
    "graph neural network expressivity and message passing limits",
    "equivariant neural networks for molecular and geometric data",
    "how can diffusion models be made more sample efficient",
    "controlling memorization and privacy leakage of large language models",
    "scaling laws and compute optimal training",
    "rlhf preference optimization alignment",
]


def rank(model, vectors, ids, titles, query, k=5):
    q = semantics.expand_aliases(query)
    qv = model.transform([q])[0]
    sims = vectors @ qv
    order = np.argsort(-sims)[:k]
    return [(titles[i], float(sims[i])) for i in order]


def main():
    ids, titles, docs = load_corpus()
    n = len(ids)
    print(f"corpus: {n} ICML 2024 papers")
    if n < 8:
        print("corpus too small for meaningful benchmark")
        return

    model = semantics.SemanticModel(dims=48).fit(docs)
    vectors = model.transform(docs)
    print(f"model: features={model.feature_names_.shape[0]} vectors={vectors.shape}")

    n_clusters = min(24, max(2, int(np.sqrt(n / 40))))
    labels = kmeans_labels(vectors, n_clusters)
    print(f"clusters: {n_clusters} (sizes: {np.bincount(labels).tolist()})")

    pur = knn_purity(vectors, labels, k=5)
    w, a = intra_inter(vectors, labels)
    print(f"kNN purity@5      : {pur:.3f}   (higher=clusters align with neighbours)")
    print(f"intra-cluster cos : {w:.3f}")
    print(f"inter-cluster cos : {a:.3f}")
    print(f"separation ratio  : {w / a if a else float('inf'):.3f}   (higher=better)")

    print("\n=== query probes (top-5 titles) ===")
    for q in PROBES:
        print(f"\nQ: {q}")
        for title, score in rank(model, vectors, ids, titles, q):
            print(f"   {score:+.3f}  {title[:95]}")

    print("\n=== alias invariance probe ===")
    for acro, canon in [("GNN expressivity", "graph neural network expressivity"),
                        ("LLM memorization", "large language model memorization")]:
        ra = [t for t, _ in rank(model, vectors, ids, titles, acro)]
        rc = [t for t, _ in rank(model, vectors, ids, titles, canon)]
        overlap = len(set(ra) & set(rc))
        print(f"  {acro!r} vs {canon!r}: top-5 overlap = {overlap}/5")

    print("\n=== gap sanity ===")
    top_terms = {}
    try:
        tfidf = model.vectorizer.transform(docs)
        labels_str = semantics.label_clusters_from_tfidf(
            tfidf, labels, model.feature_names_, top_k=3
        )
        top_terms = {c: [s.strip() for s in v.split(" · ") if s.strip()] for c, v in labels_str.items()}
        print("cluster labels:")
        for c in sorted(top_terms):
            print(f"  [{c}] size={int((labels == c).sum()):3d}  {labels_str[c][:100]}")
    except Exception as exc:
        print("labeling failed:", type(exc).__name__, exc)

    abstracts = [" ".join(d.split()) for d in docs]
    gaps = semantics.detect_gaps(vectors, labels, abstracts, ids, titles, top_terms)
    from collections import Counter

    print(f"\ngaps: {len(gaps)}  by type: {dict(Counter(g['type'] for g in gaps))}")
    for g in gaps[:5]:
        print(f"  [{g['type']:6}] score={g['score']:.3f}  {g['title'][:88]}")


if __name__ == "__main__":
    main()
