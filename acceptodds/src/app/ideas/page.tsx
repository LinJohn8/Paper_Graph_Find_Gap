import type { Metadata } from 'next';
import Link from 'next/link';
import { IdeaFinder } from '@/components/IdeaFinder';

export const dynamic = 'force-dynamic';
export const metadata: Metadata = {
  title: 'Idea Finder',
  description: 'Retrieve paper abstracts and surface research gaps',
};

export default function IdeaFinderPage() {
  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <Link href="/" className="text-sm text-subtle">
        Back to papers
      </Link>
      <h1 className="mt-5 font-serif text-3xl leading-tight">Idea Finder</h1>
      <p className="mt-3 max-w-3xl text-sm text-subtle">
        Describe a research idea or question to retrieve the most relevant paper abstracts, then scan the research-gap
        panel for under-explored directions worth pursuing.
      </p>
      <IdeaFinder />
    </main>
  );
}
