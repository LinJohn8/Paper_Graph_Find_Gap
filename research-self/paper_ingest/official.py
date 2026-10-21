"""Metadata-only official proceedings adapters. No PDFs or private data fetched."""
from __future__ import annotations
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, parse_qs
import httpx
from bs4 import BeautifulSoup
from . import db
from . import crawl

PROVIDERS={'icml':'pmlr','acl':'acl_anthology','cvpr':'cvf','neurips':'neurips_proceedings'}
NAMES={'icml':'ICML','acl':'ACL','cvpr':'CVPR','neurips':'NeurIPS'}

#: Detail pages are network bound; one-at-a-time is far too slow for a 3-6k
#: paper proceedings year. A small pool keeps a single venue polite.
DEFAULT_WORKERS=int(os.environ.get('PAPER_HTTP_WORKERS','8') or 8)


class PublicSource:
    """Cached HTTP source for one publisher's venue-year directory.

    Clients are per-thread. Detail-page enrichment fans out over a thread pool,
    and a pooled ``httpx.Client`` shared between threads serialises on one
    connection pool and lets one thread's dead socket poison every other thread.
    Each thread lazily creates (and can rebuild) its own client instead.
    """

    def __init__(self, provider, venue, year):
        self.root=db.DATA_ROOT/'raw'/provider/venue/str(year)
        self._local=threading.local()
        self._clients=[]
        self._clients_lock=threading.Lock()
        # The shared retry policy (paper_ingest/crawl.py). Six attempts and a
        # 30 s ceiling: a proceedings year is thousands of detail pages, and a
        # single dropped keep-alive connection must not end the year.
        self._policy=crawl.RetryPolicy(attempts=6,base_delay=1.0,max_delay=30.0,jitter=0.0)

    def _new_client(self):
        client=httpx.Client(timeout=35,follow_redirects=True,
                            headers={'User-Agent':'PaperGraph/0.1 public research metadata'})
        with self._clients_lock:
            self._clients.append(client)
        return client

    @property
    def client(self):
        client=getattr(self._local,'client',None)
        if client is None:
            client=self._new_client()
            self._local.client=client
        return client

    def close(self):
        """Close every client this source created, on every thread."""
        with self._clients_lock:
            clients,self._clients=self._clients,[]
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        self._local.client=None

    def _fresh_client(self):
        """Replace this thread's pooled client after a transport failure.

        A dropped keep-alive connection (httpx raises ``RemoteProtocolError``, a
        ``TransportError``) poisons the pool: every subsequent request on the same
        client fails the same way. Rebuilding the client is what lets the retry
        actually succeed instead of burning all attempts on a dead socket.
        """
        client=getattr(self._local,'client',None)
        self._local.client=None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            with self._clients_lock:
                try:
                    self._clients.remove(client)
                except ValueError:
                    pass

    def get(self,url):
        self.root.mkdir(parents=True,exist_ok=True)
        path=self.root/(hashlib.sha256(url.encode()).hexdigest()+'.html')
        # Cached snapshots allow resuming large metadata batches without refetching.
        if path.exists(): return BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser'),str(path)
        # More than 3 attempts: a single proceedings year is thousands of detail
        # pages, and under concurrent load a connection drops often enough that
        # 3 tries is not enough to finish the year. Observed: ACL 2023 ended
        # `failed` after 812 papers on `ConnectError: [WinError 10054]`.
        #
        # The decision "is this failure worth retrying?" and the wait itself
        # come from the shared crawl policy (paper_ingest/crawl.py), so this
        # harvester and the API collectors agree on what a 429, a 403 and a
        # dropped socket each mean. The arithmetic is unchanged from what this
        # method always used: min(2**attempt, 30).
        policy=self._policy
        for attempt in range(policy.attempts):
            try:
                response=self.client.get(url)
                status=response.status_code
                if status==429 or status>=500:
                    # Retryable: the server is overloaded or rate-limiting.
                    if attempt+1<policy.attempts:
                        time.sleep(policy.delay_for(attempt));continue
                    response.raise_for_status()
                response.raise_for_status()
                response.encoding='utf-8'
                path.write_text(response.text,encoding='utf-8')
                path.with_suffix('.json').write_text(json.dumps({'url':url,'retrieved_at':db.now(),'status':response.status_code}),encoding='utf-8')
                return BeautifulSoup(response.text,'html.parser'),str(path)
            except (httpx.TimeoutException,httpx.TransportError) as exc:
                # TransportError covers RemoteProtocolError (a keep-alive drop),
                # ReadError, ConnectError and NetworkError. Catching only
                # NetworkError let a dropped connection abort a whole year's
                # harvest after a handful of papers -- observed on the ACL
                # Anthology, which closes keep-alive after ~6 requests.
                #
                # Both RemoteProtocolError and ConnectError (WinError 10054) mean
                # the pooled connection is dead, so rebuild it before retrying;
                # otherwise every attempt fails on the same poisoned socket.
                if isinstance(exc, (httpx.RemoteProtocolError, httpx.ConnectError)):
                    self._fresh_client()
                if attempt+1>=policy.attempts:raise
                time.sleep(policy.delay_for(attempt))
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
        # The PMLR index lists workshop volumes *before* the main conference, so a
        # naive "first entry mentioning ICML <year>" match silently returns a
        # workshop instead of the main proceedings. The shipped code keyed only on
        # "workshop vs main" with a word-boundary regex, which classified
        # "Proceedings of ICML 2022 Workshop on Healthcare AI and COVID-19" (v184)
        # as a main volume; v184 sorts before the real main volume v162, so ICML
        # 2022 imported 14 workshop papers instead of 1,233. Require the whole
        # label to BE "Proceedings of ICML <year>".
        matches=[]
        main_re=re.compile(rf'^Proceedings of ICML {year}$')
        for li in soup.select('li'):
            text=li.get_text(' ',strip=True)
            if f'ICML {year}' not in text or not li.find('a'):
                continue
            href=li.find('a')['href'].rstrip('/')+'/'
            # The <li> reads "Volume 162 Proceedings of ICML 2022"; workshop rows
            # read "Volume 184 Proceedings of <Workshop> at ICML 2022".
            label=re.sub(r'^Volume \d+\s*','',text).strip()
            is_main = main_re.match(label) is not None or label==f'ICML {year} Proceedings'
            matches.append((0 if is_main else 1, href, label))
        if matches:
            matches.sort(key=lambda m:(m[0],m[1]))
            return urljoin(base,matches[0][1]),None
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
        if meta.get(key):
            # PMLR emits citation_title with UNESCAPED double quotes, e.g.
            # content="On the "Induction Bias" in Sequence Models"; BeautifulSoup
            # then parses the content as `On the ` and the index title (correct,
            # complete) was overwritten by the truncated fragment. Only accept a
            # meta title when it is strictly longer than the index title.
            if key=='citation_title' and len(meta[key][0])<=len(paper.get(field) or ''):
                continue
            paper[field]=meta[key][0]
    if meta.get('citation_author'):paper['authors']=meta['citation_author']
    selectors={'icml':'#abstract','acl':'.acl-abstract','cvpr':'#abstract','neurips':'.abstract'}
    abstract=soup.select_one(selectors[venue])
    if not abstract:
        heading=next((h for h in soup.select('h2,h3,h4') if h.get_text(strip=True)=='Abstract'),None)
        if heading:abstract=heading.find_next('p')
    if abstract:paper['abstract']=abstract.get_text(' ',strip=True)
    paper['raw_data_path']=raw
    return paper


