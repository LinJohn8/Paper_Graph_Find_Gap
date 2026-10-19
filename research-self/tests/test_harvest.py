"""Tests for the multi-source harvest orchestrator (offline, no network)."""
from __future__ import annotations

import pytest

from paper_ingest import harvest
from paper_ingest.harvest import (
    MultiSourceHarvester,
    ProviderUnavailable,
    merge_papers,
    normalize_title,
    paper_keys,
    provider_report,
)


def paper(pid, title, *, doi=None, arxiv_id=None, abstract="", pdf_url=None, authors=None):
    return {
        "id": pid, "source": "test", "source_paper_id": pid, "title": title,
        "abstract": abstract, "doi": doi, "arxiv_id": arxiv_id,
        "pdf_url": pdf_url, "authors": authors or [], "paper_url": None,
    }


class FakeProvider:
    def __init__(self, name, papers=None, exc=None, truncated=False):
        self.name = name
        self.papers = papers or []
        self.exc = exc
        self.calls = 0
        self.last_limit = None

    def harvest(self, venue, year, *, limit=None):
        self.calls += 1
        self.last_limit = limit
        if self.exc is not None:
            raise self.exc
        batch = self.papers
        if limit is not None:
            batch = batch[:limit]
        return batch


# --------------------------------------------------------------------------- #
# Title / key normalization                                                   #
# --------------------------------------------------------------------------- #
def test_normalize_title_ignores_punctuation_and_case():
    a = normalize_title("DeBayes: A Bayesian Method for Debiasing Embeddings")
    b = normalize_title("debayes - a bayesian method for debiasing embeddings.")
    assert a == b
    assert normalize_title(None) == ""


def test_paper_keys_prefers_doi_then_arxiv_then_title():
    keys = paper_keys(paper("p", "Some sufficiently long research title here",
                            doi="https://doi.org/10.1234/ABC", arxiv_id="2401.00001v2"))
    assert keys[0] == "doi:10.1234/abc"
    assert "arxiv:2401.00001" in keys  # version suffix stripped
    assert any(k.startswith("title:") for k in keys)


def test_short_titles_are_not_used_as_keys():
    # Very short titles collide too easily to be a safe dedup key.
    keys = paper_keys(paper("p", "GNN"))
    assert not any(k.startswith("title:") for k in keys)


# --------------------------------------------------------------------------- #
# Merging                                                                     #
# --------------------------------------------------------------------------- #
def test_merge_dedups_across_sources_by_doi():
    a = [paper("a1", "A Paper About Things", doi="10.1/x", abstract="short")]
    b = [paper("b1", "A Paper About Things", doi="https://doi.org/10.1/X",
               abstract="a much longer and richer abstract here")]
    merged = merge_papers([a, b])
    assert len(merged) == 1
    # The richer record wins.
    assert merged[0]["abstract"].startswith("a much longer")


def test_merge_dedups_by_normalized_title():
    a = [paper("a1", "DeBayes: A Bayesian Method", abstract="x" * 50)]
    b = [paper("b1", "DeBayes - a Bayesian Method.", abstract="y" * 10)]
    assert len(merge_papers([a, b])) == 1


def test_merge_keeps_distinct_papers_and_order():
    a = [paper("a1", "First Distinct Paper Title", doi="10.1/a")]
    b = [paper("b1", "Second Distinct Paper Title", doi="10.1/b")]
    merged = merge_papers([a, b])
    assert [m["id"] for m in merged] == ["a1", "b1"]


def test_merge_arxiv_version_dedup():
    a = [paper("a", "Same Paper Title Long Enough", arxiv_id="2401.00001v1")]
    b = [paper("b", "Same Paper Title Long Enough", arxiv_id="2401.00001v3")]
    assert len(merge_papers([a, b])) == 1


# --------------------------------------------------------------------------- #
# Failover                                                                    #
# --------------------------------------------------------------------------- #
def test_falls_back_when_first_provider_unavailable():
    bad = FakeProvider("blocked", exc=ProviderUnavailable("403 from source"))
    good = FakeProvider("openalex", [paper("p1", "A Real Paper Title Goes Here")])
    result = MultiSourceHarvester([bad, good]).harvest("icml", 2024)
    assert result.ok
    assert [p["id"] for p in result.papers] == ["p1"]
    statuses = {a.provider: a.status for a in result.attempts}
    assert statuses["blocked"] == "unavailable"
    assert statuses["openalex"] == "ok"


def test_unexpected_error_does_not_abort_chain():
    boom = FakeProvider("boom", exc=RuntimeError("kaboom"))
    good = FakeProvider("arxiv", [paper("p1", "A Real Paper Title Goes Here")])
    result = MultiSourceHarvester([boom, good]).harvest("iclr", 2024)
    assert result.ok
    assert any(a.provider == "boom" and a.status == "error" for a in result.attempts)


def test_all_providers_failing_returns_empty_not_crash():
    p1 = FakeProvider("a", exc=ProviderUnavailable("no coverage"))
    p2 = FakeProvider("b", exc=ProviderUnavailable("blocked"))
    result = MultiSourceHarvester([p1, p2]).harvest("icml", 2022)
    assert not result.ok
    assert result.papers == []
    assert result.summary()["status"] == "no_data"


def test_stops_early_once_target_reached():
    first = FakeProvider("openalex", [paper(f"p{i}", f"Paper Number {i} Long Title")
                                      for i in range(10)])
    second = FakeProvider("arxiv", [paper("extra", "Another Distinct Long Paper")])
    result = MultiSourceHarvester([first, second], target=5).harvest("neurips", 2024)
    assert len(result.papers) == 5
    assert second.calls == 0  # never needed
    assert all(a.provider == "openalex" for a in result.attempts)


def test_runs_all_providers_when_no_target():
    first = FakeProvider("openalex", [paper("p1", "First Distinct Paper Title")])
    second = FakeProvider("arxiv", [paper("p2", "Second Distinct Paper Title")])
    result = MultiSourceHarvester([first, second]).harvest("cvpr", 2024)
    assert len(result.papers) == 2
    assert first.calls == 1 and second.calls == 1


def test_limit_is_forwarded_and_applied():
    prov = FakeProvider("openalex", [paper(f"p{i}", f"Paper Number {i} Long Title")
                                     for i in range(20)])
    result = MultiSourceHarvester([prov]).harvest("acl", 2024, limit=7)
    assert prov.last_limit == 7
    assert len(result.papers) == 7


def test_duplicate_papers_across_providers_are_merged_but_providers_still_run():
    a = [paper("a", "Shared Paper Title For Dedup", doi="10.1/s", abstract="x" * 30)]
    b = [paper("b", "Shared Paper Title For Dedup", doi="10.1/s", abstract="x" * 30)]
    result = MultiSourceHarvester([FakeProvider("one", a), FakeProvider("two", b)]).harvest("icml", 2024)
    assert len(result.papers) == 1
    assert len(result.attempts) == 2


def test_requires_at_least_one_provider():
    with pytest.raises(ValueError):
        MultiSourceHarvester([])


def test_summary_reports_provider_diagnostics():
    good = FakeProvider("openalex", [paper("p1", "A Real Paper Title Goes Here",
                                           abstract="hello")])
    bad = FakeProvider("blocked", exc=ProviderUnavailable("403"))
    result = MultiSourceHarvester([bad, good]).harvest("icml", 2024)
    s = result.summary()
    assert s["papers"] == 1
    assert s["with_abstract"] == 1
    assert {p["provider"] for p in s["providers"]} == {"blocked", "openalex"}
    text = provider_report(result)
    assert "blocked: unavailable" in text
    assert "openalex: ok" in text
