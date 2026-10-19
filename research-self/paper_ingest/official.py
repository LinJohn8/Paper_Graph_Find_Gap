"""Metadata-only official proceedings adapters. No PDFs or private data fetched."""
from __future__ import annotations
import hashlib
import json
import time
from urllib.parse import urljoin, urlparse, parse_qs
import httpx
from bs4 import BeautifulSoup
from . import db

PROVIDERS={'icml':'pmlr','acl':'acl_anthology','cvpr':'cvf','neurips':'neurips_proceedings'}
NAMES={'icml':'ICML','acl':'ACL','cvpr':'CVPR','neurips':'NeurIPS'}


class PublicSource:
    def __init__(self, provider, venue, year):
        self.root=db.DATA_ROOT/'raw'/provider/venue/str(year)
        self.client=httpx.Client(timeout=35,follow_redirects=True,headers={'User-Agent':'PaperGraph/0.1 public research metadata'})

    def get(self,url):
        self.root.mkdir(parents=True,exist_ok=True)
        path=self.root/(hashlib.sha256(url.encode()).hexdigest()+'.html')
        # Cached snapshots allow resuming large metadata batches without refetching.
        if path.exists(): return BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser'),str(path)
        for attempt in range(3):
            try:
                response=self.client.get(url)
                if response.status_code==429 or response.status_code>=500:
                    if attempt<2: time.sleep(2**attempt);continue
                response.raise_for_status()
                response.encoding='utf-8'
                path.write_text(response.text,encoding='utf-8')
                path.with_suffix('.json').write_text(json.dumps({'url':url,'retrieved_at':db.now(),'status':response.status_code}),encoding='utf-8')
                return BeautifulSoup(response.text,'html.parser'),str(path)
            except (httpx.TimeoutException,httpx.NetworkError):
                if attempt==2:raise
                time.sleep(2**attempt)
        raise RuntimeError('request retries exhausted')


def parse_pmlr_index(soup,base):
    papers=[]
    for block in soup.select('div.paper'):
        title=block.select_one('.title'); authors=block.select_one('.authors')
        link=next((a['href'] for a in block.select('a[href]') if a.get_text(strip=True).lower()=='abs'),None)
        pdf=next((a['href'] for a in block.select('a[href]') if 'pdf' in a.get_text(strip=True).lower()),None)
        forum=next((parse_qs(urlparse(a['href']).query).get('id',[None])[0] for a in block.select('a[href]') if 'openreview.net/forum' in a['href']),None)
        if title and link:
            papers.append({'title':title.get_text(' ',strip=True),'authors':[a.strip() for a in authors.get_text(' ',strip=True).split(',') if a.strip()] if authors else [],'paper_url':urljoin(base,link),'pdf_url':urljoin(base,pdf) if pdf else None,'forum_id':forum})
    return papers


def discover(source,venue,year):
    if venue=='icml':
        base='https://proceedings.mlr.press/'
        soup,_=source.get(base)
        for li in soup.select('li'):
            text=li.get_text(' ',strip=True)
            if f'ICML {year}' in text and li.find('a'):
                url=urljoin(base,li.find('a')['href'].rstrip('/')+'/')
                return url,None
        raise LookupError(f'No ICML {year} volume listed in official PMLR index')
    urls={'acl':f'https://aclanthology.org/events/acl-{year}/','cvpr':f'https://openaccess.thecvf.com/CVPR{year}?day=all','neurips':f'https://proceedings.neurips.cc/paper_files/paper/{year}'}
    return urls[venue],None


