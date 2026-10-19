"""Verify the actual generated map used by the Windows EXE launcher."""
import json
import os
import time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect


def main():
    out=Path(__file__).resolve().parents[2]/'Data/cache/exe-verification'
    out.mkdir(parents=True,exist_ok=True)
    for attempt in range(40):
        try:
            if httpx.get('http://127.0.0.1:3000',timeout=2).status_code==200:break
        except httpx.HTTPError:pass
        time.sleep(.5)
    graph=httpx.get('http://127.0.0.1:8000/api/graph?venue=icml&year=2025',timeout=20).json()
    assert len(graph['slugs'])==3330
    errors=[]
    with sync_playwright() as p:
        executable=next((Path(os.environ['LOCALAPPDATA'])/'ms-playwright').glob('chromium-1243/chrome-win64/chrome.exe'))
        browser=p.chromium.launch(executable_path=str(executable),headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:3000/map?venue=icml&year=2025')
        expect(page.locator('canvas')).to_be_visible(timeout=20000)
        expect(page.get_by_text('3,330 papers, near the papers most like them')).to_be_visible()
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out/'icml-2025-map.png'),full_page=True)
        search=page.get_by_placeholder('Search titles, authors, abstracts and keywords')
        search.fill('Graph')
        result_button=page.locator('ul button').first
        expect(result_button).to_be_visible(timeout=15000)
        selected_title=result_button.inner_text()
        result_button.click()
        detail=page.get_by_role('link',name=selected_title,exact=True)
        expect(detail).to_be_visible()
        detail.click()
        expect(page.get_by_role('heading',name=selected_title,exact=True)).to_be_visible()
        page.get_by_role('heading',name='Paper map',exact=True).scroll_into_view_if_needed()
        expect(page.locator('figure canvas')).to_be_visible(timeout=20000)
        page.screenshot(path=str(out/'real-paper-minimap.png'),full_page=True)
        assert not errors,errors
        browser.close()
    result={'real_map_nodes':3330,'global_map':'pass','search':'pass','selected_paper_detail':'pass','minimap':'pass','javascript_errors':errors}
    (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
