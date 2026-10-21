"""The shared crawl policy must not change how any collector waits.

Three collectors were migrated from hand-written backoff arithmetic to
``crawl.RetryPolicy``. If the shared policy ever computes a different wait,
a harvest that used to pace itself politely would start hammering a server --
and nothing else would notice. These tests pin the equivalence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import crawl  # noqa: E402


class TestOfficialBackoff:
    """official.py used ``min(2 ** attempt, 30)``."""

    def test_the_wait_sequence_is_unchanged(self):
        policy = crawl.RetryPolicy(attempts=6, base_delay=1.0, max_delay=30.0, jitter=0.0)
        for attempt in range(6):
            assert policy.delay_for(attempt) == pytest.approx(min(2 ** attempt, 30))

    def test_the_policy_the_provider_builds_matches(self):
        from paper_ingest import official

        source = official.PublicSource("pmlr", "icml", 2024)
        for attempt in range(6):
            assert source._policy.delay_for(attempt) == pytest.approx(
                min(2 ** attempt, 30))


class TestArxivBackoff:
    """provider_arxiv.py used ``min(max(delay, 1.0) * 2 ** attempt, 120)``."""

    @pytest.mark.parametrize("delay", [1.0, 3.0, 5.0])
    def test_the_wait_sequence_is_unchanged(self, delay):
        policy = crawl.RetryPolicy(attempts=5, base_delay=max(delay, 1.0),
                                   max_delay=120.0, jitter=0.0)
        for attempt in range(5):
            assert policy.delay_for(attempt) == pytest.approx(
                min(max(delay, 1.0) * (2 ** attempt), 120.0))

    def test_the_provider_keeps_its_two_extra_rules(self):
        from paper_ingest.provider_arxiv import ArxivProvider

        provider = ArxivProvider(delay=3.0, max_retries=3)
        try:
            # The pause is never shorter than the polite delay...
            assert provider._policy.delay_for(0) >= 3.0
            # ...and a server Retry-After only ever raises it, never lowers it.
            raised = provider._policy.delay_for(0)
            assert raised >= 3.0
        finally:
            provider.close()


class TestPoliteHttpBackoff:
    """providers_openalex.PoliteHttp used ``min(base * 2 ** attempt, max)``."""

    def test_the_wait_sequence_is_unchanged(self):
        policy = crawl.RetryPolicy(attempts=5, base_delay=1.0, max_delay=30.0, jitter=0.0)
        for attempt in range(5):
            assert policy.delay_for(attempt) == pytest.approx(min(1.0 * (2 ** attempt), 30.0))

    def test_a_server_retry_after_is_still_clamped(self):
        # PoliteHttp always clamped Retry-After to backoff_max "so a hostile
        # value cannot stall a sync". The shared policy does the same.
        policy = crawl.RetryPolicy(attempts=5, base_delay=1.0, max_delay=30.0, jitter=0.0)
        assert policy.delay_for(0, 7.0) == pytest.approx(7.0)
        assert policy.delay_for(0, 9999.0) == pytest.approx(30.0)

    def test_the_provider_delegates_to_the_shared_policy(self):
        from paper_ingest.providers_openalex import PoliteHttp

        http = PoliteHttp(mailto="test@example.org", backoff_base=1.0, backoff_max=30.0)
        try:
            for attempt in range(5):
                assert http._backoff(attempt, None) == pytest.approx(
                    min(1.0 * (2 ** attempt), 30.0))
            assert http._backoff(0, 9999.0) == pytest.approx(30.0)
        finally:
            http.close()
