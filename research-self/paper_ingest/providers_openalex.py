"""Official-API paper providers: OpenAlex (primary) and Crossref (supplement).

Why this module exists
----------------------
The harvester originally discovered papers by scraping proceedings HTML. That
is fragile in a way no amount of selector tuning fixes: a site can add a WAF,
change its markup, or start answering ``403`` at any time, and then a whole
venue-year sync silently produces nothing. The replacement strategy is to treat
"metadata for venue V in year Y" as a *capability* and satisfy it from
machine-readable, legitimate APIs that publish stable contracts and explicit
rate-limit rules.

Two providers are implemented here, with different jobs:

* :class:`OpenAlexProvider` -- the **primary** lister. OpenAlex indexes
  proceedings venues directly, so one venue + one year is a single filtered
  query. Verified per-year coverage is recorded in
  :data:`OPENALEX_VERIFIED_COVERAGE`.
* :class:`CrossrefProvider` -- a **supplement keyed by DOI**, never a primary
  lister. Crossref's ``query.container-title`` is a free-text match, not a
  venue identity: the same string also matches unrelated same-named
  conferences, so it returns ~400k results for a query whose real answer is
  ~4k. It is still valuable for the one thing it does well: authoritative
  per-DOI metadata records (see :meth:`CrossrefProvider.get_by_doi`).

Both stay inside the legitimate, documented API surface of the two services.
Nothing here contacts a PDF mirror, a shadow library, or any source that is not
the publisher's own metadata API, and nothing here tries to look like a browser.

Politeness is not decoration
----------------------------
Both services run a "polite pool": clients that identify themselves with an
honest ``User-Agent`` and a contact ``mailto`` get higher, more predictable
rate limits than anonymous traffic. So we always:

* send ``User-Agent: PaperGraph/0.2 (academic metadata; mailto:<address>)``;
* send ``mailto`` on every request;
* honour ``Retry-After`` and otherwise back off exponentially on ``429``/``5xx``;
* cap in-flight concurrency;
* avoid logging request URLs carrying an address (see :meth:`PoliteHttp._log_target`).

That is the opposite of rotating proxies or UA spoofing, which is both against
these services' terms and counter-productive (spoofing puts you in the anonymous
pool with a *lower* limit).

Provider contract
-----------------
:class:`OpenAlexProvider` additionally satisfies ``paper_ingest.harvest``'s
duck-typed provider protocol -- ``name`` plus
``harvest(venue, year, *, limit) -> list[dict]`` -- so it can be dropped into
``MultiSourceHarvester`` without changes. It deliberately does *not* import
``harvest`` (that module owns failover policy, this module owns one source's
wire format); a venue with no known OpenAlex source raises ``ValueError``, which
the integrator maps to its own "unavailable" signal.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlsplit

import httpx

from . import crawl
from . import normalize

__all__ = [
    "DEFAULT_MAILTO",
    "OPENALEX_API",
    "OPENALEX_SOURCES",
    "OPENALEX_LOW_COVERAGE_VENUES",
    "OPENALEX_VERIFIED_COVERAGE",
    "OPENALEX_SELECT",
    "CROSSREF_API",
    "CROSSREF_SELECT",
    "CONTAINER_RELEVANCE_THRESHOLD",
    "OpenAlexProvider",
    "CrossrefProvider",
    "PoliteHttp",
    "PoliteHttpError",
    "MalformedWorkError",
    "PoliteHttpStats",
    "ProviderStats",
    "rebuild_abstract",
    "normalize_doi",
    "clean_markup_text",
    "user_agent",
]

LOGGER = logging.getLogger("paper_ingest.providers_openalex")

#: Contact address for the polite pools. Overridable per provider instance.
DEFAULT_MAILTO = "research@example.org"

USER_AGENT_TEMPLATE = "PaperGraph/0.2 (academic metadata; mailto:{mailto})"

OPENALEX_API = "https://api.openalex.org"
CROSSREF_API = "https://api.crossref.org"

# OpenAlex hard-caps ``per-page`` at 200; larger values are rejected.
OPENALEX_MAX_PER_PAGE = 200
# Crossref caps ``rows`` at 1000 (2000 is a validation error) and rejects
# ``offset`` past roughly 10k ("deep paging" is disallowed), so a listing is
# bounded by design rather than by luck.
CROSSREF_MAX_ROWS = 1000
CROSSREF_MAX_OFFSET = 10_000

#: Target size for an *unbounded* Crossref harvest.
#:
#: ``harvest(..., limit=None)`` previously asked for only 200 records, so a
#: source that should contribute a whole proceedings volume could never return
#: more than 200 -- an invisible cap that made every Crossref-backed year look
#: small. This is a target, not a hard limit: the offset pager stops early when
#: relevance filtering runs out of records or ``CROSSREF_MAX_OFFSET`` is reached.
CROSSREF_FULL_HARVEST_ROWS = 2000

#: OpenAlex ``select`` projection.
#:
#: The first block is the minimum needed for ``db.upsert_paper``. The second
#: block exists because the DB contract has columns we would otherwise have to
#: leave NULL forever: ``keywords`` (from ``keywords``), ``primary_area`` (from
#: ``primary_topic.subfield``), and ``created_at_source``/``updated_at_source``
#: (from ``created_date``/``updated_date``). ``open_access`` costs little and
#: supplies a PDF link fallback. Abstracts dominate the payload regardless, so
#: the extension is close to free while making normalized rows far more useful.
OPENALEX_SELECT = ",".join(
    (
        "id",
        "doi",
        "title",
        "abstract_inverted_index",
        "publication_year",
        "type",
        "authorships",
        "primary_location",
        "referenced_works",
        "cited_by_count",
        # --- fields that populate optional DB columns ---
        "keywords",
        "primary_topic",
        "created_date",
        "updated_date",
        "open_access",
    )
)

#: Crossref ``select`` projection (exactly these names -- Crossref answers ``400``
#: for any field it does not recognise, so this list is not free-form).
CROSSREF_SELECT = ",".join(
    (
        "DOI",
        "title",
        "abstract",
        "author",
        "issued",
        "published",
        "created",
        "deposited",
        "type",
        "URL",
        "container-title",
        "subject",
        "link",
    )
)

# --------------------------------------------------------------------------- #
# Venue -> OpenAlex source id                                                  #
# --------------------------------------------------------------------------- #
#: Map of this project's venue keys to OpenAlex *source* ids (``S...``).
#:
#: These are verified against the live API. Do not substitute
#: ``primary_location.source.id`` in the work filter: that narrower path indexes
#: only the record's preferred location, and on these proceedings venues it
#: silently matches 0 works per year. ``locations.source.id`` matches any
#: location the work is attached to, which is what actually recovers the corpus.
OPENALEX_SOURCES: dict[str, str] = {
    "neurips": "S4363606243",
    "cvpr": "S4210176548",
    "acl": "S4363608652",
    # Kept for completeness and DOI lookups, but OpenAlex only holds a handful of
    # works per year for these two venues (2-8), which is far below a real
    # corpus. ICML/ICLR are harvested elsewhere (arXiv/DBLP); do not treat an
    # "empty" result here as an outage.
    "icml": "S4306419644",
    "iclr": "S4306419637",
}

#: Venues whose OpenAlex coverage is known to be too low to serve as a listing.
#: Callers should route these to another provider instead of accepting a
#: 3-item "success".
OPENALEX_LOW_COVERAGE_VENUES: frozenset[str] = frozenset({"icml", "iclr"})

#: Verified live counts (works found per venue-year) at the time this module was
#: written. Kept as evidence and as a regression baseline: a provider reporting a
#: number orders of magnitude below these is broken, not merely sparse.
OPENALEX_VERIFIED_COVERAGE: dict[str, dict[int, int]] = {
    "neurips": {2022: 2806, 2023: 3509, 2024: 4322, 2025: 5832},
    "cvpr": {2022: 2082, 2023: 2368, 2024: 2724, 2025: 2876},
    "acl": {2022: 700, 2023: 1249, 2024: 1036, 2025: 1973, 2026: 2655},
    "icml": {},  # broken on OpenAlex -- another provider owns ICML
    "iclr": {},  # broken on OpenAlex -- another provider owns ICLR
}

#: Minimum share of the target container title's significant tokens that a
#: Crossref record's own ``container-title`` must contain to be kept.
#:
#: Calibrated on live data for "Advances in Neural Information Processing
#: Systems": the genuine proceedings titles score 1.00, a verbose variant
#: ("Proceedings of the 37th International Conference on Neural Information
#: Processing Systems") scores 0.80, and unrelated same-token venues score 0.40
#: ("Quantum Information Processing", "Information Processing Letters").
#: 0.6 separates those populations with margin on both sides.
CONTAINER_RELEVANCE_THRESHOLD = 0.6


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #
class PoliteHttpError(RuntimeError):
    """A request failed for a reason retrying cannot fix.

    Carries ``status_code`` when the failure was an HTTP error response so the
    caller can distinguish "rate-limited out" (``429``) from "blocked" (``403``)
    from "gone" (``404``, which is reported as ``None`` rather than raised).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MalformedWorkError(ValueError):
    """A single API record cannot be turned into a paper row.

    Raised by the normalizers so a batch harvest can *skip and count* one bad
    record instead of losing the whole page. Identity and title are the only
    genuinely required fields: ``papers.id`` is a primary key and
    ``papers.title`` is ``NOT NULL`` plus the FTS5 anchor.
    """


