from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware

import numpy as np

from . import db
from . import manifest
from .config import venues as venue_registry
from .config import DATA_ROOT
from .service import graph, graph_titles, list_papers, _graph_docs
from . import global_layout, retrieval
from . import semantics
from . import years

app=FastAPI(title="Paper Graph local API",version="0.1.0")
# The map payload is thousands of coordinate numbers, so it compresses ~5-6x.
# Without this the browser downloads and parses a multi-MB JSON document.
app.add_middleware(GZipMiddleware, minimum_size=1024)

#: Parsed vectors per semantic scope: (venue, year, model_venue) -> (row_count, ids, meta).
#: Re-parsing a year's graph vectors from JSON cost ~17 s per search; the matrix is
#: a property of the stored graph, so it is built once and reused until it changes.
_SEMANTIC_VECTORS: dict[tuple, tuple[int, list[str], dict]] = {}
_VECTOR_MATRICES: dict[tuple, "np.ndarray"] = {}
#: Gap scans take 20-30 s per year and only change when the graph is rebuilt.
_GAPS_CACHE: dict[tuple, tuple[float, dict]] = {}
GAPS_CACHE_TTL = 3600.0
#: Above this many papers a gap scan is refused: detect_gaps is quadratic in the
#: corpus, so a whole-year 20k-paper scan is already at the practical limit.
GAPS_MAX_PAPERS = 25_000

@app.on_event("startup")
def startup(): db.init_db()

@app.get("/api/health")
def health(): return {"status":"ok","database":"sqlite","path":str(db.DB_PATH)}

def _latest_sync_job(con,venue,year):
    """The newest ``sync_jobs`` row of a venue-year, with its coverage verdict.

    The coverage columns (``expected_count``/``coverage_status``) were added
    after the first installations, so the extended query falls back to the old
    one: a database that could not be widened must not turn the venue list
    into a 500.
    """
    try:
        return con.execute("SELECT status,error_message,expected_count,coverage_status FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 1",(venue,year)).fetchone()
    except sqlite3.OperationalError:
        return con.execute("SELECT status,error_message FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 1",(venue,year)).fetchone()


@app.get("/api/venues")
def venues():
    db.init_db()
    config=venue_registry()
    # The manifest is read once per request: it is a small file, and the
    # verdict for every venue-year is computed from it.
    manifest_cache=manifest.load_manifest()
    with db.connect() as con:
        statuses=[dict(r) for r in con.execute("SELECT * FROM venues ORDER BY year DESC,name")]
    keyed={(r["id"],r["year"]):r for r in statuses}
    # The selector used to pin a literal 2027, which pointed at a year that has no
    # data. Offer the years that actually exist instead.
    stored_all = {r["year"] for r in statuses}
    data_years = set(years.available_years())
    current = years.current_year()
    out=[]
    for key,cfg in config.items():
        stored_years = {r['year'] for r in statuses if r['id']==key}
        # Show every year we know about: years with data, years with a venue row,
        # and the current calendar year (so a fresh install still has something
        # to select and sync into).
        candidate_years = stored_years | data_years | {current}
        for year in sorted(candidate_years, reverse=True):
            stored=keyed.get((key,year))
            with db.connect() as con:
                count=con.execute("SELECT count(DISTINCT paper_id) FROM paper_venues WHERE venue_id=? AND year=?",(key,year)).fetchone()[0]
                job=_latest_sync_job(con,key,year)
            # Coverage of the last harvest: how many papers were expected and
            # whether the run met that expectation ("ok" / "short" / "over" /
            # "unknown"). `None` means the venue-year was never checked.
            expected=job["expected_count"] if job is not None and "expected_count" in job.keys() else None
            coverage=job["coverage_status"] if job is not None and "coverage_status" in job.keys() else None
            # A venue-year that was never harvested through the sync path (the
            # journal collector writes its own rows) has no verdict on its job
            # row. The expectation manifest still applies, so the verdict is
            # computed here from the stored count: the check must not depend on
            # which collector filled the venue.
            if coverage is None:
                verdict=manifest.check_venue(key,year,count,manifest_cache)
                coverage=verdict.get("status")
                expected=verdict.get("min_papers") if verdict.get("status")!="unknown" else None
            out.append({"id":key,"name":cfg["name"],"year":year,"provider":cfg["provider"],"type":cfg["type"],
                        "status":stored["availability_status"] if stored else "not_checked","source_venue_id":stored["source_venue_id"] if stored else None,
                        "last_sync_at":stored["last_sync_at"] if stored else None,"year_rule":cfg.get("year_rule"),"paper_count":count,"latest_result":job["status"] if job else "not_checked","error_message":job["error_message"] if job else None,
                        "expected_count":expected,"coverage_status":coverage})
    return {"default_year":years.default_year(),"available_years":years.available_years(),
            "all_years":years.ALL_YEARS,"current_year":current,"items":out}

