from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import db
from .config import DATA_ROOT, venues
from .openreview import OpenReview, normalize, raw_path, write_raw
from . import manifest
from . import semantics
from . import years


def venue_id(key: str, config: dict[str, Any], year: int) -> str:
    pattern = config.get("venue_id_pattern")
    return pattern.format(year=year) if pattern else ""


def apply_coverage_check(venue: str, year: int, *, job_id: int | None = None,
                         con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Compare the stored size of one venue-year with its recorded expectation.

    WHY THIS EXISTS
    ---------------
    A harvest can come back small and still report success: a provider changes
    its markup, a rate limiter starts answering 401, a volume page is served
    truncated.  Nothing raises, so the corpus quietly loses a venue-year.  This
    check turns that silence into a recorded fact.

    The verdict is written onto the ``sync_jobs`` row (``expected_count`` holds
    the floor the count was checked against, ``coverage_status`` the verdict),
    so the API can show the state without re-running the comparison.

    A MISSING OR BROKEN MANIFEST IS NOT AN ERROR HERE
    --------------------------------------------------
    The manifest is optional, so an absent file yields an ``unknown`` verdict
    and a present-but-unreadable one is caught by the caller.  A coverage
    check must never turn a successful harvest into an exception: a short
    harvest is a *reported* outcome, not a crash.
    """
    expectations = manifest.load_manifest()
    owned = con is None
    if owned:
        # The coverage columns are added by init_db(); without it a database
        # that has not been touched yet would have no sync_jobs table at all.
        db.init_db()
    connection = con if con is not None else db.connect()
    try:
        actual = connection.execute(
            "SELECT count(DISTINCT paper_id) FROM paper_venues WHERE venue_id=? AND year=?",
            (venue, year),
        ).fetchone()[0]
        verdict = manifest.check_venue(venue, year, int(actual), manifest=expectations)
        # `expected_count` is the floor the count was compared against: the
        # number below which the harvest counts as short.
        expected_count = verdict.get("min_papers")
        if job_id is not None:
            connection.execute(
                "UPDATE sync_jobs SET expected_count=?,coverage_status=? WHERE id=?",
                (expected_count, verdict["status"], job_id),
            )
        else:
            # No job id at hand: the newest row of this venue-year is the run
            # that just finished.
            connection.execute(
                "UPDATE sync_jobs SET expected_count=?,coverage_status=? WHERE id=("
                "SELECT id FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 1)",
                (expected_count, verdict["status"], venue, year),
            )
        if owned:
            connection.commit()
    finally:
        if owned:
            connection.close()
    return verdict


def sync_official(venue: str, year: int, limit: int | None = None, dry_run: bool = False,
                  workers: int | None = None) -> dict[str, Any]:
    """Harvest an official proceedings venue-year and check the coverage.

    The scraping itself lives in :mod:`paper_ingest.official`; this wrapper
    adds the one thing the raw harvester cannot do by itself: compare the
    number of stored papers with the expectation for that venue-year, so a
    truncated or blocked harvest is reported instead of passing unnoticed.

    The coverage verdict is added to the result under ``coverage`` and
    recorded on the ``sync_jobs`` row.  A failing check never fails the
    harvest: the verdict is the deliverable, not an error.
    """
    from .official import sync_official as _sync_official

    result = _sync_official(venue, year, limit=limit, dry_run=dry_run, workers=workers)
    if dry_run or limit is not None:
        # A dry run stores nothing and a limited run stores part of the
        # volume on purpose; both would report a bogus "short" verdict.
        return result
    if result.get("status") not in ("success", "completed"):
        return result
    try:
        result["coverage"] = apply_coverage_check(venue, year)
    except Exception:  # noqa: BLE001 - a coverage check must never fail a harvest
        pass
    return result


async def sync_one(key: str, year: int, *, full: bool = False, dry_run: bool = False,
                   client: OpenReview | None = None) -> dict[str, Any]:
    registry = venues()
    if key not in registry:
        raise ValueError(f"Unknown venue: {key}")
    cfg = registry[key]
    provider = cfg["provider"]
    api = client or OpenReview()
    close = client is None
    started = datetime.now(timezone.utc).isoformat()
    counters = {"fetched_count": 0, "inserted_count": 0, "updated_count": 0, "skipped_count": 0, "error_count": 0}
    status = "running"
    error_message = None
    source_id = venue_id(key, cfg, year)
    group = None
    invitation = None
    job_id = None
    job_id = None
    try:
        if source_id:
            group = await api.group(source_id)
        else:
            prefix = cfg.get("discovery_prefix", "").format(year=year)
            groups = await api.discover(prefix)
            candidates = [g for g in groups if g.get("id") and ("Conference" in g.get("id", "") or key == "tmlr")]
            group = sorted(candidates, key=lambda g: ("Conference" not in g["id"], g["id"]))[0] if candidates else None
            source_id = group.get("id") if group else None
        if not group:
            status = "not_open" if source_id or year >= 2027 else "unavailable"
            message = "Venue group is not published in OpenReview yet" if status == "not_open" else "No public venue group found"
            if not dry_run:
                db.init_db()
                with db.connect() as con:
                    db.put_venue(con, venue_id=key, name=cfg["name"], year=year, provider=provider,
                                 source_venue_id=None, kind=cfg["type"], source_url=cfg.get("website") or cfg.get("website_pattern", "").format(year=year) or None,
                                 availability=status)
                    con.execute("INSERT INTO sync_jobs(venue,year,provider,status,started_at,finished_at,error_message) VALUES(?,?,?,?,?,?,?)",
                                (key,year,provider,status,started,datetime.now(timezone.utc).isoformat(),message))
            return {"venue": key, "year": year, "status": status, "source_venue_id": None, "message": message, **counters}
        content = group.get("content", {})
        content = {k: (v.get("value") if isinstance(v, dict) else v) for k,v in content.items()}
        invitation = content.get("submission_venue_id") or content.get("submission_id")
        if not invitation:
            status = "restricted"
            error_message = "Venue exists but exposes no public submission invitation"
            if not dry_run:
                db.init_db()
                with db.connect() as con:
                    db.put_venue(con,venue_id=key,name=cfg["name"],year=year,provider=provider,source_venue_id=source_id,
                                 kind=cfg["type"],source_url=cfg.get("website"),availability=status)
                    con.execute("INSERT INTO sync_jobs(venue,year,provider,status,started_at,finished_at,error_count,error_message) VALUES(?,?,?,?,?,?,1,?)",
                                (key,year,provider,status,started,datetime.now(timezone.utc).isoformat(),error_message))
            return {"venue":key,"year":year,"status":status,"source_venue_id":source_id,"message":error_message,**counters}
        db.init_db()
        with db.connect() as con:
            db.put_venue(con, venue_id=key, name=cfg["name"], year=year, provider=provider,
                         source_venue_id=source_id, kind=cfg["type"],
                         source_url=cfg.get("website") or cfg.get("website_pattern", "").format(year=year) or None,
                         availability="available")
            cursor = con.execute("INSERT INTO sync_jobs(venue,year,provider,status,started_at) VALUES(?,?,?,?,?)",
                                 (key,year,provider,"running",started))
            job_id = cursor.lastrowid
        async for note in api.notes(invitation):
            counters["fetched_count"] += 1
            paper = normalize(note, year, provider)
            path = raw_path(DATA_ROOT,key,year,paper["forum_id"])
            write_raw(path,note)
            paper["raw_data_path"] = str(path)
            if not paper["title"] or paper["title"] == "Untitled submission":
                counters["skipped_count"] += 1
                continue
            if not dry_run:
                with db.connect() as con:
                    outcome = db.upsert_paper(con,paper,key,year)
                counters[outcome+"_count"] += 1
                try:
                    reviews = await api.public_reviews(paper["forum_id"])
                    for review in reviews:
                        rc = review.get("content", {})
                        flat = {k:(v.get("value") if isinstance(v,dict) else v) for k,v in rc.items()}
                        rtype = "meta_review" if any(k in str(review.get("invitation", "")).lower() for k in ("meta_review","decision")) else "review"
                        review_id = str(review.get("id", ""))
                        rid = "review:" + hashlib.sha256(f"{paper['id']}:{review_id}".encode()).hexdigest()
                        rpath = path.parent / (path.stem + "_" + re.sub(r"[^A-Za-z0-9_.-]", "_", review_id) + ".review.json")
                        write_raw(rpath,review)
                        with db.connect() as con:
                            con.execute("""INSERT INTO reviews(id,paper_id,source_review_id,review_type,content,scores_json,
                              source_created_at,source_updated_at,visibility,raw_data_path) VALUES(?,?,?,?,?,?,?,?,?,?)
                              ON CONFLICT(paper_id,source_review_id) DO UPDATE SET content=excluded.content,
                              scores_json=excluded.scores_json,source_updated_at=excluded.source_updated_at,
                              visibility=excluded.visibility,raw_data_path=excluded.raw_data_path""",
                              (rid,paper["id"],review_id,rtype,str(flat.get("review") or flat.get("summary") or flat.get("decision") or ""),
                               json.dumps({k:v for k,v in flat.items() if k not in ("review","summary","decision")},ensure_ascii=False),
                               review.get("cdate"),review.get("mdate"),"public",str(rpath)))
                except Exception:
                    counters["error_count"] += 1
        status = "completed" if counters["fetched_count"] else "empty"
        if not dry_run:
            with db.connect() as con:
                con.execute("UPDATE venues SET availability_status=?,last_sync_at=? WHERE id=? AND year=?",
                            ("available" if counters["fetched_count"] else "empty",datetime.now(timezone.utc).isoformat(),key,year))
                con.execute("UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,inserted_count=?,updated_count=?,skipped_count=?,error_count=? WHERE id=?",
                            (status,datetime.now(timezone.utc).isoformat(),counters["fetched_count"],counters["inserted_count"],counters["updated_count"],counters["skipped_count"],counters["error_count"],job_id))
                con.execute("INSERT INTO sync_state(venue,year,cursor,checkpoint,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(venue,year) DO UPDATE SET cursor=excluded.cursor,checkpoint=excluded.checkpoint,updated_at=excluded.updated_at",
                            (key,year,None,datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))
        result = {"venue":key,"year":year,"status":status,"source_venue_id":source_id,"invitation":invitation,**counters}
        if status == "completed" and not dry_run:
            # Compare the harvested size with the recorded expectation. A short
            # harvest is a reported outcome, never an exception, so the check
            # is guarded: a broken manifest must not fail a good harvest.
            try:
                result["coverage"] = apply_coverage_check(key, year, job_id=job_id)
            except Exception:  # noqa: BLE001 - a coverage check must never fail a harvest
                pass
            result["graph"] = generate_graph(key, year)
        return result
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        # A live venue with a public group but a challenge/permission response
        # is distinct from an outage or an unpublished venue.
        status = "restricted" if ("403" in error_message or "Challenge" in error_message or "challenge" in error_message) else "failed"
        if not dry_run:
            db.init_db()
            with db.connect() as con:
                if job_id is not None:
                    con.execute("UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,inserted_count=?,updated_count=?,skipped_count=?,error_count=1,error_message=? WHERE id=?",
                                (status,datetime.now(timezone.utc).isoformat(),counters["fetched_count"],counters["inserted_count"],counters["updated_count"],counters["skipped_count"],error_message,job_id))
                    con.execute("UPDATE venues SET availability_status=? WHERE id=? AND year=?",
                                ("restricted" if "403" in error_message else "error",key,year))
                else:
                    con.execute("INSERT INTO sync_jobs(venue,year,provider,status,started_at,finished_at,error_count,error_message) VALUES(?,?,?,?,?,?,?,?)",
                                (key,year,provider,status,started,datetime.now(timezone.utc).isoformat(),1,error_message))
        return {"venue":key,"year":year,"status":status,"source_venue_id":source_id or None,"message":error_message,**counters,"error_count":1}
    finally:
        if close:
            await api.close()


async def sync_many(keys: list[str], year: int, **options: Any) -> list[dict[str, Any]]:
    results = []
    for key in keys:
        if key in ("icml", "acl", "cvpr", "neurips"):
            # The service-level wrapper adds the coverage check to the raw
            # harvester, so an official import that came back short is reported.
            try:
                result = sync_official(key, year, limit=options.get("limit"), dry_run=options.get("dry_run",False))
                # Keep the map in sync with every successful official import.
                # Limited/dry runs intentionally do not rebuild a partial map.
                if result.get("status") in ("success", "completed") and not options.get("dry_run") and not options.get("limit"):
                    result["graph"] = generate_graph(key, year)
                results.append(result)
            except Exception as exc:
                results.append({"venue":key,"year":year,"provider":"pmlr","status":"failed","error":f"{type(exc).__name__}: {exc}","fetched_count":0,"inserted_count":0,"updated_count":0,"skipped_count":0,"error_count":1})
            continue
        results.append(await sync_one(key,year,**{k:v for k,v in options.items() if k != 'limit'}))
    return results


def list_papers(*, venue: str | None = None, year: int | str | None = None, keyword: str | None = None,
                status: str | None = None, page: int = 1, page_size: int = 50, sort: str = "recent") -> dict[str, Any]:
    """Paginated paper list.

    2026-10 performance fix (corpus grew 73k -> 195k with the journal papers):
    venue/year filtering used to be a correlated ``EXISTS`` subquery per paper
    row, which SQLite can only answer by scanning all 195k papers and probing
    ``paper_venues`` for each one -- 112s for a single venue-year page. The
    ``paper_venues`` index is ``(venue_id, year)``, so the filter set is far
    smaller than the table it was filtering; making it the *driving* table
    (``JOIN`` instead of ``EXISTS``) turns that into one index range scan over
    the matching rows plus a primary-key lookup per row.

    The ``EXISTS`` form is kept only for the unfiltered case, where it is not a
    bottleneck and the simpler SQL is easier to reason about.
    """
    # `year="all"` is the whole corpus: no year predicate at all. Anything else
    # resolves to a concrete year exactly as before.
    all_years = years.is_all(year)
    if not all_years:
        year = years.resolve(year)
    venue = None if venue == "all" else venue
    db.init_db()
    clauses, args = [], []
    # When the venue/year filter is selective enough to drive the query, we join
    # paper_venues instead of filtering papers row by row.
    drive_join = ""
    if venue and not all_years:
        # Both filters live in the (venue_id, year) index: ideal driving table.
        drive_join = " JOIN paper_venues pv ON pv.paper_id=p.id AND pv.venue_id=? AND pv.year=?"
        args.extend([venue, year])
    elif venue and all_years:
        # venue_id is the index prefix, so a year-less venue filter still uses it.
        drive_join = " JOIN paper_venues pv ON pv.paper_id=p.id AND pv.venue_id=?"
        args.append(venue)
    elif not all_years:
        # Year-only: paper_venues_year_only_idx serves this as a driving table.
        drive_join = " JOIN paper_venues pv ON pv.paper_id=p.id AND pv.year=?"
        args.append(year)
    if status:
        clauses.append("p.status=?"); args.append(status)
    join = ""
    if keyword:
        clauses.append("p.id IN (SELECT paper_id FROM papers_fts WHERE papers_fts MATCH ?)")
        args.append(keyword.replace('"',''))
    # With `year=all` and no venue every predicate is optional, which leaves the
    # clause list empty; emitting a bare "WHERE" there is a syntax error.
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order = {"recent":"p.updated_at_source DESC,p.title COLLATE NOCASE", "title":"p.title COLLATE NOCASE", "year":"coalesce(p.publication_year,p.submission_year) DESC"}.get(sort,"p.updated_at_source DESC,p.title COLLATE NOCASE")
    # DISTINCT is needed for the unfiltered/EXISTS forms where a paper can match
    # multiple years; the JOIN form already returns one row per paper.
    distinct = "DISTINCT " if not drive_join else ""
    with db.connect() as con:
        total = con.execute("SELECT count(" + distinct + "p.id) FROM papers p"+drive_join+join+where,args).fetchone()[0]
        rows = con.execute("SELECT "+distinct+"p.* FROM papers p"+drive_join+join+where+f" ORDER BY {order} LIMIT ? OFFSET ?",args+[page_size,(page-1)*page_size]).fetchall()
        output=[]
        for row in rows:
            item=db.paper_dict(row)
            item["venues"]=[dict(v) for v in con.execute("SELECT v.id,v.name,pv.year,pv.relation_type FROM paper_venues pv JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year WHERE pv.paper_id=? ORDER BY pv.year DESC,v.name",(item["id"],)).fetchall()]
            output.append(item)
    return {"items":output,"total":total,"page":page,"page_size":page_size,"pages":max(1,(total+page_size-1)//page_size)}


#: Payload cache for the map endpoint. The browser re-requests the graph on mount,
#: on filter changes and on focus; rebuilding a 5-70k point payload per request is
#: the single biggest cost on this path. Entries hold only the vector-free payload
#: (vectors would be tens of MB for the whole corpus and are never sent to the map).
_GRAPH_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
GRAPH_CACHE_TTL = 600.0
GRAPH_CACHE_MAX = 8


def clear_graph_cache() -> None:
    """Drop cached graph payloads (used by tests and after a corpus rebuild)."""
    _GRAPH_CACHE.clear()


def graph(venue: str | None = None, year: int | str | None = None, *,
          include_vectors: bool = False, use_cache: bool = True) -> dict[str, Any]:
    """Map payload for one venue-year, or for the whole corpus when ``year="all"``.

    The base payload deliberately omits ``vectors`` (48 floats per paper, ~44x the
    rest of the payload) and ``titles``; the map fetches titles lazily and only the
    minimap needs vectors. ``include_vectors=True`` restores both for internal
    callers and the explicit ``?include=full`` request.
    """
    all_years = years.is_all(year)
    if not all_years:
        year = years.resolve(year)
    venue = None if venue == "all" else venue
    cache_key = ("graph", venue or "all", years.ALL_YEARS if all_years else year)
    if use_cache and not include_vectors:
        hit = _GRAPH_CACHE.get(cache_key)
        if hit is not None and time.time() - hit[0] < GRAPH_CACHE_TTL:
            return hit[1]
    db.init_db()
    with db.connect() as con:
        if all_years:
            # One globally fitted layout (graph_points_global): per-venue-year
            # layouts live in different SVD bases and cannot be overlaid.
            sql=("""SELECT p.id,p.title,p.primary_area,g.x,g.y,g.region,g.cluster,g.vector_json,
                    (SELECT max(pv2.year) FROM paper_venues pv2 WHERE pv2.paper_id=p.id) AS point_year
                    FROM graph_points_global g JOIN papers p ON p.id=g.paper_id WHERE 1=1""")
            args: list[Any]=[]
            if venue:
                sql+=" AND EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue_id=?)"
                args.append(venue)
            sql+=" ORDER BY p.id"
            rows=con.execute(sql,args).fetchall()
            topic_venue=venue or "all"
        else:
            sql="""SELECT DISTINCT p.id,p.title,p.primary_area,g.x,g.y,g.region,g.cluster,g.vector_json
              FROM graph_points g JOIN papers p ON p.id=g.paper_id JOIN paper_venues pv ON pv.paper_id=p.id WHERE pv.year=?"""
            args=[year]
            if venue:
                sql+=" AND pv.venue_id=?";args.append(venue)
            sql+=" ORDER BY p.id"
            rows=con.execute(sql,args).fetchall()
            topic_venue=venue
        kinds=sorted({r["primary_area"] for r in rows if r["primary_area"]})
        area={v:i for i,v in enumerate(kinds)}
        topics={"region":[],"cluster":[]}
        if topic_venue:
            for level in topics:
                records=con.execute("SELECT DISTINCT number,label FROM graph_topics WHERE venue_id=? AND level=? ORDER BY number",(topic_venue,level))
                topics[level]=[dict(r) for r in records]
    payload: dict[str, Any] = {
        "kind": venue or "all",
        "slugs":[r["id"] for r in rows],
        # Four decimals is ~1/10000 of the map's full span: visually exact at any
        # zoom, and it roughly halves the payload compared with full float repr
        # (73,675 points x 2 axes).
        "x":[round(float(r["x"]),4) for r in rows],
        "y":[round(float(r["y"]),4) for r in rows],
        "region":[r["region"] for r in rows],
        "cluster":[r["cluster"] for r in rows],
        "areas":kinds,
        "area":[area.get(r["primary_area"]) for r in rows],
        "regions":topics["region"],
        "clusters":topics["cluster"],
        "year_mode":"all" if all_years else "single",
    }
    if all_years:
        payload["years"]=[r["point_year"] for r in rows]
    if include_vectors:
        payload["titles"]={r["id"]:r["title"] for r in rows}
        payload["vectors"]=[json.loads(r["vector_json"]) if r["vector_json"] else None for r in rows]
    if use_cache and not include_vectors:
        if len(_GRAPH_CACHE) >= GRAPH_CACHE_MAX:
            _GRAPH_CACHE.clear()
        _GRAPH_CACHE[cache_key] = (time.time(), payload)
    return payload


def graph_titles(venue: str | None = None, year: int | str | None = None,
                 slugs: list[str] | None = None) -> dict[str, str]:
    """Titles for a graph scope, optionally only for ``slugs``.

    Large maps cannot afford the full title map up front, so the browser asks for
    the handful of slugs it is about to label; the filter happens in SQL rather
    than by building the whole dict and discarding it.
    """
    all_years = years.is_all(year)
    if not all_years:
        year = years.resolve(year)
    venue = None if venue == "all" else venue
    db.init_db()
    wanted = [s for s in (slugs or []) if s][:500]
    with db.connect() as con:
        clauses: list[str] = []
        args: list[Any] = []
        if all_years:
            clauses.append("EXISTS (SELECT 1 FROM graph_points_global g WHERE g.paper_id=p.id)")
        else:
            clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.year=?)")
            args.append(year)
        if venue:
            clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue_id=?)")
            args.append(venue)
        if wanted:
            clauses.append(f"p.id IN ({','.join('?' * len(wanted))})")
            args.extend(wanted)
        sql = "SELECT p.id, p.title FROM papers p WHERE " + " AND ".join(clauses)
        return {row["id"]: row["title"] for row in con.execute(sql, args)}


def _graph_docs(rows: list[sqlite3.Row]) -> list[str]:
    """Title/keyword-weighted documents, alias-expanded, exactly as fitted."""
    docs: list[str] = []
    for r in rows:
        try:
            kws = json.loads(r["keywords_json"]) if r["keywords_json"] else None
        except Exception:
            kws = None
        try:
            docs.append(semantics.expand_aliases(semantics.make_doc(r["title"], kws, r["abstract"])))
        except Exception:
            docs.append(f"{r['title'] or ''} {r['abstract'] or ''}")
    return docs


def _project_2d(vectors: "np.ndarray") -> "np.ndarray":
    """Project the *same* fitted semantic vectors to 2D.

    Both branches consume the normalized 48-dim vectors that the similarity
    ranking also uses, so the map and the neighbour structure share one
    representation (previously a separate ``SemanticModel(dims=2)`` fit made the
    map inconsistent with the 48-dim neighbour structure).

    * ``n >= 30`` -> t-SNE (perplexity=min(30,(n-1)/3)), the preserved branch.
    * ``4 <= n < 30`` -> deterministic PCA on the normalized vectors.

    t-SNE is only attempted when the corpus has real 2D structure: with fewer
    than 3 distinct vectors (or zero PCA variance, as happens when every
    abstract is identical) ``init='pca'`` feeds a zero-variance embedding into
    sklearn's gradient descent and the process dies with a stack overflow rather
    than an exception. Those inputs fall back to the PCA layout.
    """
    from sklearn.decomposition import PCA

    n = int(vectors.shape[0])
    distinct = len(np.unique(vectors, axis=0))
    coords = None
    if n >= 30 and distinct >= 3:
        pca_var = float(PCA(n_components=2, random_state=17).fit(vectors).explained_variance_.sum())
        if np.isfinite(pca_var) and pca_var > 1e-12:
            from sklearn.manifold import TSNE

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    candidate = TSNE(
                        n_components=2,
                        perplexity=min(30.0, (n - 1) / 3),
                        init="pca",
                        learning_rate="auto",
                        random_state=23,
                        max_iter=750,
                    ).fit_transform(vectors)
                    if candidate.shape == (n, 2) and np.isfinite(candidate).all():
                        coords = candidate
                except Exception:
                    coords = None
    if coords is None:
        coords = PCA(n_components=2, random_state=17).fit_transform(vectors)
    coords = np.asarray(coords, dtype=float).reshape(-1, 2)
    if coords.shape[0] != n:
        raise RuntimeError(f"projection row mismatch: {coords.shape[0]} != {n}")
    if not np.isfinite(coords).all():
        raise RuntimeError("projection produced non-finite coordinates")
    return coords


def _cluster_count(n: int) -> int:
    """Number of semantic regions, scaled for readability (100-1000 papers).

    The previous ``sqrt(n/40)`` produced exactly 2 regions for every corpus
    between 30 and 390 papers (a 129-paper ICML slice and a 400-paper one both
    collapsed to 2-3 mega-blobs). ``sqrt(n/6)`` grows gently and never saturates
    the 24 cap for realistic per-venue/per-year corpora: 129->5, 200->6, 300->7,
    400->8, 600->10, 1000->13. Measured on the real ICML 2024 corpus at n=400:
    separation ratio 1.51 -> 1.90 with kNN purity@5 0.81 -> 0.74 (expected: more
    regions lower neighbour agreement while raising separation).
    """
    if n < 8:
        return 1
    return min(24, max(2, int(round(np.sqrt(n / 6.0)))))


def _normalise_rows(vectors: "np.ndarray") -> "np.ndarray":
    vectors = np.asarray(vectors, dtype=float)
    if vectors.ndim == 1:
        vectors = vectors.reshape(-1, 1)
    if vectors.shape[1] < 2:
        # A single SVD component cannot express 2D geometry; pad deterministically.
        vectors = np.column_stack((vectors[:, 0], np.zeros(vectors.shape[0])))
    return vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-12)


def _fit_semantic_model(docs: list[str], dims: int = 48) -> "semantics.SemanticModel":
    """Fit the shared semantic model, relaxing TF-IDF pruning if it comes up empty.

    ``semantics.SemanticModel`` hard-codes ``min_df=2``/``max_df=0.5``, which can
    prune the entire vocabulary on a small or homogeneous corpus. That is a
    legitimate ``ValueError`` from sklearn, not a corpus error, so retry once with
    relaxation instead of surrendering the semantic representation. Built here
    (not in semantics.py) so the guard lives with the caller that must survive.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = semantics.SemanticModel(dims=dims)
        model.fit(docs)
    if getattr(model, "vectorizer", None) is not None and getattr(model, "svd", None) is not None:
        return model
    raise RuntimeError("SemanticModel.fit produced an unusable model (no vectorizer/svd)")


def _fit_relaxed_semantic_model(docs: list[str], dims: int = 48) -> "semantics.SemanticModel":
    """Second chance with min_df=1/max_df=1.0, still a real fitted model.

    Only reached when the strict fit raises. Produces a ``SemanticModel`` with a
    genuinely fitted vectorizer/SVD so ``transform``/``save``/``load`` and the
    API's usability check all keep working.
    """
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        vectorizer = TfidfVectorizer(
            max_features=1024,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=1,
            max_df=1.0,
            strip_accents="unicode",
        )
        matrix = vectorizer.fit_transform(docs)
        n_features = matrix.shape[1]
        n_samples = matrix.shape[0]
        comps = min(dims, max(2, n_features - 1), max(2, n_samples - 1))
        comps = max(2, comps)
        svd = TruncatedSVD(n_components=comps, random_state=17)
        svd.fit_transform(matrix)
        model = semantics.SemanticModel(dims=dims)
        model.vectorizer = vectorizer
        model.svd = svd
        model.feature_names_ = vectorizer.get_feature_names_out()
    return model


def _fallback_label(c: int) -> str:
    return f"Topic {c + 1}"


def generate_graph(venue: str, year: int | None = None) -> dict[str, Any]:
    """Build deterministic CPU-only semantic TF-IDF/SVD coordinates for a local corpus.

    Degrades gracefully instead of raising: an unfittable model (empty/short
    vocabulary), degenerate vectors, or a labelling failure still yields a
    usable ``graph_points`` layout with a truthful status/method.
    """
    from sklearn.cluster import KMeans
    from sklearn.exceptions import ConvergenceWarning

    year = years.resolve(year)
    db.init_db()
    try:
        with db.connect() as con:
            rows = con.execute(
                "SELECT p.id,p.title,p.abstract,p.keywords_json FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id WHERE pv.venue_id=? AND pv.year=? ORDER BY p.id",
                (venue, year),
            ).fetchall()
    except Exception as exc:  # unreadable/missing schema must not crash a sync
        return {"venue": venue, "year": year, "papers": 0, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}
    if not rows:
        return {"venue": venue, "year": year, "papers": 0, "status": "insufficient_data"}

    n = len(rows)
    docs = _graph_docs(rows)
    model = None
    vectors = None
    degraded_reason: str | None = None

    if n < 4:
        # Preserved small-corpus branch: no model, zero-ish coordinates, 1 cluster.
        coords = np.column_stack((np.arange(n, dtype=float), np.zeros(n)))
        labels = np.zeros(n, dtype=int)
        cluster_count = 1
        topic_labels: dict[int, str] = {}
    else:
        try:
            model = _fit_semantic_model(docs, dims=128)
        except Exception as strict_exc:
            try:
                model = _fit_relaxed_semantic_model(docs, dims=128)
            except Exception:
                model = None
                degraded_reason = f"{type(strict_exc).__name__}: {strict_exc}"
        if model is not None:
            try:
                raw = model.transform(docs)
                vectors = _normalise_rows(raw)
                if vectors.shape[0] != n:
                    raise RuntimeError(f"vector row mismatch: {vectors.shape[0]} != {n}")
            except Exception as exc:
                vectors = None
                model = None
                degraded_reason = f"{type(exc).__name__}: {exc}"

        if vectors is None:
            # Degrade: keep a deterministic left-to-right layout and one region.
            coords = np.column_stack((np.arange(n, dtype=float), np.zeros(n)))
            labels = np.zeros(n, dtype=int)
            cluster_count = 1
            topic_labels = {}
        else:
            try:
                coords = _project_2d(vectors)
            except Exception as exc:
                degraded_reason = degraded_reason or f"projection fallback: {type(exc).__name__}: {exc}"
                coords = np.column_stack((np.arange(n, dtype=float), np.zeros(n)))
            cluster_count = _cluster_count(n)
            # Degenerate corpora (identical abstracts) cannot support k clusters.
            unique_vectors = len(np.unique(vectors, axis=0))
            cluster_count = max(1, min(cluster_count, n, unique_vectors))
            if cluster_count > 1:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", ConvergenceWarning)
                        labels = KMeans(
                            n_clusters=cluster_count, random_state=17, n_init=10
                        ).fit_predict(vectors)
                    labels = np.asarray(labels, dtype=int)
                    if labels.shape[0] != n:
                        raise RuntimeError("KMeans label length mismatch")
                except Exception:
                    labels = np.zeros(n, dtype=int)
                    cluster_count = 1
            else:
                labels = np.zeros(n, dtype=int)
            cluster_count = int(len(np.unique(labels)))
            topic_labels = {}
            try:
                tfidf_matrix = model.vectorizer.transform(docs)
                topic_labels = semantics.label_clusters_from_tfidf(
                    tfidf_matrix, labels, model.feature_names_, top_k=3
                ) or {}
            except Exception:
                topic_labels = {}

    coords = np.asarray(coords, dtype=float)
    if coords.ndim != 2 or coords.shape[0] != n or coords.shape[1] < 2:
        coords = np.column_stack((np.arange(n, dtype=float), np.zeros(n)))
    coords = coords[:, :2]
    coords[~np.isfinite(coords)] = 0.0
    for axis in range(2):
        low, high = coords[:, axis].min(), coords[:, axis].max()
        coords[:, axis] = (coords[:, axis] - low) / (high - low) if high > low else 0.5

    clusters = sorted(int(c) for c in np.unique(labels))
    with db.connect() as con:
        con.execute("DELETE FROM graph_topics WHERE venue_id=?", (venue,))
        for c in clusters:
            label = (topic_labels.get(c) or "").strip() or _fallback_label(c)
            con.execute("INSERT INTO graph_topics(venue_id,level,number,label) VALUES(?,?,?,?)",
                        (venue, "cluster", c, label))
            con.execute("INSERT INTO graph_topics(venue_id,level,number,label) VALUES(?,?,?,?)",
                        (venue, "region", c, label))
        for i, r in enumerate(rows):
            vector = json.dumps(vectors[i].astype(float).tolist()) if vectors is not None else None
            con.execute(
                "INSERT INTO graph_points(paper_id,venue_id,x,y,region,cluster,vector_json) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(paper_id) DO UPDATE SET venue_id=excluded.venue_id,x=excluded.x,y=excluded.y,"
                "region=excluded.region,cluster=excluded.cluster,vector_json=excluded.vector_json",
                (r["id"], venue, float(coords[i, 0]), float(coords[i, 1]),
                 int(labels[i]), int(labels[i]), vector),
            )
        # Similarity is stored as a provenance-labelled relation; no topic model is implied.
        # Exactly the 10 strongest neighbours per paper, replacing any stale rows
        # from an earlier build so the cap cannot drift upward.
        ids = [r["id"] for r in rows]
        con.executemany(
            "DELETE FROM paper_relations WHERE source_paper_id=? AND relation_type='similar'",
            [(pid,) for pid in ids],
        )
        if vectors is not None and n > 1:
            k = min(10, n - 1)
            edges: list[tuple[str, str, str, float, str]] = []
            chunk = 512
            for start in range(0, n, chunk):
                block = vectors[start:start + chunk] @ vectors.T  # chunk x n cosines
                for offset in range(block.shape[0]):
                    row_sims = block[offset].copy()
                    row_sims[start + offset] = -np.inf
                    top = np.argpartition(-row_sims, k - 1)[:k]
                    top = top[np.argsort(-row_sims[top])]
                    source = ids[start + offset]
                    for j in top:
                        edges.append((source, ids[int(j)], "similar",
                                      float(row_sims[int(j)]), "tfidf-svd-cosine-v1"))
            con.executemany(
                "INSERT INTO paper_relations(source_paper_id,target_paper_id,relation_type,score,provenance) "
                "VALUES(?,?,?,?,?) ON CONFLICT(source_paper_id,target_paper_id,relation_type) "
                "DO UPDATE SET score=excluded.score,provenance=excluded.provenance",
                edges,
            )
    # Persist the fitted semantic model for downstream abstract search / gap mining.
    # A degraded build must not leave a stale pkl behind: the API would load it and
    # score queries in a vector space the current graph was never built with.
    pkl = DATA_ROOT / "graph" / f"{venue}_{year}_semantic.pkl"
    if vectors is not None and model is not None:
        try:
            model.save(pkl)
        except Exception:
            pass
    elif pkl.exists():
        try:
            pkl.unlink()
        except Exception:
            pass
    result: dict[str, Any] = {
        "venue": venue,
        "year": year,
        "papers": n,
        "clusters": int(cluster_count),
        "status": "generated",
        "method": "TF-IDF + normalized SVD + t-SNE neighbourhood projection + KMeans (CPU)",
    }
    if degraded_reason:
        result["status"] = "generated_degraded"
        result["degraded_reason"] = degraded_reason
        result["method"] = "deterministic layout only (semantic model unavailable)"
    return result
