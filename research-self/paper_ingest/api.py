from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

import numpy as np

from . import db
from .config import venues as venue_registry
from .config import DATA_ROOT
from .service import graph, list_papers, _graph_docs
from . import semantics
from . import years

app=FastAPI(title="Paper Graph local API",version="0.1.0")

@app.on_event("startup")
def startup(): db.init_db()

@app.get("/api/health")
def health(): return {"status":"ok","database":"sqlite","path":str(db.DB_PATH)}

@app.get("/api/venues")
def venues():
    db.init_db()
    config=venue_registry()
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
                job=con.execute("SELECT status,error_message FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 1",(key,year)).fetchone()
            out.append({"id":key,"name":cfg["name"],"year":year,"provider":cfg["provider"],"type":cfg["type"],
                        "status":stored["availability_status"] if stored else "not_checked","source_venue_id":stored["source_venue_id"] if stored else None,
                        "last_sync_at":stored["last_sync_at"] if stored else None,"year_rule":cfg.get("year_rule"),"paper_count":count,"latest_result":job["status"] if job else "not_checked","error_message":job["error_message"] if job else None})
    return {"default_year":years.default_year(),"available_years":years.available_years(),"current_year":current,"items":out}

@app.get("/api/venues/{venue_id}/status")
def venue_status(venue_id:str,year:int|None=None):
    year = years.resolve(year)
    with db.connect() as con:
        row=con.execute("SELECT * FROM venues WHERE id=? AND year=?",(venue_id,year)).fetchone()
        jobs=[dict(r) for r in con.execute("SELECT * FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 10",(venue_id,year))]
        count=con.execute("SELECT count(*) FROM paper_venues WHERE venue_id=? AND year=?",(venue_id,year)).fetchone()[0]
    return {"venue":dict(row) if row else None,"papers":count,"jobs":jobs}