@app.get("/api/venues/{venue_id}/status")
def venue_status(venue_id:str,year:int|None=None):
    year = years.resolve(year)
    with db.connect() as con:
        row=con.execute("SELECT * FROM venues WHERE id=? AND year=?",(venue_id,year)).fetchone()
        jobs=[dict(r) for r in con.execute("SELECT * FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 10",(venue_id,year))]
        count=con.execute("SELECT count(*) FROM paper_venues WHERE venue_id=? AND year=?",(venue_id,year)).fetchone()[0]
    return {"venue":dict(row) if row else None,"papers":count,"jobs":jobs}

@app.get("/api/papers")
def papers(venue:str|None=None,year:str|None=None,keyword:str|None=None,status:str|None=None,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=200),sort:str="recent"):
    # `year` is a string so the `all` sentinel survives; the service resolves a
    # concrete year when it is one.
    try:return list_papers(venue=venue,year=year,keyword=keyword,status=status,page=page,page_size=page_size,sort=sort)
    except Exception as exc: raise HTTPException(400,str(exc)) from exc

@app.get("/api/papers/{paper_id:path}/reviews")
def reviews(paper_id:str):
    with db.connect() as con:return {"items":[dict(r) for r in con.execute("SELECT * FROM reviews WHERE paper_id=? AND visibility='public' ORDER BY source_created_at",(paper_id,))]}

@app.get("/api/papers/{paper_id:path}/related")
def related(paper_id:str,limit:int=20):
    with db.connect() as con:
        rows=con.execute("SELECT p.*,r.score,r.provenance FROM paper_relations r JOIN papers p ON p.id=r.target_paper_id WHERE r.source_paper_id=? ORDER BY r.score DESC LIMIT ?",(paper_id,limit)).fetchall()
    return {"related":[{"id":r["id"],"slug":r["id"],"title":r["title"],"authors":json_value(r["authors_json"]),
                        "score":r["score"],"provenance":r["provenance"]} for r in rows],"hidden":0}

@app.get("/api/papers/{paper_id:path}")
def paper(paper_id:str):
    with db.connect() as con:
        row=con.execute("SELECT * FROM papers WHERE id=?",(paper_id,)).fetchone()
        if not row: raise HTTPException(404,"Paper not found")
        item=db.paper_dict(row)
        item["venues"]=[dict(r) for r in con.execute("SELECT v.id,v.name,pv.year,pv.relation_type FROM paper_venues pv JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year WHERE pv.paper_id=?",(paper_id,))]
        item["reviews"]=[dict(r) for r in con.execute("SELECT * FROM reviews WHERE paper_id=? AND visibility='public'",(paper_id,))]
        item["rqs"]=semantics.extract_rqs(item.get("abstract") or "")
        item["gap_cues"]=semantics.extract_gap_cues(item.get("abstract") or "")
        # Questions *derived* from this paper's stated gaps: the thing a user can
        # actually try to answer when brainstorming a new idea.
        item["derived_questions"]=semantics.derive_research_questions(item.get("abstract") or "")
    return item

def json_value(value:str):
    return json.loads(value)

def _model_usable(model: "semantics.SemanticModel|None") -> bool:
    """A loaded pkl is only usable when the fitted state survived the round-trip."""
    if model is None:
        return False
    vectorizer=getattr(model,"vectorizer",None)
    svd=getattr(model,"svd",None)
    if vectorizer is None or svd is None:
        return False
    try:
        # Both must expose the attributes transform() actually consumes.
        if getattr(vectorizer,"transform",None) is None or getattr(svd,"transform",None) is None:
            return False
        if getattr(svd,"components_",None) is None:
            return False
        return True
    except Exception:
        return False

def _load_semantic_model(venue:str|None,year:int)->"semantics.SemanticModel|None":
    """Load the persisted SemanticModel for (venue,year).

    Order is deterministic: exact venue match first, then every other
    ``*_{year}_semantic.pkl`` in sorted name order (a glob previously returned an
    arbitrary file, so different requests could silently use different corpora).
    A stale/corrupt/unfitted pkl is skipped instead of crashing the request.
    """
    return _load_semantic_model_scoped(venue,year)[1]

def _pkl_venue(path:Path)->str:
    """``icml_2024_semantic.pkl`` -> ``icml`` (empty when the name is unusual)."""
    name=path.name
    suffix="_semantic.pkl"
    if name.endswith(suffix):
        name=name[:-len(suffix)]
    parts=name.rsplit("_",1)
    return parts[0] if len(parts)==2 and parts[0] else ""

