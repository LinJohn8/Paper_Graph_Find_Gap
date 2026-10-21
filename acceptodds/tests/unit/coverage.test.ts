import { describe, expect, it } from 'vitest';

import { coverageDetail, coverageLabel } from '@/lib/coverage';

describe('coverageLabel', () => {
  it('labels a complete venue-year', () => {
    expect(coverageLabel('ok')).toEqual({ text: 'Complete', className: 'text-muted' });
  });

  it('marks a short venue-year with the attention colour', () => {
    expect(coverageLabel('short')).toEqual({
      text: 'Below expected',
      className: 'text-accent',
    });
  });

  it('labels an over-full venue-year quietly', () => {
    expect(coverageLabel('over')).toEqual({
      text: 'Above expected',
      className: 'text-subtle',
    });
  });

  it('says nothing when there is no expectation', () => {
    // A venue-year without a recorded expectation is not a problem, so it
    // must not render as a warning.
    expect(coverageLabel('unknown')).toBeNull();
    expect(coverageLabel(null)).toBeNull();
    expect(coverageLabel(undefined)).toBeNull();
    expect(coverageLabel('')).toBeNull();
  });

  it('ignores a status it does not know', () => {
    expect(coverageLabel('something-new')).toBeNull();
  });
});

describe('coverageDetail', () => {
  it('gives the floor for a short venue-year', () => {
    expect(coverageDetail('short', 5250)).toBe('expected at least 5250');
  });

  it('says nothing for the other verdicts', () => {
    expect(coverageDetail('ok', 5250)).toBeNull();
    expect(coverageDetail('over', 5250)).toBeNull();
    expect(coverageDetail('unknown', 5250)).toBeNull();
  });

  it('says nothing when the floor is unknown', () => {
    expect(coverageDetail('short', null)).toBeNull();
  });
});
