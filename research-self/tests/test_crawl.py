"""Offline tests for the shared crawl policy (:mod:`paper_ingest.crawl`).

Every test here runs with **no network access**. Time is faked: the rate
limiter and the retry policy take an injectable clock/sleep, so pacing and
backoff are asserted exactly and the suite still runs in milliseconds.

The point of the module is that *every* provider in this package shares one
answer to "is this failure retryable, how long do we wait, and when do we give
up". These tests pin that answer down, because the three existing copies of it
(``providers_openalex.PoliteHttp``, ``official.PublicSource.get``,
``journal_abstracts``' proportional breaker) already disagree in the small
ways that cost a venue-year.
"""
from __future__ import annotations

import json
import random
import threading
import time

import httpx
import pytest

from paper_ingest.crawl import (
    BlockedError,
    CircuitBreaker,
    CrawlError,
    CrawlRecorder,
    PermanentError,
    RateLimiter,
    RetryPolicy,
    TransientError,
    classify_status,
    fetch_with_policy,
    polite_user_agent,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #
class FakeClock:
    """A monotonic clock that only moves when something sleeps on it.

    The rate limiter reads the clock to refill its bucket and calls the sleep
    to wait, so advancing the clock inside ``sleep`` makes the limiter's
    behaviour exactly predictable -- and instantaneous.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    """The minimum a response has to look like for :func:`fetch_with_policy`."""

    def __init__(self, status_code: int = 200, text: str = "ok", headers=None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {})


class ScriptedClient:
    """A client that plays back a script of responses and exceptions.

    Duck-typed exactly like the real thing: ``.get(url, params=...)`` and
    nothing else. Each call is recorded so a test can assert how many
    requests were actually made.
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, object]] = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if not self.script:
            raise AssertionError("unexpected extra request")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class CountingLimiter:
    """A limiter stand-in that only counts how often it was asked for a token."""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


# --------------------------------------------------------------------------- #
# (a) status classification                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "status, expected",
    [
        (200, None),
        (204, None),
        (301, PermanentError),  # the client did not follow the redirect
        (400, PermanentError),
        (401, BlockedError),
        (403, BlockedError),
        (404, PermanentError),
        (410, PermanentError),
        (429, TransientError),
        (500, TransientError),
        (503, TransientError),
    ],
)
def test_classify_status_maps_every_status_to_the_right_class(status, expected):
    assert classify_status(status) is expected


def test_error_hierarchy_puts_every_failure_under_crawl_error():
    """One ``except CrawlError`` must catch the lot; the order carries meaning."""
    assert issubclass(CrawlError, RuntimeError)
    for error in (TransientError, PermanentError, BlockedError):
        assert issubclass(error, CrawlError)
    # The three are siblings: a blocker is not a transient failure.
    assert not issubclass(BlockedError, TransientError)
    assert not issubclass(PermanentError, TransientError)


# --------------------------------------------------------------------------- #
# (b) rate limiting                                                           #
# --------------------------------------------------------------------------- #
def test_rate_limiter_spends_the_burst_immediately_then_paces():
    """A burst may be spent at once; only the requests after it wait."""
    clock = FakeClock()
    limiter = RateLimiter(rate_per_s=2.0, burst=3, sleep=clock.sleep, clock=clock)

    waited = [limiter.acquire() for _ in range(3)]
    assert waited == [0.0, 0.0, 0.0]
    assert clock.sleeps == [], "the burst must not sleep"

    # The bucket is empty now: the next request waits for one token.
    waited = limiter.acquire()
    assert waited == pytest.approx(0.5)  # 1 token at 2 tokens/second
    assert clock.sleeps == [0.5]


def test_rate_limiter_paces_at_the_configured_rate():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_s=4.0, burst=1, sleep=clock.sleep, clock=clock)

    limiter.acquire()
    assert clock.now == 0.0  # the burst (1) was free
    limiter.acquire()
    assert clock.now == pytest.approx(0.25)  # one token at 4/second
    limiter.acquire()
    assert clock.now == pytest.approx(0.5)


def test_rate_limiter_rejects_a_nonsense_rate():
    with pytest.raises(ValueError):
        RateLimiter(rate_per_s=0)
    with pytest.raises(ValueError):
        RateLimiter(rate_per_s=-1.0)


def test_rate_limiter_is_thread_safe():
    """Several venue-years share one host's budget, so the limiter must hold."""
    limiter = RateLimiter(rate_per_s=500.0, burst=1)
    granted = []

    def worker() -> None:
        for _ in range(5):
            limiter.acquire()
            granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    assert len(granted) == 20
    # 20 requests at 500/s with a burst of 1 cannot finish faster than
    # 19/500 s; a limiter that forgot its rate would be far quicker.
    assert elapsed >= 19 / 500


