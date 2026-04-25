"""Tests for rate limiting and concurrent query limiting."""
from __future__ import annotations

import pytest
import time
import jwt
from starlette.requests import Request

from app.middleware.rate_limiter import RateLimiter, _extract_username


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

    def test_undo_rate_count_removes_exact_stamp(self, limiter):
        """undo_rate_count removes only the specific stamp, not another request's entry."""
        _, _, stamp_a = limiter.check_rate_limit("user1")
        _, _, stamp_b = limiter.check_rate_limit("user1")

        limiter.undo_rate_count("user1", stamp_a)

        # stamp_b should still be there — 1 entry remains
        assert len(limiter._requests["user1"]) == 1
        assert limiter._requests["user1"][0] == stamp_b

    def test_undo_rate_count_does_not_affect_other_users(self, limiter):
        """Undoing one user's stamp must not touch another user's bucket."""
        _, _, stamp1 = limiter.check_rate_limit("user1")
        _, _, stamp2 = limiter.check_rate_limit("user2")

        limiter.undo_rate_count("user1", stamp1)

        assert len(limiter._requests["user1"]) == 0
        assert len(limiter._requests["user2"]) == 1

    def test_undo_nonexistent_stamp_is_noop(self, limiter):
        """Undoing a stamp that doesn't exist should not raise or corrupt state."""
        limiter.check_rate_limit("user1")
        limiter.undo_rate_count("user1", 0.0)  # bogus stamp
        assert len(limiter._requests["user1"]) == 1

    def test_401_undo_restores_capacity_after_forged_request(self, limiter):
        """Simulates the full forged-token flow: fill bucket, undo on 401, real user still has capacity."""
        stamps = []
        for _ in range(5):
            _, _, stamp = limiter.check_rate_limit("victim")
            stamps.append(stamp)

        # Bucket full
        allowed, _, _ = limiter.check_rate_limit("victim")
        assert allowed is False

        # Auth returns 401 for all 5 forged requests — undo each
        for s in stamps:
            limiter.undo_rate_count("victim", s)

        # Real user should now have full capacity restored
        allowed, _, _ = limiter.check_rate_limit("victim")
        assert allowed is True


def _request_with_authorization(value: str) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/query/execute",
        "headers": [(b"authorization", value.encode())],
    })


def test_extract_username_does_not_trust_unverified_bearer_claims():
    forged_token = jwt.encode(
        {"sub": "victim-user", "role": "admin"},
        "attacker-controlled-secret-for-testing",
        algorithm="HS256",
    )

    request = _request_with_authorization(f"Bearer {forged_token}")

    assert _extract_username(request) is None