# --------------------------------------------------------------------------- #
# Shared polite HTTP layer                                                    #
# --------------------------------------------------------------------------- #
def user_agent(mailto: str | None = None) -> str:
    """Build the honest ``User-Agent`` string used by every request.

    Identifies the tool and a contact address, which is what earns the polite
    pool's higher limits on both OpenAlex and Crossref. It deliberately does not
    imitate a browser.
    """
    return USER_AGENT_TEMPLATE.format(mailto=mailto or DEFAULT_MAILTO)


@dataclass
class PoliteHttpStats:
    """Counters for one :class:`PoliteHttp` instance (useful in sync reports)."""

    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    network_errors: int = 0
    not_found: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "server_errors": self.server_errors,
            "network_errors": self.network_errors,
            "not_found": self.not_found,
        }


@dataclass
class ProviderStats:
    """Counters for one provider run: how many records arrived, and their fate."""

    fetched: int = 0
    returned: int = 0
    skipped: int = 0
    duplicates: int = 0
    pages: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "returned": self.returned,
            "skipped": self.skipped,
            "duplicates": self.duplicates,
            "pages": self.pages,
        }


def _parse_retry_after(value: str | None) -> float | None:
    """Read a ``Retry-After`` header as a delay in seconds.

    The header is defined to carry *either* a delta-seconds integer *or* an
    HTTP-date. Both shapes occur in the wild, so both are accepted; anything
    unparseable returns ``None`` and the caller falls back to exponential
    backoff rather than sleeping for a garbage duration.
    """
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:  # RFC 7231 dates are GMT, but be forgiving
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