# --------------------------------------------------------------------------- #
# (c) retry policy                                                            #
# --------------------------------------------------------------------------- #
def test_retry_policy_backoff_grows_exponentially():
    policy = RetryPolicy(attempts=5, base_delay=0.5, max_delay=30.0, jitter=0.0)
    assert policy.delay_for(0) == pytest.approx(0.5)
    assert policy.delay_for(1) == pytest.approx(1.0)
    assert policy.delay_for(2) == pytest.approx(2.0)
    assert policy.delay_for(3) == pytest.approx(4.0)


def test_retry_policy_caps_the_backoff():
    policy = RetryPolicy(attempts=5, base_delay=0.5, max_delay=30.0, jitter=0.0)
    assert policy.delay_for(20) == pytest.approx(30.0)
    assert policy.delay_for(63) <= 30.0


def test_retry_policy_jitter_stays_inside_the_documented_fraction():
    """Jitter breaks up a lockstep retry, but must never blow the budget."""
    policy = RetryPolicy(
        attempts=5, base_delay=1.0, max_delay=30.0, jitter=0.25,
        rng=random.Random(20261021),
    )
    for attempt in range(6):
        base = min(30.0, 1.0 * (2 ** attempt))
        for _ in range(50):
            delay = policy.delay_for(attempt)
            assert base <= delay <= base * 1.25
            assert delay <= 30.0


def test_retry_policy_is_deterministic_with_an_injected_rng():
    first = RetryPolicy(jitter=0.25, rng=random.Random(7))
    second = RetryPolicy(jitter=0.25, rng=random.Random(7))
    assert [first.delay_for(i) for i in range(5)] == [second.delay_for(i) for i in range(5)]


def test_retry_policy_honours_retry_after_and_it_takes_precedence():
    """The server knows when its budget resets; our opinion is second to that."""
    policy = RetryPolicy(attempts=5, base_delay=0.5, max_delay=30.0, jitter=0.0)
    assert policy.delay_for(0, retry_after=7.0) == pytest.approx(7.0)
    # It beats the exponential value for the same attempt.
    assert policy.delay_for(0, retry_after=7.0) != policy.delay_for(0)


def test_retry_policy_can_ignore_retry_after():
    policy = RetryPolicy(attempts=5, base_delay=0.5, jitter=0.0, retry_after=False)
    assert policy.delay_for(0, retry_after=7.0) == pytest.approx(0.5)


def test_retry_policy_clamps_a_hostile_retry_after():
    """A broken or hostile Retry-After must not stall a sync forever.

    The other collector in this package (providers_openalex.PoliteHttp) has
    always clamped the header to its backoff ceiling for exactly this reason,
    so the shared policy clamps it too -- the two now agree.
    """
    policy = RetryPolicy(attempts=5, base_delay=0.5, max_delay=30.0, jitter=0.0)
    assert policy.delay_for(0, retry_after=9999.0) == pytest.approx(30.0)
    # A value inside the ceiling is honoured as-is.
    assert policy.delay_for(0, retry_after=7.0) == pytest.approx(7.0)
    # The clamp can be turned off explicitly.
    unclamped = RetryPolicy(max_delay=30.0, jitter=0.0, clamp_retry_after=False)
    assert unclamped.delay_for(0, retry_after=9999.0) == pytest.approx(9999.0)


def test_retry_policy_normalises_its_arguments():
    assert RetryPolicy(attempts=0).attempts == 1  # at least one try
    assert RetryPolicy(base_delay=-5).base_delay == 0.0
    assert RetryPolicy(jitter=-1).jitter == 0.0


# --------------------------------------------------------------------------- #
# (d) circuit breaker                                                         #
# --------------------------------------------------------------------------- #
def test_breaker_does_not_trip_before_the_window_is_full():
    """A short batch that fails must not be mistaken for a dead source."""
    breaker = CircuitBreaker(min_hit_rate=0.25, window=10)
    for _ in range(9):
        assert breaker.record(False) is False
    assert breaker.trips == 0
    assert breaker.hit_rate == pytest.approx(0.0)


def test_breaker_trips_when_the_hit_rate_falls_below_the_threshold():
    breaker = CircuitBreaker(min_hit_rate=0.25, window=10)
    results = [breaker.record(False) for _ in range(10)]
    assert results[-1] is True  # it trips on the attempt that completes the window
    assert breaker.trips == 1


def test_breaker_never_trips_on_an_all_hit_window():
    breaker = CircuitBreaker(min_hit_rate=0.25, window=10)
    assert not any(breaker.record(True) for _ in range(10))
    assert breaker.trips == 0
    assert breaker.hit_rate == pytest.approx(1.0)


