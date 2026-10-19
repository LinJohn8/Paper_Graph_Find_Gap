"""Regression tests for OpenAlex cursor->offset paging fallback.

Why this matters: OpenAlex gates deep cursor paging behind an API key. The first
page works unkeyed; the second returns 401 "API key required for cursor paging".
A cursor-only harvester is therefore silently capped at 200 works per venue-year,
which made real corpora look like tiny samples (the actual reported bug).
Offset paging (`page=N`) is NOT gated, so the provider must fall back to it.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from paper_ingest.providers_openalex import OpenAlexProvider, PoliteHttpError, PoliteHttp


class _FakeTransport(httpx.BaseTransport):
    """Emulates OpenAlex: cursor page 1 OK, cursor page 2 -> 401, offset OK."""

    def __init__(self, total: int = 450, per_page: int = 200) -> None:
        self.total = total
        self.per_page = per_page
        self.requests: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        page = int(params.get("page", 0) or 0)
        cursor = params.get("cursor")
        size = int(params.get("per-page", self.per_page))

        if cursor is not None and cursor != "*":
            return httpx.Response(
                401,
                json={
                    "error": "API key required for cursor paging",
                    "message": "Paging past the first page with cursor= needs a free API key.",
                },
            )

        start = 0 if cursor == "*" else max(0, (page - 1)) * size
        end = min(self.total, start + size)
        results = [self._work(i) for i in range(start, end)]
        next_cursor = "CURSOR_2" if (cursor == "*" and end < self.total) else None
        return httpx.Response(200, json={"meta": {"count": self.total, "next_cursor": next_cursor},
                                        "results": results})

    @staticmethod
    def _work(i: int) -> dict[str, Any]:
        return {
            "id": f"https://openalex.org/W{i:09d}",
            "doi": f"https://doi.org/10.1/{i}",
            "title": f"Paper number {i} with a decently long title",
            "abstract_inverted_index": {"word": [0], "test": [1]},
            "publication_year": 2024,
            "type": "article",
            "authorships": [],
            "primary_location": {"source": {"display_name": "Neural Information Processing Systems",
                                            "id": "https://openalex.org/S4363606243"}},
            "referenced_works": [],
            "cited_by_count": 0,
        }


def _provider(transport: _FakeTransport, per_page: int = 200) -> OpenAlexProvider:
    client = httpx.Client(transport=transport)
    http = PoliteHttp(mailto="research@example.org", client=client, sleep=lambda _s: None)
    return OpenAlexProvider(mailto="research@example.org", http=http, per_page=per_page)


def test_cursor_gating_falls_back_to_offset_and_exceeds_first_page():
    """The regression: must return the FULL corpus, not stop at 200."""
    transport = _FakeTransport(total=450)
    prov = _provider(transport)
    papers = prov.search_source("neurips", 2024)
    assert len(papers) == 450, "harvest was capped at the first cursor page"
    assert len({p["id"] for p in papers}) == 450, "duplicate rows after fallback"


def test_fallback_survives_multiple_pages():
    transport = _FakeTransport(total=1050, per_page=200)
    prov = _provider(transport)
    papers = prov.search_source("cvpr", 2023)
    assert len(papers) == 1050
    # Offset paging must actually have been used.
    assert any(int(p.get("page", 0) or 0) >= 2 for p in transport.requests)


def test_limit_is_still_honoured_under_fallback():
    transport = _FakeTransport(total=900)
    prov = _provider(transport)
    papers = prov.search_source("neurips", 2024, limit=250)
    assert len(papers) == 250


def test_no_fallback_needed_when_corpus_fits_first_page():
    transport = _FakeTransport(total=120)
    prov = _provider(transport)
    papers = prov.search_source("acl", 2024)
    assert len(papers) == 120
    assert not any(int(p.get("page", 0) or 0) >= 1 for p in transport.requests)


def test_zero_limit_returns_empty_without_requests():
    transport = _FakeTransport(total=500)
    prov = _provider(transport)
    assert prov.search_source("neurips", 2024, limit=0) == []
    assert transport.requests == []
