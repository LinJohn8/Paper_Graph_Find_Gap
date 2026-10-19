import { describe, expect, it } from 'vitest';
import { currentYear, parseYearInfo, resolveYear, yearOptions } from '@/lib/years';

describe('resolveYear', () => {
  const info = { defaultYear: 2025, availableYears: [2025, 2024], currentYear: 2026 };

  it('uses an explicit URL year', () => {
    expect(resolveYear('2024', info)).toBe(2024);
  });

  it('falls back to the backend default year, not a hardcoded literal', () => {
    expect(resolveYear(undefined, info)).toBe(2025);
  });

  it('ignores malformed years', () => {
    expect(resolveYear('not-a-year', info)).toBe(2025);
    expect(resolveYear('999', info)).toBe(2025);
    expect(resolveYear('9999', info)).toBe(2025);
  });

  it('handles array-valued search params', () => {
    expect(resolveYear(['2023', '2024'], info)).toBe(2023);
  });

  it('falls back to the calendar year when the backend is unavailable', () => {
    expect(resolveYear(undefined, null)).toBe(currentYear());
  });
});

describe('yearOptions', () => {
  it('lists available years newest first and includes the selection', () => {
    const info = { defaultYear: 2025, availableYears: [2024, 2025], currentYear: 2026 };
    expect(yearOptions(info, 2023)).toEqual([2026, 2025, 2024, 2023]);
  });

  it('never returns an empty list', () => {
    expect(yearOptions(null, undefined)).toEqual([currentYear()]);
  });

  it('de-duplicates overlapping sources', () => {
    const info = { defaultYear: 2025, availableYears: [2025, 2025], currentYear: 2025 };
    expect(yearOptions(info, 2025)).toEqual([2025]);
  });
});

describe('parseYearInfo', () => {
  it('reads the backend contract', () => {
    const info = parseYearInfo({
      default_year: 2024,
      available_years: [2024, 2023],
      current_year: 2026,
      items: [
        { id: 'icml', year: 2024 },
        { id: 'icml', year: 2023 },
      ],
    });
    expect(info).not.toBeNull();
    expect(info!.defaultYear).toBe(2024);
    expect(info!.availableYears).toEqual([2024, 2023]);
    expect(info!.currentYear).toBe(2026);
  });

  it('derives years from items when available_years is absent (older server)', () => {
    const info = parseYearInfo({ items: [{ id: 'icml', year: 2023 }, { id: 'icml', year: 2022 }] });
    expect(info!.availableYears).toEqual([2023, 2022]);
  });

  it('returns null for a non-object payload', () => {
    expect(parseYearInfo(null)).toBeNull();
    expect(parseYearInfo('nope')).toBeNull();
  });
});
