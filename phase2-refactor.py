from pathlib import Path
import re, json, hashlib, shutil

ROOT=Path(__file__).resolve().parent
FRONT=ROOT/'acceptodds'
def read(p): return (FRONT/p).read_text(encoding='utf-8')
def write(p,s):
    path=FRONT/p; path.parent.mkdir(parents=True,exist_ok=True);path.write_text(s,encoding='utf-8')

# Preserve a recoverable copy of the existing edits before removing unused modules.
backup=ROOT/'Data/cache/phase2-source-backup'
backup.mkdir(parents=True,exist_ok=True)
for folder in ('src','tests','scripts','drizzle','config'):
    shutil.copytree(FRONT/folder,backup/folder,dirs_exist_ok=True)
for p in ('package.json','package-lock.json','.env.example','.env.production.example','next.config.ts','AGENTS.md','README.md','IMPLEMENTATION.md'):
    shutil.copy2(FRONT/p,backup/p)
before=[p.relative_to(FRONT).as_posix() for p in (FRONT/'src').rglob('*') if p.is_file()]
hashes={p:hashlib.sha256((FRONT/p).read_bytes()).hexdigest() for p in ('src/components/map/MapCanvas.tsx','src/lib/map-layout.ts','src/lib/map-layout.worker.ts','src/lib/map-controller.ts')}
write('src/lib/paper-types.ts','''export interface Topic { number: number; label: string }
export interface MapPoint { slug: string; title: string; primaryArea: string | null; x: number; y: number; region: number | null; cluster: number | null }
export interface PaperMap { kind: string; slugs: string[]; x: number[]; y: number[]; region: (number|null)[]; cluster: (number|null)[]; area: (number|null)[]; areas: string[]; regions: Topic[]; clusters: Topic[] }
export interface MapTitles { titles: Record<string,string> }
export interface Related { related: {id:string;slug:string;title:string}[]; hidden:number }
export interface MapSearch { slugs:string[]; errors:string[] }
export interface MapRelated { slugs:string[]; edges:number[] }
export interface MinimapData { points:MapPoint[]; vectors:number[][]|null; self:number; related:number[]; relatedElsewhere:number; edges:[number,number][]; clusters:Topic[] }
''')
s=read('src/components/map/PaperMapView.tsx')
s=s.replace("import { RELATED_UNBET } from '@/lib/related';",'const RELATED_PREVIEW = 10;')
for line in ["import { pct } from '@/lib/format';", "import { likelihood, likelihoodClass } from '@/lib/likelihood';", "import { SEARCH_MAX_LENGTH } from '@/lib/search';", "import type * as S from '@/server/api/schemas';"]: s=s.replace(line,'')
s=s.replace('  headlineRgb,\n','').replace('  LIKELIHOOD_RGB,\n','').replace('  MAP_NONE,\n','')
s=s.replace("type PaperMap = z.output<typeof S.PaperMap>;\ntype MapTitles = z.output<typeof S.MapTitles>;", "import type { PaperMap, MapTitles, Related, MapSearch, MapRelated } from '@/lib/paper-types';\nconst SEARCH_MAX_LENGTH = 1000;")
s=re.sub(r'type Related = z.output<.*?type Portfolio = z.output<typeof S.Portfolio>;',"type ColourBy = 'region' | 'cluster' | 'area';",s,flags=re.S)
s=s.replace('  headline: number | null;\n','').replace("const viewerJson = json('same-origin');",'').replace("  ['odds', 'odds'],\n",'')
s=s.replace("z.enum(['region', 'cluster', 'area', 'odds'])","z.enum(['region', 'cluster', 'area'])")
s=s.replace('  showMine: z.boolean(),\n','').replace('  signedIn,\n','  year = 2027,\n').replace('  signedIn: boolean;','  year?: number;')
s=s.replace('`venue=${encodeURIComponent(kind)}&year=2027`','`venue=${encodeURIComponent(kind)}&year=${year}`')
s=s.replace('  const [showMine, setShowMine] = useState(true);\n','').replace('        setShowMine(saved.data.showMine);\n','').replace(', showMine','')
a=s.index("  // The viewer's own papers")
b=s.index('  const none: number[]',a)
s=s[:a]+s[b:]
s=s.replace('RELATED_UNBET','RELATED_PREVIEW')
a=s.index('  // Pointing at "starred"')
b=s.index('  const shown =',a)
s=s[:a]+'  const lit = matches;\n'+s[b:]
s=s.replace('starred={showMine ? mine.starred : none}','starred={none}').replace('held={showMine ? mine.held : none}','held={none}')
a=s.index('              {signedIn &&')
b=s.index('\n            </div>',a)
s=s[:a]+s[b:]
s=s.replace('No graph coordinates are available yet. Build the graph from collected papers.','No papers available. Sync a conference to populate the map.')
s=s.replace("[clusterName(points[hover.index]), headlineText(points[hover.index].headline)]",'[clusterName(points[hover.index])]')
s=re.sub(r'\s*<Odds headline=\{[^\n]*?\} />','',s)
s=re.sub(r'\s*\{i !== undefined && \(\s*<>\s*\{\x27 \x27\}\s*</>\s*\)\}', '',s)
s=s.replace('Search papers: words, author:, accept>=60 …','Search titles, authors, abstracts and keywords')
a=s.index('/**\n * A legend entry')
b=s.index('function Toggle',a)
s=s[:a]+s[b:]
a=s.index('  const items: [string, Rgb][] =')
b=s.index('  if (items.length === 0)',a)
s=s[:a]+"  const items: [string, Rgb][] = colourBy === 'area' ? areas.map((a,i)=>[a || 'none given',categoryRgb(a ? i : null)]) : [];\n"+s[b:]
s=s.replace('    headline: data.headline[i],\n','')
s=s.replace("      by === 'odds'\n        ? headlineRgb(p.headline)\n        : by === 'area'", "      by === 'area'")
s=re.sub(r'/\*\*\n \* `/map`,.*?\*/', '/** Global paper map with local search, topic colours and the original layout worker. */',s,flags=re.S)
write('src/components/map/PaperMapView.tsx',s)
s=read('src/components/map/Minimap.tsx')
s=s.replace("import type { z } from 'zod';",'').replace("import { pct } from '@/lib/format';",'').replace("import type * as S from '@/server/api/schemas';", "import type { MinimapData } from '@/lib/paper-types';")
s=s.replace('type MinimapView = z.output<typeof S.Minimap>;','type MinimapView = MinimapData;')
s=s.replace('Minimap({ slug, figure }: { slug: string; figure: number })','Minimap({ slug, figure, year = 2027 }: { slug: string; figure: number; year?: number })')
s=s.replace('/minimap?year=2027`','/minimap?year=${year}`')
s=s.replace("{error ? 'The map could not be loaded.' : 'Loading the map…'}", "{error ? 'No graph data available for this paper.' : 'Loading the map…'}")
s=re.sub(r'\s*\{hovered.headline !== null &&.*?</span>\}', '',s)
write('src/components/map/Minimap.tsx',s)
s=read('src/lib/map.ts')
s=re.sub(r'^import .*\n','',s,flags=re.M)
a=s.index('export function labelTerms')
b=s.index('/** Whether a topic label',a)
s=s[:a]+'''export function labelTerms(query: string): string[] {
  return [...new Set((query.match(/[\\p{L}\\p{N}]+/gu) ?? []).map(t=>t.toLowerCase()))].filter(t=>t.length>=MIN_LABEL_TERM);
}

'''+s[b:]
a=s.index('/** Mirrors the `accept`')
b=s.index('/**\n * Dot radius multiplier',a)
s=s[:a]+s[b:]
write('src/lib/map.ts',s)
write('src/components/SearchSyntax.tsx','''export function SearchSyntax() { return <p>Words search titles, abstracts, authors and keywords. Use AND, OR and parentheses to combine terms. Use the venue and year controls to filter papers.</p>; }
''')
write('src/app/layout.tsx','''import type { Metadata } from 'next';
import { PaperHeader } from '@/components/PaperHeader';
import './globals.css';
export const metadata:Metadata={title:{default:'Paper Graph',template:'%s | Paper Graph'},description:'Personal Academic Paper Explorer'};
export default function RootLayout({children}:{children:React.ReactNode}) { return <html lang="en"><body><PaperHeader/>{children}</body></html>; }
''')
write('src/components/PaperHeader.tsx','''import Link from 'next/link';
export function PaperHeader(){return <header className="flex flex-wrap items-center justify-between gap-3 border-b border-rule px-6 py-3"><Link href="/" className="font-serif text-xl">Paper Graph<span className="ml-3 text-xs text-subtle">Personal Academic Paper Explorer</span></Link><nav aria-label="Main navigation" className="flex gap-5 text-sm"><Link href="/">Papers</Link><Link href="/map">Graph</Link><Link href="/sources">Data Sources</Link></nav></header>;}
''')
write('next.config.ts',"import type { NextConfig } from 'next';\nconst config:NextConfig={poweredByHeader:false,output:'standalone'};\nexport default config;\n")
write('src/lib/paper-api.ts','''export const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export async function load(path:string) { try { const response=await fetch(`${API}${path}`,{cache:'no-store',signal:AbortSignal.timeout(10000)});return response.ok ? response.json() : null; } catch {return null;} }
''')
write('src/app/sources/page.tsx','''import { load } from '@/lib/paper-api';
export const dynamic='force-dynamic';
export default async function Sources(){const registry=await load('/api/venues');return <main className="mx-auto max-w-6xl px-6 py-8"><h1 className="font-serif text-3xl">Data Sources</h1>{!registry&&<p>Local paper API is unavailable.</p>}<div className="mt-6 grid gap-4 sm:grid-cols-2">{registry?.items.map((v:{id:string;name:string;year:number;status:string;paper_count:number;last_sync_at:string|null;latest_result:string|null;error_message:string|null;year_rule:string|null})=><section className="border border-rule p-4" key={`${v.id}-${v.year}`}><h2 className="font-serif text-xl">{v.name} {v.year}</h2><dl className="mt-2 text-sm"><dt>Configured</dt><dd>Yes</dd><dt>Local papers</dt><dd>{v.paper_count}</dd><dt>Availability</dt><dd>{v.status}</dd><dt>Last sync/check</dt><dd>{v.last_sync_at ?? 'Never'}</dd><dt>Last result</dt><dd>{v.latest_result ?? 'Not checked'}</dd></dl>{v.error_message&&<p className="mt-2 break-words text-xs text-subtle">{v.error_message}</p>}{v.year_rule&&<p className="mt-2 text-xs">{v.year_rule}</p>}</section>)}</div></main>;}
''')
s=read('src/app/map/page.tsx').replace('signedIn={false}','year={Number(first(sp.year)??2027)}')
s=s.replace("import { PaperMapView }", "import { load } from '@/lib/paper-api';\nimport { PaperMapView }")
s=s.replace('  return <main>', '''  const registry=await load('/api/venues');
  return <main><form action="/map" className="flex gap-3 px-6 py-3"><label>Venue <select name="venue" defaultValue={kind}><option value="all">All venues</option>{registry?.items.map((v:{id:string;name:string})=><option value={v.id} key={v.id}>{v.name}</option>)}</select></label><label>Year <input name="year" type="number" min="1900" max="2100" defaultValue={first(sp.year)??2027} className="w-24 border border-rule"/></label><button type="submit">Apply</button></form>''')
