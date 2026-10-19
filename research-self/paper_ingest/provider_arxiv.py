"""Official arXiv API provider (Atom) for venue-scoped paper metadata.

Why this module exists
----------------------
The OpenAlex-backed harvester has a hard coverage gap: ICML / ICLR proceedings are
barely represented there, so those venues come back nearly empty. arXiv is the
legitimate, official source that fills the gap, and it exposes a public Atom API
(`http://export.arxiv.org/api/query`) with no authentication.

Why the `co:` field
-------------------
arXiv has **no venue field**, so there is no `venue:ICML` filter. The one field
that reliably carries the venue is the *comment* field (`co:`), because authors
write "ICML 2024", "In ICLR 2024", "Accepted at NeurIPS 2024" there. Verified
live: `search_query=co:"ICML 2024"` returns 1167 results whose comments really do
name ICML 2024, complete with full abstracts.

Why local verification is mandatory
-----------------------------------
Searching a comment index is a *recall* tool, not a *precision* tool: it is a
substring/token match over free text. Live sampling of `co:"ICML 2024"` hits
surfaced genuine false positives, e.g.::

    "AI4Math workshop, ICML 2024"
    "accepted in ML4LMS workshop at ICML 2024"
    "video appeared in ICML 2024 tutorial"

Those are ICML-adjacent, not accepted main-conference papers, so ingesting them
would pollute the venue graph. Every candidate is therefore re-checked locally by
:func:`matches_venue` against the parsed comment/journal_ref before ingestion.

Politeness
----------
arXiv asks clients to space requests ~3 seconds apart, so the delay is configurable
(default 3.0s) and applied between *every* HTTP attempt (including retries, which
additionally back off exponentially and honour `Retry-After`). The User-Agent is
honest and carries contact information, per arXiv's API terms.

Only the official arXiv Atom API is used — metadata only, no PDFs are downloaded,
and no third-party mirrors or unofficial full-text hosting are contacted.
"""
from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import httpx

__all__ = [
    "API_URL",
    "DEFAULT_DELAY",
    "DEFAULT_USER_AGENT",
    "DEFAULT_YEARS",
    "MAX_PAGE_SIZE",
    "VENUE_ALIASES",
    "VENUE_COMMENT_PATTERNS",
    "ArxivProvider",
    "clean_text",
    "clean_title",
    "comment_queries",
    "extract_arxiv_id",
    "matches_venue",
    "parse_total_results",
]

API_URL = "http://export.arxiv.org/api/query"
DEFAULT_DELAY = 3.0
DEFAULT_USER_AGENT = (
    "PaperGraph/0.2 (academic metadata harvester; "
    "mailto:research@example.org)"
)
#: arXiv's documented hard ceiling for `max_results`; we stay well below it.
MAX_PAGE_SIZE = 200
#: Venue years the harvester cares about.
DEFAULT_YEARS: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)

#: venue key -> names authors actually write in the comment field. `neurips` also
#: appears as `nips` (and "NIPS 2023"/"NeurIPS 2023" are different tokens), so the
#: alias list is used both for query generation and for local verification.
VENUE_ALIASES: dict[str, tuple[str, ...]] = {
    "icml": ("ICML",),
    "iclr": ("ICLR",),
    "neurips": ("NeurIPS", "NIPS"),
    "cvpr": ("CVPR",),
    "acl": ("ACL",),
    "tmlr": ("TMLR",),
}

#: Venues that publish on a *rolling* basis rather than in yearly proceedings.
#:
#: TMLR (Transactions on Machine Learning Research) has no "TMLR 2024" cohort:
#: authors write "TMLR" or "accepted at TMLR" in their arXiv comment and never a
#: year. Measured live: ``co:"TMLR"`` returns ~1315 entries while every
#: ``co:"TMLR <year>"`` form returns nothing usable, so requiring a year (the
#: right rule for conferences) silently discarded 100% of TMLR papers.
#:
#: For these venues the year constraint is applied to the *paper's* dates
#: instead: a TMLR paper belongs to the year it was published on arXiv.
ROLLING_JOURNAL_VENUES: frozenset[str] = frozenset({"tmlr"})