def _normalize_title_key(title: str | None) -> str:
    """Comparison key for cross-source title matching.

    Sources differ in punctuation, capitalisation and LaTeX, so an exact match
    misses most twins. Lowercasing and dropping non-alphanumerics catches
    "DeBayes: a Bayesian Method" vs "DeBayes - A Bayesian Method".
    """
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def _find_existing_for_official(con, venue: str, year: int, paper: dict):
    """Find a paper already stored from a different source for this venue-year.

    The official adapters used to check only ``(provider, paper_url)``, so a paper
    previously imported from Crossref/arXiv was inserted AGAIN under the official
    id. On real data that produced ~37% duplicate rows (cvpr 2023: 1452 duplicate
    rows out of 3849) and inflated every corpus count.

    Matching is by normalized title within the same venue-year (titles are stable
    across sources; DOIs often are not, because proceedings records frequently
    lack one until enrichment). Returns the existing row or ``None``.
    """
    key = _normalize_title_key(paper.get("title"))
    if len(key) < 20:
        # Too short to be a reliable identity; a false merge would be worse than
        # a duplicate, so fall through to insertion.
        return None
    rows = con.execute(
        """SELECT p.* FROM papers p
           JOIN paper_venues pv ON pv.paper_id=p.id
           WHERE pv.venue_id=? AND pv.year=?""",
        (venue, year),
    ).fetchall()
    for row in rows:
        if _normalize_title_key(row["title"]) == key:
            return row
    return None


