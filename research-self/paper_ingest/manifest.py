"""Expected corpus sizes per venue-year ("the manifest").

Why this module exists
----------------------
This project harvests ~197,000 papers from OpenAlex, arXiv, publisher
proceedings and journal sites.  The recurring failure mode is a **silently
empty harvest**: a provider changes its markup, a rate limiter starts
answering 401, a volume page comes back truncated -- and the sync simply
writes fewer papers.  Nothing raises, nothing is logged as an error, and the
corpus quietly loses a venue-year.

A harvest therefore has to be compared against an *expectation*: how many
papers this venue-year should contain, recorded once from a healthy run.
This module owns that expectation, the verdict of a check against it, and a
corpus-wide audit.

Design notes
------------
* The manifest is **optional**.  A missing file is not an error: it simply
  means "nothing is expected yet" and every venue-year reports ``unknown``.
  A harvester must never fail because nobody wrote a manifest.
* The check is **advisory, never fatal**.  A short harvest is a *reported*
  outcome, not a crash: the caller decides what to do with the verdict.
* Counts are compared against the *floor* (``min_papers``) and the *ceiling*
  (``max_papers``) recorded for that venue-year.  Both are optional; an
  expectation with neither is reported as ``unknown``.

File format
-----------
YAML (PyYAML is a declared dependency of this project) with a JSON fallback,
so the manifest can also be written as ``expected_counts.json`` with the same
shape::

    venues:
      neurips:
        2024: {min_papers: 4000, max_papers: 6000, note: "NeurIPS 2024 main track"}
      jmlr:
        2025: {min_papers: 250}

The loader also accepts the same mapping under an ``expected:`` key, and a
bare ``{venue: {year: spec}}`` document, so the tracked registry template
(``venues.example.yaml``) can carry the expectations next to the venues it
describes.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from . import db

try:  # PyYAML is a declared dependency; the JSON fallback keeps this usable anyway.
    import yaml
except ImportError:  # pragma: no cover - only reached on a PyYAML-less install
    yaml = None  # type: ignore[assignment]


#: Name of the manifest file inside the data root.
MANIFEST_FILENAME = "expected_counts.yaml"

#: A verdict is one of these four states.  ``unknown`` means "no usable
#: expectation was recorded", not "something went wrong".
CoverageStatus = Literal["ok", "short", "over", "unknown"]

OK: CoverageStatus = "ok"
SHORT: CoverageStatus = "short"
OVER: CoverageStatus = "over"
UNKNOWN: CoverageStatus = "unknown"

#: Sentinel for "the YAML parser produced nothing usable".
_MISSING = object()

#: Keys accepted for the floor/ceiling of one venue-year, in priority order.
_MIN_KEYS = ("min_papers", "min")
_MAX_KEYS = ("max_papers", "max")


@dataclass
class Expectation:
    """How many papers one venue-year is expected to contain.

    ``min_papers`` is the floor: below it the harvest is *short* and the
    venue-year is (probably) missing rows.  ``max_papers`` is the ceiling:
    above it the harvest is *over*, which usually means a duplicated or
    mis-scoped import rather than a good harvest.  Both are optional; an
    expectation with neither bound is reported as ``unknown``.

    ``note`` explains where the number came from, ``measured_at`` when it was
    measured and ``source`` how (e.g. ``sqlite:paper_venues``), so a stale
    expectation can be told apart from a wrong one.
    """

    venue: str
    year: int
    min_papers: int | None = None
    max_papers: int | None = None
    note: str | None = None
    measured_at: str | None = None
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The expectation as a plain JSON-serialisable dict."""
        return {
            "venue": self.venue,
            "year": self.year,
            "min_papers": self.min_papers,
            "max_papers": self.max_papers,
            "note": self.note,
            "measured_at": self.measured_at,
            "source": self.source,
        }


def manifest_path() -> Path:
    """Where the manifest is expected to live: ``<DATA_ROOT>/expected_counts.yaml``."""
    return Path(db.DATA_ROOT) / MANIFEST_FILENAME


def load_manifest(path: Path | None = None) -> dict[tuple[str, int], Expectation]:
    """Load the venue-year expectations, keyed by ``(venue, year)``.

    WHY A MISSING FILE IS NOT AN ERROR
    -----------------------------------
    The manifest is an opt-in instrument.  Deployments that never write one
    must still be able to harvest, so a missing (or unreadable) file yields an
    empty mapping and every venue-year simply reports ``unknown``.

    A file that exists but cannot be parsed *is* an error: silently ignoring a
    broken manifest would disable the very check it exists for.  The raised
    ``ValueError`` names the file so the operator can fix it.
    """
    target = Path(path) if path is not None else manifest_path()
    try:
        if not target.exists():
            return {}
        text = target.read_text(encoding="utf-8")
    except OSError:
        # An unreadable manifest must not stop a harvest either.
        return {}
    document = _parse_document(text, target)
    if not isinstance(document, Mapping):
        return {}
    return _expectations_from_document(document)