def _load_semantic_model_scoped(venue:str|None,year:int)->tuple[dict|None,"semantics.SemanticModel|None"]:
    """Return ``(info, model)`` where info identifies the corpus the model was fit on."""
    folder=DATA_ROOT/"graph"
    candidates:list[Path]=[]
    if venue:
        candidates.append(folder/f"{venue}_{year}_semantic.pkl")
    if folder.exists():
        try:
            for p in sorted(folder.glob(f"*_{year}_semantic.pkl")):
                if p not in candidates:
                    candidates.append(p)
        except Exception:
            pass
    for path in candidates:
        try:
            if not path.exists():
                continue
            model=semantics.SemanticModel.load(path)
        except Exception:
            continue
        if _model_usable(model):
            return {"path":str(path),"venue":_pkl_venue(path) or (venue or None)}, model
    return None, None

def _fts_tokens(query:str)->list[str]:
    """Word tokens safe to feed to FTS5 as a bare MATCH term.

    FTS5 gives ``- + * ( ) : ^ " ...`` syntax meaning, and ``list_papers`` strips
    double quotes from the query, so a word like ``co-lora`` or a trailing ``?``
    would raise an OperationalError instead of matching. Reduce to plain words.
    """
    out:list[str]=[]
    for token in re.findall(r"[A-Za-z0-9]+",query.lower())[:24]:
        if len(token)<2 or token in _FTS_STOPWORDS or token in out:
            continue
        out.append(token)
    return out

def _fts_or_query(query:str)->str:
    """FTS5 treats bare space-separated terms as AND, so a natural-language
    question matches nothing. Build an OR query over the significant tokens."""
    return " OR ".join(_fts_tokens(query))

_FTS_STOPWORDS={"the","a","an","of","for","and","or","in","on","to","with","how","can","be","is",
                "are","do","does","what","which","that","this","these","those","by","as","at",
                "from","it","its","we","our","their","not","use","used","using","into","between"}

def _semantic_items_from_keyword(query:str,venue:str|None,year:int,limit:int,note:str)->dict:
    """Keyword (FTS) fallback normalised to the SemanticItem contract.

    Tries the query as typed first (preserves the previous behaviour for useful
    single-term queries), then falls back to an OR query over its tokens so a
    full-sentence idea still returns candidates instead of nothing. An FTS5
    syntax error on the raw query never surfaces as a 500.
    """
    result={"items":[]}
    try:
        result=list_papers(venue=venue,year=year,keyword=query,page=1,page_size=limit)
    except Exception:
        result={"items":[]}
    if not result["items"]:
        or_query=_fts_or_query(query)
        if or_query:
            try:
                result=list_papers(venue=venue,year=year,keyword=or_query,page=1,page_size=limit)
            except Exception:
                result={"items":[]}
    items=[]
    for it in result["items"]:
        items.append({
            "id":it["id"],"slug":it["id"],"title":it.get("title") or "",
            "abstract":it.get("abstract") or "",
            "authors":it.get("authors") or [],
            "venues":it.get("venues") or [],
            "year":year,"score":0.0,
        })
    return {"items":items[:limit],"method":"keyword-fallback","note":note,"query":query}

def _unit(v:"np.ndarray")->"np.ndarray":
    return v/(np.linalg.norm(v)+1e-12)

def _int_param(value,default:int,*,low:int|None=None,high:int|None=None)->int:
    """Coerce a JSON body field to int without letting a type error become a 500."""
    try:
        out=int(value)
    except (TypeError,ValueError):
        return default
    if low is not None and out<low:
        return low
    if high is not None and out>high:
        return high
    return out