def test_breaker_uses_a_sliding_window():
    """Old results fall out of the window; the current rate is what counts."""
    breaker = CircuitBreaker(min_hit_rate=0.5, window=4)
    for hit in (True, True, True, False):
        assert breaker.record(hit) is False  # 3/4 == 0.75
    assert breaker.record(False) is False  # 2/4 == 0.50, still not below 0.50
    assert breaker.record(False) is True  # 1/4 == 0.25 -> tripped
    assert breaker.trips == 1


def test_breaker_stays_tripped_until_it_is_reset():
    breaker = CircuitBreaker(min_hit_rate=0.25, window=4)
    for _ in range(4):
        breaker.record(False)
    assert breaker.record(True) is True  # a lucky hit does not un-trip it

    breaker.reset()
    assert breaker.trips == 0
    assert breaker.hit_rate is None
    assert breaker.record(True) is False  # usable again


# --------------------------------------------------------------------------- #
# (e) recorder                                                                #
# --------------------------------------------------------------------------- #
def test_recorder_counts_events_by_kind():
    recorder = CrawlRecorder("openalex")
    recorder.event("fetch", url="https://example.org/a")
    recorder.event("fetch", url="https://example.org/b")
    recorder.event("parse")

    stats = recorder.stats()
    assert stats["name"] == "openalex"
    assert stats["total"] == 3
    assert stats["kinds"] == {"fetch": 2, "parse": 1}


def test_recorder_keeps_only_the_last_n_events():
    recorder = CrawlRecorder("acl", keep=3)
    for index in range(10):
        recorder.event("page", index=index)

    recent = recorder.stats()["recent"]
    assert len(recent) == 3
    assert [event["index"] for event in recent] == [7, 8, 9]
    assert recent[-1]["kind"] == "page"


def test_recorder_sums_seconds_per_kind():
    recorder = CrawlRecorder("timings")
    recorder.event("fetch", seconds=1.5)
    recorder.event("fetch", seconds=2.0)
    recorder.event("parse", seconds=0.25)
    recorder.event("parse")  # no duration: the kind is simply absent

    durations = recorder.stats()["durations"]
    assert durations["fetch"] == pytest.approx(3.5)
    assert durations["parse"] == pytest.approx(0.25)


def test_recorder_stats_are_json_serialisable():
    """A report has to survive ``json.dumps`` -- that is the whole point."""
    recorder = CrawlRecorder("json")
    recorder.event("fetch", seconds=0.5, status=200)
    recorder.event("fetch", seconds=0.5, status=200)

    payload = json.dumps(recorder.stats())
    decoded = json.loads(payload)
    assert decoded["total"] == 2
    assert decoded["durations"]["fetch"] == pytest.approx(1.0)


def test_recorder_coerces_a_value_that_json_cannot_take():
    recorder = CrawlRecorder("exotic")
    recorder.event("fetch", response=object())
    assert json.dumps(recorder.stats())  # must not raise


