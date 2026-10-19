from pathlib import Path
import re
root=Path(__file__).parent
api=root/'research-self/paper_ingest/api.py'
s=api.read_text(encoding='utf-8')
start=s.index('@app.get("/api/papers/{paper_id}/related")')
end=s.index('@app.get("/api/papers/{paper_id}/reviews")',start)
s=s[:start]+s[end:]
s=s.replace('stored=keyed.get((key,year))','''stored=keyed.get((key,year))
            with db.connect() as con:
                count=con.execute("SELECT count(DISTINCT paper_id) FROM paper_venues WHERE venue_id=? AND year=?",(key,year)).fetchone()[0]
                job=con.execute("SELECT status,error_message FROM sync_jobs WHERE venue=? AND year=? ORDER BY id DESC LIMIT 1",(key,year)).fetchone()''')
s=s.replace('"year_rule":cfg.get("year_rule")','"year_rule":cfg.get("year_rule"),"paper_count":count,"latest_result":job["status"] if job else "not_checked","error_message":job["error_message"] if job else None')
start=s.index('    with db.connect() as con:',s.index('def graph_related('))
end=s.index('@app.get("/api/graph/{paper_id}/minimap")',start)
s=s[:start]+'''    data=graph(venue,year)
    slugs=data["slugs"]
    index={slug:i for i,slug in enumerate(slugs)}
    with db.connect() as con:
        rows=con.execute("SELECT source_paper_id,target_paper_id FROM paper_relations WHERE relation_type='similar' ORDER BY source_paper_id,score DESC").fetchall()
    edges=[i for row in rows if row[0] in index and row[1] in index for i in (index[row[0]],index[row[1]])]
    return {"slugs":slugs,"edges":edges}

'''+s[end:]
s=s.replace(',"headline":None','')
api.write_text(s,encoding='utf-8')
p=root/'research-self/paper_ingest/service.py';s=p.read_text(encoding='utf-8')
s=s.replace('    db.init_db()\n    clauses, args', '    venue = None if venue == "all" else venue\n    db.init_db()\n    clauses, args')
s=s.replace('"pages":(total+page_size-1)//page_size','"pages":max(1,(total+page_size-1)//page_size)')
s=s.replace('def graph(venue: str | None = None, year: int = 2027) -> dict[str, Any]:\n    db.init_db()', 'def graph(venue: str | None = None, year: int = 2027) -> dict[str, Any]:\n    venue = None if venue == "all" else venue\n    db.init_db()')
s=s.replace('SELECT p.id,p.title,p.primary_area,g.x','SELECT DISTINCT p.id,p.title,p.primary_area,g.x')
s=s.replace('"headline":[None for _ in rows],','')
p.write_text(s,encoding='utf-8')
p=root/'acceptodds/src/app/globals.css';s=p.read_text(encoding='utf-8').replace('papermarket','Paper Graph');s=re.sub(r'  /\* Likelihood.*?(?=  --font-serif)', '', s,flags=re.S);p.write_text(s,encoding='utf-8')
p=root/'acceptodds/src/components/ui.ts';s=p.read_text(encoding='utf-8').replace("the way the trade box's stake is",'for an interactive control');p.write_text(s,encoding='utf-8')
p=root/'acceptodds/src/components/map/PaperMapView.tsx';s=p.read_text(encoding='utf-8').replace("/** Public reads go anonymously, so they spend no one's rate limit; only the viewer's own papers are fetched as them. */",'/** Local paper reads require no account. */');p.write_text(s,encoding='utf-8')
p=root/'acceptodds/src/app/page.tsx';s=p.read_text(encoding='utf-8').replace("keyword={one(params.q) ?? ''}","status={one(params.status) ?? ''} sort={one(params.sort) ?? 'recent'} keyword={one(params.q) ?? ''}");p.write_text(s,encoding='utf-8')
p=root/'acceptodds/src/components/PaperExplorer.tsx';s=p.read_text(encoding='utf-8')
s=s.replace('venue, year, keyword, page','venue, year, keyword, page, status, sort').replace('page: number })','page: number; status: string; sort: string })')
s=s.replace("page_size: '40'", "page_size: '40', status, sort")
s=s.replace('year: String(year), ...(keyword ? { q:', 'year: String(year), status, sort, ...(keyword ? { q:')
start=s.index('    <header');end=s.index('    <form',start);s=s[:start]+'    <h1 className="font-serif text-3xl">Papers</h1>\n'+s[end:]
start=s.index('      <select className="border border-frame bg-card px-3 py-2" name="year"');end=s.index('      <button',start)
s=s[:start]+'''      <input aria-label="Year" className="border border-frame bg-card px-3 py-2" name="year" type="number" min="1900" max="2200" defaultValue={year} />
      <select aria-label="Status" name="status" defaultValue={status} className="border border-frame bg-card px-3 py-2"><option value="">All statuses</option>{['unknown','submitted','accepted','rejected','withdrawn'].map(s=><option key={s} value={s}>{s}</option>)}</select>
      <select aria-label="Sort" name="sort" defaultValue={sort} className="border border-frame bg-card px-3 py-2"><option value="recent">Recently updated</option><option value="title">Title</option><option value="year">Year</option></select>
'''+s[end:]
s=s.replace('  const years = [...new Set<number>(venues.map((v: {year:number}) => v.year))].sort((a,b) => b-a);\n','')
s=s.replace('        {paper.primary_area', '        {!!paper.keywords.length && <p className="mt-1 text-xs text-muted">{paper.keywords.join(" · ")}</p>}\n        {paper.primary_area')
s=s.replace('<Link href={params({ page: String(Math.max(1,page-1)) })}>Previous</Link>', '{page > 1 ? <Link href={params({ page: String(page-1) })}>Previous</Link> : <span>Previous</span>}')
s=s.replace('<Link href={params({ page: String(page+1) })}>Next</Link>', '{result && page < result.pages ? <Link href={params({ page: String(page+1) })}>Next</Link> : <span>Next</span>}')
p.write_text(s,encoding='utf-8')
p=root/'acceptodds/src/app/papers/[slug]/page.tsx';s=p.read_text(encoding='utf-8').replace('<Minimap slug={paper.id} figure={1} />','<Minimap slug={paper.id} figure={1} year={paper.venues[0]?.year ?? 2027} />');p.write_text(s,encoding='utf-8')
