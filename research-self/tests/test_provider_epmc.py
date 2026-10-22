"""Tests for the Europe PMC journal collector (paper_ingest/provider_epmc.py).

The provider is checked offline, against a recorded Europe PMC payload, so
the tests need neither the network nor a key. What they pin is the contract:
the query the service is asked, the paging, and the normalisation onto the
paper-dict contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import provider_epmc  # noqa: E402
from paper_ingest.provider_epmc import EPMC_JOURNALS, EpmcProvider, epmc_query  # noqa: E402


class TestQuery:
    def test_the_query_pins_the_journal_and_the_year(self):
        q = epmc_query("Nature", 2024)
        assert 'JOURNAL:"Nature"' in q
        assert "FIRST_PDATE:[2024-01-01 TO 2024-12-31]" in q

    def test_the_query_keeps_only_journal_articles(self):
        # Reviews, editorials and letters are not research papers, and they
        # would otherwise inflate the year's count.
        q = epmc_query("Nature", 2024)
        assert "PUB_TYPE:" in q
        assert "journal article" in q


class TestRegistry:
    def test_every_journal_has_a_title_and_a_display_name(self):
        for key, (title, display) in EPMC_JOURNALS.items():
            assert title and display, key

    def test_the_registry_covers_the_openalex_journals(self):
        # A journal that OpenAlex has but Europe PMC does not would silently
        # fall back to the per-DOI lookup, so the overlap is checked.
        from paper_ingest.provider_journals import JOURNAL_SOURCES

        missing = set(JOURNAL_SOURCES) - set(EPMC_JOURNALS)
        # jmlr is harvested from jmlr.org, and tmlr from the TMLR site; the
        # other OpenAlex journals are all indexed by Europe PMC.
        assert missing <= {"jmlr", "tmlr"}, f"not indexed by Europe PMC: {missing}"

    def test_the_europepmc_only_journals_are_a_subset(self):
        assert set(provider_epmc.EPMC_ONLY_JOURNALS) <= set(EPMC_JOURNALS)


class TestNormalisation:
    def _record(self, **overrides) -> dict:
        record = {
            "id": "MED/39797631",
            "pmid": "39797631",
            "doi": "10.1136/bmj-2024-079575",
            "title": "Trajectories of breast density change over time",
            "abstractText": "Objective To identify clusters. METHODS We used data.",
            "authorList": {"author": [
                {"fullName": "B Park", "lastName": "Park", "firstName": "B"},
                {"fullName": "Y Chang", "lastName": "Chang", "firstName": "Y"},
            ]},
            "keywordList": {"keyword": ["Breast", "Density", "Breast"]},
            "firstPublicationDate": "2024-05-14",
            "journalTitle": "BMJ",
        }
        record.update(overrides)
        return record

    def test_a_record_becomes_a_paper_dict(self):
        paper = EpmcProvider("bmj").normalize(self._record(), 2024)
        assert paper is not None
        for key in ("id", "source", "source_paper_id", "title", "abstract", "authors",
                    "keywords", "doi", "publication_year", "status", "decision",
                    "created_at_source", "updated_at_source", "raw_data_path"):
            assert key in paper, f"{key} is missing"

    def test_the_id_is_derived_from_the_pmid(self):
        paper = EpmcProvider("bmj").normalize(self._record(), 2024)
        assert paper["id"] == "epmc:39797631"

    def test_the_doi_is_reduced_to_its_bare_form(self):
        paper = EpmcProvider("bmj").normalize(
            self._record(doi="https://doi.org/10.1136/bmj-2024-079575"), 2024)
        assert paper["doi"] == "10.1136/bmj-2024-079575"

    def test_the_abstract_is_cleaned(self):
        paper = EpmcProvider("bmj").normalize(
            self._record(abstractText="<h4>Objective</h4> To identify clusters."), 2024)
        assert "<h4>" not in paper["abstract"]
        assert "Objective" in paper["abstract"]

    def test_the_authors_come_from_the_structured_list(self):
        paper = EpmcProvider("bmj").normalize(self._record(), 2024)
        assert paper["authors"] == ["B Park", "Y Chang"]

    def test_the_author_string_is_the_fallback(self):
        record = self._record()
        record.pop("authorList")
        record["authorString"] = "Park B, Chang Y"
        paper = EpmcProvider("bmj").normalize(record, 2024)
        assert paper["authors"] == ["Park B", "Chang Y"]

    def test_the_keywords_are_deduplicated(self):
        paper = EpmcProvider("bmj").normalize(self._record(), 2024)
        assert paper["keywords"] == ["Breast", "Density"]

    def test_the_publication_year_comes_from_the_first_publication_date(self):
        paper = EpmcProvider("bmj").normalize(
            self._record(firstPublicationDate="2024-05-14"), 2024)
        assert paper["publication_year"] == 2024

    def test_a_record_without_a_title_is_dropped(self):
        # papers.title is NOT NULL and anchors the FTS index.
        assert EpmcProvider("bmj").normalize(self._record(title=""), 2024) is None

    def test_a_record_without_any_identifier_is_dropped(self):
        record = self._record()
        record["pmid"] = ""
        record["id"] = ""
        record["doi"] = ""
        assert EpmcProvider("bmj").normalize(record, 2024) is None


class TestPaging:
    def test_the_cursor_stops_when_it_stops_moving(self, monkeypatch):
        """A cursor that does not move would loop forever."""
        provider = EpmcProvider("bmj")
        page = {
            "resultList": {"result": [{
                "id": "MED/1", "pmid": "1", "doi": "10.1/x", "title": "A Paper",
                "abstractText": "An abstract long enough to be worth keeping here.",
            }]},
            "nextCursorMark": "*",
            "hitCount": 1,
        }
        calls = {"n": 0}

        def fake_get(params):
            calls["n"] += 1
            return page

        monkeypatch.setattr(provider, "_get", fake_get)
        papers = provider.list_year(2024)
        assert len(papers) == 1
        # The cursor did not move, so the loop must stop after one page.
        assert calls["n"] == 1

    def test_the_limit_is_respected(self, monkeypatch):
        provider = EpmcProvider("bmj")
        page = {
            "resultList": {"result": [
                {"id": f"MED/{i}", "pmid": str(i), "doi": f"10.1/{i}",
                 "title": f"Paper Number {i}",
                 "abstractText": "An abstract long enough to be worth keeping."}
                for i in range(5)
            ]},
            "nextCursorMark": "next",
        }
        monkeypatch.setattr(provider, "_get", lambda params: page)
        assert len(provider.list_year(2024, limit=3)) == 3


class TestAbstractFilter:
    """The abstract filter decides what kind of corpus a journal becomes."""

    def test_the_query_asks_for_the_records_with_an_abstract(self):
        provider = EpmcProvider("bmj")
        assert "HAS_ABSTRACT:Y" in provider._query(2024)

    def test_the_filter_can_be_turned_off(self):
        provider = EpmcProvider("bmj")
        assert "HAS_ABSTRACT" not in provider._query(2024, has_abstract_only=False)

    def test_the_filter_reaches_the_request(self, monkeypatch):
        provider = EpmcProvider("bmj")
        seen = {}

        def fake_get(params):
            seen.update(params)
            return {"resultList": {"result": []}}

        monkeypatch.setattr(provider, "_get", fake_get)
        provider.list_year(2024)
        assert "HAS_ABSTRACT:Y" in seen["query"]

    def test_the_filter_is_off_when_asked_for(self, monkeypatch):
        provider = EpmcProvider("bmj")
        seen = {}

        def fake_get(params):
            seen.update(params)
            return {"resultList": {"result": []}}

        monkeypatch.setattr(provider, "_get", fake_get)
        provider.list_year(2024, has_abstract_only=False)
        assert "HAS_ABSTRACT" not in seen["query"]