def _parse_document(text: str, source: Path) -> Any:
    """Parse the manifest text as YAML, falling back to JSON.

    JSON is a subset of YAML, so a YAML-capable install reads both; the JSON
    path only matters when PyYAML is unavailable, and as a safety net for
    documents YAML reads as a bare scalar.
    """
    errors: list[str] = []
    if yaml is not None:
        try:
            parsed = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - any parser error falls through to JSON
            errors.append(f"YAML: {exc}")
        else:
            if parsed is _MISSING or isinstance(parsed, (Mapping, list)) or parsed is None:
                return parsed
            # A scalar YAML document is almost always a JSON file whose syntax
            # YAML read as a string: try JSON before giving up on it.
            errors.append("YAML: document is a scalar, not a mapping")
    try:
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001 - reported as one clear error
        errors.append(f"JSON: {exc}")
    raise ValueError(f"Could not parse the expectation manifest {source}: " + "; ".join(errors))


def _expectations_from_document(document: Mapping[str, Any]) -> dict[tuple[str, int], Expectation]:
    """Pull every ``(venue, year)`` expectation out of a parsed manifest.

    Both a ``venues:`` block (the documented format) and an ``expected:``
    block (the shape used by the tracked registry template) are read, so one
    file can carry both the venue registry and its expectations.
    """
    blocks = [document[key] for key in ("venues", "expected") if isinstance(document.get(key), Mapping)]
    if not blocks:
        # A bare {venue: {year: spec}} document is accepted as-is.
        blocks = [document]
    out: dict[tuple[str, int], Expectation] = {}
    for block in blocks:
        for venue, years_block in block.items():
            if not isinstance(years_block, Mapping):
                continue
            for year, spec in years_block.items():
                expectation = _expectation_from_spec(str(venue), year, spec)
                if expectation is not None:
                    out[(expectation.venue, expectation.year)] = expectation
    return out


def _expectation_from_spec(venue: str, year: Any, spec: Any) -> Expectation | None:
    """Build one :class:`Expectation` from a manifest entry.

    Returns ``None`` for entries that cannot be read as a venue-year (a venue
    block that is really a registry entry, a non-numeric year key, ...), so a
    manifest that is accidentally pointed at the venue registry degrades to
    "no expectations" instead of raising.
    """
    try:
        parsed_year = int(str(year).strip())
    except (TypeError, ValueError):
        return None
    if isinstance(spec, Mapping):
        minimum = _first_int(spec, _MIN_KEYS)
        maximum = _first_int(spec, _MAX_KEYS)
        note = _first_str(spec, ("note", "comment"))
        measured_at = _first_str(spec, ("measured_at", "measured"))
        source = _first_str(spec, ("source",))
        if minimum is None and maximum is None and note is None and measured_at is None and source is None:
            # Not an expectation at all (e.g. a venue registry entry).
            return None
    elif isinstance(spec, bool):
        return None
    elif isinstance(spec, int):
        # Shorthand: `2024: 4000` means "at least 4000 papers".
        minimum, maximum, note, measured_at, source = spec, None, None, None, None
    else:
        return None
    return Expectation(
        venue=venue,
        year=parsed_year,
        min_papers=minimum,
        max_papers=maximum,
        note=note,
        measured_at=measured_at,
        source=source,
    )