#: Comment phrasings worth querying. The bare "{Venue} {Year}" form is empirically
#: a superset of the prefixed forms (arXiv's comment index tokenizes the phrase,
#: so `co:"In ICML 2024"` and `co:"ICML-2024"` both return the same 1167 hits as
#: `co:"ICML 2024"`), but the variants are kept because they cost one request each,
#: are id-deduplicated for free, and protect recall if the tokenizer changes.
_COMMENT_TEMPLATES: tuple[str, ...] = (
    "{name} {year}",
    "In {name} {year}",
    "accepted at {name} {year}",
    "accepted to {name} {year}",
    "published at {name} {year}",
    "{name} {year} camera ready",
)

#: Phrasings that tokenize *differently* from "{Venue} {Year}" and therefore add
#: real recall (measured: `co:"ICML'24"` -> 46 hits, `co:"ICML2024"` -> 85 hits,
#: while `co:"ICML-2024"` collapsed to the same 1167 as the bare form and is
#: intentionally not queried).
_DISTINCT_TOKEN_TEMPLATES: tuple[str, ...] = (
    "{name}'{short_year}",
    "{name}{year}",
)

#: Comments that mention the venue but are not accepted *main-conference* papers.
#: Rejecting these is the core of the precision fix; `spotlight`/`oral` are
#: deliberately absent because those ARE main-conference acceptance markers.
_NON_MAIN_MARKERS = re.compile(
    r"\b(?:workshops?|tutorials?|extended\s+abstract|doctoral\s+consortium"
    r"|demo\s+track|under\s+review|under\s+submission|rejected|withdrawn)\b"
    # "submitted to/for <venue>" is a pre-decision state. Deliberately narrower than
    # a bare "submitted", because live comments include "submitted and accepted at
    # ICML 2024", which is a genuine acceptance.
    r"|\bsubmitted\s+(?:to|for)\b",
    re.IGNORECASE,
)

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


def clean_text(value: str | None) -> str:
    """Collapse every run of whitespace to one space and strip the ends.

    The Atom feed hard-wraps titles and abstracts at ~80 columns, so a raw
    `<summary>` contains newlines in the middle of sentences. Storing it verbatim
    would break search snippets and full-text matching, hence this normalization.
    """
    return " ".join((value or "").split())


def clean_title(value: str | None) -> str:
    """Normalize a title, dropping the trailing sentence period authors often add.

    The period is not part of the title ("... via Generative Simulation." vs the
    published "... via Generative Simulation"); keeping it would make title-based
    matching against proceedings metadata fail unnecessarily.
    """
    title = clean_text(value)
    if title.endswith("."):
        title = title[:-1].rstrip()
    return title


def extract_arxiv_id(entry_id_url: str | None) -> str:
    """Return the bare, version-stripped arXiv id from an Atom `<id>` URL.

    `<id>` looks like ``http://arxiv.org/abs/2311.01455v3``. The version suffix is
    dropped so that v1..vN of the same paper collapse to one identity, which keeps
    `id = f"arxiv:{arxiv_id}"` stable across re-harvests. Legacy ids such as
    ``cs/0112017v1`` keep their slash: the id is not simply the last path segment.
    """
    raw = (entry_id_url or "").strip()
    if not raw:
        return ""
    match = re.search(r"arxiv\.org/abs/([^?#\s]+)", raw) or re.search(
        r"arXiv:\s*([^\s?#]+)", raw, re.IGNORECASE
    )
    candidate = match.group(1) if match else raw.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", candidate.strip().rstrip("/"))


def _query_names(venue: str) -> tuple[str, ...]:
    """Venue spellings to query; unknown keys fall back to their upper-case name."""
    key = clean_text(venue).casefold()
    return VENUE_ALIASES.get(key) or (clean_text(venue).upper(),)


def _in_year(entry: dict[str, Any], year: int) -> bool:
    """True when the entry's own dates place it in ``year``.

    Used for rolling journals (:data:`ROLLING_JOURNAL_VENUES`), whose arXiv
    comments never carry a year. The published date wins (that is when the
    journal version appeared); when absent we fall back to the v1 submission
    year, since a paper submitted in December and published in January belongs
    to the later year in a per-year corpus.

    A missing/unparsable date is treated as a match so that a genuinely new
    record is not silently dropped just because arXiv omitted a field.
    """
    for field in ("published", "updated", "submitted"):
        raw = entry.get(field)
        if not raw:
            continue
        text = str(raw)
        match = re.search(r"(\d{4})", text)
        if not match:
            continue
        try:
            return int(match.group(1)) == int(year)
        except (TypeError, ValueError):
            continue
    return True


