from bs4 import BeautifulSoup
from paper_ingest import db
from paper_ingest.official import parse_pmlr_index, sync_official
from paper_ingest import official
from fastapi.testclient import TestClient
from paper_ingest.api import app


def test_pmlr_blocks_stable_ids_pdf_and_authors():
    soup=BeautifulSoup('''<div class="paper"><p class="title">A &amp; B</p><span class="authors">Alice,&nbsp;Bob</span><a href="a.html">abs</a><a href="assets/a.pdf">Download PDF</a><a href="https://openreview.net/forum?id=real-id">OpenReview</a></div><div class="paper"><p class="title">Second</p><a href="b.html">abs</a></div>''','html.parser')
    records=parse_pmlr_index(soup,'https://proceedings.mlr.press/v1/')
    assert len(records)==2
    assert records[0]['authors']==['Alice','Bob']
    assert records[0]['pdf_url']=='https://proceedings.mlr.press/v1/assets/a.pdf'
    assert records[0]['forum_id']=='real-id'


def test_official_idempotency_and_preserved_abstract(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'db.sqlite')
    monkeypatch.setattr(db,'DATA_ROOT',tmp_path)
    # `sync_official` now enriches every paper by default: a full run used to land
    # with 0% abstracts because enrichment was silently skipped for unbounded ICML
    # imports. Stub the detail fetch so this remains an offline test.
    monkeypatch.setattr(official,'enrich',lambda source,venue,paper: paper)
    def records(*args):
        return 'https://test.invalid/','test.html',[{'title':'[TEST FIXTURE] Paper','authors':['A'],'paper_url':'https://test.invalid/v1/test.html','abstract':None}]
    monkeypatch.setattr(official,'index_records',records)
    first=sync_official('icml',2025)
    second=sync_official('icml',2025)
    assert first['inserted_count']==1
    assert second['inserted_count']==0
    with db.connect() as con:
        assert con.execute('select count(*) from papers').fetchone()[0]==1
        assert con.execute('pragma foreign_key_check').fetchall()==[]
    dry=sync_official('icml',2026,dry_run=True)
    assert dry['fetched_count']==1 and dry['inserted_count']==0
    with db.connect() as con:
        assert con.execute('select count(*) from sync_jobs').fetchone()[0]==2


def test_failure_logged_without_deleting_papers(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'db.sqlite')
    monkeypatch.setattr(db,'DATA_ROOT',tmp_path)
    def unavailable(*args):raise LookupError('not published')
    monkeypatch.setattr(official,'index_records',unavailable)
    result=sync_official('icml',2027)
    assert result['status']=='not_open'
    with db.connect() as con:
        job=con.execute('select * from sync_jobs').fetchone()
        assert job['error_message']=='LookupError: not published'


def test_namespaced_ids_containing_slash(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'db.sqlite')
    monkeypatch.setattr(db,'DATA_ROOT',tmp_path)
    with TestClient(app) as client:
        with db.connect() as con:
            db.put_venue(con,venue_id='icml',name='ICML',year=2025,provider='test',source_venue_id=None,kind='conference',source_url=None,availability='fixture')
            db.upsert_paper(con,{'id':'test:v1/paper','source':'test','source_paper_id':'v1/paper','title':'[TEST FIXTURE] URL'},'icml',2025)
        assert client.get('/api/papers/test%3Av1%2Fpaper').status_code==200
        assert client.get('/api/papers/test%3Av1%2Fpaper/reviews').json()=={'items':[]}
        assert client.get('/api/papers/test%3Av1%2Fpaper/related').json()['related']==[]
