"""Tool-result adjacency for strict chat templates (Mistral Large 4)."""

from __future__ import annotations

import re

import pytest

from app import config
from app.messages_bridge import anthropic_request_to_openai_body
from app.responses_bridge import responses_request_to_chat_body
from app.system_norm import normalize_system_messages
from app.tool_norm import normalize_tool_messages

MISTRAL = "mistralai/Mistral-Large-4-1T-A52B"
_GATE = re.compile(r"mistral-large-4", re.IGNORECASE)

_CALL = {
    "role": "assistant",
    "content": "",
    "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "Skill", "arguments": "{}"}},
        {"id": "t2", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
    ],
}
_T1 = {"role": "tool", "tool_call_id": "t1", "content": "r1"}
_T2 = {"role": "tool", "tool_call_id": "t2", "content": "r2"}


def _roles(messages):
    return [m["role"] for m in messages]


# Mistral Large 4 chat_template.jinja: the roles each role may be followed by.
_MISTRAL_NEXT = {
    "system": {"user", "assistant", "system"},
    "user": {"assistant", "system", "user"},
    "assistant": {"assistant", "user", "tool"},
    "tool": {"assistant", "tool", "user"},
}


def _mistral_order_ok(messages) -> bool:
    roles = _roles(messages)
    if roles and roles[0] not in ("user", "system"):
        return False
    return all(b in _MISTRAL_NEXT[a] for a, b in zip(roles, roles[1:]))


# ---------------------------------------------------------------------------
# pure function
# ---------------------------------------------------------------------------


def test_moves_tool_results_ahead_of_a_wedged_user_message():
    note = {"role": "user", "content": "reminder"}
    messages = [{"role": "user", "content": "q"}, _CALL, note, _T1, _T2]
    assert normalize_tool_messages(messages) == [
        {"role": "user", "content": "q"},
        _CALL,
        _T1,
        _T2,
        note,
    ]


def test_keeps_relative_order_of_tools_and_of_the_rest():
    a = {"role": "user", "content": "a"}
    b = {"role": "system", "content": "b"}
    messages = [_CALL, a, _T2, b, _T1, {"role": "user", "content": "c"}]
    assert normalize_tool_messages(messages) == [
        _CALL,
        _T2,
        _T1,
        a,
        b,
        {"role": "user", "content": "c"},
    ]


def test_window_stops_at_the_next_assistant():
    # A tool message after a LATER assistant turn belongs to that turn, so it
    # must not be pulled into the earlier window.
    late = {"role": "assistant", "content": "text only"}
    note = {"role": "user", "content": "n"}
    orphan = {"role": "tool", "tool_call_id": "x", "content": "late"}
    messages = [_CALL, _T1, note, late, orphan]
    assert normalize_tool_messages(messages) is messages


def test_well_formed_transcript_returns_the_caller_list():
    messages = [{"role": "user", "content": "q"}, _CALL, _T1, _T2, {"role": "user", "content": "next"}]
    assert normalize_tool_messages(messages) is messages


def test_assistant_without_tool_calls_is_not_a_window():
    messages = [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "x"}, _T1]
    assert normalize_tool_messages(messages) is messages


def test_does_not_mutate_and_is_idempotent():
    note = {"role": "user", "content": "reminder"}
    messages = [_CALL, note, _T1]
    snapshot = [dict(m) for m in messages]
    once = normalize_tool_messages(messages)
    assert messages == snapshot
    assert normalize_tool_messages(once) is once


@pytest.mark.parametrize("model", ["mistral-large-4", "Mistral-Large-4-1T-A52B", MISTRAL])
def test_gate_reorders_matching_models(model):
    messages = [_CALL, {"role": "user", "content": "n"}, _T1]
    assert _roles(normalize_tool_messages(messages, model=model, model_pattern=_GATE)) == [
        "assistant",
        "tool",
        "user",
    ]


@pytest.mark.parametrize("model", ["qwen3.6-35b", "mistral-large-3", "gpt-4o", None, 1])
def test_gate_passes_other_models_through(model):
    messages = [_CALL, {"role": "user", "content": "n"}, _T1]
    assert normalize_tool_messages(messages, model=model, model_pattern=_GATE) is messages


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_default_tool_norm_pattern_is_mistral_large_4_only(monkeypatch):
    monkeypatch.delenv("CONVERTER_TOOL_NORM_MODEL_PATTERN", raising=False)
    pattern = config.settings.tool_norm_model_regex
    for model in ("mistral-large-4", MISTRAL, "hosted_vllm/Mistral-Large-4"):
        assert pattern.search(model), model
    for model in ("qwen3.6-35b", "mistral-large-3", "mistral-small-4", "gpt-4o"):
        assert pattern.search(model) is None, model


