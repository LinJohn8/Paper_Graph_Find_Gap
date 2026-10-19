"""Save GitHub metadata/tree/license evidence without copying project code."""
from concurrent.futures import ThreadPoolExecutor
import json
import httpx
from paper_ingest import db

REPOS=['openreview/openreview-py','acl-org/acl-anthology','J535D165/pyalex','Zhang-Henry/paper_collection','dotd/NeurIPS2024Scrape','pranftw/openreview_scraper','genawass/cvpr2026','mlresearch/v267']


def probe(repo):
    row={'repo':repo}
    try:
        with httpx.Client(timeout=20,follow_redirects=True) as c:
            response=c.get('https://api.github.com/repos/'+repo)
            response.raise_for_status();meta=response.json()
            row.update(license=meta.get('license'),updated_at=meta.get('updated_at'),pushed_at=meta.get('pushed_at'),default_branch=meta.get('default_branch'),archived=meta.get('archived'))
            response=c.get('https://api.github.com/repos/'+repo+'/contents')
            response.raise_for_status()
            row['files']=[r['name'] for r in response.json()]
    except Exception as e:row['error']=f'{type(e).__name__}: {e}'
    return row


def main():
    rows=list(ThreadPoolExecutor(4).map(probe,REPOS))
    result={'checked_at':db.now(),'repositories':rows}
    p=db.DATA_ROOT/'logs/github-reuse-evidence.json';p.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
