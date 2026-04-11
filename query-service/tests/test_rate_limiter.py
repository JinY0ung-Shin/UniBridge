"""Tests for rate limiting and concurrent query limiting."""
from __future__ import annotations

import pytest
import time

from app.middleware.rate_limiter import RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter(rate_limit=5, max_concurrent=2)


class TestRateLimiter:
    def test_allows_under_limit(self, limiter):
        for _ in range(5):
            allowed, msg, _ = limiter.check_rate_limit("user1")
            assert allowed is True

    def test_blocks_over_limit(self, limiter):
        for _ in range(5):
            limiter.check_rate_limit("user1")
        allowed, msg, _ = limiter.check_rate_limit("user1")
        assert allowed is False
        assert "rate limit" in msg.lower()

    def test_separate_users(self, limiter):
        for _ in range(5):
            limiter.check_rate_limit("user1")
        allowed, _, _ = limiter.check_rate_limit("user2")
        assert allowed is True

    def test_concurrent_acquire_release(self, limiter):
        assert limiter.try_acquire("user1") is True
        assert limiter.try_acquire("user1") is True
        assert limiter.try_acquire("user1") is False
        limiter.release("user1")
        assert limiter.try_acquire("user1") is True

    def test_concurrent_separate_users(self, limiter):
        assert limiter.try_acquire("user1") is True
        assert limiter.try_acquire("user1") is True
        assert limiter.try_acquire("user2") is True

    def test_expired_entries_cleaned(self, limiter):
        old_time = time.time() - 120
        limiter._requests["user1"] = [old_time] * 5
        allowed, _, _ = limiter.check_rate_limit("user1")
        assert allowed is True

    def test_update_limits(self, limiter):
        limiter.update_limits(rate_limit=2, max_concurrent=1)
        limiter.check_rate_limit("user1")
        limiter.check_rate_limit("user1")
        allowed, _, _ = limiter.check_rate_limit("user1")
        assert allowed is False