@pytest.mark.parametrize("raw", ["", "  ", "("])
def test_unset_or_bad_tool_norm_pattern_falls_back(monkeypatch, raw):
    monkeypatch.setenv("CONVERTER_TOOL_NORM_MODEL_PATTERN", raw)
    pattern = config.settings.tool_norm_model_regex
    assert pattern.pattern == config._DEFAULT_TOOL_NORM_MODEL_PATTERN
    assert pattern.flags & re.IGNORECASE


def test_tool_norm_pattern_override(monkeypatch):
    monkeypatch.setenv("CONVERTER_TOOL_NORM_MODEL_PATTERN", ".*")
    assert config.settings.tool_norm_model_regex.search("gpt-4o")


def test_default_mid_system_pattern_covers_mistral_large_4(monkeypatch):
    monkeypatch.delenv("CONVERTER_MID_SYSTEM_MODEL_PATTERN", raising=False)
    assert config.settings.mid_system_model_regex.search(MISTRAL)


# ---------------------------------------------------------------------------
# bridges (Claude Code / Codex shapes against the Mistral role-order rules)
# ---------------------------------------------------------------------------

_TOOL_USE = {
    "role": "assistant",
    "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "x"}}],
}
_TOOL_RESULT = {"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: x"}


def _anthropic(model, messages):
    body = {"model": model, "max_tokens": 16, "system": "sys", "messages": messages}
    return anthropic_request_to_openai_body(body)["messages"]


@pytest.fixture(autouse=True)
def _default_patterns(monkeypatch):
    for name in (
        "CONVERTER_MID_SYSTEM_POLICY",
        "CONVERTER_MID_SYSTEM_MODEL_PATTERN",
        "CONVERTER_TOOL_NORM_MODEL_PATTERN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_messages_mid_system_reminder_before_tool_result_is_mistral_legal():
    out = _anthropic(
        MISTRAL,
        [
            {"role": "user", "content": "hi"},
            _TOOL_USE,
            {"role": "system", "content": "reminder"},
            {"role": "user", "content": [_TOOL_RESULT]},
        ],
    )
    assert _roles(out) == ["system", "user", "assistant", "tool", "user"]
    assert out[-1] == {"role": "user", "content": "reminder"}
    assert _mistral_order_ok(out)


def test_messages_separate_user_turn_before_tool_result_is_mistral_legal():
    out = _anthropic(
        MISTRAL,
        [
            {"role": "user", "content": "hi"},
            _TOOL_USE,
            {"role": "user", "content": [{"type": "text", "text": "<skill body>"}]},
            {"role": "user", "content": [_TOOL_RESULT]},
        ],
    )
    assert _roles(out) == ["system", "user", "assistant", "tool", "user"]
    assert out[-1]["content"] == "<skill body>"
    assert _mistral_order_ok(out)


def test_messages_mid_system_after_tool_result_is_mistral_legal():
    # ``tool → system`` is also rejected; the demotion handles it.
    out = _anthropic(
        MISTRAL,
        [
            {"role": "user", "content": "hi"},
            _TOOL_USE,
            {"role": "user", "content": [_TOOL_RESULT]},
            {"role": "system", "content": "reminder"},
        ],
    )
    assert _mistral_order_ok(out)


def test_messages_other_models_keep_the_client_shape():
    out = _anthropic(
        "qwen3.6-35b",
        [
            {"role": "user", "content": "hi"},
            _TOOL_USE,
            {"role": "user", "content": [{"type": "text", "text": "<skill body>"}]},
            {"role": "user", "content": [_TOOL_RESULT]},
        ],
    )
    assert _roles(out) == ["system", "user", "assistant", "user", "tool"]


def test_messages_empty_tool_result_passes_through_for_mistral():
    # The template only rejects None/[] content; the bridge always emits a
    # string, and an empty string renders fine, so nothing is invented.
    out = _anthropic(
        MISTRAL,
        [
            {"role": "user", "content": "hi"},
            _TOOL_USE,
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": []}]},
        ],
    )
    assert out[-1] == {"role": "tool", "tool_call_id": "t1", "content": ""}


def test_responses_reorders_for_mistral():
    body = {
        "model": MISTRAL,
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
            {"type": "message", "role": "user", "content": "note"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ],
    }
    out = responses_request_to_chat_body(body)["messages"]
    assert _roles(out) == ["user", "assistant", "tool", "user"]
    assert _mistral_order_ok(out)


def test_reorder_after_system_norm_is_idempotent_for_chained_transcripts():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        _CALL,
        {"role": "system", "content": "reminder"},
        _T1,
    ]
    once = normalize_tool_messages(normalize_system_messages(messages, "user"))
    assert normalize_tool_messages(normalize_system_messages(once, "user")) == once
