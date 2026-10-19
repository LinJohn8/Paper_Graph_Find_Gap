import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Minimap } from '@/components/map/Minimap';
import { load } from '@/lib/paper-api';
import { decodePaperId } from '@/lib/paper-id';

const API = process.env.PAPER_API_URL ?? 'http://127.0.0.1:8000';
export const dynamic = 'force-dynamic';

export default async function PaperPage({ params }: { params: Promise<{ slug: string }> }) {
  const slug = decodePaperId((await params).slug);
  let response: Response;
  try {
    response = await fetch(`${API}/api/papers/${encodeURIComponent(slug)}`, { cache: 'no-store' });
  } catch {
    return <main className="p-8">Local paper API is unavailable. Start the backend and try again.</main>;
  }
  if (response.status === 404) notFound();
  if (!response.ok) return <main className="p-8">Local paper API could not load this paper.</main>;
  const paper = await response.json();
  return (
    <main className="mx-auto max-w-4xl px-6 py-8">
      <Link href="/" className="text-sm text-subtle">
        Back to papers
      </Link>
      <h1 className="mt-5 font-serif text-3xl leading-tight">{paper.title}</h1>
      <p className="mt-3 text-sm text-subtle">
        {paper.authors.join(', ') || 'Authors not provided'} ·{' '}
        {paper.venues
          .map((v: { name: string; year: number; relation_type: string }) => `${v.name} ${v.year} (${v.relation_type})`)
          .join(' / ') || 'Venue not recorded'}{' '}
        · {paper.status}
      </p>
      {paper.keywords.length > 0 && <p className="mt-3 text-sm">{paper.keywords.join(' · ')}</p>}
      <div className="mt-5 flex gap-5 text-sm">
        {paper.paper_url && (
          <a href={paper.paper_url} target="_blank" rel="noreferrer">
            Original source
          </a>
        )}
        {paper.pdf_url && (
          <a href={paper.pdf_url} target="_blank" rel="noreferrer">
            PDF
          </a>
        )}
      </div>
      <section className="mt-8">
        <h2 className="font-serif text-xl">Abstract</h2>
        <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed">
          {paper.abstract || 'Abstract not provided by the source.'}
        </p>
      </section>
      {paper.rqs?.length > 0 && (
        <section className="mt-8">
          <h2 className="font-serif text-xl">Research questions (extracted)</h2>
          <ul className="mt-2 list-disc space-y-2 pl-5 text-sm leading-relaxed">
            {paper.rqs.map((rq: string, i: number) => (
              <li key={i}>{rq}</li>
            ))}
          </ul>
        </section>
      )}
      {paper.gap_cues?.length > 0 && (
        <section className="mt-8">
          <h2 className="font-serif text-xl">Stated gaps</h2>
          <ul className="mt-2 list-disc space-y-2 pl-5 text-sm leading-relaxed">
            {paper.gap_cues.map((cue: string, i: number) => (
              <li key={i}>{cue}</li>
            ))}
          </ul>
        </section>
      )}
      <section className="mt-8">
        <h2 className="font-serif text-xl">Public reviews and decisions</h2>
        {paper.reviews.length ? (
          paper.reviews.map((r: { id: string; review_type: string; content: string }) => (
            <article key={r.id} className="border-b border-rule py-4">
              <p className="text-xs uppercase text-muted">{r.review_type}</p>
              <p className="mt-2 whitespace-pre-wrap text-sm">{r.content}</p>
            </article>
          ))
        ) : (
          <p className="mt-2 text-sm text-muted">No public reviews available from the source.</p>
        )}
      </section>
      <section className="mt-8">
        <h2 className="font-serif text-xl">Related papers</h2>
        <Related paperId={paper.id} />
      </section>
      <section className="mt-8">
        <h2 className="font-serif text-xl">Paper map</h2>
        <Minimap
          slug={paper.id}
          figure={1}
          year={paper.venues[0]?.year ?? paper.publication_year ?? new Date().getFullYear()}
        />
      </section>
    </main>
  );
}

async function Related({ paperId }: { paperId: string }) {
  const data = await load(`/api/papers/${encodeURIComponent(paperId)}/related`);
  return data?.related?.length ? (
    <ul className="mt-2 list-disc pl-5 text-sm">
      {data.related.map((paper: { id: string; title: string }) => (
        <li key={paper.id}>
          <Link href={`/papers/${encodeURIComponent(paper.id)}`}>{paper.title}</Link>
        </li>
      ))}
    </ul>
  ) : (
    <p className="mt-2 text-sm text-muted">Related-paper data is not available yet.</p>
  );
}
