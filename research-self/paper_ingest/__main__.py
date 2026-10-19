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