write('src/app/map/page.tsx',s)

# Select only current product routes; preserve reusable source by its actual import closure.
routes={'src/app/page.tsx','src/app/layout.tsx','src/app/globals.css','src/app/map/page.tsx','src/app/sources/page.tsx','src/app/papers/[slug]/page.tsx'}
routes.update(p.relative_to(FRONT).as_posix() for base in ('src/app/api/graph','src/app/api/papers') for p in (FRONT/base).rglob('route.ts'))
for p in list((FRONT/'src/app').rglob('*')):
    if p.is_file() and p.relative_to(FRONT).as_posix() not in routes: p.unlink()
if (FRONT/'src/middleware.ts').exists(): (FRONT/'src/middleware.ts').unlink()
# Retain map regression suites, adapting only tests of the deleted probability/search product features.
test_keep={'map.test.ts','map-layout.test.ts','map-controller.test.ts'}
for p in (FRONT/'tests').rglob('*'):
    if p.is_file() and p.name not in test_keep: p.unlink()
s=read('tests/unit/map.test.ts')
for item in ('  headlineRgb,\n','  LIKELIHOOD_RGB,\n','  MAP_NONE,\n'):s=s.replace(item,'')
s=re.sub(r"  it\('colours by the site.*?\n  \}\);\n",'',s,flags=re.S)
a=s.index("  it('takes positive words")
b=s.index("  it('matches a term",a)
s=s[:a]+'''  it('extracts paper search terms for topic highlighting',()=>{
    expect(labelTerms('Graph diffusion of vision')).toEqual(['graph','diffusion','vision']);
    expect(labelTerms('')).toEqual([]);
  });

'''+s[b:]
write('tests/unit/map.test.ts',s)
write('vitest.config.mts',"import {defineConfig} from 'vitest/config';\nimport {fileURLToPath} from 'node:url';\nexport default defineConfig({resolve:{alias:{'@':fileURLToPath(new URL('./src',import.meta.url))}},test:{environment:'node',include:['tests/**/*.test.ts'],testTimeout:30000}});\n")
keep=set(routes)|{'src/lib/map-layout.ts','src/lib/map-layout.worker.ts','src/lib/map-controller.ts','src/lib/paper-types.ts'}
queue=list(keep)+[p.relative_to(FRONT).as_posix() for p in (FRONT/'tests').rglob('*.ts')]
while queue:
    p=queue.pop();path=FRONT/p
    if not path.exists():continue
    for imp in re.findall(r'(?:from\s+|import\s*\(|import\s+)[\x27\x22]([^\x27\x22]+)',path.read_text(encoding='utf-8')):
        if imp.startswith('@/'):target=FRONT/'src'/imp[2:]
        elif imp.startswith('.'):target=(path.parent/imp).resolve()
        else:continue
        for candidate in (target,Path(str(target)+'.ts'),Path(str(target)+'.tsx'),target/'index.ts'):
            if candidate.is_file():
                rel=candidate.relative_to(FRONT).as_posix()
                if rel not in keep:keep.add(rel);queue.append(rel)
                break
