"""Unit tests for the in-memory conversation store."""

from __future__ import annotations

import time

from app.responses_state import ConversationStore


def test_put_get_roundtrip_is_isolated():
    store = ConversationStore(ttl_seconds=3600, max_entries=10)
    msgs = [{"role": "user", "content": "hi"}]
    store.put("resp_1", msgs)
    got = store.get("resp_1")
    assert got == msgs
    # Mutating the returned copy must not affect stored state.
    got.append({"role": "assistant", "content": "x"})
    assert store.get("resp_1") == msgs
    # Mutating the original after put must not affect stored state either.
    msgs.append({"role": "system", "content": "y"})
    assert store.get("resp_1") == [{"role": "user", "content": "hi"}]


def test_missing_key_returns_none():
    store = ConversationStore(ttl_seconds=3600, max_entries=10)
    assert store.get("nope") is None


def test_ttl_expiry():
    store = ConversationStore(ttl_seconds=0.05, max_entries=10)
    store.put("resp_1", [{"role": "user", "content": "hi"}])
    assert store.get("resp_1") is not None
    time.sleep(0.08)
    assert store.get("resp_1") is None


def test_lru_eviction_past_max():
    store = ConversationStore(ttl_seconds=3600, max_entries=2)
    store.put("a", [{"role": "user", "content": "a"}])
    store.put("b", [{"role": "user", "content": "b"}])
    store.get("a")  # touch a so b becomes least-recently-used
    store.put("c", [{"role": "user", "content": "c"}])
    assert len(store) == 2
    assert store.get("b") is None  # evicted
    assert store.get("a") is not None
    assert store.get("c") is not None


def test_delete_removes_entry_and_is_idempotent():
    store = ConversationStore(ttl_seconds=3600, max_entries=10)
    store.put("a", [{"role": "user", "content": "x"}])
    store.delete("a")
    assert store.get("a") is None
    store.delete("a")  # second delete must not raise
    assert len(store) == 0


def test_byte_budget_evicts_lru_but_keeps_latest():
    store = ConversationStore(ttl_seconds=3600, max_entries=100, max_bytes=500)
    big = [{"role": "user", "content": "z" * 300}]  # ~330 serialized bytes each
    store.put("a", big)
    store.put("b", big)  # total would exceed 500 → LRU 'a' evicted
    assert store.get("a") is None
    assert store.get("b") is not None
    assert len(store) == 1


def test_byte_budget_keeps_latest_even_if_oversized():
    store = ConversationStore(ttl_seconds=3600, max_entries=100, max_bytes=10)
    store.put("a", [{"role": "user", "content": "z" * 1000}])
    # A single entry larger than the whole budget must not be wiped out.
    assert store.get("a") is not None


def test_byte_total_decrements_on_delete_and_expiry():
    store = ConversationStore(ttl_seconds=3600, max_entries=100, max_bytes=10000)
    store.put("a", [{"role": "user", "content": "z" * 300}])
    store.put("b", [{"role": "user", "content": "z" * 300}])
    store.delete("a")
    # After freeing 'a', a fresh large put should not evict 'b' (budget restored).
    store.put("c", [{"role": "user", "content": "z" * 300}])
    assert store.get("b") is not None
    assert store.get("c") is not None