@app.post("/api/papers/semantic")
def semantic_search(body:dict):
    if not isinstance(body,dict):
        raise HTTPException(400,"JSON object body is required")
    query=str(body.get("query") or "")
    venue=body.get("venue")
    raw_year=body.get("year")
    limit=_int_param(body.get("limit"),10,low=1,high=200)
    if not query.strip():
        raise HTTPException(400,"query is required")
    venue=None if venue=="all" else (str(venue) if venue else None)
    if years.is_all(raw_year):
        return _semantic_search_all_years(query,venue,limit)
    year=_int_param(raw_year,years.default_year(),low=1900,high=2200)
    info,model=_load_semantic_model_scoped(venue,year)
    if model is None:
        # No fitted semantic model yet: fall back to keyword (FTS) search, but
        # still normalise items to the SemanticItem contract so the frontend can
        # render them identically (slug/year/score are required by the UI).
        return _semantic_items_from_keyword(query,venue,year,limit,"keyword fallback")
    try:
        qvec=np.asarray(model.transform([semantics.query_document(query)])[0],dtype=float).ravel()
    except Exception:
        return _semantic_items_from_keyword(query,venue,year,limit,"keyword fallback (semantic query failed)")
    if qvec.size<2 or not np.isfinite(qvec).all():
        return _semantic_items_from_keyword(query,venue,year,limit,"keyword fallback (degenerate model)")
    qvec=_unit(qvec)
    if not np.any(qvec):
        # Every query term is out of the model's vocabulary. Cosine would be 0 for
        # every paper, which is a meaningless ranking; use keyword matching and say so.
        return _semantic_items_from_keyword(query,venue,year,limit,
                                            "keyword fallback (no query term in the semantic vocabulary)")
    with db.connect() as con:
        # LEFT JOIN: a paper without a stored graph vector must still be ranked by
        # transforming its text, instead of silently disappearing from the list.
        sql=("SELECT p.id,p.title,p.abstract,p.authors_json,p.keywords_json,p.doi,p.paper_url,p.pdf_url,"
             "g.vector_json,g.venue_id AS point_venue FROM papers p "
             "JOIN paper_venues pv ON pv.paper_id=p.id "
             "LEFT JOIN graph_points g ON g.paper_id=p.id WHERE pv.year=?")
        args:list[object]=[year]
        if venue:
            sql+=" AND pv.venue_id=?"; args.append(venue)
        sql+=" GROUP BY p.id ORDER BY p.id"
        rows=con.execute(sql,args).fetchall()
        # The loaded pkl defines ONE vector space. Stored graph vectors are only
        # comparable when they were built for the same venue corpus; anything
        # else is re-embedded through the model that is actually in use.
        model_venue=(info or {}).get("venue")
        # Re-parsing 20k JSON vectors per request dominated this endpoint (~17 s
        # for one year). The parsed matrix is cached per (scope, model) because it
        # only changes when that venue-year's graph is rebuilt.
        cache_key=(venue or "all",year,model_venue or "")
        cached=_SEMANTIC_VECTORS.get(cache_key)
        if cached is not None and cached[0]==len(rows):
            ids,meta=cached[1],cached[2]
            matrix=_VECTOR_MATRICES[cache_key]
            scores=matrix@qvec.astype(np.float32)
            scored=[]
            for n,score in enumerate(scores):
                rid=ids[n]
                m=meta[rid]
                scored.append({"id":rid,"slug":rid,"title":m["title"],
                               "abstract":m["abstract"],"authors":m["authors"],
                               "doi":m["doi"],"paper_url":m["paper_url"],"pdf_url":m["pdf_url"],
                               "year":m["year"],"score":round(float(score),6)})
            recovered=0
        else:
            # Cold scope: build and cache the vector matrix once.
            scored=[]
            pending:list[tuple[object,str]]=[]
            ids:list[str]=[]
            columns:list["np.ndarray"]=[]
            meta:dict[str,dict]= {}
            for r in rows:
                stored=None
                if r["vector_json"]:
                    # graph_points is keyed by paper_id only, so a paper that
                    # belongs to several venues may carry a vector fitted on a
                    # different venue corpus. Only trust it for this venue.
                    if model_venue and (r["point_venue"] or "") == model_venue:
                        try:
                            stored=np.asarray(json.loads(r["vector_json"]),dtype=float).ravel()
                        except Exception:
                            stored=None
                if stored is not None and stored.shape==qvec.shape:
                    ids.append(r["id"])
                    columns.append(_unit(stored))
                else:
                    pending.append((r,_graph_docs([r])[0]))
                meta[r["id"]]={"title":r["title"],"abstract":r["abstract"],
                               "authors":json_value(r["authors_json"]),
                               "doi":r["doi"],"paper_url":r["paper_url"],"pdf_url":r["pdf_url"],
                               "year":year}
            recovered=0
            if pending:
                try:
                    vecs=np.asarray(model.transform([doc for _,doc in pending]),dtype=float)
                    for (r,_),v in zip(pending,vecs):
                        v=np.asarray(v,dtype=float).ravel()
                        if v.shape!=qvec.shape:
                            continue
                        ids.append(r["id"])
                        columns.append(_unit(v))
                        recovered+=1
                except Exception:
                    pass
            if columns:
                matrix=np.vstack(columns).astype(np.float32)
                if len(_SEMANTIC_VECTORS)>=6:
                    _SEMANTIC_VECTORS.clear()
                    _VECTOR_MATRICES.clear()
                _SEMANTIC_VECTORS[cache_key]=(len(rows),ids,meta)
                _VECTOR_MATRICES[cache_key]=matrix
                scores=matrix@qvec.astype(np.float32)
                scored=[]
                for n,score in enumerate(scores):
                    rid=ids[n]
                    m=meta[rid]
                    scored.append({"id":rid,"slug":rid,"title":m["title"],
                                   "abstract":m["abstract"],"authors":m["authors"],
                                   "doi":m["doi"],"paper_url":m["paper_url"],"pdf_url":m["pdf_url"],
                                   "year":m["year"],"score":round(float(score),6)})
        # Attach the paper's full venue list (the UI renders it), scoped lookup.
        for item in scored:
            item["venues"]=[dict(v) for v in con.execute(
                "SELECT v.id,v.name,pv.year,pv.relation_type FROM paper_venues pv "
                "JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year WHERE pv.paper_id=? "
                "ORDER BY pv.year DESC,v.name",(item["id"],))]
        # Hybrid fusion for a single year: the cosine ranking above plus the FTS
        # prefix/prefix-token channel plus the title substring channel, fused by
        # RRF. This is the same architecture as the all-years path, so "SECI" finds
        # its one real paper in 2023 even though the SVD vocabulary has no such
        # token, and ordinary queries gain recall without losing semantic order.
        terms=[t for t in _fts_tokens(query)]
        fts_ids=retrieval.fts_ranking(con,terms,limit=max(limit*3,60),venue=venue,year=year)
        like_ids=retrieval.like_ranking(con,query,limit=max(limit,30),venue=venue,year=year)
        semantic_ids=[item["id"] for item in sorted(scored,key=lambda x:(-x["score"],x["id"]))][:max(limit*3,60)]
        score_by_id={item["id"]:item["score"] for item in scored}
        fused=retrieval.rank_fuse([semantic_ids,fts_ids,like_ids])
        hints:dict={}
        try:
            vocabulary=[str(v) for v in (model.feature_names_ or [])]
            hints["spelling"]=retrieval.suggest_terms(query,vocabulary)
            hints["related"]=retrieval.pseudo_relevance_feedback(
                terms,[pid for pid,_ in fused[:5]],lambda ids2: retrieval.fetch_titles(con,ids2))
        except Exception:
            hints={"spelling":[],"related":[]}
        ranked=[]
        for pid,_fused in fused:
            if pid not in score_by_id:
                continue
            item=next(i for i in scored if i["id"]==pid)
            ranked.append(item)
        ranked=ranked[:limit] or scored[:limit]
    if not scored:
        # Model exists but no in-scope paper could be scored at all: be explicit
        # rather than returning the misleading empty semantic result.
        return _semantic_items_from_keyword(query,venue,year,limit,
                                            "keyword fallback (no scored papers for this scope)")
    note=None
    if recovered:
        note=(f"{recovered} of {len(scored)} items scored by embedding their text with the loaded "
              f"model (no stored graph vector for this model's corpus)")
    suggestions=[t for t in (hints.get("spelling") or []) if t.lower() not in query.lower()][:6]
    related=[t for t in (hints.get("related") or []) if t not in suggestions][:6]
    out=[]
    for item in ranked:
        out.append({
            "id":item["id"],"slug":item["slug"],"title":item["title"],
            "abstract":item["abstract"] or "","authors":item["authors"],
            "venues":item["venues"],"year":item["year"],"score":item["score"],
            "doi":item.get("doi"),"paper_url":item.get("paper_url"),"pdf_url":item.get("pdf_url"),
        })
    return {"items":out,"method":"hybrid-rrf","note":note,"query":query,
            "suggestions":suggestions,"related_terms":related}


