from __future__ import annotations

import argparse
import asyncio
import json

from . import db
from .config import venues
from .service import sync_many

def main():
    parser=argparse.ArgumentParser(prog="paper_ingest")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("venues")
    sync=sub.add_parser("sync");sync.add_argument("--venue",required=True);sync.add_argument("--year",type=int,default=2027)
    sync.add_argument("--full",action="store_true");sync.add_argument("--dry-run",action="store_true")
    sync.add_argument("--limit",type=int,default=None,help="limit official proceedings records (useful for a smoke test)")
    # Multi-source API harvest: the resilient path. Several official APIs
    # (OpenAlex/Crossref/arXiv) are tried in order with automatic failover, so a
    # blocked or quota-limited source no longer fails the whole venue-year.
    api=sub.add_parser("sync-api",help="harvest via official APIs (resilient multi-source)")
    api.add_argument("--venue",required=True)
    api.add_argument("--year",type=int,default=2027)
    api.add_argument("--limit",type=int,default=None)
    api.add_argument("--target",type=int,default=None,help="stop after this many unique papers")
    api.add_argument("--dry-run",action="store_true")
    api.add_argument("--mailto",default=None,help="contact email for polite API pools")
    api.add_argument("--api-key",default=None,help="OpenAlex API key (or set OPENALEX_API_KEY)")
    api.add_argument("--arxiv-delay",type=float,default=3.0,help="seconds between arXiv requests")
    api_span=sub.add_parser("sync-api-years",help="harvest several years via official APIs")
    api_span.add_argument("--venue",required=True)
    api_span.add_argument("--from-year",type=int,default=2022)
    api_span.add_argument("--to-year",type=int,default=2026)
    api_span.add_argument("--limit",type=int,default=None)
    api_span.add_argument("--dry-run",action="store_true")
    span=sub.add_parser("sync-years");span.add_argument("--venue",required=True);span.add_argument("--from-year",type=int,default=2022);span.add_argument("--to-year",type=int,default=2026);span.add_argument("--limit",type=int,default=None)
    sub.add_parser("status")
    graph=sub.add_parser("graph");graph.add_argument("--venue")
    build=sub.add_parser("build-graph");build.add_argument("--venue",required=True);build.add_argument("--year",type=int,default=2027)
    enrich=sub.add_parser("enrich-abstracts");enrich.add_argument("--venue",required=True);enrich.add_argument("--year",type=int,default=2027);enrich.add_argument("--limit",type=int,default=100)
    args=parser.parse_args();registry=venues()
    if args.command=='sync' and args.limit is not None and args.limit<1:
        parser.error('--limit must be positive')
    if args.command=="venues":
        print(json.dumps({"default_year":2027,"venues":registry},indent=2,ensure_ascii=False));return
    db.init_db()
    if args.command=="sync":
        keys=list(registry) if args.venue=="all" else [args.venue]
        results=asyncio.run(sync_many(keys,args.year,full=args.full,dry_run=args.dry_run,limit=args.limit))
        print(json.dumps(results,indent=2,ensure_ascii=False));return
    if args.command=="sync-years":
        if args.from_year>args.to_year: parser.error('--from-year must be <= --to-year')
        keys=list(registry) if args.venue=='all' else [args.venue]
        all_results=[]
        for year in range(args.from_year,args.to_year+1):
            all_results.extend(asyncio.run(sync_many(keys,year,limit=args.limit)))
        print(json.dumps(all_results,indent=2,ensure_ascii=False));return
    if args.command=="sync-api":
        from .sync_api import sync_api
        result=sync_api(args.venue,args.year,limit=args.limit,target=args.target,
                        dry_run=args.dry_run,mailto=args.mailto,api_key=args.api_key,
                        arxiv_delay=args.arxiv_delay)
        print(json.dumps(result,indent=2,ensure_ascii=False));return
    if args.command=="sync-api-years":
        if args.from_year>args.to_year: parser.error('--from-year must be <= --to-year')
        from .sync_api import sync_api
        span_results=[]
        for year in range(args.from_year,args.to_year+1):
            span_results.append(sync_api(args.venue,year,limit=args.limit,dry_run=args.dry_run))
        print(json.dumps(span_results,indent=2,ensure_ascii=False));return
    if args.command=="status":
        with db.connect() as con:
            print(json.dumps([dict(r) for r in con.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 30")],indent=2,ensure_ascii=False));return
    if args.command=="graph":
        from .service import graph
        print(json.dumps(graph(args.venue,2027),ensure_ascii=False));return
    if args.command=="build-graph":
        from .service import generate_graph
        print(json.dumps(generate_graph(args.venue,args.year),indent=2,ensure_ascii=False));return
    if args.command=="enrich-abstracts":
        from .official import enrich_missing_abstracts
        print(json.dumps(enrich_missing_abstracts(args.venue,args.year,args.limit),indent=2,ensure_ascii=False));return

if __name__=="__main__":main()
