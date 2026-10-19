"""Read-only market-removal, route, graph hash and corpus integrity checks."""
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import httpx

ROOT=Path(__file__).resolve().parents[2]
FRONT=ROOT/'acceptodds'
OUT=ROOT/'Data/cache/phase2-tests'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((ROOT/'Data/cache/phase2-removal-manifest.json').read_text())
    core={p:hashlib.sha256((FRONT/p).read_bytes()).hexdigest() for p in manifest['core_before_sha256']}
    assert core==manifest['core_before_sha256']
    pattern=re.compile(r'\b(market|trade|trading|wallet|auth|portfolio|lmsr|jev|leaderboard|credits|reputation|settlement|acceptodds)\b',re.I)
    hits=[]
    for base in (FRONT/'src',ROOT/'research-self/paper_ingest'):
        for path in base.rglob('*'):
            if path.suffix not in ('.ts','.tsx','.py','.css'):continue
            for number,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
                if pattern.search(line):hits.append(f'{path.relative_to(ROOT)}:{number}:{line}')
    assert not hits,hits
    route_checks=[]
    with httpx.Client(base_url='http://127.0.0.1:3000',timeout=20) as client:
        for route in sorted(manifest['deleted_routes']):
            path='/'+route.removeprefix('src/app/').rsplit('/',1)[0]
            path=re.sub(r'\[[^]]+\]','test',path)
            methods=('GET','POST') if route.endswith('route.ts') else ('GET',)
            for method in methods:
                response=client.request(method,path)
                assert response.status_code==404,(method,path,response.status_code)
                route_checks.append({'method':method,'path':path,'status':404})
    # SQLite backup is non-destructive; schema and rows are read only.
    dbpath=ROOT/'Data/database/papers.sqlite'
    with sqlite3.connect(dbpath) as con:
        count=con.execute('SELECT count(*) FROM papers').fetchone()[0]
        tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        with sqlite3.connect(OUT/'papers-backup.sqlite') as backup:con.backup(backup)
    assert count==0
    raw={str(p.relative_to(ROOT/'Data')):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'Data/raw').rglob('*') if p.is_file()}
    data_status=hashlib.sha256((ROOT/'DATA_SOURCE_STATUS.md').read_bytes()).hexdigest()
    result={'core_sha256':core,'business_hits':hits,'retired_http_checks':route_checks,'paper_count':count,'tables':tables,'raw_sha256':raw,'data_source_status_sha256':data_status,'removed_dependencies':manifest['removed_dependencies'], 'deleted_source_count':len(manifest['deleted_src']),'deleted_page_count':sum(p.endswith('page.tsx') for p in manifest['deleted_routes']),'deleted_handler_count':sum(p.endswith('route.ts') for p in manifest['deleted_routes']),'deleted_api_handler_count':sum(p.startswith('src/app/api/') for p in manifest['deleted_routes'])}
    (OUT/'audit-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(f'PASS: {len(route_checks)} retired HTTP checks; zero business matches; four core hashes unchanged; {count} formal papers; SQLite backup saved')


if __name__=='__main__':main()
