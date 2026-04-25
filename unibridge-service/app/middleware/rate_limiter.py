"""Rate limiting and concurrent query limiting helpers.

JWT users are rate-limited after authentication in the query endpoint so
unverified token claims cannot consume another user's quota. This middleware
only handles APISIX-forwarded API key consumers.
"""
from __future__ import annotations

import math
import threading
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class RateLimiter:
    """In-memory sliding window rate limiter + concurrent query tracker."""

    def __init__(self, rate_limit: int = 60, max_concurrent: int = 5) -> None:
        self._rate_limit = rate_limit
        self._max_concurrent = max_concurrent
        self._requests: dict[str, list[float]] = {}
        self._concurrent: dict[str, int] = {}
        self._lock = threading.Lock()

    def update_limits(self, rate_limit: int | None = None, max_concurrent: int | None = None) -> None:
        with self._lock:
            if rate_limit is not None:
                self._rate_limit = rate_limit
            if max_concurrent is not None:
                self._max_concurrent = max_concurrent

    def check_rate_limit(self, username: str) -> tuple[bool, str, float]:
        """Check if the user is within rate limits.

        Returns (allowed, message, stamp) where stamp is the timestamp
        added to the bucket. Callers must pass this stamp to undo_rate_count()
        to remove exactly their own entry.
        """
        now = time.time()
        window_start = now - 60.0

        with self._lock:
            timestamps = self._requests.get(username, [])
            timestamps = [ts for ts in timestamps if ts > window_start]

            if len(timestamps) >= self._rate_limit:
                oldest = min(timestamps)
                retry_after = math.ceil(oldest + 60.0 - now)
                self._requests[username] = timestamps
                return False, f"Rate limit exceeded ({self._rate_limit}/min). Retry after {retry_after}s", 0.0

            timestamps.append(now)
            self._requests[username] = timestamps
            return True, "", now

    def undo_rate_count(self, username: str, stamp: float) -> None:
        """Remove a specific rate limit entry identified by its timestamp.

        Only removes the exact entry this request added, safe under
        concurrent access from multiple requests with the same username.
        """
        with self._lock:
            timestamps = self._requests.get(username, [])
            try:
                timestamps.remove(stamp)
            except ValueError:
                pass  # already expired or removed
            self._requests[username] = timestamps

    def try_acquire(self, username: str) -> bool:
        """Try to acquire a concurrent query slot."""
        with self._lock:
            current = self._concurrent.get(username, 0)
            if current >= self._max_concurrent:
                return False
            self._concurrent[username] = current + 1
            return True

    def release(self, username: str) -> None:
        """Release a concurrent query slot."""
        with self._lock:
            current = self._concurrent.get(username, 0)
            if current > 0:
                self._concurrent[username] = current - 1


# Module-level singleton
rate_limiter = RateLimiter()


def _extract_username(request: Request) -> str | None:
    """Extract the APISIX-authenticated API key consumer for pre-auth rate limiting."""
    # APISIX-forwarded API key user (header set by APISIX after key-auth)
    consumer = request.headers.get("x-consumer-username")
    if consumer:
        return f"apikey:{consumer}"

    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces rate limiting and concurrent query limits.

    Only applies to POST /query/execute.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method != "POST" or not request.url.path.rstrip("/").endswith("/query/execute"):
            return await call_next(request)

        username = _extract_username(request)
        if username is None:
            return await call_next(request)

        allowed, msg, stamp = rate_limiter.check_rate_limit(username)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": msg},
                headers={"Retry-After": str(60)},
            )

        # JWT request rate limiting and all concurrent limiting run post-auth
        # in the query endpoint to prevent forged tokens from occupying
        # another user's quota or slots. See query.py execute().
        response = await call_next(request)

        # Only undo on 401 (identity not recognized / forged token).
        # 403 = authenticated but not authorized (permission denied, wrong DB, etc.)
        # — those SHOULD consume rate limit to prevent abuse.
        if response.status_code == 401:
            rate_limiter.undo_rate_count(username, stamp)

        return response
