import { describe, expect, it } from 'vitest';
import { groupedVenues, uniqueVenues, type VenueItem } from '@/lib/venues';

const item = (id: string, type: string | null, name = id): VenueItem => ({
  id,
  name,
  year: 2025,
  type,
});

describe('uniqueVenues', () => {
  it('keeps one entry per venue id, in first-seen order', () => {
    const items = [
      item('iclr', 'conference'),
      item('nature', 'journal'),
      item('iclr', 'conference'),
      item('nature', 'journal'),
    ];
    expect(uniqueVenues(items).map((v) => v.id)).toEqual(['iclr', 'nature']);
  });

  it('tolerates a missing registry', () => {
    expect(uniqueVenues(undefined)).toEqual([]);
    expect(uniqueVenues(null)).toEqual([]);
    expect(uniqueVenues([])).toEqual([]);
  });
});

describe('groupedVenues', () => {
  it('splits conferences and journals into labelled groups', () => {
    const groups = groupedVenues([
      item('iclr', 'conference'),
      item('nature', 'journal'),
      item('tmlr', 'rolling_review'),
    ]);
    const labels = groups.map((g) => g.label);
    expect(labels).toContain('Conferences & rolling venues');
    expect(labels).toContain('Journals');

    const conferences = groups.find((g) => g.label === 'Conferences & rolling venues');
    expect(conferences?.venues.map((v) => v.id)).toEqual(['iclr', 'tmlr']);
    const journals = groups.find((g) => g.label === 'Journals');
    expect(journals?.venues.map((v) => v.id)).toEqual(['nature']);
  });

  it('keeps a venue with an unknown type visible instead of dropping it', () => {
    const groups = groupedVenues([item('mystery', 'preprint'), item('nature', 'journal')]);
    const other = groups.find((g) => g.label === 'Other');
    expect(other?.venues.map((v) => v.id)).toEqual(['mystery']);
  });

  it('lists every unique venue exactly once across all groups', () => {
    const items = [
      item('iclr', 'conference'),
      item('nature', 'journal'),
      item('iclr', 'conference'),
      item('tpami', 'journal'),
      item('mystery', null),
    ];
    const all = groupedVenues(items).flatMap((g) => g.venues.map((v) => v.id));
    expect(all).toHaveLength(4);
    expect(new Set(all).size).toBe(4);
  });

  it('returns nothing for an empty registry', () => {
    expect(groupedVenues([])).toEqual([]);
    expect(groupedVenues(undefined)).toEqual([]);
  });
});