def _semantic_search_all_years(query:str,venue:str|None,limit:int)->dict:
    """Whole-corpus hybrid search: semantic cosine + FTS + substring, RRF-fused.

    The global model scores every paper with one matrix-vector product; the FTS
    channel adds prefix-token matching and the LIKE channel adds substring
    matching over titles, and the three rankings are fused by Reciprocal Rank
    Fusion. That is what makes a query like "SECI" useful: the acronym's one
    true paper surfaces even though the 1024-word SVD vocabulary has no "seci".
    """
    hints: dict = {}
    hits=global_layout.search(query,limit=limit,venue=venue,suggest=hints)
    mode=(hits[0].get("mode") if hits else None)
    if mode=="unavailable":
        # The global index genuinely is not built (no pkl/npz on disk).
        return _semantic_items_from_keyword(query,venue,years.default_year(),limit,
                                            "keyword fallback (whole-corpus semantic index unavailable)")
    with db.connect() as con:
        items=[]
        for hit in hits:
            row=con.execute("SELECT id,title,abstract,authors_json,doi,paper_url,pdf_url FROM papers WHERE id=?",(hit["id"],)).fetchone()
            if row is None:
                continue
            venue_rows=[dict(v) for v in con.execute(
                "SELECT v.id,v.name,pv.year,pv.relation_type FROM paper_venues pv "
                "JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year WHERE pv.paper_id=? "
                "ORDER BY pv.year DESC,v.name",(hit["id"],))]
            year=hit["year"] or max((int(v["year"]) for v in venue_rows if v.get("year")),default=0)
            items.append({
                "id":row["id"],"slug":row["id"],"title":row["title"],
                "abstract":row["abstract"] or "","authors":json_value(row["authors_json"]),
                "venues":venue_rows,"year":year,"score":hit["score"],
                "doi":row["doi"],"paper_url":row["paper_url"],"pdf_url":row["pdf_url"],
            })
    # "Did you mean" chips: spelling-neighbour vocabulary terms plus PRF words.
    suggestions=[t for t in (hints.get("spelling") or []) if t.lower() not in query.lower()][:6]
    related=[t for t in (hints.get("related") or []) if t not in suggestions][:6]
    out:dict={"query":query,"suggestions":suggestions,"related_terms":related}
    if not items:
        out.update({"items":[],"method":"keyword-fallback",
                    "note":("没有找到匹配的论文。下面是模型词表里与输入相近的术语，点击可重新检索。")})
        return out
    if mode=="keyword":
        out.update({"items":items,"method":"keyword-fallback",
                    "note":("查询词不在语义模型的词表里（可能是缩写、拼写错误或未覆盖的中文），"
                            "已按词面/前缀/子串匹配返回。")})
        return out
    out.update({"items":items,"method":"hybrid-rrf-global",
                "note":"whole-corpus hybrid: semantic + lexical (RRF), all years"})
    return out

