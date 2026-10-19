"""Year resolution helpers.

Why this module exists
----------------------
The app was written when the target conference year was a fixed placeholder
(2027), so ``2027`` was hardcoded as the default year across the API, the CLI and
the frontend. That breaks as soon as the calendar moves on: the UI opens on a
year with no data and every endpoint has to be told the year explicitly.

The correct rule is derived from the *data* rather than a literal:

* ``current_year()`` -- the real calendar year (used for "what should we sync
  next" decisions).
* ``default_year()`` -- the most recent year that actually has papers, falling
  back to the current year when the database is empty. This is what the UI should
  open on, so a fresh install shows real content instead of an empty placeholder.
* ``available_years()`` -- the years present in the database, newest first, so the
  frontend can offer a real selector.

``PAPER_DEFAULT_YEAR`` overrides ``default_year()`` for tests and for pinning a
deployment to a specific conference edition.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import db


def current_year() -> int:
    """The calendar year right now (local time)."""
    return datetime.now().astimezone().year


def _override() -> int | None:
    raw = os.environ.get("PAPER_DEFAULT_YEAR")
    if raw and raw.strip().isdigit():
        year = int(raw.strip())
        if 1900 <= year <= 2200:
            return year
    return None


def available_years() -> list[int]:
    """Years that actually contain papers, newest first.

    Returns an empty list when the database has not been created yet; callers
    must not assume at least one year exists.
    """
    override = _override()
    try:
        db.init_db()
        with db.connect() as con:
            rows = con.execute(
                "SELECT DISTINCT year FROM paper_venues ORDER BY year DESC"
            ).fetchall()
    except Exception:  # noqa: BLE001 - a missing/broken DB must not break the API
        return [override] if override else []
    years = [int(r["year"]) for r in rows if r["year"] is not None]
    if override is not None and override not in years:
        years.append(override)
        years.sort(reverse=True)
    return years


def default_year() -> int:
    """The year the UI should open on: newest year with data, else this year."""
    override = _override()
    if override is not None:
        return override
    years = available_years()
    if years:
        return years[0]
    return current_year()


def resolve(year: int | str | None, *, fallback: int | None = None) -> int:
    """Turn an optional/blank request parameter into a concrete year.

    FastAPI cannot call :func:`default_year` in a signature default because query
    defaults are evaluated at import time (they would freeze the first value
    forever). Endpoints therefore accept ``year: int | None = None`` and call this
    helper, which keeps the "newest available year" behaviour live per request.
    """
    if year is None or (isinstance(year, str) and not year.strip()):
        return default_year() if fallback is None else fallback
    try:
        value = int(year)
    except (TypeError, ValueError):
        return default_year() if fallback is None else fallback
    if 1900 <= value <= 2200:
        return value
    return default_year() if fallback is None else fallback