def test_recorder_is_thread_safe():
    recorder = CrawlRecorder("threads")
    threads = [
        threading.Thread(target=lambda: [recorder.event("fetch", seconds=0.1) for _ in range(50)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stats = recorder.stats()
    assert stats["total"] == 200
    assert stats["kinds"]["fetch"] == 200
    assert stats["durations"]["fetch"] == pytest.approx(20.0)


# --------------------------------------------------------------------------- #
# (f) honest identity                                                         #
# --------------------------------------------------------------------------- #
def test_polite_user_agent_carries_the_contact_when_given():
    agent = polite_user_agent(mailto="someone@example.org")
    assert agent == "PaperGraph/0.2 (academic metadata; mailto:someone@example.org)"


def test_polite_user_agent_omits_the_contact_when_there_is_none():
    agent = polite_user_agent()
    assert agent == "PaperGraph/0.2 (academic metadata)"
    assert "mailto" not in agent


def test_polite_user_agent_never_invents_an_address():
    """A fake contact in the UA is a lie in the one header that must be true."""
    for agent in (
        polite_user_agent(),
        polite_user_agent(mailto=""),
        polite_user_agent(mailto="   "),
        polite_user_agent(project="Other", version="1.0"),
    ):
        assert "@" not in agent
        assert "example" not in agent.lower()


def test_polite_user_agent_uses_the_given_project_and_version():
    agent = polite_user_agent(project="PaperGraph", version="0.3", mailto="a@b.org")
    assert agent.startswith("PaperGraph/0.3 (")


def test_polite_user_agent_is_not_a_browser_impersonation():
    agent = polite_user_agent(mailto="x@example.org")
    assert "Mozilla" not in agent
    assert "Chrome" not in agent


# --------------------------------------------------------------------------- #
# (g) the retrying fetch                                                      #
# --------------------------------------------------------------------------- #
def test_fetch_with_policy_retries_transient_failures_then_succeeds():
    client = ScriptedClient(
        [FakeResponse(503, "upstream is down"), FakeResponse(503, "down"), FakeResponse(200, "ok")]
    )
    sleeps: list[float] = []
    policy = RetryPolicy(attempts=5, base_delay=0.5, max_delay=30.0, jitter=0.0)

    response = fetch_with_policy(
        client, "https://example.org/paper", params={"q": "graph"},
        policy=policy, sleep=sleeps.append,
    )

    assert response.status_code == 200
    assert len(client.calls) == 3
    # The waits are the injected ones, and they grow: 0.5 s then 1.0 s.
    assert sleeps == [0.5, 1.0]
    # The parameters really were sent.
    assert client.calls[0] == ("https://example.org/paper", {"q": "graph"})


def test_fetch_with_policy_does_not_retry_a_blocked_client():
    """Retrying a refusal is how a temporary block becomes a permanent ban."""
    client = ScriptedClient([BlockedError("HTTP 403 -- Client Challenge")])

    with pytest.raises(BlockedError):
        fetch_with_policy(
            client, "https://example.org/paper",
            policy=RetryPolicy(attempts=5), sleep=lambda _s: None,
        )
    assert len(client.calls) == 1


def test_fetch_with_policy_raises_blocked_immediately_on_403():
    client = ScriptedClient([FakeResponse(403, "Client Challenge")])

    with pytest.raises(BlockedError):
        fetch_with_policy(client, "https://example.org/paper", sleep=lambda _s: None)
    assert len(client.calls) == 1


def test_fetch_with_policy_raises_permanent_immediately_on_404():
    client = ScriptedClient([FakeResponse(404, "not found")])

    with pytest.raises(PermanentError):
        fetch_with_policy(client, "https://example.org/paper", sleep=lambda _s: None)
    assert len(client.calls) == 1


def test_fetch_with_policy_exhausts_the_attempts_on_a_timeout():
    """An exhausted budget must be loud, not an empty result."""
    client = ScriptedClient([httpx.ReadTimeout("read timed out") for _ in range(5)])
    sleeps: list[float] = []
    policy = RetryPolicy(attempts=3, base_delay=0.5, jitter=0.0)

    with pytest.raises(TransientError) as excinfo:
        fetch_with_policy(
            client, "https://example.org/paper", policy=policy, sleep=sleeps.append
        )

    assert len(client.calls) == 3  # exactly policy.attempts tries, no more
    assert len(sleeps) == 2  # no sleep after the final attempt
    # The transport failure is preserved as the cause of the policy failure.
    assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)


def test_fetch_with_policy_honours_retry_after_on_a_429():
    client = ScriptedClient(
        [FakeResponse(429, "slow down", {"Retry-After": "7"}), FakeResponse(200, "ok")]
    )
    sleeps: list[float] = []

    response = fetch_with_policy(
        client, "https://example.org/paper",
        policy=RetryPolicy(attempts=3, base_delay=0.5, jitter=0.0), sleep=sleeps.append,
    )

    assert response.status_code == 200
    assert sleeps == [7.0]  # the server's number wins over our backoff


def test_fetch_with_policy_consults_the_limiter_before_every_attempt():
    """A retry is a request too; not pacing it is how a ban is earned."""
    client = ScriptedClient([FakeResponse(503, "down"), FakeResponse(200, "ok")])
    limiter = CountingLimiter()

    fetch_with_policy(
        client, "https://example.org/paper", limiter=limiter,
        policy=RetryPolicy(attempts=3, base_delay=0.0, jitter=0.0),
        sleep=lambda _s: None,
    )
    assert limiter.calls == 2  # one per attempt, retries included


def test_fetch_with_policy_uses_a_custom_classifier():
    """A source with its own rules can override the default mapping."""
    client = ScriptedClient([FakeResponse(418, "teapot"), FakeResponse(200, "ok")])

    def classify(status: int):
        return TransientError if status == 418 else classify_status(status)

    response = fetch_with_policy(
        client, "https://example.org/paper", classify=classify,
        policy=RetryPolicy(attempts=2, base_delay=0.0, jitter=0.0),
        sleep=lambda _s: None,
    )
    assert response.status_code == 200
    assert len(client.calls) == 2


def test_fetch_with_policy_returns_the_response_object_it_was_given():
    """The client is duck-typed; the response is passed straight through."""
    expected = FakeResponse(200, "body")
    client = ScriptedClient([expected])

    assert fetch_with_policy(client, "https://example.org/paper") is expected