def _split_label(label:str|None)->list[str]:
    """graph_topics.label is 'term · term · term'; never emit empty terms."""
    if not label:
        return []
    return [s.strip() for s in str(label).split("\u00b7") if s and s.strip()]

def _terms_from_cluster_tfidf(rows,labels,venue:str|None,year:int)->dict[int,list[str]]:
    """Recompute cluster terms from the persisted model when labels are missing.

    Covers graphs built before ``graph_topics`` was populated: the terms are the
    same distinctive cluster TF-IDF terms that labeling used, so ``top_terms``
    stays real instead of becoming an empty string.
    """
    model=_load_semantic_model(venue,year)
    if model is None or getattr(model,"vectorizer",None) is None:
        return {}
    try:
        docs=[_graph_docs([r])[0] for r in rows]
        tfidf=model.vectorizer.transform(docs)
        labelled=semantics.label_clusters_from_tfidf(tfidf,labels,model.feature_names_,top_k=3)
        return {int(c):_split_label(v) for c,v in (labelled or {}).items() if _split_label(v)}
    except Exception:
        return {}

@app.get("/api/gaps")
def gaps(venue:str|None=None,year:str|None=None):
    if years.is_all(year):
        # detect_gaps is quadratic in the corpus, so a whole-corpus scan would
        # never return. Degrade with a truthful note instead of hanging.
        return {"gaps":[],"topics":[],
                "note":"Gap scanning compares one fixed corpus; pick a year (and optionally a venue) instead of all years."}
    year = years.resolve(year)
    # Scanning a year's graph is a 20-30 s computation; the answer only changes
    # when the graph is rebuilt, so repeated tab switches are served from memory.
    cache_key=(None if venue=="all" else venue, year)
    hit=_GAPS_CACHE.get(cache_key)
    if hit is not None and time.time()-hit[0]<GAPS_CACHE_TTL:
        return hit[1]
    try:
        result = _gaps(venue,year)
    except Exception as exc:
        # Degradation contract: never 500 when there is no usable graph/model.
        return {"gaps":[],"topics":[],"note":f"gap detection unavailable: {type(exc).__name__}: {exc}"}
    if len(_GAPS_CACHE)>=6:
        _GAPS_CACHE.clear()
    _GAPS_CACHE[cache_key]=(time.time(),result)
    return result

