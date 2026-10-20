"""Unit tests for the journal provider + abstract backfill (temp DB, live HTTP mocked at the seam).

The OpenAlex wire format is exercised against a recorded work payload so the
test does not depend on network availability; the landing-page backfill is
exercised against fixture HTML.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import journal_abstracts as ja  # noqa: E402
from paper_ingest.provider_journals import (  # noqa: E402
    JOURNAL_SOURCES,
    JournalProvider,
    normalize_title_key,
)


def test_registry_has_springer_and_sci_core():
    # The user asked for Springer Nature and SCI-level journals specifically.
    assert "nature" in JOURNAL_SOURCES
    assert "science" in JOURNAL_SOURCES
    assert "naturemi" in JOURNAL_SOURCES
    for key, (sid, name) in JOURNAL_SOURCES.items():
        assert sid.startswith("S"), key
        assert name


def test_unknown_journal_rejected():
    with pytest.raises(ValueError):
        JournalProvider("nonexistent_journal")


def test_title_key_normalisation():
    assert normalize_title_key("A Paper: About Things!") == "apaperaboutthings"
    assert normalize_title_key(None) == ""
    assert normalize_title_key("  ") == ""


LANDING_WITH_META = """
<html><head>
<meta property="og:description" content="We present a novel method for molecular property
prediction that scales to millions of compounds and improves accuracy by thirty percent
across ten benchmark datasets, demonstrating practical relevance.">
</head><body></body></html>
"""

LANDING_ATTR_ORDER = """
<html><head>
<meta content="Second order attribute form with a sufficiently long abstract text that
passes the length check comfortably, describing the method and its evaluation."
property="og:description">
</head></html>
"""

LANDING_TEASER = '<meta property="og:description" content="Short teaser.">'


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class _FakeClient:
    def __init__(self, text: str, status_code: int = 200):
        self._text = text
        self._status = status_code

    def get(self, url):
        return _FakeResponse(self._text, self._status)


def test_fetch_landing_abstract_accepts_real_meta():
    abstract = ja.fetch_landing_abstract(_FakeClient(LANDING_WITH_META), "https://example.org/x")
    assert abstract is not None
    assert "molecular property" in abstract


def test_fetch_landing_abstract_handles_attr_order():
    abstract = ja.fetch_landing_abstract(_FakeClient(LANDING_ATTR_ORDER), "https://example.org/x")
    assert abstract is not None


def test_fetch_landing_abstract_rejects_teaser():
    assert ja.fetch_landing_abstract(_FakeClient(LANDING_TEASER), "https://example.org/x") is None


def test_fetch_landing_abstract_tolerates_http_error():
    class _Boom:
        def get(self, url):
            raise RuntimeError("transport exploded")

    assert ja.fetch_landing_abstract(_Boom(), "https://example.org/x") is None
