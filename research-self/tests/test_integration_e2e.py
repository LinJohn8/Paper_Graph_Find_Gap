"""Independent end-to-end check of the semantic/gap API against an isolated DB.

This is the Team Lead's own verification, separate from the teammates' unit
tests. It stands up a temp SQLite + temp DATA_ROOT, generates a graph, then hits
the real FastAPI endpoints through TestClient to assert the JSON contracts that
the frontend (acceptodds) depends on.

Each test runs in a *subprocess* on purpose. `paper_ingest.config` reads
`PAPER_DB_PATH` / `PAPER_DATA_DIR` / `PAPER_CONFIG` at import time, so exercising
the API against a temp database in-process requires `importlib.reload`, which
mutates the global module state and leaks into every other test module in the
same pytest run (that made `tests/test_ingest.py` fail only in combined runs).
Isolating each case in a child process keeps this file from polluting anything.

Run:
    python -m pytest research-self/tests/test_integration_e2e.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RESEARCH_SELF = Path(__file__).resolve().parents[1]

FIXTURE = [
    {
        "id": "p01", "source": "test", "source_paper_id": "s01", "title":
        "Large language model reasoning with chain of thought",
        "abstract": ("We propose a chain-of-thought method for LLMs. "
                     "How does in-context learning scale to long contexts? "
                     "Future work should combine reasoning and retrieval."),
        "authors": ["A. Alpha"], "keywords": ["llm", "reasoning"],
        "publication_year": 2027,
    },
    {
        "id": "p02", "source": "test", "source_paper_id": "s02", "title":
        "Reinforcement learning from human feedback for alignment",
        "abstract": ("We introduce an RLHF pipeline. Limited understanding remains "
                     "about reward hacking under distribution shift. This paper "
                     "studies preference optimization."),
        "authors": ["B. Beta"], "keywords": ["rlhf", "alignment"],
        "publication_year": 2027,
    },
    {
        "id": "p03", "source": "test", "source_paper_id": "s03", "title":
        "Diffusion models for image generation",
        "abstract": ("We present a diffusion model for high-fidelity image "
                     "generation. Remains an open problem for few-step sampling."),
        "authors": ["C. Gamma"], "keywords": ["diffusion", "generation"],
        "publication_year": 2027,
    },
    {
        "id": "p04", "source": "test", "source_paper_id": "s04", "title":
        "Graph neural networks for molecular property prediction",
        "abstract": ("Our goal is message passing on graphs. Under-explored area: "
                     "out-of-distribution generalization of GNNs."),
        "authors": ["D. Delta"], "keywords": ["gnn", "molecules"],
        "publication_year": 2027,
    },
    {
        "id": "p05", "source": "test", "source_paper_id": "s05", "title":
        "Retrieval augmented generation with knowledge graphs",
        "abstract": ("We develop RAG over a knowledge graph. Little work connects "
                     "retrieval and graph reasoning; this remains challenging."),
        "authors": ["E. Epsilon"], "keywords": ["rag", "knowledge graph"],
        "publication_year": 2027,
    },
]


_BOOTSTRAP = """
import json, os, sys
from pathlib import Path

from paper_ingest import db as dbmod
dbmod.init_db()
with dbmod.connect() as con:
    con.execute(
        "INSERT INTO venues(id,name,year,provider,type,availability_status) "
        "VALUES('testvenue','Test Venue',2027,'test','conference','unknown')"
    )
    for p in json.loads(os.environ["PG_FIXTURE"]):
        row = {
            **p,
            "forum_id": None, "primary_area": None, "pdf_url": None,
            "paper_url": None, "doi": None, "arxiv_id": None,
            "submission_year": 2027, "status": "accepted", "decision": "accept",
            "created_at_source": None, "updated_at_source": None,
            "first_seen_at": "2027-01-01", "last_seen_at": "2027-01-01",
            "raw_data_path": None,
        }
        dbmod.upsert_paper(con, row, "testvenue", 2027)
"""

_SCRIPT = """
import json, os, sys
from fastapi.testclient import TestClient
from paper_ingest import api, service

mode = os.environ["PG_MODE"]
client = TestClient(api.app)

if mode == "generate":
    res = service.generate_graph("testvenue", 2027)
    model_file = service.DATA_ROOT / "graph" / "testvenue_2027_semantic.pkl"
    # Single JSON line: _run() parses the last stdout line only.
    print(json.dumps({**res, "model_exists": model_file.exists()}))

