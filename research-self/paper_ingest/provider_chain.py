"""Wiring: build a real provider chain per venue and year.

Responsibility
--------------
Given a venue key and a year, produce an ordered list of providers most likely
to cover it, then let ``harvest.MultiSourceHarvester`` merge their output.

Ordering rationale (measured live, not guessed)
-----------------------------------------------
OpenAlex per-year work counts (``locations.source.id`` filter):

    venue     2022   2023   2024   2025   2026
    neurips   2806   3509   4322   5832      2
    cvpr      2082   2368   2724   2876      0
    acl        700   1249   1036   1973   2655
    icml         8      0      7      3      3
    iclr         2      4      3      3      0

arXiv comment-tag counts (``co:"<Venue> <Year>"``):

    venue     2025   2026
    icml      1386   2857
    iclr      1709   2279
    neurips   2900   1359
    cvpr      1343   1980
    acl       1336   1591

Two conclusions drive the ordering:

1. **ICML/ICLR are effectively absent from OpenAlex** (single digits per year, and
   zero for ICML 2023), so arXiv must lead for them regardless of year.

2. **For recent years OpenAlex lags badly** while arXiv is already populated.
   NeurIPS 2026 has 2 OpenAlex records but 1359 arXiv-tagged papers; CVPR 2026 has
   0 vs 1980. Papers get their venue tag on arXiv long before proceedings are
   indexed (proceedings often appear months after the conference). So starting
   from a configurable "recent" cut-off, arXiv leads for *every* venue -- this is
   what makes 2025/2026 harvestable at all.

Crossref is a last-resort supplement: DOI-complete, but conference records
commonly carry no abstract, so it only runs when the richer sources came back
thin.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from . import harvest as _harvest
from .provider_arxiv import ArxivProvider
from .providers_openalex import CrossrefProvider, OpenAlexProvider

#: Venues whose papers are reliably tagged in arXiv comments while OpenAlex has
#: almost no coverage, so arXiv must lead for every year.
_ARXIV_FIRST = {"icml", "iclr"}

#: Venues with strong historical OpenAlex coverage.
_OPENALEX_STRONG = {"neurips", "cvpr", "acl"}

#: From this year onward, OpenAlex is typically still catching up on proceedings
#: while arXiv already carries the accepted papers, so arXiv leads for all venues.
#: Overridable via ``PAPER_ARXIV_FIRST_YEAR``.
ARXIV_FIRST_FROM_YEAR = 2025


def _current_year() -> int:
    """This year in UTC, used for defaulting instead of a hardcoded placeholder."""
    return datetime.now().astimezone().year


def _recent_cutoff() -> int:
    raw = os.environ.get("PAPER_ARXIV_FIRST_YEAR")
    if raw and raw.strip().isdigit():
        return int(raw.strip())
    return ARXIV_FIRST_FROM_YEAR


def _openalex_key(explicit: str | None) -> str | None:
    """Resolve the OpenAlex API key, preferring an explicit value.

    A key is optional but strongly recommended: without one, OpenAlex meters the
    request against a very small daily budget shared by every client on the same
    public IP, which a normal harvest burst exhausts (it then answers 401 even
    though the query is valid). A free key gets its own larger daily allowance.
    """
    if explicit:
        return explicit
    for var in ("OPENALEX_API_KEY", "PAPER_OPENALEX_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _mailto(explicit: str | None) -> str:
    """Resolve the contact address used for polite-pool identification."""
    if explicit and explicit.strip():
        return explicit.strip()
    for var in ("PAPER_CONTACT_EMAIL", "OPENALEX_MAILTO", "CROSSREF_MAILTO"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return "research@example.org"


def plan_sources(venue: str, year: int) -> list[str]:
    """Return the provider names to try, best-first, for ``venue``/``year``.

    Exposed separately from :func:`build_providers` so it can be unit-tested
    without constructing HTTP clients, and so the UI/CLI can explain *why* a
    particular source order was chosen.
    """
    key = (venue or "").strip().lower()
    if key in _ARXIV_FIRST:
        return ["arxiv", "openalex", "crossref"]
    if year >= _recent_cutoff():
        # Recent proceedings are not indexed yet; arXiv already has the tags.
        return ["arxiv", "openalex", "crossref"]
    return ["openalex", "arxiv", "crossref"]


def build_providers(
    venue: str,
    year: int | None = None,
    *,
    mailto: str | None = None,
    api_key: str | None = None,
    arxiv_delay: float = 3.0,
    include_crossref: bool = True,
) -> list[Any]:
    """Return providers ordered best-first for ``venue`` (and ``year``)."""
    contact = _mailto(mailto)
    openalex = OpenAlexProvider(mailto=contact, api_key=_openalex_key(api_key))
    arxiv = ArxivProvider(delay=arxiv_delay)
    crossref = CrossrefProvider(mailto=contact) if include_crossref else None

    order = plan_sources(venue, year if year is not None else _current_year())
    by_name = {"openalex": openalex, "arxiv": arxiv, "crossref": crossref}
    chain: list[Any] = []
    for name in order:
        provider = by_name.get(name)
        if provider is not None:
            chain.append(provider)
    return chain


def harvest_venue(
    venue: str,
    year: int,
    *,
    limit: int | None = None,
    target: int | None = None,
    mailto: str | None = None,
    api_key: str | None = None,
    arxiv_delay: float = 3.0,
) -> _harvest.HarvestResult:
    """Harvest a venue/year from every available official source."""
    providers = build_providers(
        venue, year, mailto=mailto, api_key=api_key, arxiv_delay=arxiv_delay
    )
    harvester = _harvest.MultiSourceHarvester(providers, target=target)
    return harvester.harvest(venue, year, limit=limit)