def index_records(source,venue,year):
    url,_=discover(source,venue,year)
    soup,raw=source.get(url)
    if venue=='icml': records=parse_pmlr_index(soup,url)
    elif venue=='cvpr':
        records=[{'title':a.get_text(' ',strip=True),'paper_url':urljoin(url,a['href'])} for a in soup.select('dt.ptitle a[href]')]
    elif venue=='neurips':
        pages=[(soup,'Creative AI Track' if year>=2025 else 'Main conference')]
        # Newer proceedings split the main conference into a linked volume page.
        # Discover that link from the official root rather than guessing its slug.
        for a in soup.select('a[href]'):
            if a['href'].startswith(f'/paper_files/paper/{year}/vol'):
                child,_=source.get(urljoin(url,a['href']))
                pages.insert(0,(child,a.get_text(' ',strip=True)))
        records=[{'title':a.get_text(' ',strip=True),'paper_url':urljoin(url,a['href']),'primary_area':a['href'].split('-Abstract-',1)[1].removesuffix('.html').replace('_',' ') if '-Abstract-' in a['href'] else track} for page,track in pages for a in page.select('a[href]') if 'Abstract' in a['href'] and a['href'].endswith('.html')]
    else:
        records=[]
        for a in soup.select('a[href]'):
            href=a['href']; text=a.get_text(' ',strip=True)
            if href.startswith(f'/{year}.acl-') and href.rstrip('/').split('.')[-1].isdigit() and int(href.rstrip('/').split('.')[-1])>0 and text:
                records.append({'title':text,'paper_url':urljoin(url,href),'primary_area':href.strip('/').rsplit('.',1)[0]})
    return url,raw,list({p['paper_url']:p for p in records}.values())


def enrich(source,venue,paper):
    soup,raw=source.get(paper['paper_url'])
    meta={}
    for m in soup.select('meta[name][content]'):
        meta.setdefault(m['name'].lower(),[]).append(m['content'])
    for key,field in [('citation_title','title'),('citation_pdf_url','pdf_url'),('citation_doi','doi')]:
        if meta.get(key):paper[field]=meta[key][0]
    if meta.get('citation_author'):paper['authors']=meta['citation_author']
    selectors={'icml':'#abstract','acl':'.acl-abstract','cvpr':'#abstract','neurips':'.abstract'}
    abstract=soup.select_one(selectors[venue])
    if not abstract:
        heading=next((h for h in soup.select('h2,h3,h4') if h.get_text(strip=True)=='Abstract'),None)
        if heading:abstract=heading.find_next('p')
    if abstract:paper['abstract']=abstract.get_text(' ',strip=True)
    paper['raw_data_path']=raw
    return paper