class PoliteHttp:
    """Small JSON-over-HTTP helper with retries, backoff, and a concurrency cap.

    Shared by both providers (and importable on its own, so a later integrator
    can reuse the same polite behaviour for arXiv or DBLP instead of growing a
    fourth near-copy of this logic).

    Retry policy
    ------------
    ``429``, ``5xx``, timeouts and transport errors are retried up to
    ``max_attempts`` times. A ``Retry-After`` header is authoritative when
    present (clamped to ``backoff_max`` so a hostile value cannot stall a sync);
    otherwise the delay grows exponentially from ``backoff_base``. ``404`` is not
    an error here -- it means "this entity does not exist", which callers such as
    ``get_by_doi`` describe with ``None``.

    Concurrency
    -----------
    ``max_concurrency`` bounds requests in flight *through this instance*. A
    semaphore is used rather than an async client because the surrounding
    harvester is synchronous, and the bound is what matters: it is the difference
    between "a polite client" and "an accidental flood" when several venue-years
    are synced from a thread pool.
    """

    def __init__(
        self,
        *,
        mailto: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 45.0,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        max_concurrency: int = 4,
        logger: logging.Logger | None = None,
        api_key: str | None = None,
    ) -> None:
        # A blank/whitespace mailto would produce a malformed UA and still look
        # like an attempt at politeness, so fall back to the documented default.
        address = (mailto or "").strip() or DEFAULT_MAILTO
        self.mailto = address
        # OpenAlex gives each API key its own daily budget; without a key the
        # client shares a very small per-IP budget with everyone else on that IP
        # (observed: a burst of harvests exhausts it and the API starts answering
        # 401). Sending a key when configured is therefore the difference between
        # "works reliably" and "works until the neighbours use it up".
        self.api_key = (api_key or "").strip() or None
        self.headers: dict[str, str] = {
            "User-Agent": user_agent(address),
            "Accept": "application/json",
        }
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, headers=self.headers
        )
        # Injected clients (tests) may not carry our headers, so set them here as
        # well; httpx merges per-request headers over client defaults.
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base = max(0.0, float(backoff_base))
        self._backoff_max = max(0.0, float(backoff_max))
        # The shared retry policy owns the backoff arithmetic (see
        # paper_ingest/crawl.py). It is constructed with the values this class
        # has always used, so the behaviour is unchanged -- only the place the
        # rule lives is now shared with the other collectors.
        self._policy = crawl.RetryPolicy(
            attempts=self._max_attempts,
            base_delay=self._backoff_base,
            max_delay=self._backoff_max,
            jitter=0.0,  # the previous backoff had no jitter; keep it that way
        )
        self._semaphore: threading.BoundedSemaphore | None = (
            threading.BoundedSemaphore(max_concurrency) if max_concurrency > 0 else None
        )
        self._lock = threading.Lock()
        self.stats = PoliteHttpStats()
        self.last_error: str | None = None
        self._log = logger or LOGGER

    # -- plumbing ---------------------------------------------------------- #
    def __enter__(self) -> "PoliteHttp":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the pooled connection if we created it.

        An injected client belongs to the caller, so closing it would break
        reuse (and tests) -- mirroring ``openreview.OpenReview``'s ownership flag.
        """
        if self._owns_client:
            self._client.close()

    def reset_stats(self) -> None:
        self.stats = PoliteHttpStats()
        self.last_error = None

    def _log_target(self, url: str) -> str:
        """Return a loggable form of ``url``: scheme+host+path, never the query.

        The query string carries the contact address; keeping it out of logs
        means a shared log file does not leak it.
        """
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _bump(self, field_name: str) -> None:
        with self._lock:
            setattr(self.stats, field_name, getattr(self.stats, field_name) + 1)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before attempt ``attempt + 1``.

        The arithmetic lives in :class:`paper_ingest.crawl.RetryPolicy` so
        that every collector in this package waits the same way for the same
        reason. The policy clamps a server-supplied ``Retry-After`` to
        ``backoff_max``: obeying it is polite, but a broken or hostile header
        must not be able to stall a sync indefinitely.
        """
        return self._policy.delay_for(attempt, retry_after)

    def _send(self, url: str, params: Mapping[str, Any] | None) -> httpx.Response:
        if self._semaphore is None:
            return self._client.get(url, params=params, headers=self.headers)
        with self._semaphore:
            return self._client.get(url, params=params, headers=self.headers)

    # -- public API -------------------------------------------------------- #
    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        select: str | None = None,
    ) -> Any | None:
        """GET ``url`` and decode JSON, retrying transient failures.

        Returns ``None`` for ``404`` (entity absent). Raises
        :class:`PoliteHttpError` for other 4xx responses and once retries are
        exhausted, so an exhausted rate limit is a loud, described failure
        rather than a quietly empty result.
        """
        query: dict[str, Any] = {k: v for k, v in (params or {}).items() if v is not None}
        if select:
            query.setdefault("select", select)
        # Send the contact address even when the caller forgot; both services use
        # it to place the client in the polite pool.
        query.setdefault("mailto", self.mailto)
        # A configured API key gets its own daily budget instead of the tiny
        # shared per-IP one.
        if self.api_key:
            query.setdefault("api_key", self.api_key)

        target = self._log_target(url)
        last_reason = "no attempt made"
        for attempt in range(self._max_attempts):
            retry_after: float | None = None
            try:
                response = self._send(url, query)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_reason = f"{type(exc).__name__}"
                self._bump("network_errors")
                self._log.debug("GET %s failed (%s), attempt %d", target, last_reason, attempt + 1)
                if attempt + 1 >= self._max_attempts:
                    self.last_error = last_reason
                    raise PoliteHttpError(f"{target}: {last_reason}") from exc
                self._bump("retries")
                self._sleep(self._backoff(attempt, None))
                continue

            self._bump("requests")
            status = response.status_code

            if status == 404:
                self._bump("not_found")
                return None

            if status == 429 or status >= 500:
                if status == 429:
                    self._bump("rate_limited")
                else:
                    self._bump("server_errors")
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_reason = f"HTTP {status}"
                if attempt + 1 >= self._max_attempts:
                    self.last_error = last_reason
                    raise PoliteHttpError(
                        f"{target}: {last_reason} after {self._max_attempts} attempts",
                        status_code=status,
                    )
                self._bump("retries")
                delay = self._backoff(attempt, retry_after)
                self._log.debug(
                    "GET %s -> %s, retrying in %.2fs (attempt %d/%d)",
                    target,
                    status,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                )
                self._sleep(delay)
                continue

            if status >= 400:
                # Not retryable: 400 means *our* query is wrong and repeating it
                # will fail identically. Surface it instead of burning the budget.
                #
                # 401/403 deserve a specific hint. OpenAlex now meters requests
                # per IP against a small shared daily budget when no API key is
                # supplied, so an exhausted free budget surfaces as 401 and the
                # generic "HTTP 401" message gives the operator nothing to act
                # on. (Observed live mid-session: a burst of harvests turned a
                # working endpoint into 401 until the budget reset.)
                self.last_error = f"HTTP {status}"
                hint = ""
                if status in (401, 403):
                    hint = (
                        " (OpenAlex meters per-IP without an API key; the shared "
                        "free daily budget may be exhausted - get a free key at "
                        "https://openalex.org/settings/api and pass api_key=...)"
                    )
                raise PoliteHttpError(
                    f"{target}: HTTP {status}{hint}", status_code=status
                )

            try:
                return response.json()
            except ValueError as exc:
                # A 200 with unparseable body is a transient proxy/CDN artifact;
                # treat it like a server hiccup rather than corrupting the caller.
                last_reason = "invalid JSON"
                if attempt + 1 >= self._max_attempts:
                    self.last_error = last_reason
                    raise PoliteHttpError(f"{target}: {last_reason}") from exc
                self._bump("retries")
                self._sleep(self._backoff(attempt, None))
                continue

        # Unreachable: every branch above returns or raises.
        raise PoliteHttpError(f"{target}: {last_reason}")


# --------------------------------------------------------------------------- #
# Text helpers                                                                #
# --------------------------------------------------------------------------- #
_WHITESPACE_RE = re.compile(r"\s+")
# Tags as they appear in JATS/XML metadata payloads. Deliberately narrow enough
# to leave comparison operators ("a < b") alone: a tag must start with a letter,
# "/", "!" or "?" after "<".
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>|</?[!?][^<>]*>")
_JATS_TITLE_RE = re.compile(r"<jats:title\b[^<>]*>.*?</jats:title>", re.IGNORECASE | re.DOTALL)
_ESCAPED_MARKUP_RE = re.compile(r"&lt;|&#0*60;|&#x0*3c;", re.IGNORECASE)

# Block-level tags bound words, so they become a space ("end.</jats:p><jats:p>Next"
# must not concatenate into "end.Next"). Inline tags do *not* bound words and are
# deleted outright -- replacing them with a space would split "FLAPS" from its
# own punctuation and produce "FLAPS ,".
_BLOCK_TAGS = (
    "abstract", "br", "caption", "div", "fig", "li", "ol", "p", "sec",
    "table", "td", "th", "title", "tr", "ul",
)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:" + "|".join(_BLOCK_TAGS) + r")(?:\s[^<>]*)?/?>", re.IGNORECASE
)


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _strip_markup(text: str) -> str:
    """Remove JATS/XML tags, keeping word boundaries only where they existed."""
    text = _JATS_TITLE_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub(" ", text)
    return _TAG_RE.sub("", text)


