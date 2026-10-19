from __future__ import annotations

import json
from fastapi import FastAPI, HTTPException, Query

from . import db
from .config import venues as venue_registry
from .service import graph, list_papers

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
    out=[]
    for key,cfg in config.items():
        # Keep the compact six-source selector on the default endpoint. Historical
        # years appear once they have an actual sync record, while configured
        # future years remain selectable through the papers API/sync CLI.
        configured = 2027
        stored_years = {r['year'] for r in statuses if r['id']==key}
        for year in sorted({configured} | stored_years, reverse=True):
            stored=keyed.get((key,year))
            with db.connect() as con:
                count=con.execute("SELECT count(DISTINCT paper_id) FROM paper_venues WHERE venue_id=? AND year=?",(key,year)).fetchone()[0]
                job=con.execute("SELECT status,error_message FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 1",(key,year)).fetchone()
            out.append({"id":key,"name":cfg["name"],"year":year,"provider":cfg["provider"],"type":cfg["type"],
                        "status":stored["availability_status"] if stored else "not_checked","source_venue_id":stored["source_venue_id"] if stored else None,
                        "last_sync_at":stored["last_sync_at"] if stored else None,"year_rule":cfg.get("year_rule"),"paper_count":count,"latest_result":job["status"] if job else "not_checked","error_message":job["error_message"] if job else None})
    return {"default_year":2027,"items":out}

@app.get("/api/venues/{venue_id}/status")
def venue_status(venue_id:str,year:int=2027):
    with db.connect() as con:
        row=con.execute("SELECT * FROM venues WHERE id=? AND year=?",(venue_id,year)).fetchone()
        jobs=[dict(r) for r in con.execute("SELECT * FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 10",(venue_id,year))]
        count=con.execute("SELECT count(*) FROM paper_venues WHERE venue_id=? AND year=?",(venue_id,year)).fetchone()[0]
    return {"venue":dict(row) if row else None,"papers":count,"jobs":jobs}

@app.get("/api/papers")
def papers(venue:str|None=None,year:int=2027,keyword:str|None=None,status:str|None=None,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=200),sort:str="recent"):
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
    return item

def json_value(value:str):
    return json.loads(value)

@app.get("/api/graph")
def graph_data(venue:str|None=None,year:int=2027): return graph(venue,year)

@app.get("/api/graph/titles")
def graph_titles(venue:str|None=None,year:int=2027):
    result=graph(venue,year)
    return {"titles":result["titles"]}

@app.get("/api/graph/search")
def graph_search(q:str,venue:str|None=None,year:int=2027):
    result=list_papers(venue=venue,year=year,keyword=q,page=1,page_size=10000)
    return {"slugs":[p["id"] for p in result["items"]],"errors":[]}

@app.get("/api/graph/related")
def graph_related(venue:str|None=None,year:int=2027):
    data=graph(venue,year)
    slugs=data["slugs"]
    index={slug:i for i,slug in enumerate(slugs)}
    with db.connect() as con:
        rows=con.execute("SELECT source_paper_id,target_paper_id FROM paper_relations WHERE relation_type='similar' ORDER BY source_paper_id,score DESC").fetchall()
    edges=[i for row in rows if row[0] in index and row[1] in index for i in (index[row[0]],index[row[1]])]
    return {"slugs":slugs,"edges":edges}

@app.get("/api/graph/{paper_id:path}/minimap")
def graph_minimap(paper_id:str,venue:str|None=None,year:int=2027):
    all_data=graph(venue,year)
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