def sync_official(venue,year,limit=None,dry_run=False):
    provider=PROVIDERS[venue]; started=db.now()
    counts={'fetched_count':0,'inserted_count':0,'updated_count':0,'skipped_count':0,'error_count':0}
    source=PublicSource(provider,venue,year)
    db.init_db(); job=None
    if not dry_run:
        with db.connect() as con:
            job=con.execute('INSERT INTO sync_jobs(venue,year,provider,status,started_at) VALUES(?,?,?,?,?)',(venue,year,provider,'running',started)).lastrowid
    error=None; status='failed';url=None
    try:
        url,raw,papers=index_records(source,venue,year)
        if not papers:raise RuntimeError('Source responded, but no paper records parsed; not assumed empty')
        selected=papers[:limit] if limit else papers
        for paper in selected:
            # Keep the entire PMLR index import cheap; detail enrichment is bounded
            # on a small run. Subsequent runs can enrich any missing paper fields.
            if limit or venue!='icml':enrich(source,venue,paper)
            paper.setdefault('raw_data_path',raw)
            identifier=urlparse(paper['paper_url']).path.strip('/').removesuffix('.html')
            paper.update(id=f'{provider}:{identifier}',source=provider,source_paper_id=identifier,publication_year=year,status='published')
            counts['fetched_count']+=1
            if dry_run:continue
            with db.connect() as con:
                db.put_venue(con,venue_id=venue,name=NAMES[venue],year=year,provider=provider,source_venue_id=url,kind='conference',source_url=url,availability='available')
                # Preserve the sole legacy PMLR record via its exact original URL,
                # not by fuzzy title matching or its former unstable index ID.
                existing=con.execute('SELECT * FROM papers WHERE source=? AND paper_url=?',(provider,paper['paper_url'])).fetchone()
                if existing:
                    paper['id']=existing['id'];paper['source_paper_id']=existing['source_paper_id']
                    for k in ('abstract','pdf_url','doi','forum_id','primary_area'):
                        if not paper.get(k) and existing[k]:paper[k]=existing[k]
                    if not paper.get('authors'):paper['authors']=json.loads(existing['authors_json'])
                    unchanged=all(paper.get(k)==existing[k] for k in ('title','abstract','pdf_url','doi','forum_id','primary_area','publication_year','status')) and paper.get('authors',[])==json.loads(existing['authors_json'])
                    if unchanged:
                        con.execute('UPDATE papers SET last_seen_at=? WHERE id=?',(db.now(),paper['id']))
                        counts['skipped_count']+=1
                        continue
                db.upsert_paper(con,paper,venue,year,relation_type='publication')
                counts['updated_count' if existing else 'inserted_count']+=1
        status='partial' if limit and len(papers)>limit else 'success'
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}';counts['error_count']+=1
        status='not_open' if isinstance(exc,LookupError) or (isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==404) else ('restricted' if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (401,403) else 'failed')
    finally:
        source.client.close()
        if not dry_run:
            with db.connect() as con:
                if not con.execute('SELECT 1 FROM venues WHERE id=? AND year=?',(venue,year)).fetchone():
                    db.put_venue(con,venue_id=venue,name=NAMES[venue],year=year,provider=provider,source_venue_id=url,kind='conference',source_url=url,availability=status)
                con.execute('UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,inserted_count=?,updated_count=?,skipped_count=?,error_count=?,error_message=? WHERE id=?',(status,db.now(),*counts.values(),error,job))
                con.execute('UPDATE venues SET last_sync_at=?,availability_status=? WHERE id=? AND year=?',(db.now(),'available' if counts['fetched_count'] else status,venue,year))
    return {'venue':venue,'year':year,'provider':provider,'status':status,**counts,'error':error}


def sync_pmlr_icml(year,limit=None):return sync_official('icml',year,limit)

def enrich_missing_abstracts(venue: str, year: int, limit: int | None = None) -> dict:
    """Fill only empty abstracts from cached/official detail pages.

    This is deliberately separate from index synchronization so large proceedings
    imports remain cheap and can be enriched in resumable batches.
    """
    provider = PROVIDERS[venue]
    source = PublicSource(provider, venue, year)
    db.init_db(); updated = skipped = errors = 0
    try:
        with db.connect() as con:
            rows = con.execute("""SELECT p.id,p.paper_url FROM papers p
              JOIN paper_venues pv ON pv.paper_id=p.id
              WHERE pv.venue_id=? AND pv.year=? AND (p.abstract IS NULL OR trim(p.abstract)='')
              ORDER BY p.id LIMIT ?""", (venue, year, limit or -1)).fetchall()
        for row in rows:
            try:
                paper = {'paper_url': row['paper_url']}
                enrich(source, venue, paper)
                abstract = (paper.get('abstract') or '').strip()
                if not abstract:
                    skipped += 1; continue
                with db.connect() as con:
                    con.execute("UPDATE papers SET abstract=?,updated_at_source=?,last_seen_at=? WHERE id=? AND (abstract IS NULL OR trim(abstract)='')",
                                (abstract, db.now(), db.now(), row['id']))
                    con.execute("DELETE FROM papers_fts WHERE paper_id=?", (row['id'],))
                    p = con.execute("SELECT * FROM papers WHERE id=?", (row['id'],)).fetchone()
                    con.execute("INSERT INTO papers_fts(paper_id,title,abstract,authors,keywords,venue) VALUES(?,?,?,?,?,?)",
                                (row['id'],p['title'],abstract,p['authors_json'],p['keywords_json'],venue))
                updated += 1
            except Exception:
                errors += 1
    finally:
        source.client.close()
    return {'venue': venue, 'year': year, 'examined': len(rows), 'updated': updated, 'skipped': skipped, 'errors': errors}
