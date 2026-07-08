"""Conversation store for Responses API ``previous_response_id`` chaining.

The Responses API is stateful: a client sends only new input plus
``previous_response_id``, and the server replays the prior transcript. Since the
converter sits in front of a stateless ``/v1/chat/completions`` upstream, it must
emulate that store itself. We persist the accumulated Chat Completions
``messages`` array (the whole transcript up to and including a turn) keyed by the
``resp_<id>`` we mint, so a follow-up can resolve it and prepend the history.

The default runtime can use a small SQLite database on a shared compose volume
so entries survive a converter restart and blue/green color swap. Tests and
single-process development can still use the in-memory implementation. Entries
are bounded by a TTL, an entry-count LRU cap, a total-byte budget, and an
optional per-entry byte cap (NOT the 30-day OpenAI semantics).

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
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Optional


class ConversationStore:
    """Thread-safe TTL + LRU map of ``resp_<id>`` → accumulated chat messages,
    bounded by both an entry-count cap and a total-byte budget."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        max_bytes: int = 0,
        max_entry_bytes: int = 0,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max(1, max_entries)
        self._max_bytes = max(0, max_bytes)  # 0 disables the byte budget
        self._max_entry_bytes = max(0, max_entry_bytes)  # 0 disables per-entry cap
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

    def put(self, resp_id: str, messages: list[dict]) -> bool:
        """Store a deep copy of the transcript under ``resp_id`` and prune.

        Returns False when the transcript exceeds the per-entry cap and is not
        stored. Callers should only delete a chained-from parent after True.
        """
        with self._lock:
            # On replace, drop the previous byte count before adding the new one.
            self._forget(resp_id)
            nbytes = self._sizeof(messages)
            if self._max_entry_bytes > 0 and nbytes > self._max_entry_bytes:
                return False
            self._data[resp_id] = (time.time(), copy.deepcopy(messages), nbytes)
            self._total_bytes += nbytes
            self._data.move_to_end(resp_id)
            self._prune()
            return True

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


class SQLiteConversationStore:
    """SQLite-backed variant with the same public API as ConversationStore."""

    def __init__(
        self,
        path: str,
        ttl_seconds: float,
        max_entries: int,
        max_bytes: int = 0,
        max_entry_bytes: int = 0,
    ) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._max = max(1, max_entries)
        self._max_bytes = max(0, max_bytes)
        self._max_entry_bytes = max(0, max_entry_bytes)
        self._lock = threading.Lock()

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                resp_id TEXT PRIMARY KEY,
                stored_at REAL NOT NULL,
                accessed_at REAL NOT NULL,
                messages TEXT NOT NULL,
                approx_bytes INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def _is_expired(self, ts: float, now: float) -> bool:
        return self._ttl > 0 and (now - ts) > self._ttl

    @staticmethod
    def _sizeof(messages: list[dict]) -> int:
        return ConversationStore._sizeof(messages)

    def _delete_expired_locked(self, now: float) -> None:
        if self._ttl > 0:
            self._conn.execute(
                "DELETE FROM conversations WHERE stored_at < ?",
                (now - self._ttl,),
            )

    def _prune_locked(self, now: float) -> None:
        self._delete_expired_locked(now)

        count = self._conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        overflow = count - self._max
        if overflow > 0:
            self._conn.execute(
                """
                DELETE FROM conversations
                WHERE resp_id IN (
                    SELECT resp_id FROM conversations
                    ORDER BY accessed_at ASC
                    LIMIT ?
                )
                """,
                (overflow,),
            )

        if self._max_bytes <= 0:
            return

        while True:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(approx_bytes), 0), COUNT(*) FROM conversations"
            ).fetchone()
            total, count = int(row[0]), int(row[1])
            if total <= self._max_bytes or count <= 1:
                return
            oldest = self._conn.execute(
                "SELECT resp_id FROM conversations ORDER BY accessed_at ASC LIMIT 1"
            ).fetchone()
            if oldest is None:
                return
            self._conn.execute(
                "DELETE FROM conversations WHERE resp_id = ?",
                (oldest[0],),
            )

    def get(self, resp_id: str) -> Optional[list[dict]]:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT stored_at, messages FROM conversations WHERE resp_id = ?",
                (resp_id,),
            ).fetchone()
            if row is None:
                return None
            stored_at, raw_messages = row
            if self._is_expired(float(stored_at), now):
                self._conn.execute(
                    "DELETE FROM conversations WHERE resp_id = ?",
                    (resp_id,),
                )
                self._conn.commit()
                return None
            try:
                messages = json.loads(raw_messages)
            except (TypeError, ValueError):
                self._conn.execute(
                    "DELETE FROM conversations WHERE resp_id = ?",
                    (resp_id,),
                )
                self._conn.commit()
                return None
            self._conn.execute(
                "UPDATE conversations SET accessed_at = ? WHERE resp_id = ?",
                (now, resp_id),
            )
            self._conn.commit()
            return messages if isinstance(messages, list) else None

    def put(self, resp_id: str, messages: list[dict]) -> bool:
        now = time.time()
        raw_messages = json.dumps(messages, ensure_ascii=False)
        nbytes = len(raw_messages.encode("utf-8"))
        if self._max_entry_bytes > 0 and nbytes > self._max_entry_bytes:
            return False

        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO conversations
                    (resp_id, stored_at, accessed_at, messages, approx_bytes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (resp_id, now, now, raw_messages, nbytes),
            )
            self._prune_locked(now)
            self._conn.commit()
            return True

    def delete(self, resp_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM conversations WHERE resp_id = ?", (resp_id,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM conversations")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __len__(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])


def _build_store() -> ConversationStore | SQLiteConversationStore:
    from app.config import settings

    store_path = settings.response_store_path
    kwargs = {
        "ttl_seconds": settings.response_store_ttl,
        "max_entries": settings.response_store_max,
        "max_bytes": settings.response_store_max_bytes,
        "max_entry_bytes": settings.response_store_max_entry_bytes,
    }
    if store_path:
        return SQLiteConversationStore(store_path, **kwargs)
    return ConversationStore(
        **kwargs,
    )


# Module-level singleton used by the route. Built lazily so tests can adjust env
# first via reset().
conversation_store = _build_store()


def reset() -> None:
    """Rebuild the singleton from current env (test helper)."""
    global conversation_store
    old_store = conversation_store
    close = getattr(old_store, "close", None)
    if callable(close):
        close()
    conversation_store = _build_store()
