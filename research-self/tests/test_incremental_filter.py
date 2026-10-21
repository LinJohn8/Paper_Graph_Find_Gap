"""The incremental contract: the watermark must reach the request.

An incremental sync is only cheaper if the filter actually narrows the query.
This is checked offline, against the recorded OpenAlex payload, so the
contract is pinned without a live API (and without a key).

The replay harness is used here for what it is for: a recorded response goes
through the *production* request-building and parsing code, and the request
that was actually issued is inspected.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import replay  # noqa: E402
from paper_ingest.provider_journals import JournalProvider  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def provider():
    """A JournalProvider whose HTTP client replays the recorded payload."""
    from paper_ingest import providers_openalex

    client = replay.ReplayClient(directory=FIXTURES)
    instance = JournalProvider("nature", mailto="research@example.org")
    # The provider owns its client; the recorded one replaces it.
    instance.http._client = client
    instance.http._owns_client = False
    yield instance
    client.close()


def _requests(provider) -> list[str]:
    """The URLs the provider actually requested, in order."""
    return [url for _method, url in provider.http._client.transport.used]


class TestSinceFilter:
    def test_a_since_date_reaches_the_query(self, provider, monkeypatch):
        from paper_ingest import provider_journals

        monkeypatch.setitem(provider_journals.JOURNAL_SOURCES, "nature",
                            ("S123", "Nature"))
        provider.list_year(2024, since="2026-01-02")
        urls = _requests(provider)
        assert urls, "the provider made no request"
        assert "from_created_date%3A2026-01-02" in urls[0] or \
               "from_created_date:2026-01-02" in urls[0]

    def test_without_a_since_the_query_carries_no_date_filter(self, provider):
        provider.list_year(2024)
        urls = _requests(provider)
        assert urls
        assert "from_created_date" not in urls[0]

    def test_the_source_and_year_filters_are_kept(self, provider):
        provider.list_year(2024, since="2026-01-02")
        url = _requests(provider)[0]
        assert "locations.source.id" in url
        assert "publication_year" in url
        assert "type" in url

    def test_a_full_timestamp_is_truncated_to_its_date(self, provider):
        # OpenAlex rejects a full timestamp in from_created_date, so the
        # watermark is cut to its date part before it is sent.
        provider.list_year(2024, since="2026-01-02T13:45:00Z")
        url = _requests(provider)[0]
        assert "2026-01-02" in url
        assert "13%3A45" not in url and "13:45" not in url

    def test_the_recorded_payload_is_parsed_by_the_production_code(self, provider):
        papers = provider.list_year(2024)
        assert papers, "the recorded payload produced no papers"
        paper = papers[0]
        # The production normaliser filled the paper-dict contract.
        for key in ("id", "title", "abstract", "authors", "keywords", "doi"):
            assert key in paper, f"{key} is missing from the normalised paper"
        assert paper["title"]