for p in (FRONT/'src').rglob('*'):
    if p.is_file() and p.relative_to(FRONT).as_posix() not in keep: p.unlink()
# Delete retired operations/configuration by file (no data-directory deletion).
for folder in ('scripts','drizzle','config','maintenance','public'):
    for p in (FRONT/folder).rglob('*'):
        if p.is_file():p.unlink()
for p in ('auth.config.ts','drizzle.config.ts','Dockerfile','.env.production.example','docker-compose.dev.yml','docker-compose.prod.yml','docker-compose.staging.yml','docker-compose.maintenance.yml','IMPLEMENTATION.md'):
    path=FRONT/p
    if path.exists():path.unlink()
write('AGENTS.md','''# Paper Graph local frontend
Next.js App Router, React, Tailwind, deck.gl, UMAP and d3-force. FastAPI/SQLite is the only paper-data source.
Preserve MapCanvas rendering and map layout algorithms. Do not add trading, accounts or authentication.
Keep all corpora outside the frontend in ../Data. Test fixtures must use isolated databases.
Run npm test, npm run typecheck and npm run build. Format edited code with Prettier.
This checkout has no upstream license; local personal research only.
''')
write('README.md','# Paper Graph frontend\n\nLocal Next.js paper browser and original deck.gl map. See ../README.md for installation. Run npm ci, npm run dev. API: PAPER_API_URL=http://127.0.0.1:8000. No database or account credentials are needed.\n')
write('.env.example','PAPER_API_URL=http://127.0.0.1:8000\n')
pkg=json.loads(read('package.json')); old_deps=set(pkg['dependencies'])|set(pkg['devDependencies'])
deps={'@deck.gl/core','@deck.gl/extensions','@deck.gl/layers','d3-force','next','react','react-dom','swr','umap-js','zod'}
dev={'@tailwindcss/postcss','@types/d3-force','@types/node','@types/react','@types/react-dom','@typescript/native','eslint','eslint-config-next','fast-check','prettier','tailwindcss','typescript','vitest'}
pkg['name']='paper-graph';pkg['dependencies']={k:v for k,v in pkg['dependencies'].items() if k in deps};pkg['devDependencies']={k:v for k,v in pkg['devDependencies'].items() if k in dev};pkg.pop('overrides',None)
pkg['scripts']={'dev':'next dev --hostname 127.0.0.1','build':'next build','start':'next start --hostname 127.0.0.1','typecheck':'tsc --noEmit','lint':'eslint','test':'vitest run','format':'prettier --write src tests *.ts *.mts *.json *.mjs','format:check':'prettier --check src tests *.ts *.mts *.json *.mjs'}
write('package.json',json.dumps(pkg,indent=2)+'\n')
write('eslint.config.mjs',"import {defineConfig,globalIgnores} from 'eslint/config';\nimport nextVitals from 'eslint-config-next/core-web-vitals';\nimport nextTs from 'eslint-config-next/typescript';\nexport default defineConfig([...nextVitals,...nextTs,globalIgnores(['node_modules/**','.next*/**','next-env.d.ts'])]);\n")
after=set(p.relative_to(FRONT).as_posix() for p in (FRONT/'src').rglob('*') if p.is_file())
manifest={'deleted_src':sorted(set(before)-after),'deleted_routes':[p for p in set(before)-after if p.startswith('src/app/') and (p.endswith('page.tsx') or p.endswith('route.ts'))],'removed_dependencies':sorted(old_deps-deps-dev),'core_before_sha256':hashes,'core_after_sha256':{p:hashlib.sha256((FRONT/p).read_bytes()).hexdigest() for p in hashes},'retained_src':sorted(after)}
(ROOT/'Data/cache/phase2-removal-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print(json.dumps({'deleted_source_files':len(manifest['deleted_src']),'deleted_routes':len(manifest['deleted_routes']),'retained_source_files':len(after),'unchanged_graph_core':hashes==manifest['core_after_sha256']},indent=2))
