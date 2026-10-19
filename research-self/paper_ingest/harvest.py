"""Multi-source paper harvesting with automatic failover.

Why this module exists
----------------------
The original harvester scraped a single source's HTML. When that source started
answering 403 (bot protection), the whole venue-year sync failed. Scraping HTML
is inherently brittle: the site can change markup, add a WAF, or block by IP at
any time.

The robust design is to treat "obtain papers for venue V in year Y" as a
*capability*, and to have several independent, official, machine-readable
providers that can satisfy it. We then:

1. Try providers in a configurable priority order.
2. Merge their results (deduplicating by DOI / arXiv id / normalized title).
3. Stop early once a target is met, or fall back to the next provider when one
   is rate-limited, blocked, or empty.
4. Record per-provider outcome so a failed source is visible/diagnosable instead
   of silently producing an empty corpus.

This is deliberately NOT about evading bot protection. Hiding from servers makes
things *worse*: official APIs (OpenAlex, Crossref, arXiv) publish higher rate
limits for clients that identify themselves with a contact address ("polite
pool"). We therefore always send an honest User-Agent with a mailto, honour
Retry-After, and back off exponentially.

Provider contract
-----------------
A provider is any object with::

    name: str
    def harvest(self, venue: str, year: int, *, limit: int | None) -> list[dict]

returning paper dicts shaped for ``db.upsert_paper``. Providers raise
``ProviderUnavailable`` for "cannot serve this venue/year" (no coverage, blocked,
rate-limited out) so the orchestrator can move on without treating it as a bug.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot serve a venue/year (not an internal bug)."""


class ProviderError(RuntimeError):
    """Raised when a provider failed unexpectedly while serving a request."""


# --------------------------------------------------------------------------- #
# Deduplication: the same paper usually exists in several sources              #
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str | None) -> str:
    """Reduce a title to a comparison key.

    Sources differ in punctuation, capitalisation, LaTeX, and trailing periods,
    so an exact string match misses most duplicates. Lowercase + strip
    non-alphanumerics catches "DeBayes: a Bayesian Method..." vs
    "DeBayes - A Bayesian Method".
    """
    if not title:
        return ""
    return _PUNCT_RE.sub("", title.lower())


def _normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d


def paper_keys(paper: dict[str, Any]) -> list[str]:
    """Return the identity keys for dedup, strongest first."""
    keys: list[str] = []
    doi = _normalize_doi(paper.get("doi"))
    if doi:
        keys.append(f"doi:{doi}")
    arxiv_id = (paper.get("arxiv_id") or "").strip().lower()
    if arxiv_id:
        # Strip any version suffix so v1/v2 collapse together.
        base_arxiv = re.sub(r"v\d+$", "", arxiv_id)
        keys.append("arxiv:" + base_arxiv)
    title_key = normalize_title(paper.get("title"))
    if len(title_key) >= 20:  # very short titles collide too easily to trust
        keys.append(f"title:{title_key}")
    return keys


def _is_richer(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """Prefer the record that carries more usable signal."""
    def score(p: dict[str, Any]) -> int:
        return (
            len(p.get("abstract") or "") * 4
            + (100 if p.get("doi") else 0)
            + (100 if p.get("pdf_url") else 0)
            + len(p.get("authors") or []) * 5
        )
    return score(new) > score(old)


def merge_papers(batches: Iterable[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge provider batches, de-duplicating and keeping the richest record.

    Order is preserved by first appearance so results stay deterministic.
    """
    merged: list[dict[str, Any]] = []
    by_key: dict[str, int] = {}
    for batch in batches:
        for paper in batch:
            keys = paper_keys(paper)
            hit = next((by_key[k] for k in keys if k in by_key), None)
            if hit is None:
                idx = len(merged)
                merged.append(dict(paper))
                for k in keys:
                    by_key.setdefault(k, idx)
            else:
                if _is_richer(paper, merged[hit]):
                    existing_keys = paper_keys(merged[hit])
                    merged[hit] = dict(paper)
                    # Re-point keys at the surviving record.
                    for k in existing_keys + keys:
                        by_key[k] = hit
    return merged


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Attempt:
    """Outcome of one provider for one (venue, year)."""

    provider: str
    status: str  # "ok" | "empty" | "unavailable" | "error"
    fetched: int = 0
    kept: int = 0
    message: str | None = None
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "fetched": self.fetched,
            "kept": self.kept,
            "message": self.message,
            "elapsed_s": round(self.elapsed_s, 2),
        }


@dataclass
class HarvestResult:
    venue: str
    year: int
    papers: list[dict[str, Any]]
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.papers)

    def summary(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "year": self.year,
            "papers": len(self.papers),
            "with_abstract": sum(1 for p in self.papers if p.get("abstract")),
            "status": "ok" if self.ok else "no_data",
            "providers": [a.as_dict() for a in self.attempts],
        }


class MultiSourceHarvester:
    """Run providers in priority order until the target is met.

    Parameters
    ----------
    providers:
        Ordered providers. Earlier entries are preferred (typically the highest
        coverage/quality source for the venue).
    target:
        Stop once this many *unique* papers are collected. ``None`` runs every
        provider (maximum recall).
    min_per_provider:
        A provider returning fewer than this is treated as low-coverage (status
        "empty" is only reported for zero) but does not stop the chain.
    sleep:
        Injectable sleep for tests.
    """

    def __init__(
        self,
        providers: Sequence[Any],
        *,
        target: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = list(providers)
        self.target = target
        self._sleep = sleep

    def harvest(self, venue: str, year: int, *, limit: int | None = None) -> HarvestResult:
        """Collect papers for ``venue``/``year`` from the first working providers."""
        batches: list[list[dict[str, Any]]] = []
        attempts: list[Attempt] = []
        reached = False

        for provider in self.providers:
            name = getattr(provider, "name", type(provider).__name__)
            started = time.monotonic()
            try:
                batch = provider.harvest(venue, year, limit=limit)
            except ProviderUnavailable as exc:
                attempts.append(Attempt(name, "unavailable", message=str(exc),
                                        elapsed_s=time.monotonic() - started))
                continue
            except Exception as exc:  # noqa: BLE001 - failover must not die here
                attempts.append(Attempt(name, "error",
                                        message=f"{type(exc).__name__}: {exc}",
                                        elapsed_s=time.monotonic() - started))
                continue

            batch = list(batch or [])
            batches.append(batch)
            merged_now = merge_papers(batches)
            attempts.append(Attempt(
                name,
                "ok" if batch else "empty",
                fetched=len(batch),
                kept=len(merged_now),
                elapsed_s=time.monotonic() - started,
            ))

            if self.target is not None and len(merged_now) >= self.target:
                reached = True
                break

        merged = merge_papers(batches)
        if self.target is not None:
            # `target` is both the early-stop condition and the cap: a provider
            # may return far more than we asked for, and the caller requested a
            # bounded corpus.
            merged = merged[: self.target]
        if limit is not None:
            merged = merged[:limit]
        return HarvestResult(venue=venue, year=year, papers=merged, attempts=attempts)


def provider_report(result: HarvestResult) -> str:
    """Human-readable one-liner per provider, for sync job logs."""
    lines = []
    for a in result.attempts:
        lines.append(
            f"{a.provider}: {a.status} fetched={a.fetched} kept={a.kept} "
            f"({a.elapsed_s:.1f}s){' - ' + a.message if a.message else ''}"
        )
    return "; ".join(lines) if lines else "no providers attempted"