def _first_int(spec: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in spec:
            value = spec[key]
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _first_str(spec: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = spec.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def check_expectation(expectation: Expectation, actual: int) -> dict[str, Any]:
    """Compare a measured paper count with an expectation.

    Returns a verdict dict with ``venue``, ``year``, ``actual``, ``min_papers``,
    ``max_papers``, ``status`` and a human-readable ``message``.

    * ``short``  -- fewer papers than the recorded floor (the harvest lost rows).
    * ``over``   -- more papers than the recorded ceiling (a duplicate or
      mis-scoped import).
    * ``ok``     -- inside the recorded range.
    * ``unknown`` -- no usable bound was recorded, so nothing can be said.
    """
    minimum = expectation.min_papers
    maximum = expectation.max_papers
    if minimum is None and maximum is None:
        status: CoverageStatus = UNKNOWN
    elif minimum is not None and actual < minimum:
        status = SHORT
    elif maximum is not None and actual > maximum:
        status = OVER
    else:
        status = OK
    return {
        "venue": expectation.venue,
        "year": expectation.year,
        "actual": int(actual),
        "min_papers": minimum,
        "max_papers": maximum,
        "status": status,
        "message": _verdict_message(status, expectation, int(actual)),
    }


def _verdict_message(status: CoverageStatus, expectation: Expectation, actual: int) -> str:
    """One sentence explaining a verdict, for logs and for the UI."""
    label = f"{expectation.venue} {expectation.year}"
    if status == UNKNOWN:
        return f"{label}: {actual} papers, no expectation recorded."
    if status == SHORT:
        assert expectation.min_papers is not None  # implied by the status
        return (f"{label}: {actual} papers, short of the expected minimum of "
                f"{expectation.min_papers} ({expectation.min_papers - actual} missing).")
    if status == OVER:
        assert expectation.max_papers is not None  # implied by the status
        return (f"{label}: {actual} papers, above the expected maximum of "
                f"{expectation.max_papers} ({actual - expectation.max_papers} extra).")
    if expectation.min_papers is not None and expectation.max_papers is not None:
        return (f"{label}: {actual} papers, within the expected "
                f"{expectation.min_papers}-{expectation.max_papers} range.")
    if expectation.min_papers is not None:
        return f"{label}: {actual} papers, at or above the expected minimum of {expectation.min_papers}."
    assert expectation.max_papers is not None
    return f"{label}: {actual} papers, at or below the expected maximum of {expectation.max_papers}."


def check_venue(venue: str, year: int, actual: int,
                manifest: Mapping[tuple[str, int], Expectation] | None = None) -> dict[str, Any]:
    """Look an expectation up by ``(venue, year)`` and check ``actual`` against it.

    A venue-year that has no expectation yields an ``unknown`` verdict -- not
    an error: most venue-years of a young corpus have no expectation yet.
    """
    expectations = manifest if manifest is not None else load_manifest()
    expectation = expectations.get((venue, int(year)))
    if expectation is None:
        return {
            "venue": venue,
            "year": int(year),
            "actual": int(actual),
            "min_papers": None,
            "max_papers": None,
            "status": UNKNOWN,
            "message": f"{venue} {int(year)}: {int(actual)} papers, no expectation recorded.",
        }
    return check_expectation(expectation, actual)


def verify_corpus(manifest: Mapping[tuple[str, int], Expectation] | None = None,
                  con: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Check every venue-year that has an expectation against the live corpus.

    WHY THIS EXISTS
    ---------------
    A sync only checks the venue-year it just harvested.  This audits the
    *whole* corpus, so a venue-year that quietly went missing months ago is
    still found.

    Returns the non-``ok`` verdicts followed by a summary dict
    (``checked``/``ok``/``short``/``over``/``unknown``) as the last element, so
    a caller can report the problem list and the totals in one payload.
    """
    expectations = manifest if manifest is not None else load_manifest()
    if con is None:
        db.init_db()
        with db.connect() as owned:
            verdicts = _verify_with_connection(expectations, owned)
    else:
        verdicts = _verify_with_connection(expectations, con)
    return verdicts


def verify_corpus_summary(manifest: Mapping[tuple[str, int], Expectation] | None = None,
                          con: sqlite3.Connection | None = None) -> dict[str, Any]:
    """The summary of :func:`verify_corpus` alone: counts per verdict status."""
    verdicts = verify_corpus(manifest, con)
    return verdicts[-1] if verdicts else {"checked": 0, "ok": 0, "short": 0, "over": 0, "unknown": 0}


def _verify_with_connection(expectations: Mapping[tuple[str, int], Expectation],
                            con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Run every expectation against the counts stored in ``paper_venues``."""
    counts = _paper_counts(con, expectations)
    summary: dict[str, int] = {"checked": 0, "ok": 0, "short": 0, "over": 0, "unknown": 0}
    problems: list[dict[str, Any]] = []
    for (venue, year), expectation in sorted(expectations.items()):
        # A venue-year with no rows at all counts as zero papers: that is
        # exactly the "silently empty harvest" this module exists to catch.
        actual = counts.get((venue, year), 0)
        verdict = check_expectation(expectation, actual)
        summary["checked"] += 1
        summary[str(verdict["status"])] += 1
        if verdict["status"] != OK:
            problems.append(verdict)
    problems.append(summary)
    return problems


def _paper_counts(con: sqlite3.Connection,
                  expectations: Mapping[tuple[str, int], Expectation]) -> dict[tuple[str, int], int]:
    """Count the stored papers per ``(venue_id, year)`` for the venues we care about."""
    venue_ids = sorted({venue for venue, _ in expectations})
    if not venue_ids:
        return {}
    placeholders = ",".join("?" * len(venue_ids))
    rows = con.execute(
        f"SELECT venue_id, year, count(DISTINCT paper_id) FROM paper_venues "
        f"WHERE venue_id IN ({placeholders}) GROUP BY venue_id, year",
        venue_ids,
    ).fetchall()
    counts: dict[tuple[str, int], int] = {}
    for row in rows:
        counts[(str(row[0]), int(row[1]))] = int(row[2])
    return counts
