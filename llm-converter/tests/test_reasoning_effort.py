"""Unit tests for the reasoning-effort clamp.

The ladder the clients send is wider than what a vLLM/SGLang backend accepts,
and the value now reaches the backend verbatim (``allowed_openai_params``), so
the clamp is the only thing between a Codex ``xhigh`` and a 400.
"""

from __future__ import annotations

import pytest

from app.reasoning_effort import clamp_reasoning_effort

DEFAULT = frozenset({"low", "medium", "high"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("xhigh", "high"),   # above the vocabulary -> nearest allowed level
        ("max", "high"),
        ("ultra", "high"),
        ("none", "low"),     # below it -> cheapest allowed level
        ("minimal", "low"),
        ("medium", "medium"),  # already allowed -> untouched
    ],
)
def test_default_vocabulary_clamps_to_nearest_ladder_level(value, expected):
    assert clamp_reasoning_effort(value, DEFAULT) == expected


@pytest.mark.parametrize("value", ["disabled", "", "   ", None, 5, True, {"effort": "high"}])
def test_unknown_or_non_string_values_are_dropped(value):
    # Dropping beats guessing: an out-of-ladder name has no nearest level, and
    # forwarding it would 400 the whole request.
    assert clamp_reasoning_effort(value, DEFAULT) is None


def test_custom_vocabulary_keeps_a_level_the_default_would_clamp():
    allowed = frozenset({"minimal", "low", "medium", "high"})
    assert clamp_reasoning_effort("minimal", allowed) == "minimal"


def test_passthrough_vocabulary_forwards_verbatim_but_normalized():
    assert clamp_reasoning_effort("xhigh", None) == "xhigh"
    assert clamp_reasoning_effort(" HIGH ", None) == "high"


def test_equidistant_levels_resolve_to_the_cheaper_one():
    assert clamp_reasoning_effort("medium", frozenset({"low", "high"})) == "low"


def test_vocabulary_of_pure_unknowns_leaves_nothing_to_clamp_to():
    assert clamp_reasoning_effort("high", frozenset({"turbo"})) is None
