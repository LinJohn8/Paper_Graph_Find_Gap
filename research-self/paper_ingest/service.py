from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .config import DATA_ROOT, venues
from .openreview import OpenReview, normalize, raw_path, write_raw


def venue_id(key: str, config: dict[str, Any], year: int) -> str:
    pattern = config.get("venue_id_pattern")
    return pattern.format(year=year) if pattern else ""


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
            from .official import sync_official
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


def list_papers(*, venue: str | None = None, year: int = 2027, keyword: str | None = None,
                status: str | None = None, page: int = 1, page_size: int = 50, sort: str = "recent") -> dict[str, Any]:
    venue = None if venue == "all" else venue
    db.init_db()
    clauses, args = [], []
    if venue:
        clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.venue_id=? AND pv.year=?)")
        args.extend([venue,year])
    else:
        clauses.append("EXISTS (SELECT 1 FROM paper_venues pv WHERE pv.paper_id=p.id AND pv.year=?)")
        args.append(year)
    if status:
        clauses.append("p.status=?"); args.append(status)
    join = ""
    if keyword:
        clauses.append("p.id IN (SELECT paper_id FROM papers_fts WHERE papers_fts MATCH ?)")
        args.append(keyword.replace('"',''))
    where = " WHERE " + " AND ".join(clauses)
    order = {"recent":"p.updated_at_source DESC,p.title COLLATE NOCASE", "title":"p.title COLLATE NOCASE", "year":"coalesce(p.publication_year,p.submission_year) DESC"}.get(sort,"p.updated_at_source DESC,p.title COLLATE NOCASE")
    with db.connect() as con:
        total = con.execute("SELECT count(*) FROM papers p"+join+where,args).fetchone()[0]
        rows = con.execute("SELECT DISTINCT p.* FROM papers p"+join+where+f" ORDER BY {order} LIMIT ? OFFSET ?",args+[page_size,(page-1)*page_size]).fetchall()
        output=[]
        for row in rows:
            item=db.paper_dict(row)
            item["venues"]=[dict(v) for v in con.execute("SELECT v.id,v.name,pv.year,pv.relation_type FROM paper_venues pv JOIN venues v ON v.id=pv.venue_id AND v.year=pv.year WHERE pv.paper_id=? ORDER BY pv.year DESC,v.name",(item["id"],)).fetchall()]
            output.append(item)
    return {"items":output,"total":total,"page":page,"page_size":page_size,"pages":max(1,(total+page_size-1)//page_size)}


def graph(venue: str | None = None, year: int = 2027) -> dict[str, Any]:
    venue = None if venue == "all" else venue
    db.init_db()
    with db.connect() as con:
        sql="""SELECT DISTINCT p.id,p.title,p.primary_area,g.x,g.y,g.region,g.cluster,g.vector_json FROM graph_points g
          JOIN papers p ON p.id=g.paper_id JOIN paper_venues pv ON pv.paper_id=p.id WHERE pv.year=?"""
        args: list[Any]=[year]
        if venue:
            sql+=" AND pv.venue_id=?";args.append(venue)
        sql+=" ORDER BY p.id"
        rows=con.execute(sql,args).fetchall()
        kinds=sorted({r["primary_area"] for r in rows if r["primary_area"]})
        area={v:i for i,v in enumerate(kinds)}
        topics={"region":[],"cluster":[]}
        for level in topics:
            if venue:
                records=con.execute("SELECT DISTINCT number,label FROM graph_topics WHERE venue_id=? AND level=? ORDER BY number",(venue,level))
                topics[level]=[dict(r) for r in records]
        return {"kind":venue or "all","slugs":[r["id"] for r in rows],"titles":{r["id"]:r["title"] for r in rows},
                "x":[r["x"] for r in rows],"y":[r["y"] for r in rows],"region":[r["region"] for r in rows],
                "cluster":[r["cluster"] for r in rows],"areas":kinds,"area":[area.get(r["primary_area"]) for r in rows],
                "regions":topics["region"],"clusters":topics["cluster"],
                "vectors":[json.loads(r["vector_json"]) if r["vector_json"] else None for r in rows]}


def generate_graph(venue: str, year: int = 2027) -> dict[str, Any]:
    """Build deterministic CPU-only TF-IDF/SVD coordinates for a small local corpus."""
    import numpy as np
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import KMeans
    from sklearn.manifold import TSNE
    db.init_db()
    with db.connect() as con:
        rows=con.execute("SELECT p.id,p.title,p.abstract FROM papers p JOIN paper_venues pv ON pv.paper_id=p.id WHERE pv.venue_id=? AND pv.year=? ORDER BY p.id",(venue,year)).fetchall()
        if not rows:
            return {"venue":venue,"year":year,"papers":0,"status":"insufficient_data"}
        docs=[(r["title"]+" "+(r["abstract"] or "")).strip() for r in rows]
        if len(rows)<4:
            coords=np.column_stack((np.arange(len(rows),dtype=float),np.zeros(len(rows))))
            vectors=None
        else:
            matrix=TfidfVectorizer(max_features=256,stop_words="english",ngram_range=(1,2)).fit_transform(docs)
            dims=min(32,len(rows)-1,max(2,matrix.shape[1]-1))
            vectors=TruncatedSVD(n_components=dims,random_state=17).fit_transform(matrix)
            if vectors.shape[1]<2:
                vectors=np.column_stack((vectors[:,0],np.zeros(len(rows))))
            coords=TruncatedSVD(n_components=2,random_state=23).fit_transform(matrix) if min(matrix.shape)>2 else np.column_stack((np.arange(len(rows)),np.zeros(len(rows))))
            vectors=vectors/(np.linalg.norm(vectors,axis=1,keepdims=True)+1e-12)
            # The first two TF-IDF singular components create a frequency-driven
            # wedge, not a neighbourhood map. Project the normalized semantic
            # vectors while preserving their local neighbours instead.
            if len(rows) >= 30:
                coords=TSNE(n_components=2,perplexity=min(30.0,(len(rows)-1)/3),
                            init='pca',learning_rate='auto',random_state=23,
                            max_iter=750).fit_transform(vectors)
            # Stable semantic regions from the same title+abstract representation.
            cluster_count=min(24,max(2,int(np.sqrt(len(rows)/40)))) if len(rows)>=8 else 1
            labels=KMeans(n_clusters=cluster_count,random_state=17,n_init=10).fit_predict(vectors) if cluster_count>1 else np.zeros(len(rows),dtype=int)
        if len(rows)<4:
            labels=np.zeros(len(rows),dtype=int)
            cluster_count=1
        coords=coords.astype(float)
        for axis in range(2):
            low,high=coords[:,axis].min(),coords[:,axis].max()
            coords[:,axis]=(coords[:,axis]-low)/(high-low) if high>low else 0.5
        with db.connect() as topic_con:
            topic_con.execute("DELETE FROM graph_topics WHERE venue_id=?", (venue,))
            for c in range(int(cluster_count)):
                topic_con.execute("INSERT INTO graph_topics(venue_id,level,number,label) VALUES(?,?,?,?)", (venue,'cluster',c,f'Topic {c+1}'))
                topic_con.execute("INSERT INTO graph_topics(venue_id,level,number,label) VALUES(?,?,?,?)", (venue,'region',c,f'Region {c+1}'))
        for i,r in enumerate(rows):
            vector=json.dumps(vectors[i].astype(float).tolist()) if vectors is not None else None
            con.execute("INSERT INTO graph_points(paper_id,venue_id,x,y,region,cluster,vector_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET venue_id=excluded.venue_id,x=excluded.x,y=excluded.y,region=excluded.region,cluster=excluded.cluster,vector_json=excluded.vector_json",
                        (r["id"],venue,float(coords[i,0]),float(coords[i,1]),int(labels[i]),int(labels[i]),vector))
        # Similarity is stored as a provenance-labelled relation; no topic model is implied.
        if vectors is not None:
            similarity=vectors@vectors.T
            for i,r in enumerate(rows):
                order=np.argsort(-similarity[i])
                for j in order:
                    if i==j: continue
                    target=rows[int(j)]["id"]
                    con.execute("INSERT INTO paper_relations(source_paper_id,target_paper_id,relation_type,score,provenance) VALUES(?,?,?,?,?) ON CONFLICT(source_paper_id,target_paper_id,relation_type) DO UPDATE SET score=excluded.score,provenance=excluded.provenance",
                                (r["id"],target,"similar",float(similarity[i,j]),"tfidf-svd-cosine-v1"))
                    if con.execute("SELECT count(*) FROM paper_relations WHERE source_paper_id=? AND relation_type='similar'",(r["id"],)).fetchone()[0]>=10: break
    return {"venue":venue,"year":year,"papers":len(rows),"clusters":int(cluster_count),"status":"generated","method":"TF-IDF + normalized SVD + t-SNE neighbourhood projection + KMeans (CPU)"}
