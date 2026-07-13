"""Conversation-store corruption, cleanup, and construction boundaries."""

from __future__ import annotations

import time

import app.responses_state as state
from app.responses_state import ConversationStore, SQLiteConversationStore


def test_size_estimator_fails_closed_for_unserializable_messages():
    messages = [{"role": "user", "content": object()}]
    store = ConversationStore(ttl_seconds=3600, max_entries=10, max_bytes=100)
    assert store.put("resp_1", messages) is True
    assert store.get("resp_1")[0]["role"] == "user"


def test_sqlite_store_with_disabled_byte_budget_supports_delete_and_clear(tmp_path):
    store = SQLiteConversationStore(
        str(tmp_path / "store.sqlite"),
        ttl_seconds=3600,
        max_entries=10,
        max_bytes=0,
    )
    assert store.put("a", [{"role": "user", "content": "a"}]) is True
    assert store.put("b", [{"role": "user", "content": "b"}]) is True
    store.delete("a")
    assert store.get("a") is None
    assert len(store) == 1
    store.clear()
    assert len(store) == 0
    store.close()


def test_sqlite_store_drops_corrupt_json_row(tmp_path):
    store = SQLiteConversationStore(
        str(tmp_path / "store.sqlite"),
        ttl_seconds=3600,
        max_entries=10,
    )
    now = time.time()
    store._conn.execute(
        """
        INSERT INTO conversations
            (resp_id, stored_at, accessed_at, messages, approx_bytes)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("resp_corrupt", now, now, "{broken", 7),
    )
    store._conn.commit()

    assert store.get("resp_corrupt") is None
    assert len(store) == 0
    store.close()


def test_reset_builds_sqlite_store_then_closes_it_when_switching_to_memory(
    monkeypatch, tmp_path
):
    # Keep reset() from replacing the process-global store used by route tests.
    # monkeypatch restores the original module state after this test.
    monkeypatch.setattr(
        state,
        "conversation_store",
        ConversationStore(ttl_seconds=3600, max_entries=10),
    )
    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_PATH", str(tmp_path / "reset.sqlite"))
    state.reset()
    sqlite_store = state.conversation_store
    assert isinstance(sqlite_store, SQLiteConversationStore)

    monkeypatch.setenv("CONVERTER_RESPONSE_STORE_PATH", "")
    state.reset()
    assert isinstance(state.conversation_store, ConversationStore)

    # reset() closed the replaced SQLite connection.
    try:
        sqlite_store._conn.execute("SELECT 1")
    except Exception as exc:
        assert "closed" in str(exc).lower()
    else:  # pragma: no cover - sqlite must reject operations after close
        raise AssertionError("replaced SQLite store was not closed")
