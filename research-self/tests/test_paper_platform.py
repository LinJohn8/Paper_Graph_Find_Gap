"""Isolated, explicitly labelled fixtures; never writes to the personal corpus."""
import pytest
from fastapi.testclient import TestClient
from paper_ingest import db
from paper_ingest.api import app


def populate(path):
    db.init_db(path)
    with db.connect(path) as con:
        for venue in ('iclr','cvpr'):
            db.put_venue(con,venue_id=venue,name=venue.upper(),year=2027,provider='test-fixture',source_venue_id=None,kind='conference',source_url=None,availability='fixture')
        for i in range(3):
            paper={'id':f'test-{i}','source':'test-fixture','source_paper_id':str(i),'title':f'[TEST FIXTURE] Graph retrieval {i}', 'abstract':'Fixture only; not a real academic paper.', 'authors':['Fixture Author'], 'keywords':['retrieval'], 'submission_year':2027,'status':'accepted' if i == 0 else 'submitted'}
            db.upsert_paper(con,paper,'cvpr' if i == 2 else 'iclr',2027)
            con.execute('INSERT OR IGNORE INTO graph_points(paper_id,venue_id,x,y,region,cluster,vector_json,year) VALUES(?,?,?,?,?,?,?,?)',(paper['id'],'cvpr' if i == 2 else 'iclr',0.2+i*0.3,0.2+i*0.2,0,0,'[1,0,0]',2027))
        con.execute("INSERT OR IGNORE INTO paper_relations VALUES('test-0','test-1','similar',0.9,'test-fixture')")
        for visibility in ('public','private'):
            con.execute('INSERT OR IGNORE INTO reviews(id,paper_id,source_review_id,review_type,content,visibility) VALUES(?,?,?,?,?,?)',(visibility,'test-0',visibility,'official_review','[TEST FIXTURE] Review',visibility))


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'test.sqlite')
    monkeypatch.setattr(db,'DATA_ROOT',tmp_path)
    with TestClient(app) as client:
        yield client


def test_empty_platform_and_removed_endpoints(client):
    assert client.get('/api/papers').json()['pages'] == 1
    assert client.get('/api/graph?venue=all').json()['slugs'] == []
    # The registry decides: 6 conference venues + TMLR + the curated journals.
    from paper_ingest.config import venues as venue_registry
    assert len(client.get('/api/venues').json()['items']) == len(venue_registry())
    assert all(v['paper_count']==0 for v in client.get('/api/venues').json()['items'])
    assert client.get('/api/papers/missing').status_code == 404
    for path in ('/api/trade','/api/wallet','/api/auth','/api/v1/me/portfolio'):
        assert client.get(path).status_code == 404


def test_fixture_search_filters_pagination_and_details(client):
    populate(db.DB_PATH)
    assert client.get('/api/papers?venue=all').json()['total'] == 3
    assert client.get('/api/papers?venue=iclr&keyword=retrieval&status=accepted').json()['total'] == 1
    assert client.get('/api/papers?venue=cvpr').json()['total'] == 1
    assert client.get('/api/papers?year=2026').json()['total'] == 0
    page=client.get('/api/papers?page_size=1&page=2&sort=title').json()
    assert page['pages']==3 and page['items'][0]['id']=='test-1'
    detail=client.get('/api/papers/test-0').json()
    assert detail['title'].startswith('[TEST FIXTURE]') and len(detail['reviews'])==1
    assert client.get('/api/papers/test-0/reviews').json()['items'][0]['visibility']=='public'
    related=client.get('/api/papers/test-0/related').json()['related'][0]
    assert related['id']=='test-1' and 'market' not in related
    assert client.get('/api/venues/iclr/status').json()['papers']==2


def test_graph_contract_and_minimap(client):
    populate(db.DB_PATH)
    data=client.get('/api/graph?venue=all').json()
    assert data['slugs']==['test-0','test-1','test-2'] and len(data['x'])==3
    assert 'headline' not in data
    assert client.get('/api/graph?venue=cvpr').json()['slugs']==['test-2']
    assert client.get('/api/graph/related').json()=={'slugs':data['slugs'],'edges':[0,1]}
    assert client.get('/api/graph/related?venue=cvpr').json()['edges']==[]
    assert client.get('/api/graph/search?q=retrieval&venue=iclr').json()['slugs']==['test-0','test-1']
    minimap=client.get('/api/graph/test-0/minimap').json()
    assert minimap['points'][minimap['self']]['slug']=='test-0'
    assert minimap['edges']==[[0,1]] and minimap['related']==[1]
    assert 'headline' not in minimap['points'][0]
    assert client.get('/api/graph/missing/minimap').status_code==404
