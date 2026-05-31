"""In-memory conversation store for Responses API ``previous_response_id`` chaining.

The Responses API is stateful: a client sends only new input plus
``previous_response_id``, and the server replays the prior transcript. Since the
converter sits in front of a stateless ``/v1/chat/completions`` upstream, it must
emulate that store itself. We persist the accumulated Chat Completions
``messages`` array (the whole transcript up to and including a turn) keyed by the
``resp_<id>`` we mint, so a follow-up can resolve it and prepend the history.

This is a single-process, in-memory store: entries are lost on restart and
bounded by a TTL + max-entry LRU cap (NOT the 30-day OpenAI semantics). It can
be swapped for a persistent backend later without changing the route logic.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Optional


class ConversationStore:
    """Thread-safe TTL + LRU map of ``resp_<id>`` → accumulated chat messages."""

    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max(1, max_entries)
        self._data: "OrderedDict[str, tuple[float, list[dict]]]" = OrderedDict()
        self._lock = threading.Lock()

    def _is_expired(self, ts: float, now: float) -> bool:
        return self._ttl > 0 and (now - ts) > self._ttl

    def get(self, resp_id: str) -> Optional[list[dict]]:
        """Return a deep copy of the stored transcript, or None if absent/expired."""
        now = time.time()
        with self._lock:
            entry = self._data.get(resp_id)
            if entry is None:
                return None
            ts, messages = entry
            if self._is_expired(ts, now):
                del self._data[resp_id]
                return None
            self._data.move_to_end(resp_id)
            return copy.deepcopy(messages)

    def put(self, resp_id: str, messages: list[dict]) -> None:
        """Store a deep copy of the transcript under ``resp_id`` and prune."""
        now = time.time()
        with self._lock:
            self._data[resp_id] = (now, copy.deepcopy(messages))
            self._data.move_to_end(resp_id)
            self._prune(now)

    def _prune(self, now: float) -> None:
        if self._ttl > 0:
            stale = [k for k, (ts, _) in self._data.items() if (now - ts) > self._ttl]
            for k in stale:
                del self._data[k]
        while len(self._data) > self._max:
            self._data.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:  # for tests/diagnostics
        with self._lock:
            return len(self._data)


def _build_store() -> ConversationStore:
    from app.config import settings

    return ConversationStore(
        ttl_seconds=settings.response_store_ttl,
        max_entries=settings.response_store_max,
    )


# Module-level singleton used by the route. Built lazily so tests can adjust env
# first via reset().
conversation_store = _build_store()


def reset() -> None:
    """Rebuild the singleton from current env (test helper)."""
    global conversation_store
    conversation_store = _build_store()