@app.get("/api/papers")
def papers(venue:str|None=None,year:int|None=None,keyword:str|None=None,status:str|None=None,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=200),sort:str="recent"):
    year = years.resolve(year)
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
    year=_int_param(body.get("year"),years.default_year(),low=1900,high=2200)
    limit=_int_param(body.get("limit"),10,low=1,high=200)
    if not query.strip():
        raise HTTPException(400,"query is required")
    venue=None if venue=="all" else (str(venue) if venue else None)
    info,model=_load_semantic_model_scoped(venue,year)
    if model is None:
        # No fitted semantic model yet: fall back to keyword (FTS) search, but
        # still normalise items to the SemanticItem contract so the frontend can
        # render them identically (slug/year/score are required by the UI).
        return _semantic_items_from_keyword(query,venue,year,limit,"keyword fallback")
    try:
        qvec=np.asarray(model.transform([semantics.expand_aliases(query)])[0],dtype=float).ravel()
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
        sql=("SELECT p.id,p.title,p.abstract,p.authors_json,p.keywords_json,"
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
        scored:list[dict]=[]
        pending:list[tuple[object,str]]=[]
        for r in rows:
            stored=None
            if r["vector_json"]:
                # graph_points is keyed by paper_id only, so a paper that belongs
                # to several venues may carry a vector fitted on a different
                # venue corpus. Only trust it when it was built for this venue.
                if model_venue and (r["point_venue"] or "") == model_venue:
                    try:
                        stored=np.asarray(json.loads(r["vector_json"]),dtype=float).ravel()
                    except Exception:
                        stored=None
            if stored is not None and stored.shape==qvec.shape:
                score=float(_unit(stored)@qvec)
                scored.append({"id":r["id"],"slug":r["id"],"title":r["title"],
                               "abstract":r["abstract"],"authors":json_value(r["authors_json"]),
                               "year":year,"score":round(score,6)})
            else:
                pending.append((r,_graph_docs([r])[0]))
        recovered=0
        if pending:
            try:
                vecs=np.asarray(model.transform([doc for _,doc in pending]),dtype=float)
                for (r,_),v in zip(pending,vecs):
                    v=np.asarray(v,dtype=float).ravel()
                    if v.shape!=qvec.shape:
                        continue
                    score=float(_unit(v)@qvec)
                    scored.append({"id":r["id"],"slug":r["id"],"title":r["title"],
                                   "abstract":r["abstract"],"authors":json_value(r["authors_json"]),
                                   "year":year,"score":round(score,6)})
                    recovered+=1
            except Exception:
                pass
        # Attach the paper's full venue list (the UI renders it), scoped lookup.
        for item in scored:
            item["venues"]=[dict(v) for v in con.execute(
                "SELECT v.id,v.name,pv.year,pv.relation_type FROM paper_venues pv "
                "JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year WHERE pv.paper_id=? "
                "ORDER BY pv.year DESC,v.name",(item["id"],))]
    if not scored:
        # Model exists but no in-scope paper could be scored at all: be explicit
        # rather than returning the misleading empty semantic result.
        return _semantic_items_from_keyword(query,venue,year,limit,
                                            "keyword fallback (no scored papers for this scope)")
    scored.sort(key=lambda x:(-x["score"],x["id"]))
    note=None
    if recovered:
        note=(f"{recovered} of {len(scored)} items scored by embedding their text with the loaded "
              f"model (no stored graph vector for this model's corpus)")
    out=[]
    for item in scored[:limit]:
        out.append({
            "id":item["id"],"slug":item["slug"],"title":item["title"],
            "abstract":item["abstract"] or "","authors":item["authors"],
            "venues":item["venues"],"year":item["year"],"score":item["score"],
        })
    return {"items":out,"method":"semantic-svd","note":note,"query":query}

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
def gaps(venue:str|None=None,year:int|None=None):
    year = years.resolve(year)
    try:
        return _gaps(venue,year)
    except Exception as exc:
        # Degradation contract: never 500 when there is no usable graph/model.
        return {"gaps":[],"topics":[],"note":f"gap detection unavailable: {type(exc).__name__}: {exc}"}
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
def graph_data(venue:str|None=None,year:int|None=None): return graph(venue,years.resolve(year))

@app.get("/api/graph/titles")
def graph_titles(venue:str|None=None,year:int|None=None):
    result=graph(venue,years.resolve(year))
    return {"titles":result["titles"]}

@app.get("/api/graph/search")
def graph_search(q:str,venue:str|None=None,year:int|None=None):
    result=list_papers(venue=venue,year=years.resolve(year),keyword=q,page=1,page_size=10000)
    return {"slugs":[p["id"] for p in result["items"]],"errors":[]}

@app.get("/api/graph/related")
def graph_related(venue:str|None=None,year:int|None=None):
    data=graph(venue,years.resolve(year))
    slugs=data["slugs"]
    index={slug:i for i,slug in enumerate(slugs)}
    with db.connect() as con:
        rows=con.execute("SELECT source_paper_id,target_paper_id FROM paper_relations WHERE relation_type='similar' ORDER BY source_paper_id,score DESC").fetchall()
    edges=[i for row in rows if row[0] in index and row[1] in index for i in (index[row[0]],index[row[1]])]
    return {"slugs":slugs,"edges":edges}

@app.get("/api/graph/{paper_id:path}/minimap")
def graph_minimap(paper_id:str,venue:str|None=None,year:int|None=None):
    all_data=graph(venue,years.resolve(year))
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
            labels=[dict(r) for r in con.execute("SELECT number,label FROM graph_topics WHERE venue_id=? AND level='cluster'",(venue or "",))]
    return {"points":points,"vectors":vectors if all(v is not None for v in vectors) else None,
            "self":local[paper_id],"related":[local[all_data["slugs"][i]] for i in related if all_data["slugs"][i] in local],
            "relatedElsewhere":0,"edges":edges,"clusters":labels}

@app.get("/api/sync/jobs")
def jobs(limit:int=50):
    with db.connect() as con:return {"items":[dict(r) for r in con.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT ?",(limit,))]}