def comment_queries(venue: str, year: int) -> list[str]:
    """Build the `co:"..."` comment-field queries for one venue-year.

    Generated from templates rather than written out as a 5-years x N-phrases
    literal, so adding a year or a venue is a one-line change and typos cannot
    drift between years. Each template is wrapped in the required phrase quotes;
    duplicates (e.g. an alias equal to the primary name) are removed in order.

    Rolling journals (:data:`ROLLING_JOURNAL_VENUES`) are queried WITHOUT a year:
    TMLR papers are tagged "TMLR" only, so a year-scoped query returns nothing.
    The year is applied afterwards from the paper's own dates.
    """
    year = int(year)
    short_year = f"{year % 100:02d}"
    queries: list[str] = []
    seen: set[str] = set()
    rolling = clean_text(venue).casefold() in ROLLING_JOURNAL_VENUES
    for name in _query_names(venue):
        if rolling:
            # Year-less forms: the journal name alone, plus how authors phrase
            # acceptance for a rolling journal.
            texts = [name, f"accepted at {name}", f"In {name}", f"published at {name}"]
        else:
            texts = [t.format(name=name, year=year, short_year=short_year)
                     for t in _COMMENT_TEMPLATES]
            texts += [t.format(name=name, year=year, short_year=short_year)
                      for t in _DISTINCT_TOKEN_TEMPLATES]
        for text in texts:
            query = f'co:"{text}"'
            if query not in seen:
                seen.add(query)
                queries.append(query)
    return queries