def clean_markup_text(value: Any) -> str | None:
    """Turn a JATS/XML-marked-up abstract into plain text.

    Crossref deposits abstracts as JATS XML fragments::

        <jats:p xml:lang="en">We show &lt;10ms latency &amp; better accuracy.</jats:p>

    A naive ``strip_tags`` leaves the entities behind (``&lt;10ms``), and
    unescaping *before* stripping would let entity-encoded text like ``&lt;10ms``
    be mistaken for a tag. So the order is:

    1. drop ``<jats:title>Abstract</jats:title>`` blocks -- publishers embed the
       word "Abstract" as a heading, which is not part of the abstract text;
    2. strip block-level tags as a word boundary and inline tags as nothing (see
       :func:`_strip_markup`) -- replacing *every* tag with a space would turn
       ``<jats:italic>FLAPS</jats:italic>,`` into ``FLAPS ,``;
    3. if entity-encoded tags were present (``&lt;jats:p&gt;``, occasionally
       double-encoded), unescape and strip once more;
    4. unescape entities, then collapse whitespace;
    5. finally drop the LaTeX leftovers that survive in scraped titles and
       abstracts (``$O(n \log n)$``, ``\emph{...}``) -- see
       :func:`paper_ingest.normalize.clean_text`.

    Returns ``None`` for missing/blank input so the DB stores NULL rather than
    an empty string.
    """
    if not isinstance(value, str):
        return None
    text = _strip_markup(value)
    if _ESCAPED_MARKUP_RE.search(text):
        text = _strip_markup(html.unescape(text))
    text = html.unescape(text)
    text = _collapse_whitespace(text)
    text = normalize.clean_text(text)
    return text or None


def _clean_title(value: Any) -> str | None:
    """A title as plain text: no markup, no LaTeX, single spaces.

    The LaTeX step matters for the index: a scraped title like
    ``"$O(n \\log n)$ Bounds"`` otherwise lands in FTS with the command
    fragments attached, and a query for "log" misses it.
    """
    if not isinstance(value, str):
        return None
    text = _collapse_whitespace(html.unescape(_strip_markup(value)))
    text = normalize.clean_text(text)
    return text or None


# --------------------------------------------------------------------------- #
# Abstract reconstruction                                                     #
# --------------------------------------------------------------------------- #
def rebuild_abstract(inverted_index: Mapping[str, Any] | None) -> str | None:
    """Rebuild an abstract from OpenAlex's ``abstract_inverted_index``.

    OpenAlex ships abstracts as ``{word: [positions]}`` instead of a string (a
    copyright precaution). Restoring the text means sorting the (position, word)
    pairs -- not inverting ``{position: word}`` blindly.

    Two things break the naive inversion, both handled here:

    * **A word can occur many times.** "the" commonly appears 20+ times. Assigning
      ``positions[i] = word`` in dict order happens to work for that, but it
      silently drops information: if two distinct words claim the same index (data
      glitch, or a non-space token split across entries), one overwrites the other
      and the abstract loses a word. Collecting *all* pairs keeps every token.
    * **Positions are JSON numbers but upstream code sometimes hands them over as
      strings**, and an inverted index can be present-but-empty or not a mapping
      at all. Values are coerced, non-integers are ignored, and anything
      unusable yields ``None`` rather than raising mid-harvest.

    Ordering is by position, then by first appearance in the index, so the result
    is deterministic even for colliding positions.
    """
    if not isinstance(inverted_index, Mapping) or not inverted_index:
        return None

    # (position, order, word) triples; ``order`` only breaks position ties.
    pairs: list[tuple[int, int, str]] = []
    order = 0
    for word, positions in inverted_index.items():
        if not isinstance(word, str) or not word:
            continue
        if isinstance(positions, (int, float)) or isinstance(positions, str):
            # Defensive: a scalar where a list of positions belongs.
            positions = [positions]
        if not isinstance(positions, Iterable) or isinstance(positions, (bytes, Mapping)):
            continue
        for raw_position in positions:
            position = _coerce_position(raw_position)
            if position is None:
                continue
            pairs.append((position, order, word))
            order += 1

    if not pairs:
        return None
    pairs.sort(key=lambda item: (item[0], item[1]))
    text = _collapse_whitespace(" ".join(word for _, _, word in pairs))
    return text or None


