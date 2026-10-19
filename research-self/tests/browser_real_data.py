"""Read-only verification of actual imported proceedings on running localhost services."""
import json
import os
from pathlib import Path
import httpx
from urllib.parse import quote
from playwright.sync_api import sync_playwright, expect


def main():
    root=Path(__file__).resolve().parents[2]
    out=root/'Data/cache/real-data-verification';out.mkdir(parents=True,exist_ok=True)
    errors=[];checks=[];samples=[]
    with httpx.Client(base_url='http://127.0.0.1:8000',timeout=15) as client:
        assert client.get('/api/health').status_code==200
        for venue in ('icml','acl','cvpr','neurips'):
            data=client.get('/api/papers',params={'venue':venue,'year':2025,'page_size':1}).json()
            assert data['total']>0
            checks.append({'venue':venue,'year':2025,'count':data['total']})
            samples.append(data['items'][0])
    with sync_playwright() as p:
        executable=next((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-1243/chrome-win64/chrome.exe'))
        browser=p.chromium.launch(executable_path=str(executable),headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        for paper in samples:
            page.goto('http://127.0.0.1:3000/papers/'+quote(paper['id'],safe=''))
            expect(page.get_by_role('heading',name=paper['title'],exact=True)).to_be_visible()
            expect(page.get_by_role('link',name='Original source')).to_have_attribute('href',paper['paper_url'])
        page.goto('http://127.0.0.1:3000')
        expect(page.locator('input[name=year]')).to_have_value('2027')
        expect(page.locator('select[name=venue] option')).to_have_count(7)
        page.locator('input[name=year]').fill('2025')
        page.locator('select[name=venue]').select_option('acl')
        page.locator('input[name=q]').fill('GraphNarrator')
        page.get_by_role('button',name='Search',exact=True).click()
        expect(page.locator('article')).to_have_count(1)
        page.locator('article h2 a').click()
        expect(page.get_by_role('heading',name='GraphNarrator: Generating Textual Explanations for Graph Neural Networks')).to_be_visible()
        expect(page.get_by_text('No public reviews available from the source.')).to_be_visible()
        assert page.get_by_role('link',name='Original source').get_attribute('href')=='https://aclanthology.org/2025.acl-long.2/'
        page.screenshot(path=str(out/'real-acl-detail.png'),full_page=True)
        page.goto('http://127.0.0.1:3000/?venue=icml&year=2025')
        expect(page.locator('article')).to_have_count(40)
        page.screenshot(path=str(out/'real-icml-list.png'),full_page=True)
        page.goto('http://127.0.0.1:3000/sources')
        expect(page.get_by_role('heading',name='ICML 2025',exact=True)).to_be_visible()
        page.screenshot(path=str(out/'real-sources.png'),full_page=True)
        page.goto('http://127.0.0.1:3000/map?venue=icml&year=2025')
        expect(page.get_by_text('No papers available. Sync a conference to populate the map.')).to_be_visible()
        assert not errors,errors
        browser.close()
    result={'checks':checks,'default_year':2027,'real_search':'GraphNarrator','detail':'pass','sources':'pass','graph_empty_without_generated_points':'pass','javascript_errors':errors}
    (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
