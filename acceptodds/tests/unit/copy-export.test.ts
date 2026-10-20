import { describe, expect, it } from 'vitest';
import { exportFilename, formatCitations, paperUrl, venueLabel } from '@/lib/copy-export';
import type { SemanticItem } from '@/lib/paper-types';

const base: SemanticItem = {
  id: 'iclr_virtual:2026/1',
  slug: 'iclr_virtual:2026/1',
  title: 'A Paper About Things',
  abstract: '',
  authors: ['Ada Lovelace', 'Alan Turing'],
  venues: [{ id: 'iclr', name: 'ICLR', year: 2026, relation_type: 'publication' }],
  year: 2026,
  score: 0.8712,
};

describe('paperUrl', () => {
  it('prefers the publisher landing page', () => {
    expect(paperUrl({ ...base, paper_url: 'https://example.org/p' }, 'http://local')).toBe('https://example.org/p');
  });

  it('falls back to the DOI resolver, stripping a doi: prefix', () => {
    expect(paperUrl({ ...base, doi: 'doi:10.1234/abc' }, 'http://local')).toBe('https://doi.org/10.1234/abc');
  });

  it('last resort is the local paper page, encoded and without a trailing slash', () => {
    expect(paperUrl(base, 'http://local/')).toBe('http://local/papers/iclr_virtual%3A2026%2F1');
  });

  it('treats empty strings as absent instead of emitting a blank URL', () => {
    expect(paperUrl({ ...base, paper_url: '  ', doi: '' }, 'http://local')).toContain('/papers/');
  });
});

describe('venueLabel', () => {
  it('joins venue names with years', () => {
    expect(venueLabel(base)).toBe('ICLR 2026');
  });

  it('falls back to the year alone, then to nothing', () => {
    expect(venueLabel({ ...base, venues: [] })).toBe('2026');
    expect(venueLabel({ ...base, venues: [], year: 0 })).toBe('');
  });
});

describe('formatCitations', () => {
  it('writes one Markdown bullet per item, with venue and score', () => {
    const text = formatCitations([{ ...base, paper_url: 'https://example.org/p' }], 'markdown', 'http://local');
    expect(text).toBe('- [A Paper About Things](https://example.org/p) — ICLR 2026, score 0.87');
  });

  it('writes bare titles and bare URLs', () => {
    const items = [
      { ...base, paper_url: 'https://example.org/p' },
      { ...base, title: 'Second', paper_url: 'https://example.org/q' },
    ];
    expect(formatCitations(items, 'titles', 'http://local')).toBe('A Paper About Things\nSecond');
    expect(formatCitations(items, 'urls', 'http://local')).toBe('https://example.org/p\nhttps://example.org/q');
  });

  it('emits a Braced BibTeX entry with authors joined by "and"', () => {
    const text = formatCitations([{ ...base, paper_url: 'https://example.org/p' }], 'bibtex', 'http://local');
    expect(text).toContain('@article{iclr_virtual_2026_1,');
    expect(text).toContain('title = {A Paper About Things}');
    expect(text).toContain('author = {Ada Lovelace and Alan Turing}');
    expect(text).toContain('year = {2026}');
    expect(text).toContain('url = {https://example.org/p}');
    expect(text).toContain('note = {ICLR 2026}');
  });

  it('escapes braces and ampersands so the entry stays valid', () => {
    const text = formatCitations([{ ...base, title: 'Q&A about {masks}', paper_url: '' }], 'bibtex', '');
    expect(text).toContain('title = {Q\\&A about \\{masks\\}}');
  });

  it('omits a local-only URL from BibTeX but still produces an entry', () => {
    const text = formatCitations([base], 'bibtex', 'http://local');
    expect(text).toContain('@article{');
    expect(text).not.toContain('url =');
  });

  it('keeps a starting-digit key legal for BibTeX', () => {
    expect(formatCitations([{ ...base, id: '123', slug: '123' }], 'bibtex', '')).toContain('@article{p_123,');
  });
});

describe('exportFilename', () => {
  it('uses an extension that matches the format', () => {
    expect(exportFilename('bibtex')).toBe('papers-bibtex.bib');
    expect(exportFilename('markdown')).toBe('papers-markdown.md');
    expect(exportFilename('urls')).toBe('papers-urls.txt');
  });
});
