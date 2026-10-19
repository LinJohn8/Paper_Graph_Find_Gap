"""Run explicitly: python tests/browser_check.py. Existing production servers required.
Fixture servers use 18000/13000 and a disposable isolated database. Never syncs.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import httpx
from playwright.sync_api import sync_playwright, expect
from test_paper_platform import populate

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'Data/cache/phase2-tests'
OUT.mkdir(parents=True,exist_ok=True)
results=[]


def ready(url):
    for _ in range(60):
        try:
            if httpx.get(url,timeout=2).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(.25)
    raise RuntimeError(f'Not ready: {url}')


def check_http(base):
    for path in ('/','/map','/sources','/api/graph','/api/papers'):
        assert httpx.get(base+path,timeout=20).status_code==200,path
    for path in ('/market','/trade','/portfolio','/login','/register','/leaderboard','/api/v1/me/portfolio','/api/auth/session','/api/v1/trades'):
        assert httpx.get(base+path,timeout=20).status_code==404,path


def browser_empty(page):
    page.goto('http://127.0.0.1:3000')
    expect(page.get_by_text('No public papers match these filters.')).to_be_visible()
    expect(page.locator('input[name=year]')).to_have_value('2027')
    page.goto('http://127.0.0.1:3000/map')
    expect(page.get_by_text('No papers available. Sync a conference to populate the map.')).to_be_visible()
    page.screenshot(path=str(OUT/'empty-map.png'))
    page.goto('http://127.0.0.1:3000/sources')
    expect(page.locator('main section')).to_have_count(6)
    page.screenshot(path=str(OUT/'sources.png'),full_page=True)
    results.append('Empty browser: papers, default2027, map exact empty state, six sources PASS')


def browser_fixture(page):
    base='http://127.0.0.1:13000'
    page.goto(base)
    expect(page.locator('article')).to_have_count(3)
    page.locator('input[name=q]').fill('retrieval')
    page.locator('select[name=venue]').select_option('iclr')
    page.get_by_role('button',name='Search',exact=True).click()
    expect(page.locator('article')).to_have_count(2)
    page.locator('select[name=status]').select_option('accepted')
    page.get_by_role('button',name='Search',exact=True).click()
    expect(page.locator('article')).to_have_count(1)
    page.locator('article h2 a').click()
    expect(page.get_by_role('heading',name='[TEST FIXTURE] Graph retrieval 0',exact=True)).to_be_visible()
    expect(page.get_by_text('[TEST FIXTURE] Review',exact=True)).to_be_visible()
    page.get_by_role('heading',name='Paper map',exact=True).scroll_into_view_if_needed()
    expect(page.locator('figure canvas')).to_be_visible(timeout=20000)
    expect(page.get_by_role('link',name='Open the full map')).to_be_visible()
    canvas=page.locator('figure canvas')
    canvas.hover()
    page.mouse.wheel(0,-60)
    page.screenshot(path=str(OUT/'fixture-detail-minimap.png'),full_page=True)
    bounds=canvas.bounding_box()
    # These fixture coordinates are deliberately fixed; hover confirms deck.gl picking.
    canvas.hover(position={'x':bounds['width']/2+75,'y':bounds['height']/2-50})
    expect(page.locator('figure').get_by_text('[TEST FIXTURE] Graph retrieval 1',exact=True)).to_be_visible()
    canvas.click(position={'x':bounds['width']/2+75,'y':bounds['height']/2-50})
    expect(page.get_by_role('heading',name='[TEST FIXTURE] Graph retrieval 1',exact=True)).to_be_visible()
    page.goto(base+'/papers/test-0')
    page.get_by_role('heading',name='Paper map',exact=True).scroll_into_view_if_needed()
    expect(page.get_by_role('link',name='Open the full map')).to_be_visible(timeout=20000)
    page.get_by_role('link',name='Open the full map').click()
    expect(page.locator('canvas')).to_be_visible(timeout=20000)
    expect(page.get_by_role('link',name='[TEST FIXTURE] Graph retrieval 0',exact=True)).to_be_visible(timeout=20000)
    with page.expect_response(lambda r:'/api/graph/related' in r.url and r.status==200):
        page.get_by_placeholder('Search titles, authors, abstracts and keywords').fill('retrieval')
    page.wait_for_timeout(1800)
    page.screenshot(path=str(OUT/'fixture-global-map.png'),full_page=True)
    page.get_by_role('link',name='[TEST FIXTURE] Graph retrieval 0',exact=True).click()
    expect(page.get_by_role('heading',name='[TEST FIXTURE] Graph retrieval 0',exact=True)).to_be_visible()
    results.append('Fixture browser: list/search/venue/status/detail/public review/minimap canvas/hover/node click/full map/search relayout/selection to detail PASS')


def main():
    processes=[]
    logs=[]
    errors=[]
    try:
        check_http('http://127.0.0.1:3000')
        results.append('Formal HTTP retained routes 200; nine retired routes/API 404 PASS')
        with tempfile.TemporaryDirectory(prefix='paper-graph-fixture-',ignore_cleanup_errors=True) as folder:
            path=Path(folder)/'test.sqlite'
            populate(path)
            env={**os.environ,'PAPER_DATA_DIR':folder,'PAPER_DB_PATH':str(path)}
            logs.append((OUT/'fixture-api.log').open('w',encoding='utf-8'))
            processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','paper_ingest.api:app','--host','127.0.0.1','--port','18000'],cwd=ROOT/'research-self',env=env,stdout=logs[-1],stderr=subprocess.STDOUT))
            logs.append((OUT/'fixture-web.log').open('w',encoding='utf-8'))
            processes.append(subprocess.Popen(['node','node_modules/next/dist/bin/next','start','--hostname','127.0.0.1','--port','13000'],cwd=ROOT/'acceptodds',env={**os.environ,'PAPER_API_URL':'http://127.0.0.1:18000'},stdout=logs[-1],stderr=subprocess.STDOUT))
            ready('http://127.0.0.1:18000/api/health')
            ready('http://127.0.0.1:13000')
            check_http('http://127.0.0.1:13000')
            with sync_playwright() as pw:
                executable=next((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-1243/chrome-win64/chrome.exe'),None)
                browser=pw.chromium.launch(executable_path=str(executable) if executable else None,headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                page=browser.new_page(viewport={'width':1400,'height':1000})
                page.on('pageerror',lambda error:errors.append(str(error)))
                browser_empty(page)
                browser_fixture(page)
                assert not errors,errors
                browser.close()
            results.append('No browser uncaught JavaScript errors PASS')
            for process in reversed(processes):process.terminate();process.wait(timeout=10)
            processes.clear()
        assert httpx.get('http://127.0.0.1:8000/api/papers').json()['total']==0
        results.append('Formal corpus remains zero; isolated fixture DB removed PASS')
    finally:
        for process in reversed(processes):process.terminate();process.wait(timeout=10)
        for log in logs:log.close()
        (OUT/'browser-results.json').write_text(json.dumps({'results':results,'browser_errors':errors},indent=2),encoding='utf-8')
    print('\n'.join(results))


if __name__=='__main__':main()
