"""Shared crawl policy: failures, pacing, backoff, breakers, and a recorder.

Why this module exists
----------------------
Four modules in this package already answer the same three questions -- *is this
failure retryable?*, *how long do we wait before trying again?*, and *when do we
give up on this source?* -- and each grew its own answer:

* :class:`paper_ingest.providers_openalex.PoliteHttp` retries ``429``/``5xx``/
  transport errors, honours ``Retry-After`` (clamped to ``backoff_max``) and caps
  concurrency with a semaphore.
* :meth:`paper_ingest.official.PublicSource.get` retries up to six times,
  rebuilds a poisoned keep-alive connection after a transport error, and backs
  off ``min(2**attempt, 30)``. The rebuild is not optional: the ACL Anthology
  closes keep-alive after roughly six requests, and ACL 2023 died at 812 papers
  with ``ConnectError: [WinError 10054]`` when a dropped socket was reused.
* :mod:`paper_ingest.journal_abstracts` runs two lanes with *different* fan-outs
  (Europe PMC at 8 workers, the publisher landing page at 2) because the
  publisher answers a burst with a "Client Challenge" page -- measured success
  35% at 2 workers and 0% at 10.
* :mod:`paper_ingest.harvest` orchestrates providers and records per-provider
  outcomes.

Each copy is slightly different, and the differences are where the bugs live:
one honours ``Retry-After`` and one does not, one caps the delay and one does
not, one rebuilds the client and one does not. This module states those
decisions **once**, so the next provider (arXiv, DBLP, an OpenReview API)
inherits a reviewed policy instead of growing a fifth near-copy.

What is deliberately *not* here
-------------------------------
* **No HTTP client.** ``httpx`` is already a dependency, but every entry point
  here accepts any object with a ``.get(url, params=...)`` method, so a provider
  can pass the pooled client it already owns and a test can pass a double.
* **No provider knowledge.** Nothing here knows what an OpenAlex work is.
* **No scraping, no UA spoofing, no bypasses.** "Polite" here means pacing,
  backoff, honest identification, and giving up loudly -- the same policy the
  rest of the collector follows (see :mod:`paper_ingest.harvest`).

The shared vocabulary
---------------------
Three error classes replace the "was it a 4xx or a 5xx?" checks that every
provider used to open-code:

* :class:`TransientError` -- try again (timeout, connection reset, ``429``,
  ``5xx``).
* :class:`PermanentError` -- do not try again; the request itself is wrong
  (``400``, ``404``, a malformed payload).
* :class:`BlockedError` -- the server refused us (``403``, a bot-challenge page,
  ``401`` without an API key). Retrying a refusal harder is exactly the
  behaviour that gets an IP banned, so it is a distinct, loud outcome.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

import httpx

__all__ = [
    "BlockedError",
    "CircuitBreaker",
    "CrawlError",
    "CrawlRecorder",
    "PermanentError",
    "RateLimiter",
    "RetryPolicy",
    "TransientError",
    "classify_status",
    "fetch_with_policy",
    "polite_user_agent",
]

#: The purpose string every request declares. Kept short and truthful on
#: purpose: an honest "academic metadata" claim is what earns the polite pool
#: on OpenAlex/Crossref, and a browser impersonation would be both untrue and
#: counterproductive (spoofing puts the client in the *anonymous* pool, which
#: has the lower limit).
USER_AGENT_PURPOSE = "academic metadata"


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #
class CrawlError(RuntimeError):
    """Base class for every crawl failure this package can talk about.

    A :class:`RuntimeError` and not a bare ``Exception``: a crawl failure is a
    runtime condition of the *remote* side, not a programming error, and
    callers routinely catch it separately from real bugs.
    """


class TransientError(CrawlError):
    """A failure that is worth trying again.

    Timeouts, connection resets, ``429 Too Many Requests`` and ``5xx``. The
    server is complaining about *now*, not about the request: waiting and
    retrying is the correct response, and giving up too early is what turned a
    five-minute hiccup into a failed venue-year.
    """


class PermanentError(CrawlError):
    """A failure that retrying cannot fix.

    ``404``/``410`` (the entity is not there), ``400`` (our query is wrong), a
    malformed payload. Repeating the identical request would fail identically
    and only burn the retry budget, so the caller is told immediately.
    """


class BlockedError(CrawlError):
    """The server refused us.

    ``403``, a bot-challenge page, or a ``401`` that means "no valid key".
    This is deliberately *not* a :class:`TransientError`: hammering a refusal
    is what turns a temporary block into a permanent IP ban, and the polite
    answer is to stop, report it, and let the orchestrator try another source.
    """


def classify_status(status_code: int) -> type[CrawlError] | None:
    """Map an HTTP status onto the error class that describes it.

    ``None`` means "this response is fine" (any ``2xx``). The mapping is the
    one every provider open-coded, with the reasoning kept:

    * ``429`` and ``5xx`` -> :class:`TransientError` -- the server is busy or
      broken *now*; wait and try again.
    * ``401``/``403`` -> :class:`BlockedError` -- we were refused. Retrying a
      refusal is how an IP gets banned. (``401`` without an API key is the
      OpenAlex per-IP daily budget running out; it looks like an auth error
      but the fix is a free key, not a retry.)
    * ``404``/``410`` and every other ``4xx`` -> :class:`PermanentError` -- our
      request is wrong, not the server's mood.

    A ``3xx`` that reaches us means the client did *not* follow the redirect
    (``httpx`` only follows when asked to). Repeating the same URL returns the
    same redirect, so it is treated as :class:`PermanentError` rather than
    silently retried.
    """
    code = int(status_code)
    if 200 <= code < 300:
        return None
    if code == 429 or code >= 500:
        return TransientError
    if code in (401, 403):
        return BlockedError
    if code in (404, 410):
        return PermanentError
    if 400 <= code < 500:
        return PermanentError
    return PermanentError  # 3xx that the client did not follow


# --------------------------------------------------------------------------- #
# Pacing                                                                      #
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Token bucket that paces the requests sent to **one** host.

    Why a bucket rather than ``sleep(1 / rate_per_s)`` between requests: a
    plain sleep cannot express a burst, so a client that wants "five requests
    immediately, then two per second" ends up either too slow at the start or
    flooding at the start. The bucket holds up to ``burst`` tokens and refills
    at ``rate_per_s``; a request takes one token.

    Use one instance per host. Sharing one across hosts would let a busy host
    spend the other host's budget -- and, more importantly, would make the
    per-host rate meaningless, which is the whole point of the polite pool.

    Thread-safe: several venue-years are synced from a thread pool, and each
    of them must be paced, not just the first.

    The clock and the sleep are injectable so tests can assert the pacing
    without spending wall-clock time.
    """

    def __init__(
        self,
        rate_per_s: float,
        burst: int = 1,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        rate = float(rate_per_s)
        if rate <= 0:
            raise ValueError(f"rate_per_s must be positive, got {rate_per_s!r}")
        self.rate_per_s = rate
        self.burst = max(1, int(burst))
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        # The bucket starts full: a burst may be spent immediately.
        self._tokens = float(self.burst)
        self._updated = clock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available; return the seconds waited.

        A request that cannot be served right now *reserves* the tokens it
        needs (the bucket goes into debt) and sleeps exactly long enough to
        earn them back. Reserving rather than re-checking keeps several
        threads from all waking up to claim the same single token, and it makes
        the wait exact instead of approximate.
        """
        need = float(tokens)
        if need <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            elapsed = now - self._updated
            if elapsed > 0:
                self._tokens = min(
                    float(self.burst), self._tokens + elapsed * self.rate_per_s
                )
                self._updated = now
            if self._tokens >= need:
                self._tokens -= need
                return 0.0
            deficit = need - self._tokens
            self._tokens -= need
            wait = deficit / self.rate_per_s
        # Sleep outside the lock: a thread holding the lock while sleeping
        # would serialise the waiters behind it and add their wait to its own.
        self._sleep(wait)
        return wait


# --------------------------------------------------------------------------- #
# Backoff                                                                     #
# --------------------------------------------------------------------------- #
class RetryPolicy:
    """Exponential backoff with jitter, and an optional server-supplied delay.

    The shape of the policy used by every provider in this package, stated once:

    * ``attempts`` -- the **total** number of tries, the first one included
      (the same meaning as ``PoliteHttp``'s ``max_attempts``).
    * ``base_delay`` -- the first wait; each further wait doubles it.
    * ``max_delay`` -- the cap on the computed wait.
    * ``jitter`` -- the fraction of the computed wait that is randomised
      upwards, so a fleet of workers that all failed at the same moment does
      not come back in lockstep and trip the rate limiter again.
    * ``retry_after`` -- whether a server-provided ``Retry-After`` wins over the
      computed backoff. It should: the server knows when its budget resets.

    The random source is injectable so a test can assert exact delays.
    """

    def __init__(
        self,
        attempts: int = 5,
        base_delay: float = 0.5,
        max_delay: float = 30.0,
        jitter: float = 0.25,
        retry_after: bool = True,
        *,
        clamp_retry_after: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self.attempts = max(1, int(attempts))
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(0.0, float(max_delay))
        self.jitter = max(0.0, float(jitter))
        self.retry_after = bool(retry_after)
        # A server-supplied Retry-After is honoured, but only up to max_delay.
        # The other collector in this package already clamps it, for a reason:
        # a hostile or broken value (a date far in the future, a retry-after of
        # a million seconds) would otherwise stall a sync indefinitely. The
        # clamp is the default so every caller inherits the safe behaviour.
        self.clamp_retry_after = bool(clamp_retry_after)
        self._rng = rng if rng is not None else random.Random()

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before the attempt that follows ``attempt``.

        ``attempt`` is zero-based: ``delay_for(0)`` is the wait after the first
        failure, ``delay_for(1)`` the wait after the second.

        A server-provided ``retry_after`` is honoured (when this policy accepts
        it): the server asked for a specific pause and obeying it is what keeps
        us in its polite pool. It is still capped at ``max_delay`` unless
        ``clamp_retry_after`` is turned off, so a broken header cannot stall a
        sync. The locally computed backoff is always capped.
        """
        if self.retry_after and retry_after is not None:
            value = max(0.0, float(retry_after))
            return min(value, self.max_delay) if self.clamp_retry_after else value
        # The exponent is bounded so a silly attempt number cannot build a
        # 1000-digit integer before the cap is applied.
        exponent = min(max(0, int(attempt)), 63)
        base = min(self.max_delay, self.base_delay * (2 ** exponent))
        if self.jitter:
            base += base * self.jitter * self._rng.random()
        # The cap is absolute: a delay longer than max_delay is never handed
        # out, jitter or not.
        return min(self.max_delay, base)


# --------------------------------------------------------------------------- #
# Giving up                                                                   #
# --------------------------------------------------------------------------- #
class CircuitBreaker:
    """Proportional breaker over a sliding window of attempts.

    Why proportional and not "N failures in a row": the refusals in this
    collection are *intermittent*. Nature's news pages answer with an abstract
    sometimes and with a "Client Challenge" page other times, so a
    consecutive-miss counter is reset by a single success and the lane grinds
    through the batch at ~1 paper per 10 seconds, recovering nothing (measured:
    35% success at 2 workers, 0% at 10). A hit *rate* over a window sees the
    collapse for what it is.

    The breaker only trips once ``window`` attempts have been seen, so a short
    batch never trips it -- a 10-page run that fails 3 pages is a bad run, not
    a dead source.

    Once tripped it stays tripped until :meth:`reset` is called: a breaker that
    closed itself again on the next lucky hit would flap.
    """

    def __init__(self, min_hit_rate: float = 0.25, window: int = 50) -> None:
        self.min_hit_rate = float(min_hit_rate)
        self.window = max(1, int(window))
        self._samples: deque[bool] = deque(maxlen=self.window)
        self._tripped = False
        self._lock = threading.Lock()
        #: How many times this breaker has tripped. Reset by :meth:`reset`.
        self.trips = 0

    @property
    def hit_rate(self) -> float | None:
        """Hit rate over the current window, or ``None`` if nothing was seen."""
        with self._lock:
            if not self._samples:
                return None
            return sum(1 for hit in self._samples if hit) / len(self._samples)

    def record(self, hit: bool) -> bool:
        """Record one attempt; return whether the breaker is tripped.

        ``hit`` is whether the attempt produced what we wanted. The return
        value is the breaker's state *after* the record, so a caller can write
        ``if breaker.record(ok): stop_the_lane()``.
        """
        with self._lock:
            self._samples.append(bool(hit))
            if len(self._samples) >= self.window and not self._tripped:
                rate = sum(1 for sample in self._samples if sample) / len(self._samples)
                if rate < self.min_hit_rate:
                    self._tripped = True
                    self.trips += 1
            return self._tripped

    def reset(self) -> None:
        """Return the breaker to its initial state.

        Everything is forgotten -- the window, the tripped flag and the trip
        counter -- so the counter describes the current batch. A caller that
        wants a lifetime count keeps its own.
        """
        with self._lock:
            self._samples.clear()
            self._tripped = False
            self.trips = 0


# --------------------------------------------------------------------------- #
# Identity                                                                    #
# --------------------------------------------------------------------------- #
def polite_user_agent(
    project: str = "PaperGraph",
    version: str = "0.2",
    mailto: str | None = None,
) -> str:
    """Build the honest ``User-Agent`` string every request should carry.

    The shape is ``Project/version (purpose; mailto:address)``. The contact
    address is what earns the polite pool's higher limits on OpenAlex and
    Crossref, and it is the difference between "works reliably" and "works
    until the neighbours use up the shared budget".

    **Without a contact address the ``mailto`` part is omitted entirely.**
    Inventing one (a placeholder, a made-up ``example.org``) would be a lie in
    a header whose whole purpose is to be true, and an operator reading the
    logs would have no way to tell a placeholder from a real mailbox. A UA
    without a contact is merely less privileged; a UA with a fake contact is
    a lie.
    """
    name = (project or "").strip() or "PaperGraph"
    release = (version or "").strip() or "0.2"
    contact = (mailto or "").strip().replace("\r", "").replace("\n", "")
    head = f"{name}/{release} ({USER_AGENT_PURPOSE}"
    if contact:
        head += f"; mailto:{contact}"
    return head + ")"


# --------------------------------------------------------------------------- #
# Recording                                                                   #
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    """Coerce ``value`` into something ``json.dumps`` accepts.

    The recorder is a reporting tool, so its output has to survive
    ``json.dumps`` even when a caller passes something exotic (a response
    object, a ``datetime``). Anything that is not a JSON primitive becomes its
    ``repr``, which is still readable in a report.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return repr(value)


class CrawlRecorder:
    """A tiny structured recorder, so a run can be reported after the fact.

    What it keeps:

    * ``total`` -- how many events were recorded;
    * ``kinds`` -- a counter per event kind;
    * ``durations`` -- the summed ``seconds=`` per kind (a kind that never
      carried a duration is simply absent);
    * ``recent`` -- the last ``keep`` events, so a report can show what
      happened just before a failure.

    The tail is bounded on purpose: a venue-year run records thousands of
    events, and a report that kept all of them would be a memory leak with a
    JSON dump at the end.

    Thread-safe: providers record from a thread pool.
    """

    def __init__(self, name: str, *, keep: int = 50) -> None:
        self.name = name
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()
        self._total = 0
        self._counts: dict[str, int] = {}
        self._durations: dict[str, float] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=self.keep)

    def event(self, kind: str, **fields: Any) -> None:
        """Record one event of ``kind`` with arbitrary extra ``fields``.

        A ``seconds=`` field is added to that kind's duration total, which is
        how a run reports where its time went.
        """
        record: dict[str, Any] = {"kind": kind}
        record.update({key: _jsonable(value) for key, value in fields.items()})
        seconds = fields.get("seconds")
        with self._lock:
            self._total += 1
            self._counts[kind] = self._counts.get(kind, 0) + 1
            if isinstance(seconds, bool):
                pass  # a bool is not a duration
            elif isinstance(seconds, (int, float)):
                self._durations[kind] = self._durations.get(kind, 0.0) + float(seconds)
            self._events.append(record)

    def stats(self) -> dict[str, Any]:
        """A JSON-serialisable summary of everything recorded so far."""
        with self._lock:
            return {
                "name": self.name,
                "total": self._total,
                "kinds": dict(self._counts),
                "durations": dict(self._durations),
                "recent": [dict(event) for event in self._events],
            }


# --------------------------------------------------------------------------- #
# The convenience wrapper                                                      #
# --------------------------------------------------------------------------- #
def _parse_retry_after(value: str | None) -> float | None:
    """Read a ``Retry-After`` header as a delay in seconds.

    The header carries *either* a delta-seconds integer *or* an HTTP-date, and
    both shapes occur in the wild. Anything unparseable returns ``None`` so
    the caller falls back to exponential backoff instead of sleeping for a
    garbage duration.
    """
    if not value:
        return None
    text = str(value).strip()
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


def _retry_after_seconds(response: Any) -> float | None:
    """The ``Retry-After`` delay of ``response``, if it carries one.

    Duck-typed on purpose: a response may be an ``httpx.Response`` (whose
    headers are case-insensitive) or a test double with a plain ``dict``.
    """
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw: Any = None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        try:
            raw = headers.get("retry-after")
        except AttributeError:
            return None
    if raw is None:
        return None
    return _parse_retry_after(str(raw))


def _body_snippet(response: Any, limit: int = 200) -> str:
    """A short, single-line excerpt of the response body, for error messages.

    Worth carrying: the interesting failure bodies are short and specific
    ("Client Challenge", "rate limit exceeded", "Invalid API key"), and an
    error message that only says "HTTP 403" sends the operator looking.
    """
    text = getattr(response, "text", "")
    if not isinstance(text, str):
        return ""
    snippet = " ".join(text.split())
    if not snippet:
        return ""
    if len(snippet) > limit:
        snippet = snippet[:limit] + "..."
    return f" -- {snippet}"


def fetch_with_policy(
    client: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    policy: RetryPolicy | None = None,
    limiter: RateLimiter | None = None,
    classify: Callable[[int], type[CrawlError] | None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> httpx.Response:
    """GET ``url`` under a retry policy and return the response.

    The attempt loop:

    * a transport error (timeout, connection reset) is a
      :class:`TransientError`;
    * a status that ``classify`` maps to :class:`TransientError` is retried
      after ``policy.delay_for(attempt, retry_after)``;
    * :class:`BlockedError` and :class:`PermanentError` are raised immediately
      -- there is nothing a retry could change, and hammering a refusal is
      what gets an IP banned;
    * after ``policy.attempts`` tries the **last** error is raised, so an
      exhausted budget is a loud, described failure rather than a quietly empty
      result.

    ``client`` is duck-typed: anything with ``.get(url, params=...)`` works,
    which is what lets a provider pass the pooled ``httpx.Client`` it already
    owns and a test pass a scripted double.

    ``limiter`` is asked for a token before *every* attempt, retries included:
    a retry is a request too, and not pacing it is how a rate limit turns into
    a ban.

    ``sleep`` is injectable for tests; it defaults to :func:`time.sleep`.
    """
    active = policy if policy is not None else RetryPolicy()
    decide = classify if classify is not None else classify_status
    wait = sleep if sleep is not None else time.sleep
    attempts = max(1, active.attempts)
    last_error: CrawlError | None = None

    for attempt in range(attempts):
        if limiter is not None:
            limiter.acquire()

        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:
            # A timeout or a dropped connection is the server's mood, not our
            # request: retry after a backoff.
            last_error = TransientError(f"{url}: {type(exc).__name__}: {exc}")
            if attempt + 1 >= attempts:
                raise last_error from exc
            wait(active.delay_for(attempt))
            continue

        status = int(getattr(response, "status_code", 200))
        error_cls = decide(status)
        if error_cls is None:
            return response

        last_error = error_cls(f"{url}: HTTP {status}{_body_snippet(response)}")
        if issubclass(error_cls, TransientError):
            if attempt + 1 >= attempts:
                raise last_error
            wait(active.delay_for(attempt, _retry_after_seconds(response)))
            continue
        # Blocked or permanent: raise now.
        raise last_error

    if last_error is not None:  # pragma: no cover - the loop always returns or raises
        raise last_error
    raise TransientError(f"{url}: no attempt was made")  # pragma: no cover