def parse_total_results(xml_text: str) -> int | None:
    """Read `<opensearch:totalResults>` so pagination can stop at the real end.

    Used alongside "short page" detection: the total is authoritative and lets us
    avoid one extra request at the tail of every harvest.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        match = re.search(r"<(?:\w+:)?totalResults>(\d+)<", xml_text)
        return int(match.group(1)) if match else None
    node = root.find(f"{_OPENSEARCH}totalResults")
    if node is not None and (node.text or "").strip().isdigit():
        return int((node.text or "").strip())
    return None


def _year_patterns(year: int) -> tuple[re.Pattern[str], ...]:
    """Regexes for the year spellings seen in real comments: 2024, '24, ’24.

    Digit guards prevent "2024" from matching inside a longer number and prevent
    "'24" from matching a longer quoted string.
    """
    year = int(year)
    short = f"{year % 100:02d}"
    return (
        re.compile(rf"(?<!\d){year}(?!\d)"),
        # No lookbehind on the apostrophe: in "ICML'24" the preceding char is a
        # letter, which a `\w` guard would wrongly reject.
        re.compile(rf"(?<!\d)['\u2019]{short}(?!\d)"),
    )


def matches_venue(
    comment: str | None,
    journal_ref: str | None,
    venue: str,
    year: int,
    *,
    accepted_only: bool = True,
) -> bool:
    """Return True only if this entry really is a `<venue> <year>` accepted paper.

    This is the precision gate applied to *every* candidate that the comment-field
    search returns, because `co:` is a free-text substring index and therefore
    matches workshop mentions, tutorials and unrelated year references.

    Handles, per live evidence: "ICML 2024", "In ICLR 2024", "Accepted at ICML 2024",
    "to appear at ICML 2024", "ICML'24", "ICML2024", "ICML-2024", any case, and a
    venue stated only in `journal_ref`. Rejects "ICML 2023" when asked for 2024,
    and (with ``accepted_only``) "ICML 2024 workshop"/tutorial/under-review notes.

    ``accepted_only=False`` disables only the main-track filter, never the
    venue-or-year requirement.
    """
    haystack = clean_text(f"{comment or ''} {journal_ref or ''}")
    if not haystack:
        return False
    lowered = haystack.casefold()

    # Venue token must stand on its own: "icml" matches "icml2024" and "ICML-2024"
    # but not an embedded fragment such as "snips" for the NIPS alias.
    venue_hit = any(
        re.search(rf"(?<![a-z]){re.escape(alias.casefold())}(?![a-z])", lowered)
        for alias in _query_names(venue)
        if alias
    )
    if not venue_hit:
        return False

    # Rolling journals have no "Venue <year>" cohort in the comment field: TMLR
    # authors write "TMLR" alone. Requiring a year here discarded every TMLR
    # paper (measured: `co:"TMLR"` has ~1315 hits, all year-less). The year for
    # these venues is enforced later from the paper's own publication date.
    rolling = clean_text(venue).casefold() in ROLLING_JOURNAL_VENUES
    if not rolling:
        year_hit = any(pattern.search(haystack) for pattern in _year_patterns(year))
        if not year_hit:
            return False

    if accepted_only and _NON_MAIN_MARKERS.search(haystack):
        return False
    return True


class ArxivProvider:
    """Polite, paginated client for the official arXiv Atom API.

    Only metadata is fetched (no PDFs). One instance may be reused across venues
    and years; the inter-request delay is tracked per client so switching venues
    does not bypass arXiv's politeness requirement.
    """

    #: Identifier used by ``harvest.MultiSourceHarvester`` for reporting/failover.
    name = "arxiv"

    def __init__(
        self,
        delay: float = DEFAULT_DELAY,
        per_page: int = MAX_PAGE_SIZE,
        *,
        timeout: float = 90.0,
        max_retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.Client | None = None,
        raw_dir: Path | str | None = None,
    ) -> None:
        """Configure pacing, page size and optional raw-feed caching.

        Args:
            delay: seconds to wait between HTTP requests (arXiv asks for ~3s).
            per_page: page size; clamped to ``MAX_PAGE_SIZE`` because larger
                `max_results` values are both slow and unreliable server-side.
            timeout: per-request timeout in seconds.
            max_retries: attempts for 429/5xx/network failures per page.
            user_agent: honest UA string with contact information (arXiv policy).
            client: injectable ``httpx.Client`` (tests pass a fake; the caller keeps
                ownership and must not rely on this module closing it).
            raw_dir: when set, each fetched Atom feed is cached there and its path is
                recorded in the paper's ``raw_data_path`` (project-wide convention).
        """
        self.delay = max(0.0, float(delay))
        self.per_page = max(1, min(int(per_page), MAX_PAGE_SIZE))
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.user_agent = user_agent
        self.raw_dir = Path(raw_dir) if raw_dir is not None else None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self.timeout, follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )
        self._last_request_at: float | None = None
        #: Observability for the last `search_venue` call (candidates checked/rejected).
        self.last_stats: dict[str, int] = {}
        #: HTTP status of the most recent request. ``429`` means arXiv is throttling
        #: this client (often because several processes share one public IP), which
        #: is a different situation from "no papers match the venue".
        self.last_status_code: int | None = None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """Close the HTTP client we created; injected clients are left to their owner."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ArxivProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- HTTP --------------------------------------------------------------
    def _polite_wait(self) -> None:
        """Sleep out the remainder of the politeness delay since the last attempt."""
        if self.delay <= 0 or self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _request(self, params: dict[str, Any]) -> httpx.Response:
        """GET one feed page, retrying 429/5xx and network errors with backoff.

        A retry is itself politeness-delayed (never a tight loop), and `Retry-After`
        from the server takes precedence when it asks for a longer pause.

        A persistent 429 is recorded in :attr:`last_status_code` so callers can tell
        "arXiv has no papers for this venue" apart from "we are being throttled".
        That distinction matters: during a shared-IP rate limit every query returns
        429 and the harvester would otherwise report an empty (but successful)
        corpus, hiding the real problem.
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._polite_wait()
            try:
                response = self._client.get(
                    API_URL, params=params,
                    headers={"User-Agent": self.user_agent}, timeout=self.timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                self._last_request_at = time.monotonic()
                self._sleep_backoff(attempt, None)
                continue
            finally:
                self._last_request_at = time.monotonic()

            self.last_status_code = response.status_code
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    self._sleep_backoff(attempt, retry_after)
                    continue
            response.raise_for_status()
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("arXiv request failed after retries")

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        """Exponential backoff, honouring `Retry-After` when the server sends one."""
        pause = max(self.delay, 1.0) * (2 ** attempt)
        if retry_after:
            try:
                pause = max(pause, float(retry_after))
            except (TypeError, ValueError):
                pass
        time.sleep(min(pause, 120.0))

    def _cache_feed(self, xml_text: str, params: dict[str, Any]) -> str | None:
        """Persist a raw feed when caching is enabled, returning its path.

        Kept opt-in so importing or unit-testing the provider never writes to disk;
        the project convention is that `raw_data_path` points at the exact bytes a
        record came from, which is what makes later re-parsing auditable.
        """
        if self.raw_dir is None:
            return None
        try:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(
                repr(sorted(params.items())).encode("utf-8")
            ).hexdigest()[:16]
            path = self.raw_dir / f"{digest}.xml"
            path.write_text(xml_text, encoding="utf-8")
            return str(path)
        except OSError:
            return None

    # -- parsing -----------------------------------------------------------
    def parse_feed(self, xml_text: str) -> list[dict[str, Any]]:
        """Parse an Atom feed into normalized entry dicts, skipping bad entries.

        Uses ``xml.etree.ElementTree`` with explicit Atom/arXiv namespaces (no bs4):
        the feed is machine-generated XML, and ElementTree correctly resolves the
        ``arxiv:comment`` / ``arxiv:journal_ref`` / ``arxiv:doi`` elements that a
        regex or HTML parser would mangle.

        A single malformed `<entry>` (missing id/title, bad date) is skipped so one
        bad record cannot abort a whole venue harvest. A completely unparseable
        document raises ``ValueError`` for the caller to treat as a failed page.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"arXiv Atom feed is not parseable XML: {exc}") from exc

        entries: list[dict[str, Any]] = []
        for node in root.findall(f"{_ATOM}entry"):
            entry = self._parse_entry(node)
            if entry is not None:
                entries.append(entry)
        return entries

    def _parse_entry(self, node: ET.Element) -> dict[str, Any] | None:
        """Map one `<entry>` to a dict, or None when it lacks a usable identity/title."""
        try:
            arxiv_id = extract_arxiv_id(self._text(node.find(f"{_ATOM}id")))
            title = clean_title(self._text(node.find(f"{_ATOM}title")))
            if not arxiv_id or not title:
                return None

            summary = clean_text(self._text(node.find(f"{_ATOM}summary")))
            published = clean_text(self._text(node.find(f"{_ATOM}published")))
            updated = clean_text(self._text(node.find(f"{_ATOM}updated")))

            authors = [
                clean_text(self._text(author.find(f"{_ATOM}name")))
                for author in node.findall(f"{_ATOM}author")
            ]
            categories = [
                (category.get("term") or "").strip()
                for category in node.findall(f"{_ATOM}category")
            ]
            primary_node = node.find(f"{_ARXIV}primary_category")
            primary = (primary_node.get("term") or "").strip() if primary_node is not None else ""

            pdf_url = ""
            paper_url = ""
            for link in node.findall(f"{_ATOM}link"):
                href = (link.get("href") or "").strip()
                title_attr = (link.get("title") or "").strip().casefold()
                rel = (link.get("rel") or "").strip().casefold()
                if not href:
                    continue
                if title_attr == "pdf" and not pdf_url:
                    pdf_url = href
                elif rel == "alternate" and not paper_url:
                    paper_url = href
            if not paper_url and arxiv_id:
                paper_url = f"http://arxiv.org/abs/{arxiv_id}"

            return {
                "arxiv_id": arxiv_id,
                "entry_id": clean_text(self._text(node.find(f"{_ATOM}id"))),
                "title": title,
                "abstract": summary,
                "authors": [name for name in authors if name],
                "categories": [term for term in categories if term],
                "primary_category": primary or (categories[0] if categories else ""),
                "comment": clean_text(self._text(node.find(f"{_ARXIV}comment"))),
                "journal_ref": clean_text(self._text(node.find(f"{_ARXIV}journal_ref"))),
                "doi": clean_text(self._text(node.find(f"{_ARXIV}doi"))),
                "pdf_url": pdf_url,
                "paper_url": paper_url,
                "published": published,
                "updated": updated,
                "raw_data_path": None,
            }
        except (AttributeError, TypeError, ValueError):
            # Defensive: a structurally odd entry must not abort the page.
            return None

    @staticmethod
    def _text(node: ET.Element | None) -> str:
        """Concatenate an element's text, tolerating missing nodes (Atom allows empty ones)."""
        return "".join(node.itertext()) if node is not None else ""

    # -- search ------------------------------------------------------------
    def search(
        self, query: str, *, max_results: int | None = None
    ) -> list[dict[str, Any]]:
        """Run one arXiv query with `start`-based pagination.

        arXiv returns at most ``MAX_PAGE_SIZE`` useful rows per request, so full
        harvesting requires walking `start` upward. Pagination stops on: the
        collected count reaching ``max_results``, an empty page, a short page, or
        `<opensearch:totalResults>` being reached.

        A failed page never discards work already collected: the error is swallowed
        and whatever arrived is returned (fail-soft), because a partial venue is far
        more useful to the harvester than an exception. Results keep feed order
        (relevance) and are de-duplicated by arXiv id, so repeated ids across pages
        cannot double-count.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        start = 0
        total: int | None = None
        limit = None if max_results is None else max(0, int(max_results))
        if limit == 0:
            return collected

        while True:
            remaining = None if limit is None else limit - len(collected)
            page_size = self.per_page if remaining is None else min(self.per_page, remaining)
            if page_size <= 0:
                break
            params = {
                "search_query": query,
                "start": start,
                "max_results": page_size,
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
            try:
                response = self._request(params)
                entries = self.parse_feed(response.text)
            except Exception:
                # Fail soft: an unparseable/failed page must not abandon the pages
                # already collected, nor the caller's whole venue harvest.
                break

            total = total if total is not None else parse_total_results(response.text)
            raw_path = self._cache_feed(response.text, params)
            if raw_path:
                for entry in entries:
                    entry["raw_data_path"] = raw_path

            if not entries:
                break
            for entry in entries:
                arxiv_id = entry["arxiv_id"]
                if arxiv_id in seen:
                    continue
                seen.add(arxiv_id)
                collected.append(entry)
                if limit is not None and len(collected) >= limit:
                    return collected
            start += len(entries)
            if total is not None and start >= total:
                break
            if len(entries) < page_size:
                break
        return collected

    def harvest(
        self, venue: str, year: int, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Adapter for ``harvest.MultiSourceHarvester``'s duck-typed protocol.

        The orchestrator calls ``harvest(venue, year, *, limit)`` and expects
        either DB-shaped paper dicts or ``ProviderUnavailable`` when this source
        simply cannot serve the request (an unknown venue, or a year we have no
        comment patterns for). Anything else that goes wrong is raised as a real
        error so the orchestrator records it as an "error" attempt rather than
        silently treating the source as having no coverage.

        ``search_venue`` is already fail-soft per query, so an empty list here
        means "arXiv genuinely has nothing tagged for this venue/year".
        """
        from .harvest import ProviderUnavailable

        if venue not in VENUE_COMMENT_PATTERNS:
            raise ProviderUnavailable(f"arxiv has no venue comment patterns for {venue!r}")
        if not comment_queries(venue, year):
            raise ProviderUnavailable(f"arxiv has no comment queries for {venue} {year}")
        papers = self.search_venue(venue, year, max_results=limit)
        if not papers:
            # Distinguish throttling from genuine absence: during a shared-IP rate
            # limit every query returns 429, and reporting "no papers tagged for
            # this venue" would be actively misleading.
            if self.last_status_code == 429:
                raise ProviderUnavailable(
                    f"arxiv is rate-limiting this client (HTTP 429) - retry later; "
                    f"{venue} {year} was not harvested"
                )
            raise ProviderUnavailable(
                f"arxiv returned no venue-tagged papers for {venue} {year}"
            )
        return papers

    def search_venue(
        self, venue: str, year: int, *, max_results: int | None = None
    ) -> list[dict[str, Any]]:
        """Harvest accepted main-conference papers for ``venue``/``year`` from arXiv.

        Runs every :func:`comment_queries` query, de-duplicates candidates by arXiv
        id, and applies :func:`matches_venue` to each candidate before normalizing.
        Returns DB-shaped paper dicts (ready for ``db.upsert_paper``) sorted by
        arXiv id, so the output order is deterministic regardless of which query
        surfaced a paper first or how the API ordered it.

        ``max_results`` caps the number of *papers* returned. Precision is not cap-
        relaxed: rejected comments are counted in ``last_stats`` for auditing.
        """
        limit = None if max_results is None else max(0, int(max_results))
        papers: dict[str, dict[str, Any]] = {}
        checked = rejected = 0
        rolling = clean_text(venue).casefold() in ROLLING_JOURNAL_VENUES
        for query in comment_queries(venue, year):
            if limit is not None and len(papers) >= limit:
                break
            remaining = None if limit is None else limit - len(papers)
            try:
                entries = self.search(query, max_results=remaining)
            except Exception:
                # `search` is already fail-soft; this guards future changes.
                continue
            for entry in entries:
                arxiv_id = entry["arxiv_id"]
                if arxiv_id in papers:
                    continue
                checked += 1
                if not matches_venue(
                    entry.get("comment"), entry.get("journal_ref"), venue, year
                ):
                    rejected += 1
                    continue
                if rolling and not _in_year(entry, year):
                    # Year-less venue: the comment cannot carry the year, so the
                    # paper's own arXiv date decides which year's corpus it joins.
                    rejected += 1
                    continue
                papers[arxiv_id] = self.normalize(entry, venue, year)
        self.last_stats = {
            "queries": len(comment_queries(venue, year)),
            "checked": checked,
            "rejected": rejected,
            "accepted": len(papers),
        }
        return [papers[key] for key in sorted(papers)]

    # -- normalization -----------------------------------------------------
    def normalize(self, entry: dict[str, Any], venue: str, year: int) -> dict[str, Any]:
        """Map a parsed entry onto the project's `papers` DB shape.

        `db.upsert_paper` requires: id, source, source_paper_id, forum_id, title,
        abstract, authors, keywords, primary_area, pdf_url, paper_url, doi,
        arxiv_id, submission_year, publication_year, status, decision,
        created_at_source, updated_at_source, raw_data_path.

        Choices worth noting:
          * ``id = "arxiv:<arxiv_id>"`` keeps arXiv identity namespaced and stable
            (version-stripped), so it can never collide with an OpenReview id.
          * ``forum_id`` stays None: arXiv has no forum/discussion thread.
          * ``keywords`` are the arXiv category terms (a real topical signal, and
            what the FTS index expects), not the raw comment text.
          * ``submission_year`` is the arXiv v1 year (when it was submitted);
            ``publication_year`` is the venue year being harvested — the two differ
            for the common "posted in November, accepted next year" case.
          * ``status="published"`` because a verified `co:` venue comment means the
            paper is accepted; ``decision`` stays None rather than echoing free text
            ("to appear", "spotlight") into a structured field.
        """
        arxiv_id = entry.get("arxiv_id") or ""
        published = clean_text(entry.get("published"))
        submission_year = int(published[:4]) if published[:4].isdigit() else None
        return {
            "id": f"arxiv:{arxiv_id}",
            "source": "arxiv",
            "source_paper_id": arxiv_id,
            "forum_id": None,
            "title": clean_title(entry.get("title")),
            "abstract": clean_text(entry.get("abstract")) or None,
            "authors": list(entry.get("authors") or []),
            "keywords": list(entry.get("categories") or []),
            "primary_area": entry.get("primary_category") or None,
            "pdf_url": entry.get("pdf_url") or None,
            "paper_url": entry.get("paper_url") or (
                f"http://arxiv.org/abs/{arxiv_id}" if arxiv_id else None
            ),
            "doi": entry.get("doi") or None,
            "arxiv_id": arxiv_id,
            "submission_year": submission_year,
            "publication_year": int(year),
            "status": "published",
            "decision": None,
            "created_at_source": published or None,
            "updated_at_source": clean_text(entry.get("updated")) or None,
            "raw_data_path": entry.get("raw_data_path"),
        }


#: venue key -> every `co:"..."` query string for the default year range (2022-2026).
#: Built from :func:`comment_queries` rather than written as a giant literal, so the
#: template list stays the single source of truth; it is a plain dict of plain
#: strings for callers that want to inspect or extend the query set.
#:
#: Rolling journals are year-less by nature, so their query list is identical for
#: every year. De-duplicating keeps this catalogue honest (a year is not "covered"
#: by repeating the same string five times) while still exposing the queries.
VENUE_COMMENT_PATTERNS: dict[str, list[str]] = {}
for _venue in VENUE_ALIASES:
    _queries: list[str] = []
    _seen: set[str] = set()
    for _year in DEFAULT_YEARS:
        for _query in comment_queries(_venue, _year):
            if _query not in _seen:
                _seen.add(_query)
                _queries.append(_query)
    VENUE_COMMENT_PATTERNS[_venue] = _queries
del _venue, _queries, _seen, _query