def _coerce_position(value: Any) -> int | None:
    """Coerce an inverted-index position to ``int``; ``None`` when impossible.

    Accepts ints, floats that are whole numbers, and numeric strings ("12"),
    because the index is JSON and its numbers are not guaranteed to survive
    round-tripping through every intermediate consumer.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a position
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Shared record helpers                                                       #
# --------------------------------------------------------------------------- #
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_STOPWORDS = frozenset(
    "a an and at for in of on the to with from by &".split()
)
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def normalize_doi(value: Any) -> str | None:
    """Return a bare DOI (``10.x/y``), or ``None``.

    The DB stores one ``doi`` column but sources disagree on shape: OpenAlex
    returns ``https://doi.org/10.x/y``, Crossref returns ``10.x/y``, and scraped
    tags used ``doi:10.x/y``. Reducing to the bare form here keeps rows
    comparable; case is preserved because DOIs are case-insensitive for matching
    anyway and downstream code lowercases when building dedup keys.
    """
    if not isinstance(value, str):
        return None
    doi = _DOI_PREFIX_RE.sub("", value.strip())
    doi = doi.strip()
    return doi or None


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """Recover an arXiv id from DataCite's arXiv DOI (``10.48550/arXiv.NNNN``).

    arXiv mints DOIs under ``10.48550``, so a Crossref/OpenAlex DOI is often the
    only arXiv pointer available; extracting it lets ``harvest.paper_keys``
    deduplicate arXiv and proceedings copies of the same paper.
    """
    if not doi:
        return None
    match = _ARXIV_DOI_RE.match(doi)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _first_str(value: Any) -> str | None:
    """First usable string from a str-or-list-of-str field (Crossref does both)."""
    if isinstance(value, str):
        return _collapse_whitespace(value) or None
    for item in _as_sequence(value):
        if isinstance(item, str) and item.strip():
            return _collapse_whitespace(item)
    return None


def _date_parts_year(value: Any) -> int | None:
    """Pull a year out of a Crossref date object (``{"date-parts": [[2024]]}``)."""
    parts = _as_sequence(_as_mapping(value).get("date-parts"))
    for entry in parts:
        for part in _as_sequence(entry):
            if isinstance(part, int) and 1000 <= part <= 3000:
                return part
    return None


def _container_tokens(text: str | None) -> list[str]:
    """Significant lowercase tokens of a container title (stopwords removed)."""
    if not text:
        return []
    tokens = [token for token in _TOKEN_RE.split(text.lower()) if token]
    return [token for token in tokens if token not in _STOPWORDS and len(token) > 1]


def container_relevance(container: str | None, target: str | None) -> float:
    """Share of ``target``'s significant tokens present in ``container``.

    Used to post-filter Crossref ``query.container-title`` hits client-side. That
    query is a relevance-ranked free-text match, so it is correct *per page* but
    drifts to merely similar venues on deep pages; comparing token coverage
    against the requested container is what keeps the supplement honest.
    """
    target_tokens = _container_tokens(target)
    if not target_tokens:
        return 0.0
    container_tokens = set(_container_tokens(container))
    matched = sum(1 for token in target_tokens if token in container_tokens)
    return matched / len(target_tokens)


# --------------------------------------------------------------------------- #
# OpenAlex                                                                    #
# --------------------------------------------------------------------------- #
class OpenAlexProvider:
    """Harvest works for a venue-year from OpenAlex's ``/works`` API.

    Filter syntax is the load-bearing detail::

        filter=locations.source.id:S4363606243,publication_year:2024

    ``locations.source.id`` matches a work attached to *any* location whose
    source is that venue, which is what a proceedings corpus needs.
    ``primary_location.source.id`` looks equivalent but only considers the
    record's single preferred location: on these venues it matches literally zero
    works per year (verified), producing a silently empty harvest. Every filter
    this class builds is therefore asserted to start from ``locations.source.id``.

    Pagination uses OpenAlex cursors (``cursor=*`` then ``meta.next_cursor``).
    Cursor paging is required here: page-number paging over a large filtered set
    is not stable, and works can shift between pages mid-harvest.
    """

    name = "openalex"

    def __init__(
        self,
        mailto: str | None = None,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        http: PoliteHttp | None = None,
        sleep: Callable[[float], None] = time.sleep,
        per_page: int = OPENALEX_MAX_PER_PAGE,
        max_pages: int = 1000,
        timeout: float = 45.0,
        max_concurrency: int = 4,
    ) -> None:
        self.mailto = (mailto or "").strip() or DEFAULT_MAILTO
        # A shared helper can be injected (one connection pool, one rate-limit
        # budget across several venue-years); otherwise build a private one.
        self.http = http or PoliteHttp(
            mailto=self.mailto,
            api_key=api_key,
            client=client,
            sleep=sleep,
            timeout=timeout,
            max_concurrency=max_concurrency,
        )
        self.per_page = max(1, min(int(per_page), OPENALEX_MAX_PER_PAGE))
        # Safety valve: a paginating loop with no natural end is how harvesters
        # turn a server bug into an infinite sync.
        self.max_pages = max(1, int(max_pages))
        self.stats = ProviderStats()

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "OpenAlexProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- harvesting -------------------------------------------------------- #
    def source_id(self, venue: str) -> str:
        """Return the OpenAlex source id for ``venue``.

        Raises ``ValueError`` with the known venue keys for unknown input: a typo
        ("neurips_2024") must not be mistaken for an empty year.
        """
        key = (venue or "").strip().lower()
        try:
            return OPENALEX_SOURCES[key]
        except KeyError:
            raise ValueError(
                f"no OpenAlex source id configured for venue {venue!r}; "
                f"known venues: {', '.join(sorted(OPENALEX_SOURCES))}"
            ) from None

    def build_filter(self, venue: str, year: int) -> str:
        """Build the work filter for ``venue``/``year``.

        Documented as a method (not inlined) because the filter string is the one
        place a wrong field name silently returns an empty corpus, so it is worth
        asserting directly in tests.
        """
        return f"locations.source.id:{self.source_id(venue)},publication_year:{int(year)}"

    def search_source(
        self, venue: str, year: int, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Harvest normalized papers for ``venue``/``year``.

        ``limit`` caps the number of *returned papers*; the page size shrinks to
        match on the final page so a ``limit=20`` run transfers 20 records, not
        the default 200. Malformed works are skipped and counted in
        :attr:`stats`, never raised.

        Returns papers in API order (newest publication date first) with
        first-seen deduplication, so repeated or overlapping pages cannot produce
        duplicate rows.

        **Paging mode.** OpenAlex gates deep ``cursor`` paging behind an API key:
        the first page works unkeyed, but requesting page 2 returns

            {"error":"API key required for cursor paging"}

        Cursor-only paging therefore silently capped every harvest at the first
        page (200 works) no matter the venue size -- which is exactly why real
        corpora looked like tiny samples. Offset paging (``page=N``) is NOT gated,
        so this method starts with cursor paging (fastest, stable when keyed) and
        transparently falls back to offset paging the moment the cursor path is
        refused. Both modes share the same dedup/anchor logic.
        """
        self.stats = ProviderStats()
        if limit is not None and limit <= 0:
            return []

        filter_value = self.build_filter(venue, year)
        url = f"{OPENALEX_API}/works"

        def _paging_params(**extra: Any) -> dict[str, Any]:
            params: dict[str, Any] = {
                "filter": filter_value,
                "per-page": self.per_page,
                # Explicit sort makes the page sequence reproducible instead of
                # depending on a default that could change between requests.
                "sort": "publication_date:desc",
                "select": OPENALEX_SELECT,
            }
            params.update(extra)
            return params

        def _collect(payload: Any, papers: list[dict[str, Any]], seen: set[str]) -> int:
            """Normalize one page into ``papers``; return how many were added."""
            if not isinstance(payload, Mapping):
                return 0
            results = _as_sequence(payload.get("results"))
            added = 0
            self.stats.pages += 1
            for work in results:
                self.stats.fetched += 1
                if not isinstance(work, Mapping):
                    self.stats.skipped += 1
                    continue
                try:
                    paper = self.normalize(work, venue, year)
                except MalformedWorkError as exc:
                    self.stats.skipped += 1
                    self._log_skip(exc)
                    continue
                if paper["id"] in seen:
                    self.stats.duplicates += 1
                    continue
                seen.add(paper["id"])
                papers.append(paper)
                added += 1
            return added

        papers: list[dict[str, Any]] = []
        seen: set[str] = set()

        # ---- attempt 1: cursor paging (works without a key only on page 1) ----
        cursor_usable = True
        cursor = "*"
        requested_cursors: set[str] = {"*"}
        for _ in range(self.max_pages):
            remaining = None if limit is None else limit - len(papers)
            if remaining is not None and remaining <= 0:
                break
            per_page = self.per_page if remaining is None else min(self.per_page, remaining)
            try:
                payload = self.http.get_json(
                    url, _paging_params(**{"per-page": per_page, "cursor": cursor})
                )
            except PoliteHttpError as exc:
                if getattr(exc, "status_code", None) in (401, 403) and papers:
                    # Cursor paging refused after the first page: the rest of the
                    # corpus is still reachable via offset paging.
                    cursor_usable = False
                    break
                raise
            if not isinstance(payload, Mapping):
                break
            added = _collect(payload, papers, seen)
            if added == 0 and self.stats.pages > 1 and not _as_sequence(payload.get("results")):
                break
            next_cursor = _as_mapping(payload.get("meta")).get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break  # documented end of results
            if next_cursor in requested_cursors:
                break
            cursor = next_cursor
            requested_cursors.add(cursor)
            if limit is not None and len(papers) >= limit:
                break

        # ---- attempt 2: offset paging (unkeyed, full corpus) -----------------
        if not cursor_usable:
            # The cursor attempt already consumed page 1's worth of records, but
            # offset paging is index-based and does not know that. Start from the
            # first page and let `seen` discard the overlap, then continue past it.
            # Skipping page 1 here would silently lose the first 200 works.
            page = 1
            while page <= self.max_pages:
                remaining = None if limit is None else limit - len(papers)
                if remaining is not None and remaining <= 0:
                    break
                per_page = self.per_page if remaining is None else min(self.per_page, remaining)
                payload = self.http.get_json(
                    url, _paging_params(**{"per-page": per_page, "page": page})
                )
                if not isinstance(payload, Mapping):
                    break
                results = _as_sequence(payload.get("results"))
                if not results:
                    break
                _collect(payload, papers, seen)
                if len(results) < per_page:
                    break  # short page: reached the end
                page += 1

        if limit is not None:
            papers = papers[:limit]
        self.stats.returned = len(papers)
        return papers

    def harvest(
        self, venue: str, year: int, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """``paper_ingest.harvest`` provider-protocol entry point.

        Thin alias so this class can be handed to ``MultiSourceHarvester``
        without the orchestrator knowing about OpenAlex specifics.
        """
        return self.search_source(venue, year, limit=limit)

    def _log_skip(self, exc: MalformedWorkError) -> None:
        # Debug, not warning: a bushy harvest legitimately contains a few records
        # with no title, and a warning per record would drown the log.
        LOGGER.debug("skipping malformed OpenAlex work: %s", exc)

    # -- lookups ----------------------------------------------------------- #
    def get_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Fetch one normalized paper by DOI, or ``None`` when unknown.

        Verified both ``/works/doi:10.x/y`` and the URL-encoded form resolve, so
        the prefix is stripped and the remainder quoted (DOIs can contain ``#``
        and ``?``, which would otherwise truncate the path).

        OpenAlex answers an unknown DOI with an HTML ``404``; that is a normal
        miss, not an error, hence ``None``.
        """
        bare = normalize_doi(doi)
        if not bare:
            return None
        payload = self.http.get_json(f"{OPENALEX_API}/works/doi:{quote(bare, safe='/')}", None)
        if not isinstance(payload, Mapping):
            return None
        try:
            # No venue/year context here: pass 0 so ``normalize`` falls back to
            # the record's own publication_year for submission_year.
            return self.normalize(payload, "", 0)
        except MalformedWorkError:
            return None

    # -- normalization ----------------------------------------------------- #
    def normalize(self, work: Mapping[str, Any], venue: str, year: int) -> dict[str, Any]:
        """Map one OpenAlex work onto ``db.upsert_paper``'s paper-dict contract.

        Required-by-DB keys (see ``paper_ingest.db.upsert_paper``):
        ``id, source, source_paper_id, forum_id, title, abstract, authors,
        keywords, primary_area, pdf_url, paper_url, doi, arxiv_id,
        submission_year, publication_year, status, decision,
        created_at_source, updated_at_source, raw_data_path``.

        ``id`` is derived deterministically from the OpenAlex work id
        (``openalex:W123``) so re-harvesting the same year updates rows instead of
        duplicating them -- ``papers.id`` is the primary key.

        ``venue``/``year`` describe the *harvest request*, not the record, and are
        carried as ``source_venue``/``submission_year`` so a row stays traceable
        when the same work appears in two venue-years.
        """
        if not isinstance(work, Mapping):
            raise MalformedWorkError("work is not a mapping")

        work_id = self._work_id(work)
        title = _clean_title(work.get("title"))
        if not title:
            # papers.title is NOT NULL and anchors the FTS5 index.
            raise MalformedWorkError(f"work {work_id or '(no id)'} has no usable title")

        publication_year = self._int_or_none(work.get("publication_year"))
        requested_year = self._int_or_none(year)
        source_year = requested_year or publication_year
        if not publication_year:
            publication_year = source_year

        primary_location = _as_mapping(work.get("primary_location"))
        doi = normalize_doi(work.get("doi")) or normalize_doi(
            _as_mapping(work.get("ids")).get("doi")
        )
        keywords = self._keywords(work.get("keywords"))
        primary_area = self._primary_area(work.get("primary_topic"))

        return {
            "id": f"openalex:{work_id}" if work_id else f"openalex:doi:{doi or title[:64]}",
            "source": "openalex",
            "source_paper_id": work_id,
            "forum_id": None,  # OpenAlex has no discussion forum
            "title": title,
            "abstract": rebuild_abstract(work.get("abstract_inverted_index")),
            "authors": self._authors(work.get("authorships")),
            "keywords": keywords,
            "primary_area": primary_area,
            "pdf_url": self._pdf_url(primary_location, work.get("open_access")),
            "paper_url": self._paper_url(primary_location, doi, work_id),
            "doi": doi,
            "arxiv_id": arxiv_id_from_doi(doi),
            "submission_year": source_year,
            "publication_year": publication_year,
            "status": "published",
            "decision": None,
            "created_at_source": self._str_or_none(work.get("created_date")),
            "updated_at_source": self._str_or_none(work.get("updated_date")),
            "raw_data_path": None,  # set by the caller if it persists raw JSON
            # --- provenance / graph-useful extras (not DB columns) ---
            "source_venue": venue or None,
            "cited_by_count": self._int_or_none(work.get("cited_by_count")),
            "referenced_works": [
                ref for ref in _as_sequence(work.get("referenced_works")) if isinstance(ref, str)
            ],
            "openalex_type": self._str_or_none(work.get("type")),
            "openalex_url": self._work_url(work),
        }

    # -- normalization helpers --------------------------------------------- #
    @staticmethod
    def _work_url(work: Mapping[str, Any]) -> str | None:
        raw = work.get("id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    @classmethod
    def _work_id(cls, work: Mapping[str, Any]) -> str:
        """Extract the bare work id (``W123``) from id/ids, without a URL."""
        for candidate in (
            work.get("id"),
            _as_mapping(work.get("ids")).get("openalex"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                value = candidate.strip().rstrip("/")
                return value.rsplit("/", 1)[-1]
        return ""

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value.strip()))
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _str_or_none(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _authors(authorships: Any) -> list[str]:
        """Display names in authorship order, skipping unusable entries.

        OpenAlex nests the name at ``authorships[i].author.display_name``;
        ``raw_author_name`` is the fallback for records whose author entity is
        unresolved (common for proceedings-only works).

        The name is put through
        :func:`paper_ingest.normalize.normalize_author_name` so that the
        spellings different sources use for one person -- "Joshua R. Wang",
        "Wang, Joshua R." -- land on one form in the corpus, and an author
        search finds both.
        """
        names: list[str] = []
        for entry in _as_sequence(authorships):
            entry_map = _as_mapping(entry)
            name = _as_mapping(entry_map.get("author")).get("display_name")
            if not isinstance(name, str) or not name.strip():
                name = entry_map.get("raw_author_name")
            if isinstance(name, str) and name.strip():
                names.append(normalize.normalize_author_name(name))
        return names

    @staticmethod
    def _keywords(keywords: Any) -> list[str]:
        """OpenAlex concepts/keywords become plain strings; scores are dropped.

        They are ranked by relevance already, so order is preserved and
        duplicates (case variants) are removed while keeping first occurrence.
        """
        out: list[str] = []
        seen: set[str] = set()
        for entry in _as_sequence(keywords):
            entry_map = _as_mapping(entry)
            name = entry_map.get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            label = _collapse_whitespace(name)
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(label)
        return out

    @staticmethod
    def _primary_area(primary_topic: Any) -> str | None:
        """Map ``primary_topic`` to the project's ``primary_area`` column.

        ``subfield`` is the closest analogue to a conference track ("Computer
        Vision and Pattern Recognition"); ``field`` is too coarse ("Computer
        Science") to be useful for grouping, so it is only a fallback.
        """
        topic = _as_mapping(primary_topic)
        for key in ("subfield", "field"):
            name = _as_mapping(topic.get(key)).get("display_name")
            if isinstance(name, str) and name.strip():
                return _collapse_whitespace(name)
        name = topic.get("display_name")
        if isinstance(name, str) and name.strip():
            return _collapse_whitespace(name)
        return None

    @staticmethod
    def _looks_like_pdf(url: str) -> bool:
        """Heuristic for "this URL serves a PDF rather than a landing page".

        ``open_access.oa_url`` is frequently a *landing* page (often
        ``https://doi.org/...``), and storing that in ``papers.pdf_url`` would
        break any downloader. But requiring a ``.pdf`` suffix is too strict: arXiv
        serves PDFs at ``/pdf/2405.14458`` with no extension, and that is exactly
        the open-access URL OpenAlex reports most often for ML papers. So accept
        either a ``.pdf`` suffix or a ``/pdf/`` path segment, and ignore query
        strings/fragments when checking.
        """
        parts = urlsplit(url)
        if parts.path.lower().endswith(".pdf"):
            return True
        segments = [segment.lower() for segment in parts.path.split("/") if segment]
        return "pdf" in segments

    @classmethod
    def _pdf_url(cls, primary_location: Mapping[str, Any], open_access: Any) -> str | None:
        """Best available direct PDF link, preferring the publisher's own."""
        direct = primary_location.get("pdf_url")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        oa_url = _as_mapping(open_access).get("oa_url")
        if isinstance(oa_url, str) and oa_url.strip() and cls._looks_like_pdf(oa_url.strip()):
            return oa_url.strip()
        return None

    @staticmethod
    def _paper_url(
        primary_location: Mapping[str, Any], doi: str | None, work_id: str
    ) -> str | None:
        """Best human-facing landing page: publisher, then DOI, then OpenAlex."""
        for candidate in (primary_location.get("landing_page_url"),):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if doi:
            return f"https://doi.org/{doi}"
        if work_id:
            return f"https://openalex.org/{work_id}"
        return None


# --------------------------------------------------------------------------- #
# Crossref                                                                    #
# --------------------------------------------------------------------------- #
class CrossrefProvider:
    """Crossref client used as a **DOI-keyed supplement**, not a venue lister.

    ``query.container-title`` is a free-text relevance query, not a venue
    identity, so it happily matches unrelated venues that share words: a live
    query for "Advances in Neural Information Processing Systems" reports ~390k
    results against a real corpus of ~4.3k. Two consequences shape this class:

    * :meth:`search_container` post-filters client-side by token coverage of the
      container title (:data:`CONTAINER_RELEVANCE_THRESHOLD`), and
    * it pages with ``offset``, **not** ``cursor``. Crossref's ``cursor`` mode
      drops the relevance ranking entirely -- verified: the same query that
      returns the right proceedings at offset 0 returns IEEE signal-processing
      journals under ``cursor=*``. Cursor mode is only safe for deterministic
      sorts, which a free-text query does not have.

    Crossref's distinct value is authoritative per-DOI metadata (and abstracts,
    when deposited), so :meth:`get_by_doi` is the primary entry point for the
    harvester's enrichment path.
    """

    name = "crossref"

    #: Display names used when Crossref is asked to list a venue's proceedings.
    #: Crossref's container titles are the published proceedings names, which
    #: differ from the short venue keys used elsewhere.
    _CONTAINER_TITLES: dict[str, str] = {
        "neurips": "Advances in Neural Information Processing Systems",
        "icml": "International Conference on Machine Learning",
        "iclr": "International Conference on Learning Representations",
        "cvpr": "Computer Vision and Pattern Recognition",
        "acl": "Annual Meeting of the Association for Computational Linguistics",
        "tmlr": "Transactions on Machine Learning Research",
    }

    def harvest(
        self, venue: str, year: int, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Adapter for ``harvest.MultiSourceHarvester``'s duck-typed protocol.

        Crossref is a *supplement*: it is DOI-complete but its conference records
        commonly carry no abstract, and its container search is free-text (see the
        class docstring). It therefore runs last in the chain and is treated as
        "unavailable" for venues whose proceedings it does not reasonably model,
        so the orchestrator records a clear reason instead of a generic error.
        """
        from .harvest import ProviderUnavailable

        container = self._CONTAINER_TITLES.get((venue or "").strip().lower())
        if not container:
            raise ProviderUnavailable(
                f"crossref has no proceedings container mapping for {venue!r}"
            )
        # A full venue-year can be thousands of papers; capping at 200 silently
        # truncated Crossref contributions (a real corpus would then look like it
        # only had 200 records). `search_container` pages with offset and stops on
        # its own when it runs out or hits CROSSREF_MAX_OFFSET, so ask for the
        # real target and let the pager decide.
        rows = CROSSREF_FULL_HARVEST_ROWS if limit is None else max(1, int(limit))
        records = self.search_container(container, year, rows=rows)
        if not records:
            raise ProviderUnavailable(
                f"crossref returned no relevant {venue} {year} proceedings records"
            )
        papers = [self.normalize(record, venue, year) for record in records]
        # Normalize may drop unusable records; keep only dicts that survived.
        papers = [p for p in papers if isinstance(p, dict) and p.get("id")]
        if not papers:
            raise ProviderUnavailable(
                f"crossref records for {venue} {year} could not be normalized"
            )
        return papers if limit is None else papers[: int(limit)]

    def __init__(
        self,
        mailto: str | None = None,
        *,
        client: httpx.Client | None = None,
        http: PoliteHttp | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 45.0,
        max_concurrency: int = 4,
    ) -> None:
        self.mailto = (mailto or "").strip() or DEFAULT_MAILTO
        self.http = http or PoliteHttp(
            mailto=self.mailto,
            client=client,
            sleep=sleep,
            timeout=timeout,
            max_concurrency=max_concurrency,
        )
        self.stats = ProviderStats()

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "CrossrefProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- listing ----------------------------------------------------------- #
    def search_container(
        self,
        container_title: str,
        year: int,
        rows: int = 100,
        *,
        page_size: int = 200,
        min_relevance: float = CONTAINER_RELEVANCE_THRESHOLD,
    ) -> list[dict[str, Any]]:
        """Return raw Crossref records plausibly belonging to a container+year.

        ``rows`` is the maximum number of *kept* records, while ``page_size`` is
        how many are requested per call. The two are deliberately separate:
        Crossref's ``rows`` is a page size, so tying the page size to the wanted
        total would make a second page unreachable (``rows=4, page_size=4`` keeps
        at most 4 and then stops). Since the relevance filter drops records, the
        loop must be able to page past a page that yields few survivors.

        Paging uses ``offset``, bounded by ``CROSSREF_MAX_OFFSET``: Crossref
        refuses deep paging (``offset=50000`` is a ``400``) and its relevance
        ranking has drifted to unrelated venues well before that.

        Returns raw ``message.items`` entries -- call :meth:`normalize` to get a
        paper dict -- because a supplement is usually merged by DOI and the caller
        may want to decide which of two sources wins.
        """
        self.stats = ProviderStats()
        if rows <= 0 or not (container_title or "").strip():
            return []

        page = max(1, min(int(page_size), CROSSREF_MAX_ROWS))
        kept: list[dict[str, Any]] = []
        offset = 0
        while len(kept) < rows and offset < CROSSREF_MAX_OFFSET:
            payload = self.http.get_json(
                f"{CROSSREF_API}/works",
                {
                    "query.container-title": container_title,
                    "filter": (
                        f"from-pub-date:{int(year)}-01-01,until-pub-date:{int(year)}-12-31"
                    ),
                    "rows": page,
                    "offset": offset,
                    "select": CROSSREF_SELECT,
                },
            )
            if not isinstance(payload, Mapping):
                break
            message = _as_mapping(payload.get("message"))
            items = _as_sequence(message.get("items"))
            if not items:
                break
            self.stats.pages += 1
            self.stats.fetched += len(items)

            for item in items:
                if not isinstance(item, Mapping):
                    self.stats.skipped += 1
                    continue
                record = dict(item)
                score = container_relevance(_first_str(record.get("container-title")), container_title)
                if score < min_relevance:
                    # Same words, different venue -- the common noise case.
                    self.stats.skipped += 1
                    continue
                kept.append(record)
                if len(kept) >= rows:
                    break

            total = message.get("total-results")
            offset += len(items)
            if isinstance(total, int) and offset >= total:
                break  # exhausted the result set
            if len(items) < page:
                break  # short page: no more data

        self.stats.returned = len(kept)
        return kept

    # -- lookups ----------------------------------------------------------- #
    def get_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Fetch the raw Crossref record for ``doi``, or ``None`` when absent.

        Returns the raw record (not a normalized paper) so the caller can pass it
        to :meth:`normalize` with its own venue/year context; an absent DOI is a
        normal ``404`` and maps to ``None``.
        """
        bare = normalize_doi(doi)
        if not bare:
            return None
        payload = self.http.get_json(f"{CROSSREF_API}/works/{quote(bare, safe='/')}", None)
        if not isinstance(payload, Mapping):
            return None
        message = payload.get("message")
        return dict(message) if isinstance(message, Mapping) else None

    # -- normalization ----------------------------------------------------- #
    def normalize(self, record: Mapping[str, Any], venue: str, year: int) -> dict[str, Any]:
        """Map a Crossref record onto ``db.upsert_paper``'s paper-dict contract.

        ``id`` is ``crossref:<doi>`` (lowercased) so re-enriching the same DOI is
        idempotent. A record without a DOI cannot be addressed deterministically
        and is rejected as malformed.

        The abstract is JATS/XML markup in many deposits, so it is passed through
        :func:`clean_markup_text`; publishing the raw ``<jats:p>&lt;</jats:p>``
        string would corrupt both FTS search and every abstract preview.
        """
        if not isinstance(record, Mapping):
            raise MalformedWorkError("Crossref record is not a mapping")

        doi = normalize_doi(record.get("DOI"))
        if not doi:
            raise MalformedWorkError("Crossref record has no DOI; cannot address it")
        key = doi.lower()

        title = _clean_title(_first_str(record.get("title")))
        if not title:
            raise MalformedWorkError(f"Crossref record {doi} has no usable title")

        published_year = (
            _date_parts_year(record.get("issued"))
            or _date_parts_year(record.get("published"))
            or self._int_or_none(year)
        )
        requested_year = self._int_or_none(year)

        return {
            "id": f"crossref:{key}",
            "source": "crossref",
            "source_paper_id": doi,
            "forum_id": None,
            "title": title,
            "abstract": clean_markup_text(record.get("abstract")),
            "authors": self._authors(record.get("author")),
            "keywords": self._keywords(record.get("subject")),
            # Crossref has no venue-independent track/area concept; inventing one
            # from the container title would pollute the project's area taxonomy.
            "primary_area": None,
            "pdf_url": self._pdf_url(record.get("link")),
            "paper_url": self._paper_url(record, doi),
            "doi": doi,
            "arxiv_id": arxiv_id_from_doi(doi),
            "submission_year": requested_year or published_year,
            "publication_year": published_year,
            "status": "published",
            "decision": None,
            "created_at_source": self._date_time(record.get("created")),
            "updated_at_source": self._date_time(record.get("deposited")),
            "raw_data_path": None,
            # --- provenance extras (not DB columns) ---
            "source_venue": venue or None,
            "container_title": _first_str(record.get("container-title")),
            "crossref_type": self._str_or_none(record.get("type")),
            "openalex_type": None,
        }

    # -- normalization helpers --------------------------------------------- #
    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        return OpenAlexProvider._int_or_none(value)

    @staticmethod
    def _str_or_none(value: Any) -> str | None:
        return OpenAlexProvider._str_or_none(value)

    @staticmethod
    def _date_time(value: Any) -> str | None:
        """ISO timestamp from a Crossref date object (``date-time`` preferred)."""
        date = _as_mapping(value)
        stamp = date.get("date-time")
        if isinstance(stamp, str) and stamp.strip():
            return stamp.strip()
        year = _date_parts_year(date)
        return f"{year:04d}-01-01T00:00:00Z" if year else None

    @staticmethod
    def _authors(authors: Any) -> list[str]:
        """Crossref authors as display names ("Given Family").

        Organisation-level contributors carry ``name`` instead of given/family and
        are kept verbatim -- dropping them would misattribute consortium papers.

        Crossref is one of the sources that spells a name "Family, Given", so
        the result goes through
        :func:`paper_ingest.normalize.normalize_author_name`: without it the
        same researcher appears under two identities in the corpus depending on
        which source delivered the paper.
        """
        names: list[str] = []
        for entry in _as_sequence(authors):
            entry_map = _as_mapping(entry)
            if isinstance(entry_map.get("name"), str) and entry_map["name"].strip():
                names.append(normalize.normalize_author_name(entry_map["name"]))
                continue
            given = entry_map.get("given")
            family = entry_map.get("family")
            parts = [
                _collapse_whitespace(part)
                for part in (given, family)
                if isinstance(part, str) and part.strip()
            ]
            if parts:
                names.append(normalize.normalize_author_name(" ".join(parts)))
        return names

    @staticmethod
    def _keywords(subject: Any) -> list[str]:
        """Crossref ``subject`` list as plain keywords, de-duplicated."""
        out: list[str] = []
        seen: set[str] = set()
        for entry in _as_sequence(subject):
            if not isinstance(entry, str) or not entry.strip():
                continue
            label = _collapse_whitespace(entry)
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(label)
        return out

    @staticmethod
    def _pdf_url(link: Any) -> str | None:
        """First full-text PDF among Crossref ``link`` entries.

        ``content-type`` is checked because the list also carries XML/HTML
        full-text links, and a PDF column holding an HTML URL breaks downloaders.
        """
        for entry in _as_sequence(link):
            entry_map = _as_mapping(entry)
            url = entry_map.get("URL")
            content_type = entry_map.get("content-type")
            if not isinstance(url, str) or not url.strip():
                continue
            if isinstance(content_type, str) and "pdf" in content_type.lower():
                return url.strip()
        return None

    @staticmethod
    def _paper_url(record: Mapping[str, Any], doi: str) -> str | None:
        url = record.get("URL")
        if isinstance(url, str) and url.strip():
            return url.strip()
        return f"https://doi.org/{doi}"
