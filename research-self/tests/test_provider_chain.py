"""Tests for provider-chain source ordering (offline)."""
from __future__ import annotations

import pytest

from paper_ingest import provider_chain
from paper_ingest.harvest import ProviderUnavailable


def test_icml_and_iclr_always_lead_with_arxiv():
    """OpenAlex has single-digit works/year for ICML/ICLR (0 for ICML 2023)."""
    for venue in ("icml", "iclr", "ICML", " ICLR "):
        for year in (2022, 2023, 2024, 2025, 2026):
            assert provider_chain.plan_sources(venue, year)[0] == "arxiv", (venue, year)


def test_recent_years_lead_with_arxiv_for_every_venue():
    """Live counts show OpenAlex lags on 2025+ while arXiv is already populated:
    NeurIPS 2026 = 2 OpenAlex vs 1359 arXiv; CVPR 2026 = 0 vs 1980. Without this
    rule, harvests of 2025/2026 return almost nothing."""
    for venue in ("neurips", "cvpr", "acl"):
        for year in (2025, 2026):
            assert provider_chain.plan_sources(venue, year)[0] == "arxiv", (venue, year)


def test_older_years_lead_with_openalex_for_strong_venues():
    for venue in ("neurips", "cvpr", "acl"):
        for year in (2022, 2023, 2024):
            assert provider_chain.plan_sources(venue, year)[0] == "openalex", (venue, year)


def test_crossref_is_always_last():
    for venue in ("icml", "neurips", "cvpr", "acl", "unknown"):
        for year in (2022, 2025, 2026):
            assert provider_chain.plan_sources(venue, year)[-1] == "crossref"


def test_unknown_venue_still_gets_a_plan():
    plan = provider_chain.plan_sources("someweirdvenue", 2024)
    assert plan == ["openalex", "arxiv", "crossref"]
    plan_recent = provider_chain.plan_sources("someweirdvenue", 2026)
    assert plan_recent == ["arxiv", "openalex", "crossref"]


def test_cutoff_is_configurable(monkeypatch):
    monkeypatch.setenv("PAPER_ARXIV_FIRST_YEAR", "2020")
    assert provider_chain.plan_sources("neurips", 2021)[0] == "arxiv"
    monkeypatch.setenv("PAPER_ARXIV_FIRST_YEAR", "2030")
    assert provider_chain.plan_sources("neurips", 2026)[0] == "openalex"


def test_build_providers_matches_plan(monkeypatch):
    chain = provider_chain.build_providers("icml", 2024)
    assert [p.name for p in chain] == ["arxiv", "openalex", "crossref"]
    chain_old = provider_chain.build_providers("neurips", 2023)
    assert [p.name for p in chain_old] == ["openalex", "arxiv", "crossref"]


def test_build_providers_can_exclude_crossref():
    chain = provider_chain.build_providers("icml", 2024, include_crossref=False)
    assert all(p.name != "crossref" for p in chain)


class StubProvider:
    def __init__(self, name, papers=None, exc=None):
        self.name = name
        self.papers = papers or []
        self.exc = exc
        self.calls = 0

    def harvest(self, venue, year, *, limit=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.papers


def _paper(pid, title, **kw):
    base = {
        "id": pid, "source": "test", "source_paper_id": pid, "title": title,
        "abstract": "x" * 80, "doi": None, "arxiv_id": None, "authors": [],
    }
    base.update(kw)
    return base


def test_harvest_venue_fails_over_from_blocked_provider(monkeypatch):
    blocked = StubProvider("arxiv", exc=ProviderUnavailable("403"))
    backup = StubProvider("openalex", [_paper("p1", "A Real Paper Title Goes Here")])
    monkeypatch.setattr(provider_chain, "build_providers",
                        lambda *a, **kw: [blocked, backup])
    result = provider_chain.harvest_venue("icml", 2024, target=10)
    assert len(result.papers) == 1
    statuses = {a.provider: a.status for a in result.attempts}
    assert statuses["arxiv"] == "unavailable"
    assert statuses["openalex"] == "ok"


def test_harvest_venue_respects_target(monkeypatch):
    prov = StubProvider("arxiv",
                        [_paper(f"p{i}", f"Distinct Paper Title Number {i}")
                         for i in range(50)])
    monkeypatch.setattr(provider_chain, "build_providers", lambda *a, **kw: [prov])
    result = provider_chain.harvest_venue("icml", 2026, target=12)
    assert len(result.papers) == 12


def test_harvest_venue_merges_duplicates(monkeypatch):
    a = StubProvider("arxiv", [_paper("a", "Shared Title Across Sources Here", doi="10.1/s")])
    b = StubProvider("openalex", [_paper("b", "Shared Title Across Sources Here", doi="10.1/s")])
    monkeypatch.setattr(provider_chain, "build_providers", lambda *a, **kw: [a, b])
    assert len(provider_chain.harvest_venue("icml", 2024).papers) == 1


def test_harvest_venue_empty_when_all_fail(monkeypatch):
    a = StubProvider("arxiv", exc=ProviderUnavailable("down"))
    b = StubProvider("openalex", exc=ProviderUnavailable("down"))
    monkeypatch.setattr(provider_chain, "build_providers", lambda *a, **kw: [a, b])
    result = provider_chain.harvest_venue("icml", 2026)
    assert result.papers == []
    assert result.summary()["status"] == "no_data"
