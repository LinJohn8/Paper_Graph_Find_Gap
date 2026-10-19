"""Final end-to-end effect report on the REAL 400-paper corpus.

Builds the graph for real, then exercises the real API endpoints and prints
before/after-relevant numbers plus concrete examples.
"""
from __future__ import annotations

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from paper_ingest import api, service
from paper_ingest.config import DB_PATH


def main():
    t0 = time.time()
    res = service.generate_graph("icml", 2024)
    print("=== generate_graph ===")
    print(json.dumps(res, indent=2))
    print(f"elapsed: {time.time()-t0:.1f}s\n")

    client = TestClient(api.app)

    print("=== POST /api/papers/semantic ===")
    queries = [
        "graph neural network expressivity and message passing limits",
        "equivariant networks for molecular and geometric data",
        "sample efficient diffusion models",
        "rlhf preference optimization alignment",
    ]
    for q in queries:
        r = client.post("/api/papers/semantic", json={"query": q, "venue": "icml", "year": 2024, "limit": 5})
        body = r.json()
        print(f"\nQ: {q}")
        print(f"   method={body.get('method')} items={len(body.get('items', []))}")
        for it in body.get("items", [])[:5]:
            print(f"   {it['score']:+.3f}  {it['title'][:88]}")

    print("\n=== GET /api/gaps ===")
    r = client.get("/api/gaps", params={"venue": "icml", "year": 2024})
    body = r.json()
    gaps = body.get("gaps", [])
    topics = body.get("topics", [])
    from collections import Counter

    print(f"gaps={len(gaps)}  by type={dict(Counter(g['type'] for g in gaps))}")
    print(f"topics={len(topics)}")
    for t in topics:
        print(f"   [{t['number']}] size={t['size']:3d}  label={t['label'][:80]}  terms={t['top_terms'][:4]}")
    print("\n  sample gaps:")
    for g in gaps[:6]:
        print(f"   [{g['type']:6}] {g['score']:.2f}  {g['title'][:80]}")
        print(f"            {g['explanation'][:110]}")

    print("\n=== GET /api/papers/{id} (rqs / derived_questions) ===")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT p.id FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id
           WHERE pv.venue_id='icml' AND pv.year=2024 ORDER BY p.id LIMIT 40"""
    ).fetchall()
    shown = 0
    for row in rows:
        r = client.get(f"/api/papers/{row['id']}")
        if r.status_code != 200:
            continue
        p = r.json()
        dq = p.get("derived_questions") or []
        gq = p.get("gap_cues") or []
        if dq or gq:
            print(f"\n[{p['title'][:80]}]")
            for q in dq[:2]:
                print(f"   DERIVED Q: {q[:160]}")
            for g in gq[:2]:
                print(f"   GAP CUE  : {g[:160]}")
            for q in (p.get("rqs") or [])[:1]:
                print(f"   RQ       : {q[:160]}")
            shown += 1
        if shown >= 5:
            break

    # Aggregate counts over the whole corpus.
    with_dq = with_gap = 0
    for row in con.execute(
        """SELECT p.id FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id
           WHERE pv.venue_id='icml' AND pv.year=2024"""
    ):
        p = client.get(f"/api/papers/{row['id']}").json()
        if p.get("derived_questions"):
            with_dq += 1
        if p.get("gap_cues"):
            with_gap += 1
    total = len(rows) if False else con.execute(
        "SELECT count(DISTINCT p.id) FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id "
        "WHERE pv.venue_id='icml' AND pv.year=2024"
    ).fetchone()[0]
    print(f"\n=== corpus coverage (n={total}) ===")
    print(f"  papers with >=1 gap cue          : {with_gap} ({with_gap/total:.1%})")
    print(f"  papers with >=1 derived question : {with_dq} ({with_dq/total:.1%})")


if __name__ == "__main__":
    main()