elif mode == "gaps":
    service.generate_graph("testvenue", 2027)
    r = client.get("/api/gaps", params={"year": 2027})
    print(json.dumps({"status": r.status_code, "body": r.json()}))

elif mode == "semantic":
    service.generate_graph("testvenue", 2027)
    r = client.post("/api/papers/semantic",
                    json={"query": "how do large language models reason",
                          "venue": "testvenue", "year": 2027, "limit": 5})
    print(json.dumps({"status": r.status_code, "body": r.json()}))

elif mode == "fallback":
    r = client.post("/api/papers/semantic",
                    json={"query": "diffusion", "year": 2027, "limit": 5})
    print(json.dumps({"status": r.status_code, "body": r.json()}))

elif mode == "paper":
    r = client.get("/api/papers/p01")
    print(json.dumps({"status": r.status_code, "body": r.json()}))
"""


def _run(mode: str, tmp_path: Path) -> dict:
    """Run one API scenario in an isolated subprocess with its own DB."""
    venues_yaml = tmp_path / "venues.yaml"
    venues_yaml.write_text(
        "venues:\n"
        "  testvenue:\n"
        "    name: Test Venue\n"
        "    provider: test\n"
        "    type: conference\n"
        "    year_rule: 2027\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(RESEARCH_SELF)
    env["PAPER_DB_PATH"] = str(tmp_path / "database" / "papers.sqlite")
    env["PAPER_DATA_DIR"] = str(tmp_path)
    env["PAPER_CONFIG"] = str(venues_yaml)
    env["PG_FIXTURE"] = json.dumps(FIXTURE)
    env["PG_MODE"] = mode
    env["PYTHONIOENCODING"] = "utf-8"

    script = textwrap.dedent(_BOOTSTRAP + _SCRIPT)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(RESEARCH_SELF), env=env,
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        pytest.fail(f"subprocess failed (mode={mode}):\n{proc.stdout}\n{proc.stderr}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        pytest.fail(f"no output (mode={mode}):\n{proc.stderr}")
    return json.loads(lines[-1])


def test_generate_graph_and_persist(tmp_path):
    out = _run("generate", tmp_path)
    assert out["papers"] == len(FIXTURE)
    assert out["status"] == "generated"
    assert out["model_exists"] is True


def test_gaps_endpoint_contract(tmp_path):
    out = _run("gaps", tmp_path)
    assert out["status"] == 200
    body = out["body"]
    assert {"gaps", "topics"} <= set(body.keys())
    for g in body["gaps"]:
        assert {"type", "title", "explanation", "score", "paper_ids", "papers"} <= set(g.keys())
        for p in g["papers"]:
            assert {"id", "title", "abstract"} <= set(p.keys())
    for t in body["topics"]:
        assert {"number", "label", "size", "top_terms"} <= set(t.keys())
        assert isinstance(t["top_terms"], list)


def test_semantic_search_contract(tmp_path):
    out = _run("semantic", tmp_path)
    assert out["status"] == 200
    body = out["body"]
    # The per-year path is now a hybrid (semantic + lexical channels, RRF-fused).
    assert body["method"] == "hybrid-rrf"
    assert "items" in body and body["items"]
    for it in body["items"]:
        assert {"id", "slug", "title", "abstract", "authors", "venues", "year", "score"} <= set(it.keys())
        assert isinstance(it["score"], (int, float))
        assert isinstance(it["year"], int)


def test_semantic_search_fallback_contract(tmp_path):
    # No graph generated -> keyword fallback, still contract-shaped.
    out = _run("fallback", tmp_path)
    assert out["status"] == 200
    body = out["body"]
    assert body["method"] == "keyword-fallback"
    for it in body["items"]:
        assert {"id", "slug", "title", "abstract", "authors", "venues", "year", "score"} <= set(it.keys())


def test_paper_enrichment_contract(tmp_path):
    out = _run("paper", tmp_path)
    assert out["status"] == 200
    body = out["body"]
    assert "rqs" in body and isinstance(body["rqs"], list)
    assert "gap_cues" in body and isinstance(body["gap_cues"], list)
    assert "derived_questions" in body and isinstance(body["derived_questions"], list)