def _preload_title_index(con, venue: str, year: int) -> dict:
    """Normalized title -> stored row for one venue-year, loaded once.

    The old ``_find_existing_for_official`` re-read every row of the venue-year
    for every paper (O(n^2): a 5,887-paper NeurIPS year meant ~35M row reads).
    One dict load makes the whole import O(n).
    """
    index: dict = {}
    for row in con.execute(
        """SELECT p.* FROM papers p
           JOIN paper_venues pv ON pv.paper_id=p.id
           WHERE pv.venue_id=? AND pv.year=?""",
        (venue, year),
    ):
        index.setdefault(_normalize_title_key(row["title"]), row)
    return index


def _enrich_many(source, venue: str, papers: list, workers: int) -> int:
    """Fetch every paper's detail page concurrently; return the failure count.

    One dead detail page (genuine 404s exist in CVPR 2022) used to abort the whole
    year after 97 papers. A page that cannot be fetched now costs only its own
    abstract, and the pool is what makes a 3-6k-paper year finish in minutes.
    """
    errors = 0
    lock = threading.Lock()

    def work(paper):
        nonlocal errors
        try:
            enrich(source, venue, paper)
        except Exception:  # noqa: BLE001 - one page must never kill the year
            with lock:
                errors += 1

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        list(pool.map(work, papers))
    return errors


def sync_official(venue,year,limit=None,dry_run=False,workers=None):
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
        # Detail-page enrichment is on by default: without abstracts the semantic
        # search/gap features have nothing to work on (measured: icml 2023 landed
        # with 0% abstract coverage). PAPER_SKIP_ENRICH=1 keeps the fast
        # index-only path for a separate, resumable enrich pass.
        skip_enrich = os.environ.get('PAPER_SKIP_ENRICH','').strip() in ('1','true','yes')
        if not skip_enrich:
            counts['error_count'] += _enrich_many(source, venue, selected, workers or DEFAULT_WORKERS)
        con=None
        if not dry_run:
            con=db.connect()
            db.put_venue(con,venue_id=venue,name=NAMES[venue],year=year,provider=provider,source_venue_id=url,kind='conference',source_url=url,availability='available')
            index=_preload_title_index(con,venue,year)
        try:
            for position,paper in enumerate(selected):
                paper.setdefault('raw_data_path',raw)
                identifier=urlparse(paper['paper_url']).path.strip('/').removesuffix('.html')
                paper.update(id=f'{provider}:{identifier}',source=provider,source_paper_id=identifier,publication_year=year,status='published')
                counts['fetched_count']+=1
                if dry_run:continue
                # Cross-source dedup. A previous harvester (Crossref/arXiv) may
                # already hold this exact paper under a different id scheme;
                # without this check the corpus double-counted every paper that
                # was imported twice (measured 37% duplicate rows on cvpr 2023/24).
                title_key=_normalize_title_key(paper['title'])
                existing=index.get(title_key) if len(title_key)>=20 else None
                if existing is None:
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
                if len(title_key)>=20:
                    index.setdefault(title_key,con.execute('SELECT * FROM papers WHERE id=?',(paper['id'],)).fetchone())
                # Commit in batches: one transaction for a whole 6k-paper year
                # holds the write lock for minutes and blocks every other venue.
                if position%200==199:
                    con.commit()
            if con is not None:con.commit()
        finally:
            if con is not None:con.close()
        status='partial' if limit and len(papers)>limit else 'success'
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}';counts['error_count']+=1
        status='not_open' if isinstance(exc,LookupError) or (isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==404) else ('restricted' if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (401,403) else 'failed')
    finally:
        source.close()
        if not dry_run:
            with db.connect() as con:
                if not con.execute('SELECT 1 FROM venues WHERE id=? AND year=?',(venue,year)).fetchone():
                    db.put_venue(con,venue_id=venue,name=NAMES[venue],year=year,provider=provider,source_venue_id=url,kind='conference',source_url=url,availability=status)
                con.execute('UPDATE sync_jobs SET status=?,finished_at=?,fetched_count=?,inserted_count=?,updated_count=?,skipped_count=?,error_count=?,error_message=? WHERE id=?',(status,db.now(),*counts.values(),error,job))
                con.execute('UPDATE venues SET last_sync_at=?,availability_status=? WHERE id=? AND year=?',(db.now(),'available' if counts['fetched_count'] else status,venue,year))
    return {'venue':venue,'year':year,'provider':provider,'status':status,**counts,'error':error}


