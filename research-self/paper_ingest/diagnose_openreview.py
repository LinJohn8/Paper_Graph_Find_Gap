"""Repeatable public OpenReview access diagnostic; never authenticates or bypasses ACLs."""
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
import httpx

def probe(client, label, path, params=None):
    try:
        r=client.get('https://api2.openreview.net'+path,params=params)
        try: body=r.json(); kind=type(body).__name__
        except ValueError: body=None; kind='non-json'
        return {'check':label,'url':str(r.url),'status_code':r.status_code,'response_type':kind,'summary':(body.get('message') if isinstance(body,dict) else None)}
    except Exception as exc: return {'check':label,'error':f'{type(exc).__name__}: {exc}'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--venue',default='ICLR.cc/2024/Conference');p.add_argument('--output',default=None);a=p.parse_args()
    checks=[]
    with httpx.Client(timeout=30,headers={'User-Agent':'PaperGraph local research diagnostic'}) as c:
        checks.append(probe(c,'api groups','/groups',{'id':a.venue,'limit':1}))
        group=probe(c,'historical group','/groups',{'id':a.venue,'limit':1}); checks.append(group)
        checks.append(probe(c,'public submission invitation','/notes',{'content.venueid':a.venue,'limit':1}))
        checks.append(probe(c,'invitation query','/notes',{'invitation':a.venue+'/-/Submission','limit':1}))
        checks.append(probe(c,'historical note id','/notes',{'id':'nonexistent-paper-graph-diagnostic','limit':1}))
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'venue':a.venue,'checks':checks,'conclusion':'403 on notes is an access restriction; empty results are not evidence that a venue has no papers.'}
    print(json.dumps(result,indent=2))
    if a.output: open(a.output,'w',encoding='utf-8').write(json.dumps(result,indent=2))
if __name__=='__main__':main()
