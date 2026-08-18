"""Unit tests for system-message placement normalization.

Strict chat templates (newer Qwen) 400 on any system message past index 0, and
Claude Code sends both a multi-block system prompt and mid-history
``role: "system"`` reminders. These tests pin the three placement policies, plus
the two properties the ``/v1/responses`` chain depends on: idempotency (the
stored transcript is already normalized and gets re-normalized every turn) and
non-mutation (the caller's list is also the persisted one).
"""

from __future__ import annotations

import copy

import pytest

from app import config
from app.system_norm import normalize_system_messages


# ---------------------------------------------------------------------------
# policy "user"
# ---------------------------------------------------------------------------


def test_user_policy_demotes_mid_history_system_in_place():
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "q1"},
        {"role": "system", "content": "reminder"},
        {"role": "user", "content": "q2"},
    ]
    out = normalize_system_messages(messages, "user")
    # Head untouched; the reminder keeps its content AND its index — only the
    # role changes, so the model still reads it at the point it was injected.
    assert out == [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "q1"},
        {"role": "user", "content": "reminder"},
        {"role": "user", "content": "q2"},
    ]


def test_user_policy_merges_leading_system_run_into_one_head():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "hi"},
    ]
    out = normalize_system_messages(messages, "user")
    assert out == [
        {"role": "system", "content": "A\n\nB"},
        {"role": "user", "content": "hi"},
    ]


def test_user_policy_without_leading_system_leaves_no_system_at_all():
    messages = [{"role": "user", "content": "hi"}, {"role": "system", "content": "late"}]
    out = normalize_system_messages(messages, "user")
    assert out == [{"role": "user", "content": "hi"}, {"role": "user", "content": "late"}]
    assert not any(message["role"] == "system" for message in out)


def test_user_policy_fast_path_returns_the_input_untouched():
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    out = normalize_system_messages(messages, "user")
    # Already legal — the common case must not even allocate a new list.
    assert out is messages


def test_user_policy_is_idempotent_and_does_not_mutate_the_input():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": "reminder"},
    ]
    original = copy.deepcopy(messages)

    once = normalize_system_messages(messages, "user")
    twice = normalize_system_messages(once, "user")

    assert twice == once
    assert messages == original


def test_user_policy_merges_a_leading_system_whose_content_is_a_parts_list():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "from parts"}]},
        {"role": "system", "content": "plain"},
        {"role": "user", "content": "hi"},
    ]
    out = normalize_system_messages(messages, "user")
    assert out[0] == {"role": "system", "content": "from parts\n\nplain"}


def test_user_policy_emits_one_head_even_when_every_text_is_empty():
    messages = [
        {"role": "system", "content": None},
        {"role": "system", "content": 123},
        {"role": "user", "content": "hi"},
    ]
    out = normalize_system_messages(messages, "user")
    # A junk content field flattens to "" rather than a stringified repr, but the
    # head message itself still has to exist so index 0 stays the system slot.
    assert out == [
        {"role": "system", "content": ""},
        {"role": "user", "content": "hi"},
    ]


def test_user_policy_tolerates_non_dict_entries():
    messages = [None, {"role": "system", "content": "late"}]
    assert normalize_system_messages(messages, "user") == [
        None,
        {"role": "user", "content": "late"},
    ]


def test_user_policy_preserves_extra_keys_on_the_head_message():
    messages = [
        {"role": "system", "content": "A", "name": "sys"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "hi"},
    ]
    out = normalize_system_messages(messages, "user")
    assert out[0] == {"role": "system", "content": "A\n\nB", "name": "sys"}


# ---------------------------------------------------------------------------
# policy "hoist"
# ---------------------------------------------------------------------------


def test_hoist_policy_collects_every_system_into_one_leading_message():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "system", "content": "A"},
        {"role": "assistant", "content": "a"},
        {"role": "system", "content": "B"},
    ]
    out = normalize_system_messages(messages, "hoist")
    assert out == [
        {"role": "system", "content": "A\n\nB"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]


def test_hoist_policy_is_idempotent_and_noops_without_system_messages():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": "B"},
    ]
    original = copy.deepcopy(messages)
    once = normalize_system_messages(messages, "hoist")
    assert normalize_system_messages(once, "hoist") == once
    assert messages == original

    systemless = [{"role": "user", "content": "q"}]
    assert normalize_system_messages(systemless, "hoist") is systemless


# ---------------------------------------------------------------------------
# policy "asis" and edge inputs
# ---------------------------------------------------------------------------


def test_asis_policy_passes_everything_through():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": "B"},
    ]
    assert normalize_system_messages(messages, "asis") is messages


def test_empty_message_list_is_returned_for_every_policy():
    for policy in ("user", "hoist", "asis"):
        assert normalize_system_messages([], policy) == []


def test_unrecognized_policy_takes_the_user_path():
    # Matches the config layer's silent fallback, so a typo degrades to the
    # default behavior rather than to an unfixed 400.
    messages = [{"role": "user", "content": "q"}, {"role": "system", "content": "late"}]
    assert normalize_system_messages(messages, "typo") == [
        {"role": "user", "content": "q"},
        {"role": "user", "content": "late"},
    ]


# ---------------------------------------------------------------------------
# config property
# ---------------------------------------------------------------------------


def test_mid_system_policy_defaults_to_user(monkeypatch):
    monkeypatch.delenv("CONVERTER_MID_SYSTEM_POLICY", raising=False)
    assert config.settings.mid_system_policy == "user"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hoist", "hoist"),
        ("  ASIS  ", "asis"),
        ("User", "user"),
        ("", "user"),
        ("nonsense", "user"),
    ],
)
def test_mid_system_policy_normalizes_case_and_falls_back(monkeypatch, raw, expected):
    monkeypatch.setenv("CONVERTER_MID_SYSTEM_POLICY", raw)
    assert config.settings.mid_system_policy == expected