def sync_pmlr_icml(year,limit=None):return sync_official('icml',year,limit)

def enrich_missing_abstracts(venue: str, year: int, limit: int | None = None,
                             workers: int | None = None) -> dict:
    """Fill only empty abstracts from cached/official detail pages.

    Deliberately separate from index synchronization so a large index import
    stays cheap; enrichment is resumable and can run concurrently with other
    venue-years. Fetches are parallel (network bound); DB writes stay serial on
    one connection so a 6k-paper pass does not open 6k SQLite connections.
    """
    provider = PROVIDERS[venue]
    source = PublicSource(provider, venue, year)
    db.init_db()
    with db.connect() as con:
        # Only enrich rows that actually came from *this* publisher's index.
        #
        # The old query took any abstract-less row for the venue-year ordered by
        # id, but ids are namespaced ('crossref:...', 'cvf:...', 'arxiv:...') so
        # 'crossref:' sorted first and every batch was spent on Crossref doi.org
        # landing pages with no abstract (measured: 0 updates / 99 wasted fetches
        # in a 100-row batch). Filtering on the official source makes yield ~100%.
        rows = con.execute("""SELECT p.id,p.paper_url FROM papers p
          JOIN paper_venues pv ON pv.paper_id=p.id
          WHERE pv.venue_id=? AND pv.year=? AND (p.abstract IS NULL OR trim(p.abstract)='')
            AND p.source=?
          ORDER BY p.id LIMIT ?""", (venue, year, provider, limit or -1)).fetchall()
    found: list[tuple[str, str]] = []
    skipped = 0
    errors = 0
    lock = threading.Lock()

    def work(row):
        nonlocal skipped, errors
        try:
            paper = {'paper_url': row['paper_url']}
            enrich(source, venue, paper)
            abstract = (paper.get('abstract') or '').strip()
            if not abstract:
                with lock:
                    skipped += 1
                return
            with lock:
                found.append((row['id'], abstract))
        except Exception:  # noqa: BLE001 - one page must not stop the batch
            with lock:
                errors += 1

    if rows:
        with ThreadPoolExecutor(max_workers=max(1, int(workers or DEFAULT_WORKERS))) as pool:
            list(pool.map(work, rows))

    updated = 0
    con = db.connect()
    try:
        for position, (paper_id, abstract) in enumerate(found):
            con.execute("UPDATE papers SET abstract=?,updated_at_source=?,last_seen_at=? WHERE id=? AND (abstract IS NULL OR trim(abstract)='')",
                        (abstract, db.now(), db.now(), paper_id))
            # Rowid-keyed FTS maintenance (see db._align_fts_rowids): deleting by
            # the UNINDEXED paper_id scans the whole FTS table (135 ms/row).
            p = con.execute("SELECT rowid,* FROM papers WHERE id=?", (paper_id,)).fetchone()
            if p is not None:
                con.execute("INSERT OR REPLACE INTO papers_fts(rowid,paper_id,title,abstract,authors,keywords,venue) VALUES(?,?,?,?,?,?,?)",
                            (p['rowid'], paper_id, p['title'], abstract, p['authors_json'], p['keywords_json'], venue))
                updated += 1
            if position % 200 == 199:
                con.commit()
        con.commit()
    finally:
        con.close()
        source.close()
    return {'venue': venue, 'year': year, 'examined': len(rows), 'updated': updated,
            'skipped': skipped, 'errors': errors}
