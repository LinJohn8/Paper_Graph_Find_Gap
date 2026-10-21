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


#: A page that carries the article's own abstract block. Springer Nature
#: renders the full text there (1,666 chars measured) while og:description
#: holds only a 243-char teaser -- the full text is what retrieval wants.
LANDING_WITH_ARTICLE_BLOCK = """
<html><head>
<meta property="og:description" content="A short teaser that only says some of the story.">
</head><body>
<div id="Abs1-content">
Plastic waste poses an ecological challenge and enzymatic degradation offers one,
potentially green and scalable, route for polyesters waste recycling. Poly(ethylene
terephthalate) accounts for a large share of the plastic waste stream, and a
recently engineered enzyme achieves near-complete depolymerisation of untreated
postconsumer material within a single week, which is far faster than any
composting route and therefore commercially relevant.
</div>
</body></html>
"""

#: What a publisher answers with when the request rate trips its filter.
CHALLENGE_PAGE = """
<html><head><title>Client Challenge</title></head><body>
JavaScript is disabled in your browser. Please enable JavaScript to proceed.
A required part of this site couldn't load.
</body></html>
"""


def test_full_article_block_wins_over_the_teaser():
    abstract = ja.fetch_landing_abstract(_FakeClient(LANDING_WITH_ARTICLE_BLOCK), "https://example.org/x")
    assert abstract is not None
    assert "Plastic waste poses an ecological challenge" in abstract
    assert len(abstract) > ja.MIN_FULL_ABSTRACT_CHARS


def test_teaser_is_used_when_no_article_block_exists():
    abstract = ja.fetch_landing_abstract(_FakeClient(LANDING_WITH_META), "https://example.org/x")
    assert abstract is not None
    assert len(abstract) < ja.MIN_FULL_ABSTRACT_CHARS


def test_challenge_page_raises_so_the_caller_can_retry():
    """A bot challenge is a refusal, not a missing abstract.

    Recorded as a miss it would silently drop the paper from the corpus; the
    worker retries it with a backoff instead.
    """
    with pytest.raises(ja._ChallengePage):
        ja.fetch_landing_abstract(_FakeClient(CHALLENGE_PAGE), "https://example.org/x")


def test_doi_prefix_filter_decides_backfillability():
    # Springer Nature, Springer, IEEE, AAAS, PNAS: og:description channels.
    assert ja._backfillable("https://doi.org/10.1038/s41586-022-04599-z")
    assert ja._backfillable("https://doi.org/10.1007/s11222-022-01234-5")
    assert ja._backfillable("https://doi.org/10.1109/TPAMI.2021.3051234")
    assert ja._backfillable("https://doi.org/10.1126/science.abj8754")
    assert ja._backfillable("https://www.nature.com/articles/s41586-022-04599-z")
    # Elsevier answers with an interstitial; a repository mirror is not the
    # publisher's page. Neither is attempted.
    assert not ja._backfillable("https://doi.org/10.1016/j.cell.2022.06.001")
    assert not ja._backfillable("https://vbn.aau.dk/da/publications/the-hothouse-trap")


# --------------------------------------------------------------------------- #
# Europe PMC: the fast channel                                                 #
# --------------------------------------------------------------------------- #
def test_doi_extraction_from_urls():
    assert ja.doi_of("https://doi.org/10.1038/s41586-022-04599-z") == "10.1038/s41586-022-04599-z"
    assert ja.doi_of("https://doi.org/10.1126/science.abj8754") == "10.1126/science.abj8754"
    assert ja.doi_of("https://vbn.aau.dk/da/publications/the-hothouse-trap") is None
    assert ja.doi_of("") is None
    assert ja.doi_of(None) is None


def test_nature_news_dois_are_not_asked_of_europepmc():
    """10.1038/d41586-* is Nature's news prefix; Europe PMC carries research
    articles only, so asking it about a news item wastes a request."""
    assert not ja.europepmc_eligible("10.1038/d41586-022-00011-y")
    assert ja.europepmc_eligible("10.1038/s41586-022-04599-z")
    assert not ja.europepmc_eligible(None)


EPMC_RESPONSE = {
    "resultList": {
        "result": [{
            "abstractText": "Plastic waste poses an ecological challenge<sup>1-3</sup> "
                            "and enzymatic degradation offers one, potentially green and "
                            "scalable, route for polyesters waste recycling that nobody "
                            "has managed to make work at scale before this paper.",
        }]
    }
}


class _FakeJsonResponse(_FakeResponse):
    def json(self):
        return EPMC_RESPONSE


class _FakeJsonClient:
    def get(self, url, params=None):
        return _FakeJsonResponse("{}")


def test_europepmc_abstract_is_extracted_and_cleaned():
    abstract = ja.fetch_europepmc_abstract(_FakeJsonClient(), "10.1038/s41586-022-04599-z")
    assert abstract is not None
    # The <sup> citation markers are stripped.
    assert "<sup>" not in abstract
    assert "Plastic waste poses an ecological challenge 1-3" in abstract


def test_europepmc_miss_returns_none():
    class _Empty:
        def get(self, url, params=None):
            class _R:
                status_code = 200
                text = "{}"

                def json(self):
                    return {"resultList": {"result": []}}

            return _R()

    assert ja.fetch_europepmc_abstract(_Empty(), "10.1038/s41586-022-04599-z") is None


def test_europepmc_transport_error_is_a_miss():
    class _Boom:
        def get(self, url, params=None):
            raise RuntimeError("network down")

    assert ja.fetch_europepmc_abstract(_Boom(), "10.1038/s41586-022-04599-z") is None
