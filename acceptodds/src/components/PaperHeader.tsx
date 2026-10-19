import Link from 'next/link';
export function PaperHeader() {
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-rule px-6 py-3">
      <Link href="/" className="font-serif text-xl">
        Paper Graph<span className="ml-3 text-xs text-subtle">Personal Academic Paper Explorer</span>
      </Link>
      <nav aria-label="Main navigation" className="flex gap-5 text-sm">
        <Link href="/">Papers</Link>
        <Link href="/ideas">Idea Finder</Link>
        <Link href="/map">Graph</Link>
        <Link href="/sources">Data Sources</Link>
      </nav>
    </header>
  );
}