def _gaps(venue:str|None,year:int)->dict:
    venue=None if venue=="all" else venue
    with db.connect() as con:
        sql="""SELECT p.id,p.title,p.abstract,g.cluster,g.x,g.y,g.vector_json,g.venue_id AS point_venue
               FROM papers p
               JOIN graph_points g ON g.paper_id=p.id
               JOIN paper_venues pv ON pv.paper_id=p.id WHERE pv.year=?"""
        args:list[object]=[year]
        if venue:
            sql+=" AND pv.venue_id=?"; args.append(venue)
        sql+=" GROUP BY p.id ORDER BY p.id"
        rows=con.execute(sql,args).fetchall()
        if not rows:
            return {"gaps":[],"topics":[]}
        ids=[r["id"] for r in rows]
        titles=[r["title"] for r in rows]
        abstracts=[r["abstract"] or "" for r in rows]
        # Keep aligned vectors: a missing row becomes None rather than shifting
        # the array so labels/vectors stay indexed by paper.
        aligned:list["np.ndarray|None"]=[]
        for r in rows:
            v=r["vector_json"]
            if not v:
                aligned.append(None)
                continue
            try:
                arr=np.asarray(json.loads(v),dtype=float).ravel()
                aligned.append(arr if arr.size>=2 else None)
            except Exception:
                aligned.append(None)
        has_vectors=all(a is not None for a in aligned)
        vectors=np.vstack(aligned) if has_vectors else None  # type: ignore[arg-type]

        # graph_topics.cluster numbers are per-venue. When no single venue is
        # requested, relabel (venue,cluster) pairs to one global index so a
        # cluster term can never be attributed to the wrong cluster.
        keys:list[tuple[str,int]]=[]
        for r in rows:
            key=(str(r["point_venue"] or venue or ""),int(r["cluster"]) if r["cluster"] is not None else -1)
            if key not in keys:
                keys.append(key)
        keys.sort()
        key_index={k:i for i,k in enumerate(keys)}
        labels=np.asarray([key_index[(str(r["point_venue"] or venue or ""),
                                      int(r["cluster"]) if r["cluster"] is not None else -1)]
                           for r in rows],dtype=int)
        cluster_sizes:dict[int,int]={}
        for lab in labels:
            cluster_sizes[int(lab)]=cluster_sizes.get(int(lab),0)+1

        stored:dict[tuple[str,int],str]={}
        topic_venues={k[0] for k in keys if k[0]}
        with db.connect() as con:
            for tv in sorted(topic_venues):
                for t in con.execute("SELECT number,label FROM graph_topics WHERE venue_id=? AND level='cluster' ORDER BY number",(tv,)):
                    stored[(tv,int(t["number"]))]=t["label"]
        top_terms:dict[int,list[str]]={}
        labels_by_cluster:dict[int,str]={}
        for key,idx in key_index.items():
            terms=_split_label(stored.get(key))
            if terms:
                top_terms[idx]=terms
                labels_by_cluster[idx]=stored[key]
        missing=[i for i in cluster_sizes if i not in top_terms and keys[i][1] >= 0]
        if missing:
            # Need per-venue fallback; compute once per venue that has gaps.
            for tv in sorted({keys[i][0] for i in missing if keys[i][0]}):
                sub_idx=[i for i in missing if keys[i][0]==tv]
                positions=[p for p in range(len(rows)) if int(labels[p]) in sub_idx]
                sub_rows=[rows[p] for p in positions]
                sub_labels=labels[positions]
                computed=_terms_from_cluster_tfidf(sub_rows,sub_labels,tv,year)
                for i in sub_idx:
                    terms=computed.get(i,[])
                    if terms:
                        top_terms[i]=terms
                        labels_by_cluster[i]=" \u00b7 ".join(terms)

        gaps_result=semantics.detect_gaps(vectors,labels,abstracts,ids,titles,top_terms)
        topics=[]
        for c in sorted(cluster_sizes):
            terms=top_terms.get(c) or []
            label=(labels_by_cluster.get(c) or "").strip() or (terms[0] if terms else f"Topic {c+1}")
            topics.append({"number":c,"label":label,"size":cluster_sizes[c],
                           "top_terms":terms or [label]})
        return {"gaps":gaps_result,"topics":topics}

@app.get("/api/graph")
def graph_data(venue:str|None=None,year:str|None=None,include:str="base"):
    """Map payload. `year=all` returns the whole corpus in one global space.

    The base payload omits the 48-dim vectors and the title map (the map fetches
    titles lazily); `include=full` restores them for callers that need them.
    """
    return graph(venue,year,include_vectors=include=="full")

@app.get("/api/graph/titles")
def graph_titles_route(venue:str|None=None,year:str|None=None,slugs:str|None=None):
    # `slugs` is a comma-separated subset: the map labels a handful of points and
    # must not download 70k titles to do it.
    wanted=[s for s in (slugs or "").split(",") if s.strip()] or None
    return {"titles":graph_titles(venue,year,slugs=wanted)}

