import { load } from '@/lib/paper-api';
import { coverageDetail, coverageLabel } from '@/lib/coverage';
export const dynamic = 'force-dynamic';

export default async function Sources() {
  const registry = await load('/api/venues');
  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="font-serif text-3xl">Data Sources</h1>
      {!registry && <p>Local paper API is unavailable.</p>}
      <div className="mt-6 grid gap-4 sm:grid-cols-2">
        {registry?.items.map(
          (v: {
            id: string;
            name: string;
            year: number;
            status: string;
            paper_count: number;
            last_sync_at: string | null;
            latest_result: string | null;
            error_message: string | null;
            year_rule: string | null;
            expected_count: number | null;
            coverage_status: string | null;
          }) => {
            const coverage = coverageLabel(v.coverage_status);
            const detail = coverageDetail(v.coverage_status, v.expected_count);
            return (
              <section className="border border-rule p-4" key={`${v.id}-${v.year}`}>
                <h2 className="font-serif text-xl">
                  {v.name} {v.year}
                </h2>
                <dl className="mt-2 text-sm">
                  <dt>Configured</dt>
                  <dd>Yes</dd>
                  <dt>Local papers</dt>
                  <dd>{v.paper_count}</dd>
                  {coverage && (
                    <>
                      <dt>Coverage</dt>
                      <dd className={coverage.className}>
                        {coverage.text}
                        {detail ? ` (${detail})` : ''}
                      </dd>
                    </>
                  )}
                  <dt>Availability</dt>
                  <dd>{v.status}</dd>
                  <dt>Last sync/check</dt>
                  <dd>{v.last_sync_at ?? 'Never'}</dd>
                  <dt>Last result</dt>
                  <dd>{v.latest_result ?? 'Not checked'}</dd>
                </dl>
                {v.error_message && <p className="mt-2 break-words text-xs text-subtle">{v.error_message}</p>}
                {v.year_rule && <p className="mt-2 text-xs">{v.year_rule}</p>}
              </section>
            );
          },
        )}
      </div>
    </main>
  );
}
