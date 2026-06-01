"""In-memory conversation store for Responses API ``previous_response_id`` chaining.

The Responses API is stateful: a client sends only new input plus
``previous_response_id``, and the server replays the prior transcript. Since the
converter sits in front of a stateless ``/v1/chat/completions`` upstream, it must
emulate that store itself. We persist the accumulated Chat Completions
``messages`` array (the whole transcript up to and including a turn) keyed by the
``resp_<id>`` we mint, so a follow-up can resolve it and prepend the history.

This is a single-process, in-memory store: entries are lost on restart and
bounded by a TTL, an entry-count LRU cap, AND a total-byte budget (NOT the 30-day
OpenAI semantics). It can be swapped for a persistent backend later without
changing the route logic.

Memory model: the route DELETES a ``previous_response_id`` once a follow-up
successfully chains off it (see :meth:`delete`), so a linear conversation only
ever retains its latest transcript — keeping total memory O(N) instead of the
O(N^2) that re-storing the full prefix under a fresh id per turn would incur.
This trades away Responses *branching* (chaining two different follow-ups off one
shared ``previous_response_id``): the second reuse will 404. The byte budget is a
safety net for many concurrent chains and for image-heavy transcripts (base64
data URLs), which an entry-count cap alone does not bound.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import OrderedDict
from typing import Optional


class ConversationStore:
    """Thread-safe TTL + LRU map of ``resp_<id>`` → accumulated chat messages,
    bounded by both an entry-count cap and a total-byte budget."""

    def __init__(self, ttl_seconds: float, max_entries: int, max_bytes: int = 0) -> None:
        self._ttl = ttl_seconds
        self._max = max(1, max_entries)
        self._max_bytes = max(0, max_bytes)  # 0 disables the byte budget
        # resp_id -> (stored_at, messages, approx_bytes)
        self._data: "OrderedDict[str, tuple[float, list[dict], int]]" = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.Lock()

    def _is_expired(self, ts: float, now: float) -> bool:
        return self._ttl > 0 and (now - ts) > self._ttl

    @staticmethod
    def _sizeof(messages: list[dict]) -> int:
        """Approximate serialized byte size used for the byte budget."""
        try:
            return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return 0

    def _forget(self, resp_id: str) -> None:
        """Drop an entry and decrement the running byte total. Caller holds the lock."""
        entry = self._data.pop(resp_id, None)
        if entry is not None:
            self._total_bytes -= entry[2]

    def get(self, resp_id: str) -> Optional[list[dict]]:
        """Return a deep copy of the stored transcript, or None if absent/expired."""
        now = time.time()
        with self._lock:
            entry = self._data.get(resp_id)
            if entry is None:
                return None
            ts, messages, _ = entry
            if self._is_expired(ts, now):
                self._forget(resp_id)
                return None
            self._data.move_to_end(resp_id)
            return copy.deepcopy(messages)

    def put(self, resp_id: str, messages: list[dict]) -> None:
        """Store a deep copy of the transcript under ``resp_id`` and prune."""
        with self._lock:
            # On replace, drop the previous byte count before adding the new one.
            self._forget(resp_id)
            nbytes = self._sizeof(messages)
            self._data[resp_id] = (time.time(), copy.deepcopy(messages), nbytes)
            self._total_bytes += nbytes
            self._data.move_to_end(resp_id)
            self._prune()

    def delete(self, resp_id: str) -> None:
        """Drop an entry if present (used to supersede a chained-from prev id)."""
        with self._lock:
            self._forget(resp_id)

    def _prune(self) -> None:
        # Entry-count cap (LRU). Expired entries are reaped lazily on access in
        # get(); we deliberately AVOID an O(n) full-map TTL scan here because
        # _prune runs under the global lock on every put and a scan would
        # serialize all concurrent requests proportionally to the store size.
        while len(self._data) > self._max:
            _, entry = self._data.popitem(last=False)  # evict least-recently-used
            self._total_bytes -= entry[2]
        # Byte budget (LRU). Keep at least the most-recently-used entry so a put
        # is never wiped out by its own size accounting (a single transcript may
        # exceed the budget; it then stays, slightly over, rather than vanishing).
        if self._max_bytes > 0:
            while self._total_bytes > self._max_bytes and len(self._data) > 1:
                _, entry = self._data.popitem(last=False)
                self._total_bytes -= entry[2]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._total_bytes = 0

    def __len__(self) -> int:  # for tests/diagnostics
        with self._lock:
            return len(self._data)


def _build_store() -> ConversationStore:
    from app.config import settings

    return ConversationStore(
        ttl_seconds=settings.response_store_ttl,
        max_entries=settings.response_store_max,
        max_bytes=settings.response_store_max_bytes,
    )


# Module-level singleton used by the route. Built lazily so tests can adjust env
# first via reset().
conversation_store = _build_store()


def reset() -> None:
    """Rebuild the singleton from current env (test helper)."""
    global conversation_store
    conversation_store = _build_store()