@app.get("/api/graph/search")
def graph_search(q:str,venue:str|None=None,year:str|None=None):
    result=list_papers(venue=venue,year=year,keyword=q,page=1,page_size=10000)
    return {"slugs":[p["id"] for p in result["items"]],"errors":[]}

@app.get("/api/graph/related")
def graph_related(venue:str|None=None,year:str|None=None):
    data=graph(venue,year)
    slugs=data["slugs"]
    index={slug:i for i,slug in enumerate(slugs)}
    with db.connect() as con:
        rows=con.execute("SELECT source_paper_id,target_paper_id FROM paper_relations WHERE relation_type='similar' ORDER BY source_paper_id,score DESC").fetchall()
    edges=[i for row in rows if row[0] in index and row[1] in index for i in (index[row[0]],index[row[1]])]
    return {"slugs":slugs,"edges":edges}

@app.get("/api/graph/{paper_id:path}/minimap")
def graph_minimap(paper_id:str,venue:str|None=None,year:str|None=None):
    # The minimap needs the vectors and titles that the map payload intentionally
    # drops, and it is a per-paper request, so it is not worth caching.
    all_data=graph(venue,year,include_vectors=True,use_cache=False)
    try:self_index=all_data["slugs"].index(paper_id)
    except ValueError:raise HTTPException(404,"Paper has no graph point")
    with db.connect() as con:
        related_rows=con.execute("SELECT target_paper_id FROM paper_relations WHERE source_paper_id=? ORDER BY score DESC LIMIT 10",(paper_id,)).fetchall()
        order=[paper_id]+[r[0] for r in related_rows]
        # Nearby points are selected by Euclidean distance in the supplied map coordinates.
        px,py=all_data["x"][self_index],all_data["y"][self_index]
        candidates=sorted((i for i,s in enumerate(all_data["slugs"]) if s!=paper_id),key=lambda i:(all_data["x"][i]-px)**2+(all_data["y"][i]-py)**2)[:100]
        wanted=list(dict.fromkeys([paper_id]+[all_data["slugs"][i] for i in candidates]+[s for s in order if s in all_data["slugs"]]))
        idx={s:i for i,s in enumerate(all_data["slugs"])}
        selected=[idx[s] for s in wanted if s in idx]
        related=[idx[s] for s in order[1:] if s in idx]
        points=[]
        for i in selected:
            paper=con.execute("SELECT title,primary_area FROM papers WHERE id=?",(all_data["slugs"][i],)).fetchone()
            points.append({"slug":all_data["slugs"][i],"title":all_data["titles"][all_data["slugs"][i]],
                           "primaryArea":paper["primary_area"],"x":all_data["x"][i],"y":all_data["y"][i],
                           "region":all_data["region"][i],"cluster":all_data["cluster"][i]})
        local={p["slug"]:i for i,p in enumerate(points)}
        vectors=[all_data["vectors"][idx[p["slug"]]] for p in points]
        edge_rows=con.execute("SELECT source_paper_id,target_paper_id FROM paper_relations WHERE relation_type='similar' AND source_paper_id IN (%s) AND target_paper_id IN (%s)" % (','.join('?' for _ in wanted),','.join('?' for _ in wanted)),wanted+wanted).fetchall() if wanted else []
        edges=[[local[r["source_paper_id"]],local[r["target_paper_id"]]] for r in edge_rows if r["source_paper_id"] in local and r["target_paper_id"] in local]
    with db.connect() as con:
        cluster_ids={p["cluster"] for p in points if p["cluster"] is not None}
        labels=[]
        if cluster_ids:
            # The topics belong to a venue-year: two years of the same venue
            # have different layouts and different labels. Without the year
            # the query would return one set per year, with colliding cluster
            # numbers.
            if years.is_all(year):
                labels=[dict(r) for r in con.execute(
                    "SELECT number,label FROM graph_topics WHERE venue_id=? AND level='cluster' "
                    "AND year=(SELECT MAX(year) FROM graph_topics WHERE venue_id=? AND level='cluster')",
                    (venue or "", venue or ""))]
            else:
                labels=[dict(r) for r in con.execute(
                    "SELECT number,label FROM graph_topics WHERE venue_id=? AND level='cluster' AND year=?",
                    (venue or "", years.resolve(year)))]
    return {"points":points,"vectors":vectors if all(v is not None for v in vectors) else None,
            "self":local[paper_id],"related":[local[all_data["slugs"][i]] for i in related if all_data["slugs"][i] in local],
            "relatedElsewhere":0,"edges":edges,"clusters":labels}

@app.get("/api/sync/jobs")
def jobs(limit:int=50):
    with db.connect() as con:return {"items":[dict(r) for r in con.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT ?",(limit,))]}
