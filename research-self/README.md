# Paper Graph collector

Python 3.11+ local collector and FastAPI service. By default all durable data is stored in `../Data`, outside this source tree. OpenReview data is public and is fetched only through its public API v2.

```powershell
cd F:\AIWork\paper_graph\research-self
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[test]'
python -m paper_ingest venues
python -m paper_ingest sync --venue iclr --year 2027
python -m paper_ingest sync --venue all --year 2027
python -m paper_ingest sync-years --venue all --from-year 2022 --to-year 2026
python -m paper_ingest status
python -m paper_ingest build-graph --venue iclr --year 2027
uvicorn paper_ingest.api:app --host 127.0.0.1 --port 8000
```

Set `PAPER_DATA_DIR` or `PAPER_DB_PATH` to override the absolute storage path. Run `pytest` for local tests. Graph generation currently uses deterministic CPU TF-IDF/SVD to provide compatible coordinates, vectors and cosine relations; it is a minimum local adapter, not a semantic embedding model or topic-clustering system.

Official published proceedings are now selected for `icml` (PMLR), `acl` (Anthology), `cvpr` (CVF), `neurips` (Proceedings). `iclr` and `tmlr` still use OpenReview. `--limit N` deliberately imports a sample; omit it for the full directory. PMLR full directory mode gets titles/authors/PDF links; use limited runs to fetch abstracts for the first N entries. ACL/NeurIPS/CVF full mode reads individual metadata pages and may take longer. Existing snapshots are cached; subsequent runs recheck them idempotently, without automatic cache freshness yet. Future proceedings return `not_open`, independent of submission availability.

```powershell
python -m paper_ingest sync --venue icml --year 2025
python -m paper_ingest sync --venue acl --year 2025 --limit 10
python -m paper_ingest sync --venue cvpr --year 2026 --limit 5
python -m paper_ingest sync --venue neurips --year 2025 --limit 10
python -m paper_ingest.diagnose_openreview --output ..\Data\logs\openreview-diagnostic.json
python tests\browser_real_data.py
```
